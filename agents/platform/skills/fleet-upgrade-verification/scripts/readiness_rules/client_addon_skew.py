#!/usr/bin/env python3
"""
client_addon_skew.py — catalogue entry 10: a client or an add-on outside the range the
target minor supports.

Clients. kubectl is supported within one minor, older or newer, of kube-apiserver
(`upgrade_shape_tables.KUBECTL_SKEW_SOURCE`). The rule reads kubectl versions from two
places and grades each against the target minor: the `kubectl/vX.Y.Z` user agents in the
shared audit-log read (one `gcloud logging read` per member, last seven days; Admin
Activity logs carry writes, so a kubectl that only reads is not there), and the image tags
of workloads that run a kubectl binary, from the same `kubectl get` the PDB rule reads. A
kubectl further than one minor from the target is a risk naming the version and the
principal or workload. A client-go user agent carries the binary's version, not the
library's, so those callers are counted in the note, never graded.

Add-ons. The cluster record's `addonsConfig` names the GKE-managed add-ons that are on;
it exposes no version for them and GKE owns their lifecycle, so they are listed in the
note and not graded. Third-party add-ons are recognised by image repository in the same
workload read; one whose vendor table (`upgrade_shape_tables.ADDON_SUPPORT`) does not
cover the target is a risk, one with no table or at a release the table does not list is
`unknown` with the page to read. Nothing is inferred.
"""

import re

import upgrade_shape_tables as tables
from readiness_rules import audit_log, finding

RULE_ID = "client-addon-skew"
CATALOGUE_ENTRY = 10

# `kubectl/v1.29.0 (linux/amd64) kubernetes/3f7a50f`: the user agent kubectl sends.
KUBECTL_USER_AGENT_RE = re.compile(r"^kubectl/v?(\d+)\.(\d+)")
# A client-go built binary ends its user agent with `kubernetes/<commit>`.
CLIENT_GO_USER_AGENT_MARKER = " kubernetes/"
# `registry.k8s.io/kubectl:v1.29.0` -> repository and tag; a digest is dropped.
IMAGE_DIGEST_SEPARATOR = "@"
IMAGE_TAG_SEPARATOR = ":"
IMAGE_PATH_SEPARATOR = "/"
DOCKER_HUB_PREFIX = "docker.io/"
# Where a workload's pod template sits, per kind read.
TEMPLATE_PATHS = {
    "Deployment": ("spec", "template"),
    "StatefulSet": ("spec", "template"),
    "DaemonSet": ("spec", "template"),
    "CronJob": ("spec", "jobTemplate", "spec", "template"),
}
CONTAINER_LISTS = ("containers", "initContainers")
WORKLOAD_FORMAT = "{kind} {namespace}/{name}"
# `addonsConfig` blocks that spell "on" as the absence of `disabled: true` (an empty block
# is on); every other block spells it `enabled: true`.
ADDONS_CONFIG_KEY = "addonsConfig"
DISABLED_STYLE_ADDONS = (
    "httpLoadBalancing",
    "horizontalPodAutoscaling",
    "kubernetesDashboard",
    "networkPolicyConfig",
    "dnsCacheConfig",
    "cloudRunConfig",
    "istioConfig",
)
ADDON_ENABLED_KEY = "enabled"
ADDON_DISABLED_KEY = "disabled"

AUDIT_READ_FAILED = "kubectl user agents not read: {reason}"
OBJECTS_NOT_READ = "cluster objects not read; kubectl images and add-ons not graded"
NO_TARGET_REASON = "no target; client and add-on skew need one"
CLIENT_NAME = "kubectl"
CLIENT_FROM_AUDIT = "principal {principal} (user agent {user_agent}, {count} call{plural} in {days}d)"
CLIENT_FROM_IMAGE = "{workload} (image {image})"
CLIENT_SKEW_DETAIL = (
    "{gap} minor{plural} {direction} the target {target}; kubectl is supported within "
    "{allowed} minor of kube-apiserver ({source})"
)
CLIENT_MAJOR_DETAIL = "major version differs from the target {target}"
DIRECTION_BEHIND = "behind"
DIRECTION_AHEAD = "ahead of"
MINOR_PLURAL = "s"
CALL_PLURAL = "s"
ADDON_UNSUPPORTED_DETAIL = "{addon} {version} supports Kubernetes {low} to {high} ({source}, read {read_on}); the target {target} is outside it"
ADDON_NO_TABLE_REASON = "{addon} {version} at {where}: no support table with a source here; check the vendor's matrix against {target}"
ADDON_RELEASE_NOT_LISTED_REASON = "{addon} {version} at {where}: release not in the table read on {read_on} ({source}); check it against {target}"
ADDON_NO_TARGET_REASON = "{addon} {version} at {where}: no target to check its support range against"
NOTE_GKE_ADDONS = "GKE add-ons on: {addons} (GKE-managed; the cluster record carries no version, so not graded)"
NOTE_CLIENT_GO = "{count} client-go caller{plural} whose user agent carries the binary's version, not the library's; not graded"
NOTE_ADDON_OK = "{addon} {version} supports the target {target} ({source})"
NOTE_CLIENTS_OK = "{count} kubectl client{plural} within one minor of the target"
LIST_SEPARATOR = ", "
DESCRIBE_CLIENT = "{client} {version} from {where}: {detail}"
DESCRIBE_ADDON = "{addon} {version} at {where}: {detail}"
VERSION_FORMAT = "v{major}.{minor}"


