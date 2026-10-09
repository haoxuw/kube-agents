"""
readiness_rules — the per-entry rules `upgrade_readiness.EXTRA_RULES` runs after the PDB,
maintenance and skew rules of `fleet_upgrade_report.py --readiness`.

One module per entry of docs/designs/upgrade-failure-catalogue.md. Each module exposes:

- `RULE_ID`, the slug the report keys the rule's counts under and stamps on its findings;
  `ENTRY`, the catalogue entry number; `CAN_BLOCK`, whether a finding of the rule can ever
  block the upgrade. A rule with `CAN_BLOCK = False` files risks and notes; an `unknown` it
  files is reported in the note and never moves the member's verdict, since the finding it
  could not make would not have blocked either, and a blocker it files is demoted to a risk
  with a note saying so.
- `evaluate(cluster, member, read, target, context) -> dict`, a pure function over what the
  report has already read, returning `blocking`, `risks`, `unknown` and `notes`, every key
  present and each a list (`new_result()` is the empty one): `cluster` is the `gcloud
  container clusters list` record; `member` the graded version row; `read` the member's
  kubectl reads (below); `target` the parsed target version tuple, None without one;
  `context` the per-member read context (below).
- `describe(finding) -> str`, one blocking or risk finding as a table cell.

`read` is the dict `fleet_upgrade_report.read_cluster_objects` returns. `items` is every
object the reads returned, None when `get-credentials` or the PDB read failed, in which case
nothing was read and `error` says why. `read_errors` is keyed by kind as the reads after the
PDB read name them (READ_KINDS), each None or the error of the read that carried the kind:
the namespaced kinds (NAMESPACED_READ_KINDS) come in one read, the Nodes in one through a
jsonpath template, the StorageClasses and PersistentVolumes in one. A rule that needs a kind
asks `read_error(read, kind)` and files one `unknown` naming the reason when the kind was not
read, grading what it has; a refused read never costs a rule that does not need it.

`context` carries `project`, `location` and `cluster_name` (for a log filter), `run_cmd`
(the report's runner) with `timeout_seconds` (the per-call cap a rule passes to it), `at`
(the aware instant the report evaluates at), `cache` (a dict a rule fills so another rule
reads the same thing once; the audit-log reads live under `audit_log.CACHE_KEY`), `clusters`
(every cluster record the run listed, for a rule that compares a member with its peers),
`pools` (the member's pools: `name`, `version`, `parsed`, the record's `config` and
`autoscaling`), `target_text`, `master` (the parsed control-plane version) and `autopilot`;
tests may add `clock`, a monotonic clock for a read's time budget.

A finding is a dict the rule shapes. The fold in `upgrade_readiness.evaluate_extra_rules`
stamps `rule` and `entry` on every finding and `text` (the rule's `describe`) on every
blocking and risk finding, and turns each `unknown` entry (a string, or a dict carrying
`reason` or `detail`) into a dict with `reason`, `can_block` and the `text` the note column
prints. A rule that raises becomes one `unknown` finding, never a crash. A rule reads; it
runs no command of its own except through `context["run_cmd"]`.

This file holds what the modules share: the result shape, the read kinds and context keys,
the quantity parser, a pod's owner and requests, node helpers, pod templates with their
selectors and node-pool pins, image tags and the cgroup transition. `finding.py` holds the
tiered-finding helpers, `audit_log.py` the Cloud Logging reads.
"""

import re

import upgrade_shape_tables as tables

RESULT_KEYS = ("blocking", "risks", "unknown", "notes")
GRADE_BLOCKING = "blocking"
GRADE_RISK = "risk"
GRADE_UNKNOWN = "unknown"
LIST_SEPARATOR = ", "

# The kinds `fleet_upgrade_report` reads per member, as `kubectl get` names them. The PDB
# read is the one on main; the three after it fail on their own, and `read_errors` is keyed
# by the kinds below.
PDB_READ_KINDS = ("pdb", "deploy", "statefulset")
NAMESPACED_READ_KINDS = ("daemonset", "cronjob", "pods", "pvc", "networkpolicy", "namespace")
NODE_READ_KIND = "node"
CLUSTER_READ_KINDS = ("storageclass", "pv")
READ_KINDS = NAMESPACED_READ_KINDS + (NODE_READ_KIND,) + CLUSTER_READ_KINDS
NOTHING_READ_REASON = "the cluster read failed, so nothing was read"

