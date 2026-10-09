#!/usr/bin/env python3
"""
upgrade_shape_tables.py — the facts behind the upgrade-failure catalogue's static shapes.

One home for the tables the readiness rules under `readiness_rules/` read. Every constant
is named for the catalogue shape it serves, so any other reader of the same shapes imports
this module instead of carrying a copy. Every table names its source and the day it was
read. The tables are deliberately small: a rule whose input has no row here grades
`unknown` with the reason rather than guessing
(docs/designs/upgrade-readiness-checks.md, "What a run produces"). Nothing here is
computed from a cluster and nothing here runs a command.

Catalogue entries, as docs/designs/upgrade-failure-catalogue.md numbers them: 6 a served
API version removed, 8 a default changes in the new minor, 9 a feature deprecated but
still served, 10 add-on and client skew.
"""

import json
import re
from pathlib import Path

# ------------------------------------------------ entries 6, 9, 10: the audit-log read

# The audit-log rules (catalogue entries 6, 9 and 10): one `gcloud logging read` per
# cluster over the last seven days. Seven days sees a CronJob caller on any schedule a
# readiness check should catch (the seeded fixture writes every ten minutes) and a weekly
# batch job once. The page cap bounds the read beside the report's 60-second per-call
# timeout; a full page is reported as a cut window rather than as an absence.
AUDIT_LOG_WINDOW_DAYS = 7
AUDIT_LOG_LIMIT = 1000

# kubectl skew (entry 10). The Kubernetes version skew policy, read 2026-10-09: "kubectl
# is supported within one minor version (older or newer) of kube-apiserver". The page
# says nothing about client-go, and client-go's README gives a compatibility matrix
# rather than a support window; a client-go user agent also carries the binary's
# version, not the library's, so the rule grades kubectl and names the rest unread.
KUBECTL_SKEW_MINORS = 1
KUBECTL_SKEW_SOURCE = "https://kubernetes.io/releases/version-skew-policy/#kubectl"
CLIENT_GO_COMPATIBILITY_SOURCE = "https://github.com/kubernetes/client-go#compatibility-matrix"

# Deprecated-but-served APIs with a named successor (entry 9), keyed as the audit-log
# helper names an API (`group/version resource`). Endpoints: Kubernetes v1.33 deprecated
# the API in favour of EndpointSlices (KEP-4974; the v1.33 upcoming-changes post, read
# 2026-10-09, says the deprecation "only impacts those who use the Endpoints API directly
# from workloads or scripts; these users should migrate to use EndpointSlices instead").
DEPRECATED_API_SUCCESSORS = {
    "core/v1 endpoints": "discovery.k8s.io/v1 EndpointSlice (Endpoints deprecated in 1.33, KEP-4974)",
}
DEPRECATED_API_SUCCESSORS_SOURCE = "https://kubernetes.io/blog/2025/03/26/kubernetes-v1-33-upcoming-changes/"

