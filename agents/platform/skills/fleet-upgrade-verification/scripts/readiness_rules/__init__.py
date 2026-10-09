"""
readiness_rules — the per-entry rules `upgrade_readiness.EXTRA_RULES` runs after the
PDB, maintenance and skew rules.

One module per entry of docs/designs/upgrade-failure-catalogue.md. Each module exposes:

- `RULE_ID`, the slug the report keys the rule's result under, and `ENTRY`, the
  catalogue entry number;
- `evaluate(cluster, member, items, target, context) -> dict`, a pure function over
  what the report script has already read: `cluster` is the `gcloud container clusters
  list` record, `member` the graded version row, `items` the objects of the member's one
  `kubectl get` (None when that read failed), `target` the parsed target version tuple
  (None without one) and `context` the run: `clusters` (every cluster record in the run),
  `pools` (the member's pools with their parsed versions), `project` and `at`. It returns
  `blocking` (findings that block the upgrade), `risks` (findings that do not block but
  must be named), `unknown` (reasons the rule could not decide) and `notes` (facts worth
  a line in the note column that are neither);
- `describe(finding) -> str`, one finding as a table cell.

A rule reads; it never runs a command. This file holds what the modules share: the
result shape, the quantity parser, a pod's owner and requests, and the kinds the read
carries.
"""

import re

# The kinds `fleet_upgrade_report.KUBECTL_RESOURCES` reads, as `kind` spells them.
KIND_POD = "Pod"
KIND_NODE = "Node"
KIND_DEPLOYMENT = "Deployment"
KIND_STATEFULSET = "StatefulSet"
KIND_DAEMONSET = "DaemonSet"
KIND_REPLICASET = "ReplicaSet"
KIND_PERSISTENT_VOLUME = "PersistentVolume"
KIND_NETWORK_POLICY = "NetworkPolicy"
TEMPLATE_KINDS = (KIND_DEPLOYMENT, KIND_STATEFULSET, KIND_DAEMONSET)
# A pod's owner and how a finding names it. A ReplicaSet is named for its Deployment:
# the ReplicaSet's name is the Deployment's plus `-<pod-template-hash>`.
POD_TEMPLATE_HASH_LABEL = "pod-template-hash"
OWNER_FORMAT = "{kind} {namespace}/{name}"
NAME_SEPARATOR = "-"
# The node label GKE stamps with the pool's name.
NODEPOOL_LABEL = "cloud.google.com/gke-nodepool"
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
RESULT_KEYS = ("blocking", "risks", "unknown", "notes")
LIST_SEPARATOR = ", "


def new_result() -> dict:
    """An empty rule result, every key present."""
    return {key: [] for key in RESULT_KEYS}


def get_path(record, path: tuple):
    """The value at a dotted path of nested dicts, None where any step is missing."""
    value = record
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def items_of_kind(items, kind: str) -> list[dict]:
    return [item for item in items or [] if isinstance(item, dict) and item.get("kind") == kind]


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
    return OWNER_FORMAT.format(**owner)


def template_specs(items) -> list[tuple[dict, dict]]:
    """(owner, pod spec) for every Deployment, StatefulSet and DaemonSet template read."""
    specs = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in TEMPLATE_KINDS:
            continue
        meta = item.get("metadata") or {}
        owner = {"kind": item.get("kind"), "namespace": meta.get("namespace", ""), "name": meta.get("name", "")}
        spec = (((item.get("spec") or {}).get("template") or {}).get("spec")) or {}
        specs.append((owner, spec))
    return specs


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


def node_allocatable(node: dict) -> tuple[int | None, int | None]:
    allocatable = (node.get("status") or {}).get("allocatable") or {}
    return cpu_millis(allocatable.get("cpu")), memory_bytes(allocatable.get("memory"))


def node_pool(node: dict) -> str:
    return ((node.get("metadata") or {}).get("labels") or {}).get(NODEPOOL_LABEL, "")


def node_schedulable(node: dict) -> bool:
    """Ready and not cordoned: a node a displaced pod could land on."""
    if (node.get("spec") or {}).get("unschedulable"):
        return False
    for condition in (node.get("status") or {}).get("conditions") or []:
        if isinstance(condition, dict) and condition.get("type") == NODE_READY_CONDITION:
            return condition.get("status") == CONDITION_TRUE
    return False


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