# The per-member context keys (the module docstring says what each holds).
CONTEXT_PROJECT = "project"
CONTEXT_LOCATION = "location"
CONTEXT_CLUSTER_NAME = "cluster_name"
CONTEXT_RUN_CMD = "run_cmd"
CONTEXT_TIMEOUT_SECONDS = "timeout_seconds"
CONTEXT_AT = "at"
CONTEXT_CACHE = "cache"
CONTEXT_CLOCK = "clock"
CONTEXT_CLUSTERS = "clusters"
CONTEXT_POOLS = "pools"
CONTEXT_TARGET_TEXT = "target_text"
CONTEXT_MASTER = "master"
CONTEXT_AUTOPILOT = "autopilot"
CONTEXT_KEYS = (
    CONTEXT_PROJECT,
    CONTEXT_LOCATION,
    CONTEXT_CLUSTER_NAME,
    CONTEXT_RUN_CMD,
    CONTEXT_TIMEOUT_SECONDS,
    CONTEXT_AT,
    CONTEXT_CACHE,
    CONTEXT_CLUSTERS,
    CONTEXT_POOLS,
    CONTEXT_TARGET_TEXT,
    CONTEXT_MASTER,
    CONTEXT_AUTOPILOT,
)

# The kinds the reads carry, as `kind` spells them.
KIND_POD = "Pod"
KIND_NODE = "Node"
KIND_DEPLOYMENT = "Deployment"
KIND_STATEFULSET = "StatefulSet"
KIND_DAEMONSET = "DaemonSet"
KIND_REPLICASET = "ReplicaSet"
KIND_CRONJOB = "CronJob"
KIND_PERSISTENT_VOLUME = "PersistentVolume"
KIND_PERSISTENT_VOLUME_CLAIM = "PersistentVolumeClaim"
KIND_STORAGE_CLASS = "StorageClass"
KIND_NETWORK_POLICY = "NetworkPolicy"
KIND_NAMESPACE = "Namespace"
KIND_POD_DISRUPTION_BUDGET = "PodDisruptionBudget"
# Where a workload's pod template sits, per kind that carries one. Bare Pods and Jobs are
# not templates; a CronJob's sits under its jobTemplate.
TEMPLATE_PATHS = {
    KIND_DEPLOYMENT: ("spec", "template"),
    KIND_STATEFULSET: ("spec", "template"),
    KIND_DAEMONSET: ("spec", "template"),
    KIND_CRONJOB: ("spec", "jobTemplate", "spec", "template"),
}
# A pod's owner and how a finding names it. A ReplicaSet is named for its Deployment:
# the ReplicaSet's name is the Deployment's plus `-<pod-template-hash>`.
POD_TEMPLATE_HASH_LABEL = "pod-template-hash"
OWNER_FORMAT = "{kind} {namespace}/{name}"
OBJECT_FORMAT = "{namespace}/{name}"
DESCRIBE_FORMAT = "{kind} {object}: {detail}"
RULE_LEVEL_FORMAT = "{rule}: {detail}"
NAME_SEPARATOR = "-"
# A static pod's mirror; the kubelet recreates it on the rebuilt node, so a drain never
# has to find it room.
MIRROR_POD_ANNOTATION = "kubernetes.io/config.mirror"
# Pod phases that hold no node capacity.
TERMINAL_PHASES = ("Succeeded", "Failed")
NODE_READY_CONDITION = "Ready"
CONDITION_TRUE = "True"
# Kubernetes resource quantities: a decimal number with an optional SI or binary suffix.
QUANTITY_RE = re.compile(r"^([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([A-Za-z]*)$")
BINARY_SUFFIXES = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4, "Pi": 1024**5, "Ei": 1024**6}
DECIMAL_SUFFIXES = {"n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18}
MILLIS_PER_CORE = 1000
BYTES_PER_MI = 1024**2
CPU_FORMAT = "{millis}m"
MEMORY_FORMAT = "{mebibytes}Mi"
# Node affinity, as the PodSpec spells it. Only required terms are read: a preferred term
# never keeps a pod off a node. The operators are the NodeSelectorRequirement's; `In` and
# `Exists` select nodes carrying the label, the others select nodes without it or by value.
AFFINITY_REQUIRED = "requiredDuringSchedulingIgnoredDuringExecution"
OP_IN = "In"
OP_NOT_IN = "NotIn"
OP_EXISTS = "Exists"
OP_DOES_NOT_EXIST = "DoesNotExist"
POSITIVE_OPERATORS = (OP_IN, OP_EXISTS)
WHERE_NODE_SELECTOR = "nodeSelector"
WHERE_NODE_AFFINITY = "nodeAffinity"
# What `cgroup_transition` answers for a pool against the target.
CGROUP_MOVES = "moves"
CGROUP_STAYS_V1 = "stays-v1"
CGROUP_ALREADY_V2 = "already-v2"
CGROUP_UNKNOWN = "unknown"
CGROUP_REASON_MIGRATION = "GKE migrates cgroup v1 pools to v2 from 1.{minor}"
CGROUP_REASON_PIN_ENDS = "the pool is pinned to cgroup v1 and GKE removes cgroup v1 at 1.{minor}"
CGROUP_REASON_STAYS = "the pool stays on cgroup v1 below 1.{minor}"
CGROUP_REASON_ALREADY = "already on cgroup v2"
CGROUP_REASON_NO_TARGET = "no target to decide whether the pool moves to cgroup v2"
CGROUP_REASON_UNREAD = "the pool record carries no effectiveCgroupMode"
IMAGE_DIGEST_SEPARATOR = "@"
IMAGE_TAG_SEPARATOR = ":"
PATH_SEPARATOR = "/"


# -------------------------------------------------------------------- results and reads


def new_result() -> dict:
    """An empty rule result, every key present."""
    return {key: [] for key in RESULT_KEYS}


def new_finding(rule: str, entry: int, grade: str, obj: dict, detail: str, **extra) -> dict:
    """One finding on `obj` (a dict from `templates`), with the rule's own fields after."""
    return {"rule": rule, "entry": entry, "grade": grade, "kind": obj["kind"], "namespace": obj["namespace"], "name": obj["name"], "object": obj["object"], "detail": detail, **extra}


def rule_unknown(rule: str, entry, detail: str) -> dict:
    """The whole rule, or one of its reads, could not run; `object` is None."""
    return {"rule": rule, "entry": entry, "grade": GRADE_UNKNOWN, "kind": None, "namespace": None, "name": None, "object": None, "detail": detail}


def describe(entry: dict) -> str:
    """`Deployment ns/name: detail` for a table cell; `rule: detail` for a rule-level entry."""
    if entry.get("object") is None:
        return RULE_LEVEL_FORMAT.format(rule=entry["rule"], detail=entry["detail"])
    return DESCRIBE_FORMAT.format(kind=entry["kind"], object=entry["object"], detail=entry["detail"])


def read_items(read) -> list:
    """Every object the member's reads returned; empty when nothing was read."""
    return ((read or {}).get("items")) or []


def read_error(read, kind: str) -> str | None:
    """Why `kind` was not read, or None when it was: the PDB read's failure when nothing was
    read at all, else the error of the read after it that carried the kind."""
    if not read or read.get("items") is None:
        return (read or {}).get("error") or NOTHING_READ_REASON
    return (read.get("read_errors") or {}).get(kind)


def unread_kinds(read, kinds) -> dict:
    """{kind: error} for the kinds among `kinds` that were not read."""
    return {kind: error for kind in kinds if (error := read_error(read, kind))}


def items_of_kind(items, kind: str) -> list[dict]:
    return [item for item in items or [] if isinstance(item, dict) and item.get("kind") == kind]


# ------------------------------------------------------------------------- quantities


def parse_quantity(text) -> float | None:
    """`"250m"` -> 0.25, `"256Mi"` -> 268435456.0, `2` -> 2.0; None when it does not parse."""
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    m = QUANTITY_RE.match(text.strip())
    if not m:
        return None
    number, suffix = m.groups()
    if suffix == "":
        scale = 1.0
    elif suffix in BINARY_SUFFIXES:
        scale = BINARY_SUFFIXES[suffix]
    elif suffix in DECIMAL_SUFFIXES:
        scale = DECIMAL_SUFFIXES[suffix]
    else:
        return None
    return float(number) * scale


def cpu_millis(text) -> int | None:
    value = parse_quantity(text)
    return None if value is None else round(value * MILLIS_PER_CORE)


def memory_bytes(text) -> int | None:
    value = parse_quantity(text)
    return None if value is None else round(value)


def format_cpu(millis: int) -> str:
    return CPU_FORMAT.format(millis=millis)


def format_memory(nbytes: int) -> str:
    return MEMORY_FORMAT.format(mebibytes=round(nbytes / BYTES_PER_MI))


# ------------------------------------------------------------------- pods and owners


def pod_owner(pod: dict) -> dict:
    """{kind, namespace, name} of what a finding should name for a pod."""
    meta = pod.get("metadata") or {}
    namespace = meta.get("namespace", "")
    refs = [ref for ref in meta.get("ownerReferences") or [] if isinstance(ref, dict)]
    if not refs:
        return {"kind": KIND_POD, "namespace": namespace, "name": meta.get("name", "")}
    ref = refs[0]
    kind, name = ref.get("kind", ""), ref.get("name", "")
    if kind == KIND_REPLICASET:
        template_hash = (meta.get("labels") or {}).get(POD_TEMPLATE_HASH_LABEL)
        suffix = NAME_SEPARATOR + str(template_hash) if template_hash else ""
        if suffix and name.endswith(suffix):
            return {"kind": KIND_DEPLOYMENT, "namespace": namespace, "name": name[: -len(suffix)]}
    return {"kind": kind, "namespace": namespace, "name": name}


def owner_label(owner: dict) -> str:
    return OWNER_FORMAT.format(kind=owner.get("kind"), namespace=owner.get("namespace", ""), name=owner.get("name", ""))


def active_pods(items) -> list[dict]:
    """Pods that hold node capacity: not Succeeded or Failed."""
    return [pod for pod in items_of_kind(items, KIND_POD) if ((pod.get("status") or {}).get("phase")) not in TERMINAL_PHASES]


def is_mirror_pod(pod: dict) -> bool:
    return MIRROR_POD_ANNOTATION in ((pod.get("metadata") or {}).get("annotations") or {})


def is_daemonset_pod(pod: dict) -> bool:
    return pod_owner(pod)["kind"] == KIND_DAEMONSET


def _container_requests(container: dict) -> tuple[int | None, int | None]:
    requests = ((container.get("resources") or {}).get("requests")) or {}
    return cpu_millis(requests.get("cpu", 0)), memory_bytes(requests.get("memory", 0))


def pod_requests(spec: dict) -> tuple[int | None, int | None]:
    """(cpu millicores, memory bytes) a pod spec asks for: the containers' sum, or an
    init container's request where that is larger, as the scheduler counts it. None in
    a slot whose quantities do not all parse."""
    cpu_total, memory_total = 0, 0
    cpu_init, memory_init = 0, 0
    for container in spec.get("containers") or []:
        cpu, memory = _container_requests(container)
        cpu_total = None if cpu is None or cpu_total is None else cpu_total + cpu
        memory_total = None if memory is None or memory_total is None else memory_total + memory
    for container in spec.get("initContainers") or []:
        cpu, memory = _container_requests(container)
        cpu_init = None if cpu is None or cpu_init is None else max(cpu_init, cpu)
        memory_init = None if memory is None or memory_init is None else max(memory_init, memory)
    cpu = None if cpu_total is None or cpu_init is None else max(cpu_total, cpu_init)
    memory = None if memory_total is None or memory_init is None else max(memory_total, memory_init)
    return cpu, memory


def containers(spec: dict) -> list[dict]:
    return [c for c in (spec.get("containers") or []) + (spec.get("initContainers") or []) if isinstance(c, dict)]


def hostpath_volumes(spec: dict) -> list[str]:
    """The host paths a pod spec mounts through `hostPath` volumes."""
    paths = []
    for volume in spec.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        host_path = volume.get("hostPath")
        if isinstance(host_path, dict) and host_path.get("path"):
            paths.append(str(host_path["path"]))
    return paths


def claim_names(spec: dict) -> list[str]:
    """The PersistentVolumeClaims a pod spec mounts by name."""
    names = []
    for volume in spec.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        claim = volume.get("persistentVolumeClaim")
        if isinstance(claim, dict) and claim.get("claimName"):
            names.append(str(claim["claimName"]))
    return names


# -------------------------------------------------------------------------- templates


def is_system_namespace(namespace: str) -> bool:
    """A namespace GKE or one of its add-ons occupies."""
    return namespace in tables.SYSTEM_NAMESPACES or namespace.startswith(tables.SYSTEM_NAMESPACE_PREFIXES)


def pod_template_spec(workload) -> dict | None:
    """The PodSpec a workload's template carries, or None for a kind without a template."""
    if not isinstance(workload, dict):
        return None
    path = TEMPLATE_PATHS.get(workload.get("kind"))
    if path is None:
        return None
    node = workload
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    spec = (node or {}).get("spec") if isinstance(node, dict) else None
    return spec if isinstance(spec, dict) else None


def templates(items, kinds: tuple = tables.TEMPLATE_KINDS, include_system: bool = False) -> list[tuple[dict, dict, dict]]:
    """(obj, pod_spec, workload) for every template of the kinds asked; the namespaces GKE
    and its add-ons occupy are skipped unless `include_system`."""
    out = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in kinds:
            continue
        meta = item.get("metadata") or {}
        namespace = meta.get("namespace", "") or ""
        if not include_system and is_system_namespace(namespace):
            continue
        spec = pod_template_spec(item)
        if not isinstance(spec, dict):
            continue
        name = meta.get("name", "") or ""
        obj = {"kind": item["kind"], "namespace": namespace, "name": name, "object": OBJECT_FORMAT.format(namespace=namespace, name=name)}
        out.append((obj, spec, item))
    return out


def template_specs(items, kinds: tuple = tables.TEMPLATE_KINDS) -> list[tuple[dict, dict]]:
    """(owner, pod spec) for every template read, system namespaces included: the view a
    rule takes when GKE's own agents count too (a socket client in kube-system)."""
    return [(obj, spec) for obj, spec, _ in templates(items, kinds, include_system=True)]


def selectors(spec: dict) -> list[dict]:
    """Every node label a template requires: `key`, `operator`, `values` (a list, empty for
    `Exists` and `DoesNotExist`) and `where` it was found. A `nodeSelector` entry is `In` one
    value; preferred affinity terms and `matchFields` are not read."""
    out = []
    for key, value in (spec.get("nodeSelector") or {}).items():
        out.append({"key": key, "operator": OP_IN, "values": [value], "where": WHERE_NODE_SELECTOR})
    node_affinity = (spec.get("affinity") or {}).get("nodeAffinity") or {}
    for term in (node_affinity.get(AFFINITY_REQUIRED) or {}).get("nodeSelectorTerms") or []:
        if not isinstance(term, dict):
            continue
        for expr in term.get("matchExpressions") or []:
            if isinstance(expr, dict) and expr.get("key") and expr.get("operator"):
                out.append({"key": expr["key"], "operator": expr["operator"], "values": [v for v in expr.get("values") or [] if v is not None], "where": WHERE_NODE_AFFINITY})
    return out


def selected_values(spec: dict, key: str) -> list[str]:
    """The values a template requires for one label through `In` terms and its nodeSelector."""
    values = []
    for selector in selectors(spec):
        if selector["key"] == key and selector["operator"] == OP_IN:
            values.extend(selector["values"])
    return values


# ------------------------------------------------------------------------------ nodes


def node_allocatable(node: dict) -> tuple[int | None, int | None]:
    allocatable = (node.get("status") or {}).get("allocatable") or {}
    return cpu_millis(allocatable.get("cpu")), memory_bytes(allocatable.get("memory"))


def node_pool(node: dict) -> str:
    return ((node.get("metadata") or {}).get("labels") or {}).get(tables.NODEPOOL_LABEL, "")


def node_schedulable(node: dict) -> bool:
    """Ready and not cordoned: a node a displaced pod could land on."""
    if (node.get("spec") or {}).get("unschedulable"):
        return False
    for condition in (node.get("status") or {}).get("conditions") or []:
        if isinstance(condition, dict) and condition.get("type") == NODE_READY_CONDITION:
            return condition.get("status") == CONDITION_TRUE
    return False


# ------------------------------------------------------------------------------ pools


def pools_for_template(spec: dict, pools: list[dict]) -> list[dict]:
    """The pools a template can land on: the ones its gke-nodepool selector names, else all."""
    named = selected_values(spec, tables.NODEPOOL_LABEL)
    if named:
        return [p for p in pools if p.get("name") in named]
    return list(pools)


def pool_config(pool: dict) -> dict:
    config = pool.get("config")
    return config if isinstance(config, dict) else {}


def pool_scales_from_zero(pool: dict) -> bool:
    """An autoscaled pool whose minimum is zero nodes: empty today is not stranded."""
    autoscaling = pool.get("autoscaling")
    if not isinstance(autoscaling, dict) or not autoscaling.get("enabled"):
        return False
    return not (autoscaling.get("minNodeCount") or 0) and not (autoscaling.get("totalMinNodeCount") or 0)


def minor_text(version: tuple | None) -> str | None:
    return tables.format_minor(version) if version else None


def pool_below_target(pool: dict, target: tuple | None) -> bool:
    parsed = pool.get("parsed")
    return target is not None and parsed is not None and parsed < target


def pools_below_target(pools: list[dict], target: tuple | None) -> list[str]:
    return [p.get("name", "") for p in pools if pool_below_target(p, target)]


def cgroup_transition(pool: dict, target: tuple | None) -> tuple[str, str]:
    """Whether the upgrade to `target` moves `pool` to cgroup v2.

    `effectiveCgroupMode` is what the pool runs today; a `linuxNodeConfig.cgroupMode` pin
    to v1 holds until GKE removes v1 (1.35), an unpinned v1 pool is migrated from 1.33.
    """
    config = pool_config(pool)
    mode = config.get("effectiveCgroupMode")
    pin = (config.get("linuxNodeConfig") or {}).get("cgroupMode")
    if mode == tables.CGROUP_V2_MODE or pin == tables.CGROUP_MODE_V2_PIN:
        return CGROUP_ALREADY_V2, CGROUP_REASON_ALREADY
    if mode == tables.CGROUP_V1_MODE or pin == tables.CGROUP_MODE_V1_PIN:
        if target is None:
            return CGROUP_UNKNOWN, CGROUP_REASON_NO_TARGET
        pinned = pin == tables.CGROUP_MODE_V1_PIN
        threshold = tables.CGROUP_V1_REMOVAL_MINOR if pinned else tables.CGROUP_V2_MIGRATION_MINOR
        if target[1] >= threshold:
            reason = CGROUP_REASON_PIN_ENDS if pinned else CGROUP_REASON_MIGRATION
            return CGROUP_MOVES, reason.format(minor=threshold)
        return CGROUP_STAYS_V1, CGROUP_REASON_STAYS.format(minor=threshold)
    return CGROUP_UNKNOWN, CGROUP_REASON_UNREAD


# ----------------------------------------------------------------------------- images


def image_repository_and_tag(image: str) -> tuple[str, str]:
    """`docker.io/library/eclipse-temurin:8u302-b08-jre` -> (`eclipse-temurin`, `8u302-b08-jre`):
    the repository is the last path component, the tag what follows the colon."""
    name = image.partition(IMAGE_DIGEST_SEPARATOR)[0]
    path, sep, tag = name.rpartition(IMAGE_TAG_SEPARATOR)
    if not sep or PATH_SEPARATOR in tag:
        path, tag = name, ""
    return path.rsplit(PATH_SEPARATOR, 1)[-1], tag