def _image_parts(image: str) -> tuple[str, str]:
    """(repository without registry alias, tag) for an image reference; the digest is dropped."""
    reference = str(image or "").split(IMAGE_DIGEST_SEPARATOR, 1)[0]
    repository, tag = reference, ""
    last = reference.rsplit(IMAGE_PATH_SEPARATOR, 1)[-1]
    if IMAGE_TAG_SEPARATOR in last:
        repository, tag = reference.rsplit(IMAGE_TAG_SEPARATOR, 1)
    return repository, tag


def _addon_name(repository: str) -> str | None:
    if repository in tables.ADDON_IMAGES:
        return tables.ADDON_IMAGES[repository]
    if repository.startswith(DOCKER_HUB_PREFIX) and repository[len(DOCKER_HUB_PREFIX) :] in tables.ADDON_IMAGES:
        return tables.ADDON_IMAGES[repository[len(DOCKER_HUB_PREFIX) :]]
    return None


def workload_images(items: list) -> list[tuple[str, str]]:
    """(workload label, image) for every container of every workload kind read."""
    found = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        path = TEMPLATE_PATHS.get(item.get("kind"))
        if path is None:
            continue
        node = item
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        pod_spec = (node or {}).get("spec") if isinstance(node, dict) else None
        if not isinstance(pod_spec, dict):
            continue
        meta = item.get("metadata") or {}
        label = WORKLOAD_FORMAT.format(kind=item.get("kind"), namespace=meta.get("namespace", ""), name=meta.get("name", ""))
        for list_key in CONTAINER_LISTS:
            for container in pod_spec.get(list_key) or []:
                if isinstance(container, dict) and container.get("image"):
                    found.append((label, str(container["image"])))
    return found


def enabled_gke_addons(cluster: dict) -> list[str]:
    """The `addonsConfig` keys whose block reads as on."""
    config = cluster.get(ADDONS_CONFIG_KEY) if isinstance(cluster, dict) else None
    enabled = []
    for key, block in sorted((config or {}).items()):
        if not isinstance(block, dict):
            continue
        if ADDON_ENABLED_KEY in block:
            on = bool(block.get(ADDON_ENABLED_KEY))
        elif ADDON_DISABLED_KEY in block:
            on = not block.get(ADDON_DISABLED_KEY)
        else:
            on = key in DISABLED_STYLE_ADDONS
        if on:
            enabled.append(key)
    return enabled


def _client_finding(version: tuple, where: str, target_minor: tuple) -> dict | None:
    """A risk for a kubectl at `version` against the target, or None when inside the window."""
    version_text = VERSION_FORMAT.format(major=version[0], minor=version[1])
    base = {"rule": RULE_ID, "tier": finding.TIER_RISK, "kind": "client", "client": CLIENT_NAME, "version": version_text, "where": where, "target": tables.format_minor(target_minor)}
    if version[0] != target_minor[0]:
        return {**base, "gap_minors": None, "detail": CLIENT_MAJOR_DETAIL.format(target=base["target"])}
    gap = target_minor[1] - version[1]
    if abs(gap) <= tables.KUBECTL_SKEW_MINORS:
        return None
    detail = CLIENT_SKEW_DETAIL.format(
        gap=abs(gap),
        plural="" if abs(gap) == 1 else MINOR_PLURAL,
        direction=DIRECTION_BEHIND if gap > 0 else DIRECTION_AHEAD,
        target=base["target"],
        allowed=tables.KUBECTL_SKEW_MINORS,
        source=tables.KUBECTL_SKEW_SOURCE,
    )
    return {**base, "gap_minors": gap, "detail": detail}