# Defaults that change at a minor (entry 8), keyed by the minor that changes them. A
# minor at or below DEFAULT_CHANGES_AS_OF with no key changes nothing in this table; a
# target above it is not covered, and the rule says `unknown`. Row fields: `setting`
# names what changes; `detector` names the object shape the row is matched against
# (namespace Pod Security labels, or workloads carrying a gitRepo volume); `side` says
# which component's version the change rides on, so the rule measures the minors crossed
# from the control plane (admission) or from the lowest node pool (a kubelet default);
# `levels` names the Pod Security Standards levels the row changes, restricted inheriting
# every baseline check; `tightens` says whether the change can reject a pod the previous
# minor admitted, which is what makes a namespace that follows `latest` a risk rather
# than a note; `change` is the clause as the source states it.
POD_SECURITY_STANDARDS_SOURCE = "https://kubernetes.io/docs/concepts/security/pod-security-standards/"
KUBERNETES_1_33_CHANGELOG_SOURCE = "https://github.com/kubernetes/kubernetes/blob/master/CHANGELOG/CHANGELOG-1.33.md"
KUBERNETES_1_36_RELEASE_SOURCE = "https://kubernetes.io/blog/2026/04/22/kubernetes-v1-36-release/"
DEFAULT_CHANGES_READ_ON = "2026-10-09"
DETECTOR_POD_SECURITY = "pod-security-admission"
DETECTOR_GITREPO_VOLUME = "gitrepo-volume"
SIDE_APISERVER = "apiserver"
SIDE_KUBELET = "kubelet"
PSA_LEVEL_PRIVILEGED = "privileged"
PSA_LEVEL_BASELINE = "baseline"
PSA_LEVEL_RESTRICTED = "restricted"
BASELINE_AND_RESTRICTED = (PSA_LEVEL_BASELINE, PSA_LEVEL_RESTRICTED)
RESTRICTED_ONLY = (PSA_LEVEL_RESTRICTED,)
NO_LEVELS = ()
PSA_SETTING = "Pod Security Admission {level}: {control}"
GITREPO_SETTING = "gitRepo volumes"
DEFAULT_CHANGES_AS_OF = (1, 36)
DEFAULT_CHANGES_BY_MINOR = {
    (1, 23): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_RESTRICTED, control="Running as Non-root user (v1.23+)"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": RESTRICTED_ONLY,
            "tightens": True,
            "change": "containers must not set runAsUser to 0",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 25): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_RESTRICTED, control="pod.spec.os.name"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": RESTRICTED_ONLY,
            "tightens": False,
            "change": "the restricted checks read pod.spec.os.name, so Windows pods are exempt from the Linux-only checks",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 27): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_BASELINE, control="Sysctls"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": BASELINE_AND_RESTRICTED,
            "tightens": False,
            "change": "net.ipv4.ip_local_reserved_ports joins the allowed sysctls",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 29): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_BASELINE, control="Sysctls"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": BASELINE_AND_RESTRICTED,
            "tightens": False,
            "change": (
                "net.ipv4.tcp_keepalive_time, net.ipv4.tcp_fin_timeout, net.ipv4.tcp_keepalive_intvl "
                "and net.ipv4.tcp_keepalive_probes join the allowed sysctls"
            ),
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 31): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_BASELINE, control="SELinux"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": BASELINE_AND_RESTRICTED,
            "tightens": False,
            "change": "container_engine_t joins the allowed SELinux types",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 33): [
        {
            "setting": GITREPO_SETTING,
            "detector": DETECTOR_GITREPO_VOLUME,
            "side": SIDE_KUBELET,
            "levels": NO_LEVELS,
            "tightens": True,
            "change": (
                "the gitRepo volume plugin is disabled by default; it mounts only with the "
                "GitRepoVolumeDriver feature gate turned back on (kubernetes/kubernetes#129923)"
            ),
            "source": KUBERNETES_1_33_CHANGELOG_SOURCE,
        }
    ],
    (1, 34): [
        {
            "setting": PSA_SETTING.format(level=PSA_LEVEL_BASELINE, control="Host Probes / Lifecycle Hooks (v1.34+)"),
            "detector": DETECTOR_POD_SECURITY,
            "side": SIDE_APISERVER,
            "levels": BASELINE_AND_RESTRICTED,
            "tightens": True,
            "change": "the host field of probes and lifecycle hooks must be unset",
            "source": POD_SECURITY_STANDARDS_SOURCE,
        }
    ],
    (1, 36): [
        {
            "setting": GITREPO_SETTING,
            "detector": DETECTOR_GITREPO_VOLUME,
            "side": SIDE_KUBELET,
            "levels": NO_LEVELS,
            "tightens": True,
            "change": "the gitRepo volume plugin is permanently disabled and cannot be turned back on",
            "source": KUBERNETES_1_36_RELEASE_SOURCE,
        }
    ],
}

# Third-party add-on support (entry 10). An add-on is recognised by the repository of its
# container image (registry kept, tag and digest stripped) and its release read from the
# tag. A support row maps an add-on release to the Kubernetes minors it supports,
# inclusive, as the vendor's page listed them on the day given. An add-on detected with
# no row here, or at a release the rows do not list, is reported `unknown` with the page
# to read; no range is ever inferred from a neighbouring release.
ADDON_IMAGES = {
    "quay.io/jetstack/cert-manager-controller": "cert-manager",
    "docker.io/istio/pilot": "istio",
    "istio/pilot": "istio",
    "nvcr.io/nvidia/gpu-operator": "nvidia-gpu-operator",
    "quay.io/argoproj/argocd": "argo-cd",
    "ghcr.io/fluxcd/source-controller": "flux",
    "registry.k8s.io/ingress-nginx/controller": "ingress-nginx",
    "quay.io/cilium/cilium": "cilium",
    "docker.io/calico/node": "calico",
    "calico/node": "calico",
    "registry.k8s.io/external-dns/external-dns": "external-dns",
    "quay.io/prometheus-operator/prometheus-operator": "prometheus-operator",
}
ADDON_SUPPORT = {
    "cert-manager": {
        "source": "https://cert-manager.io/docs/releases/",
        "read_on": "2026-10-09",
        # cert-manager release -> (lowest, highest) supported Kubernetes minor.
        "releases": {
            (1, 21): ((1, 33), (1, 36)),
            (1, 20): ((1, 32), (1, 35)),
        },
    },
}
# Image repositories whose last path element is a kubectl binary rather than an add-on:
# a workload running one is a client the skew policy covers.
KUBECTL_IMAGE_NAMES = ("kubectl",)

# The removal table the GitOps scan reads (`api_deprecation_scan.py`), reused here for
# the replacement column of a removed-API finding. The same file, so the two reads can
# never disagree about a removal.
REMOVED_APIS_PATH = Path(__file__).resolve().parent / "removed_apis.json"
REMOVED_APIS_KEY = "removed"
REMOVED_API_VERSION_KEY = "api_version"
REMOVED_KIND_KEY = "kind"
REMOVED_REPLACEMENT_KEY = "replacement"
# How a lower-cased kind becomes the resource name the audit log carries: `FlowSchema`
# -> `flowschemas`, `Ingress` -> `ingresses`, `PodSecurityPolicy` -> `podsecuritypolicies`.
PLURAL_SUFFIXES = ("s", "es")
PLURAL_Y_TO_IES = ("y", "ies")
API_SEPARATOR = " "
GROUP_VERSION_SEPARATOR = "/"
CORE_GROUP = "core"

_REMOVED_APIS_CACHE: list | None = None


def _removed_apis() -> list[dict]:
    global _REMOVED_APIS_CACHE
    if _REMOVED_APIS_CACHE is None:
        try:
            with open(REMOVED_APIS_PATH, encoding="utf-8") as f:
                raw = json.load(f)
            _REMOVED_APIS_CACHE = [e for e in raw.get(REMOVED_APIS_KEY, []) if isinstance(e, dict)]
        except (OSError, ValueError):
            _REMOVED_APIS_CACHE = []
    return _REMOVED_APIS_CACHE


def _kind_matches_resource(kind: str, resource: str) -> bool:
    lowered = str(kind or "").lower()
    candidates = {lowered + suffix for suffix in PLURAL_SUFFIXES}
    if lowered.endswith(PLURAL_Y_TO_IES[0]):
        candidates.add(lowered[: -len(PLURAL_Y_TO_IES[0])] + PLURAL_Y_TO_IES[1])
    return str(resource or "").lower() in candidates


def replacement_for(api: str) -> str | None:
    """The removal table's replacement for `group/version resource`, or None when it has none."""
    if not isinstance(api, str) or API_SEPARATOR not in api:
        return None
    group_version, resource = api.split(API_SEPARATOR, 1)
    group, _, version = group_version.partition(GROUP_VERSION_SEPARATOR)
    spellings = {group_version}
    if group == CORE_GROUP:
        spellings.add(version)
    for entry in _removed_apis():
        if entry.get(REMOVED_API_VERSION_KEY) in spellings and _kind_matches_resource(entry.get(REMOVED_KIND_KEY, ""), resource):
            return entry.get(REMOVED_REPLACEMENT_KEY)
    return None


# `MAJOR.MINOR` at the start of a version string, with or without a `v`, a patch or a
# `-gke.BUILD` suffix: the minor is what every table here is keyed by.
MINOR_RE = re.compile(r"^v?(\d+)\.(\d+)")


def parse_minor(text) -> tuple[int, int] | None:
    """`1.32`, `v1.29.0`, `1.35.1-gke.1000` -> (major, minor); None when it does not parse."""
    if not isinstance(text, str):
        return None
    m = MINOR_RE.match(text.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def format_minor(minor: tuple) -> str:
    return f"{minor[0]}.{minor[1]}"