def _addon_verdict(addon: str, version: tuple, where: str, target_minor: tuple | None, result: dict) -> None:
    version_text = VERSION_FORMAT.format(major=version[0], minor=version[1])
    support = tables.ADDON_SUPPORT.get(addon)
    target_text = tables.format_minor(target_minor) if target_minor else None
    if support is None:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, ADDON_NO_TABLE_REASON.format(addon=addon, version=version_text, where=where, target=target_text)))
        return
    span = support["releases"].get(version)
    if span is None:
        result[finding.RESULT_UNKNOWN].append(
            finding.unknown(RULE_ID, ADDON_RELEASE_NOT_LISTED_REASON.format(addon=addon, version=version_text, where=where, read_on=support["read_on"], source=support["source"], target=target_text))
        )
        return
    if target_minor is None:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, ADDON_NO_TARGET_REASON.format(addon=addon, version=version_text, where=where)))
        return
    low, high = span
    if low <= target_minor <= high:
        finding.add_note(result, NOTE_ADDON_OK.format(addon=addon, version=version_text, target=target_text, source=support["source"]))
        return
    result[finding.RESULT_RISKS].append(
        {
            "rule": RULE_ID,
            "tier": finding.TIER_RISK,
            "kind": "addon",
            "addon": addon,
            "version": version_text,
            "where": where,
            "supported": (tables.format_minor(low), tables.format_minor(high)),
            "source": support["source"],
            "detail": ADDON_UNSUPPORTED_DETAIL.format(addon=addon, version=version_text, low=tables.format_minor(low), high=tables.format_minor(high), source=support["source"], read_on=support["read_on"], target=target_text),
        }
    )


def evaluate(cluster: dict, member: dict, items: list | None, target, context: dict) -> dict:
    result = finding.empty_result()
    target_minor = (target[0], target[1]) if target else None
    clients: list[tuple[tuple, str]] = []
    client_go = 0

    log = audit_log.read_callers(context)
    if log["error"]:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, AUDIT_READ_FAILED.format(reason=log["error"])))
    else:
        for caller in log["callers"]:
            if caller["platform"]:
                continue
            match = KUBECTL_USER_AGENT_RE.match(str(caller["user_agent"]))
            if match:
                where = CLIENT_FROM_AUDIT.format(principal=caller["principal"], user_agent=caller["user_agent"], count=caller["count"], plural="" if caller["count"] == 1 else CALL_PLURAL, days=log["window_days"])
                clients.append(((int(match.group(1)), int(match.group(2))), where))
            elif CLIENT_GO_USER_AGENT_MARKER in str(caller["user_agent"]):
                client_go += 1

    if items is None:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, OBJECTS_NOT_READ))
    else:
        for workload, image in workload_images(items):
            repository, tag = _image_parts(image)
            version = tables.parse_minor(tag)
            if repository.rsplit(IMAGE_PATH_SEPARATOR, 1)[-1] in tables.KUBECTL_IMAGE_NAMES and version is not None:
                clients.append((version, CLIENT_FROM_IMAGE.format(workload=workload, image=image)))
                continue
            addon = _addon_name(repository)
            if addon is not None and version is not None:
                _addon_verdict(addon, version, CLIENT_FROM_IMAGE.format(workload=workload, image=image), target_minor, result)

    if target_minor is None:
        if clients:
            result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, NO_TARGET_REASON))
    else:
        within = 0
        for version, where in clients:
            item = _client_finding(version, where, target_minor)
            if item is None:
                within += 1
            else:
                result[finding.RESULT_RISKS].append(item)
        if within:
            finding.add_note(result, NOTE_CLIENTS_OK.format(count=within, plural="" if within == 1 else MINOR_PLURAL))

    if client_go:
        finding.add_note(result, NOTE_CLIENT_GO.format(count=client_go, plural="" if client_go == 1 else MINOR_PLURAL))
    addons = enabled_gke_addons(cluster)
    if addons:
        finding.add_note(result, NOTE_GKE_ADDONS.format(addons=LIST_SEPARATOR.join(addons)))
    return result


def describe(item: dict) -> str:
    if item["tier"] == finding.TIER_UNKNOWN:
        return item["reason"]
    if item.get("kind") == "addon":
        return DESCRIBE_ADDON.format(addon=item["addon"], version=item["version"], where=item["where"], detail=item["detail"])
    return DESCRIBE_CLIENT.format(client=item["client"], version=item["version"], where=item["where"], detail=item["detail"])
