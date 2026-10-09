"""
Entry 2 of docs/designs/upgrade-failure-catalogue.md: no spare capacity for the
displaced pods.

On GKE's default surge settings (maxSurge 1, maxUnavailable 0) the replacement node
exists before a node is drained, so the entry needs a pool that removes a node first:
`maxUnavailable` above 0 on the surge strategy, or a blue-green upgrade with the
autoscaled rollout policy, whose green pool starts empty. On such a pool the pods of
the node being drained need room elsewhere. The rule measures that room from the
nodes' allocatable and the pods' requests, per dimension (CPU and memory): a pod only
this pool's nodes can take (a node selector, a required affinity or a taint no other
schedulable node satisfies) needs room on the pool's other nodes; any other pod needs
room on any other schedulable node. The pool's largest node, by the requests of the
pods it holds, is the one measured. DaemonSet and mirror pods are left out: the kubelet
recreates them on the rebuilt node, so a drain never has to find them room.

Blocking: maxUnavailable above 0 with maxSurge 0 on a pool the autoscaler cannot grow
(at its ceiling, or not autoscaled), with less room than its largest node's pods. Risk:
the same shortfall on a pool that keeps some surge or can still grow, or on an
autoscaled blue-green pool. A pool that removes a node first and has the room is a
note, so the setting stays visible. The room is summed across nodes, so a finding says
the pods fit in aggregate, not that each fits on one node.

Source for the settings and the strategies:
https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-pool-upgrade-strategies
"""

from readiness_rules import (
    KIND_NODE,
    LIST_SEPARATOR,
    active_pods,
    format_cpu,
    format_memory,
    is_daemonset_pod,
    is_mirror_pod,
    items_of_kind,
    new_result,
    node_allocatable,
    node_pool,
    node_schedulable,
    owner_label,
    pod_owner,
    pod_requests,
)

RULE_ID = "surge-capacity"
ENTRY = 2

# Upgrade settings as `nodePools[].upgradeSettings` spells them. A pool with no block is
# on GKE's defaults; inside a block the API omits a zero `maxSurge` (the fixture catalogue's
# state assertion for `readiness-surge-blocked` reads the same), so an absent key is 0.
UPGRADE_SETTINGS_KEY = "upgradeSettings"
STRATEGY_KEY = "strategy"
STRATEGY_SURGE = "SURGE"
STRATEGY_BLUE_GREEN = "BLUE_GREEN"
MAX_SURGE_KEY = "maxSurge"
MAX_UNAVAILABLE_KEY = "maxUnavailable"
DEFAULT_MAX_SURGE = 1
DEFAULT_MAX_UNAVAILABLE = 0
BLUE_GREEN_SETTINGS_KEY = "blueGreenSettings"
AUTOSCALED_ROLLOUT_KEY = "autoscaledRolloutPolicy"
# Autoscaler limits as `nodePools[].autoscaling` spells them: a total across zones when
# set, else a per-zone maximum times the pool's zones.
AUTOSCALING_KEY = "autoscaling"
AUTOSCALING_ENABLED_KEY = "enabled"
TOTAL_MAX_NODES_KEY = "totalMaxNodeCount"
MAX_NODES_PER_ZONE_KEY = "maxNodeCount"
POOL_LOCATIONS_KEY = "locations"
CLUSTER_LOCATIONS_KEY = "locations"
SINGLE_ZONE = 1
# Scheduling vocabulary, as the pod and node APIs spell it.
NODE_SELECTOR_KEY = "nodeSelector"
REQUIRED_NODE_AFFINITY_PATH = ("affinity", "nodeAffinity", "requiredDuringSchedulingIgnoredDuringExecution", "nodeSelectorTerms")
MATCH_EXPRESSIONS_KEY = "matchExpressions"
MATCH_FIELDS_KEY = "matchFields"
OP_IN = "In"
OP_NOT_IN = "NotIn"
OP_EXISTS = "Exists"
OP_DOES_NOT_EXIST = "DoesNotExist"
TOLERATION_EQUAL = "Equal"
REPELLING_TAINT_EFFECTS = ("NoSchedule", "NoExecute")
# How many of the largest displaced workloads a finding names.
NAMED_WORKLOADS = 3
NODE_COUNT_UNREAD = "?"

# Finding and note text.
WHY_SURGE = "maxSurge {max_surge}, maxUnavailable {max_unavailable}"
WHY_AUTOSCALED_BLUE_GREEN = "autoscaled blue-green, whose green pool starts empty"
SCALING_NOT_AUTOSCALED = "not autoscaled"
SCALING_AT_CEILING = "autoscaler at its ceiling of {ceiling}"
SCALING_CAN_GROW = "autoscaler can grow to {ceiling}"
POOL_SUMMARY = "pool {pool} ({why}; {node_count} node(s), {scaling})"
FINDING_TEXT = (
    "{summary} removes a node before its replacement exists; its largest node {node} holds "
    "{cpu} CPU / {memory} of pods and the room elsewhere is {room_cpu} / {room_memory}{pinned}: {workloads}"
)
PINNED_TEXT = " ({cpu} / {memory} of them can run only on this pool, against {room_cpu} / {room_memory} free on its other nodes)"
WORKLOAD_TEXT = "{owner} ({cpu} / {memory}{pinned})"
WORKLOAD_PINNED_SUFFIX = ", pinned to the pool"
NO_WORKLOADS = "no pods"
NOTE_HEADROOM = "{summary} removes a node before its replacement exists; the room elsewhere covers its largest node's pods"
NOTE_EMPTY = "{summary} removes a node before its replacement exists and has no node"
UNKNOWN_NO_TARGET = "{summary}: no target, so whether a node upgrade is pending is unknown"
UNKNOWN_VERSION = "{summary}: version unparsable, so whether a node upgrade is pending is unknown"
UNKNOWN_READ = "{summary}: cluster read failed, so its headroom was not measured"
UNKNOWN_QUANTITY = "{summary}: {what} did not parse, so its headroom was not measured"
QUANTITY_POD = "the requests of pod {namespace}/{name}"
QUANTITY_NODE = "the allocatable of node {name}"


def _int(value, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def pool_settings(pool: dict) -> dict:
    """strategy, maxSurge, maxUnavailable and whether the blue-green rollout is autoscaled."""
    settings = pool.get(UPGRADE_SETTINGS_KEY)
    if not isinstance(settings, dict):
        return {"strategy": STRATEGY_SURGE, "max_surge": DEFAULT_MAX_SURGE, "max_unavailable": DEFAULT_MAX_UNAVAILABLE, "autoscaled_blue_green": False}
    strategy = settings.get(STRATEGY_KEY) or STRATEGY_SURGE
    blue_green = settings.get(BLUE_GREEN_SETTINGS_KEY) or {}
    return {
        "strategy": strategy,
        "max_surge": _int(settings.get(MAX_SURGE_KEY), 0),
        "max_unavailable": _int(settings.get(MAX_UNAVAILABLE_KEY), 0),
        "autoscaled_blue_green": strategy == STRATEGY_BLUE_GREEN and isinstance(blue_green, dict) and AUTOSCALED_ROLLOUT_KEY in blue_green,
    }


def removes_node_first(settings: dict) -> bool:
    """Whether the pool's upgrade takes a node away before its replacement exists."""
    return (settings["strategy"] == STRATEGY_SURGE and settings["max_unavailable"] > 0) or settings["autoscaled_blue_green"]


def autoscaler_ceiling(pool: dict, cluster: dict) -> tuple[bool, int | None]:
    """(autoscaled, the most nodes the autoscaler may give the pool)."""
    autoscaling = pool.get(AUTOSCALING_KEY) or {}
    if not isinstance(autoscaling, dict) or not autoscaling.get(AUTOSCALING_ENABLED_KEY):
        return False, None
    total = _int(autoscaling.get(TOTAL_MAX_NODES_KEY), 0)
    if total > 0:
        return True, total
    zones = len(pool.get(POOL_LOCATIONS_KEY) or cluster.get(CLUSTER_LOCATIONS_KEY) or []) or SINGLE_ZONE
    return True, _int(autoscaling.get(MAX_NODES_PER_ZONE_KEY), 0) * zones


def _term_matches(term: dict, labels: dict) -> bool:
    if term.get(MATCH_FIELDS_KEY):
        return False
    for req in term.get(MATCH_EXPRESSIONS_KEY) or []:
        if not isinstance(req, dict):
            return False
        key, op, values = req.get("key"), req.get("operator"), req.get("values") or []
        if op == OP_IN:
            if labels.get(key) not in values:
                return False
        elif op == OP_NOT_IN:
            if key in labels and labels[key] in values:
                return False
        elif op == OP_EXISTS:
            if key not in labels:
                return False
        elif op == OP_DOES_NOT_EXIST:
            if key in labels:
                return False
        else:
            return False
    return True


def _tolerates(tolerations: list, taint: dict) -> bool:
    for toleration in tolerations:
        if not isinstance(toleration, dict):
            continue
        key = toleration.get("key")
        op = toleration.get("operator") or TOLERATION_EQUAL
        effect = toleration.get("effect")
        if key and key != taint.get("key"):
            continue
        if not key and op != OP_EXISTS:
            continue
        if op != OP_EXISTS and toleration.get("value") != taint.get("value"):
            continue
        if not effect or effect == taint.get("effect"):
            return True
    return False


def node_fits_pod(node: dict, spec: dict) -> bool:
    """Whether the pod's selector, required affinity and tolerations admit this node.

    Resources are not compared here; the room is measured in aggregate by the caller. An
    affinity operator or field this does not evaluate reads as not fitting, so the pod
    counts as pinned rather than as free to move.
    """
    labels = (node.get("metadata") or {}).get("labels") or {}
    for key, value in (spec.get(NODE_SELECTOR_KEY) or {}).items():
        if labels.get(key) != value:
            return False
    terms = spec
    for key in REQUIRED_NODE_AFFINITY_PATH:
        terms = (terms or {}).get(key) if isinstance(terms, dict) else None
    if terms and not any(isinstance(term, dict) and _term_matches(term, labels) for term in terms):
        return False
    tolerations = spec.get("tolerations") or []
    for taint in (node.get("spec") or {}).get("taints") or []:
        if isinstance(taint, dict) and taint.get("effect") in REPELLING_TAINT_EFFECTS and not _tolerates(tolerations, taint):
            return False
    return True


def _node_name(node: dict) -> str:
    return (node.get("metadata") or {}).get("name", "")


def _room(nodes: list[dict], pods_by_node: dict) -> tuple[int, int] | str:
    """Allocatable minus every active pod's requests, summed over `nodes`; a string names
    what did not parse."""
    cpu_total = memory_total = 0
    for node in nodes:
        cpu, memory = node_allocatable(node)
        if cpu is None or memory is None:
            return QUANTITY_NODE.format(name=_node_name(node))
        for pod in pods_by_node.get(_node_name(node), []):
            pod_cpu, pod_memory = pod_requests(pod.get("spec") or {})
            if pod_cpu is None or pod_memory is None:
                meta = pod.get("metadata") or {}
                return QUANTITY_POD.format(namespace=meta.get("namespace", ""), name=meta.get("name", ""))
            cpu -= pod_cpu
            memory -= pod_memory
        cpu_total += max(cpu, 0)
        memory_total += max(memory, 0)
    return cpu_total, memory_total


def measure(pool_nodes: list[dict], nodes: list[dict], pods: list[dict]) -> dict | str:
    """The pool's largest node, what a drain of it displaces, and the room for that.

    Returns the measurement, or a string naming the quantity that did not parse.
    """
    pods_by_node: dict[str, list[dict]] = {}
    for pod in pods:
        pods_by_node.setdefault((pod.get("spec") or {}).get("nodeName", ""), []).append(pod)
    pool_names = {_node_name(n) for n in pool_nodes}
    others = [n for n in nodes if _node_name(n) not in pool_names and node_schedulable(n)]

    best = None
    for node in pool_nodes:
        displaced = []
        for pod in pods_by_node.get(_node_name(node), []):
            if is_daemonset_pod(pod) or is_mirror_pod(pod):
                continue
            spec = pod.get("spec") or {}
            cpu, memory = pod_requests(spec)
            if cpu is None or memory is None:
                meta = pod.get("metadata") or {}
                return QUANTITY_POD.format(namespace=meta.get("namespace", ""), name=meta.get("name", ""))
            pinned = not any(node_fits_pod(other, spec) for other in others)
            displaced.append({"owner": owner_label(pod_owner(pod)), "cpu_m": cpu, "memory_bytes": memory, "pinned": pinned})
        total = (sum(d["cpu_m"] for d in displaced), sum(d["memory_bytes"] for d in displaced))
        if best is None or total > best[0]:
            best = (total, node, displaced)
    (cpu, memory), node, displaced = best

    pool_others = [n for n in pool_nodes if _node_name(n) != _node_name(node) and node_schedulable(n)]
    room_pool = _room(pool_others, pods_by_node)
    if isinstance(room_pool, str):
        return room_pool
    room_others = _room(others, pods_by_node)
    if isinstance(room_others, str):
        return room_others
    pinned_cpu = sum(d["cpu_m"] for d in displaced if d["pinned"])
    pinned_memory = sum(d["memory_bytes"] for d in displaced if d["pinned"])
    free_cpu, free_memory = cpu - pinned_cpu, memory - pinned_memory
    # The pinned pods take the pool's other nodes first; what is left there joins the
    # rest of the cluster as room for the pods that can move anywhere.
    room_free_cpu = room_others[0] + max(room_pool[0] - pinned_cpu, 0)
    room_free_memory = room_others[1] + max(room_pool[1] - pinned_memory, 0)
    short = pinned_cpu > room_pool[0] or pinned_memory > room_pool[1] or free_cpu > room_free_cpu or free_memory > room_free_memory
    return {
        "node": _node_name(node),
        "displaced": {"cpu_m": cpu, "memory_bytes": memory},
        "pinned": {"cpu_m": pinned_cpu, "memory_bytes": pinned_memory},
        "room_pool": {"cpu_m": room_pool[0], "memory_bytes": room_pool[1]},
        "room_elsewhere": {"cpu_m": room_free_cpu, "memory_bytes": room_free_memory},
        "short": short,
        "workloads": sorted(displaced, key=lambda d: (-d["cpu_m"], -d["memory_bytes"], d["owner"]))[:NAMED_WORKLOADS],
    }


def _summary(name: str, settings: dict, node_count, autoscaled: bool, ceiling: int | None, at_ceiling: bool) -> str:
    why = WHY_AUTOSCALED_BLUE_GREEN if settings["autoscaled_blue_green"] else WHY_SURGE.format(max_surge=settings["max_surge"], max_unavailable=settings["max_unavailable"])
    if not autoscaled:
        scaling = SCALING_NOT_AUTOSCALED
    elif at_ceiling:
        scaling = SCALING_AT_CEILING.format(ceiling=ceiling)
    else:
        scaling = SCALING_CAN_GROW.format(ceiling=ceiling)
    return POOL_SUMMARY.format(pool=name, why=why, node_count=node_count, scaling=scaling)


def evaluate(cluster: dict, member: dict, items, target, context) -> dict:
    out = new_result()
    pools_parsed = {p.get("name"): p.get("parsed") for p in (context or {}).get("pools") or []}
    nodes = items_of_kind(items, KIND_NODE) if items is not None else None
    pods = active_pods(items) if items is not None else []
    for pool in cluster.get("nodePools") or []:
        if not isinstance(pool, dict):
            continue
        settings = pool_settings(pool)
        if not removes_node_first(settings):
            continue
        name = pool.get("name", "")
        autoscaled, ceiling = autoscaler_ceiling(pool, cluster)
        pool_nodes = [n for n in nodes if node_pool(n) == name] if nodes is not None else None
        node_count = NODE_COUNT_UNREAD if pool_nodes is None else len(pool_nodes)
        at_ceiling = (not autoscaled) or (ceiling is not None and pool_nodes is not None and len(pool_nodes) >= ceiling)
        summary = _summary(name, settings, node_count, autoscaled, ceiling, at_ceiling)
        if target is None:
            out["unknown"].append(UNKNOWN_NO_TARGET.format(summary=summary))
            continue
        version = pools_parsed.get(name)
        if version is None:
            out["unknown"].append(UNKNOWN_VERSION.format(summary=summary))
            continue
        if version >= target:
            continue
        if pool_nodes is None:
            out["unknown"].append(UNKNOWN_READ.format(summary=summary))
            continue
        if not pool_nodes:
            out["notes"].append(NOTE_EMPTY.format(summary=summary))
            continue
        measured = measure(pool_nodes, nodes, pods)
        if isinstance(measured, str):
            out["unknown"].append(UNKNOWN_QUANTITY.format(summary=summary, what=measured))
            continue
        if not measured["short"]:
            out["notes"].append(NOTE_HEADROOM.format(summary=summary))
            continue
        finding = {
            "pool": name,
            "strategy": settings["strategy"],
            "max_surge": settings["max_surge"],
            "max_unavailable": settings["max_unavailable"],
            "autoscaled": autoscaled,
            "ceiling": ceiling,
            "at_ceiling": at_ceiling,
            "node_count": len(pool_nodes),
            "summary": summary,
            **measured,
        }
        blocks = settings["strategy"] == STRATEGY_SURGE and settings["max_surge"] == 0 and settings["max_unavailable"] > 0 and at_ceiling
        (out["blocking"] if blocks else out["risks"]).append(finding)
    return out


def describe(finding: dict) -> str:
    workloads = LIST_SEPARATOR.join(
        WORKLOAD_TEXT.format(owner=w["owner"], cpu=format_cpu(w["cpu_m"]), memory=format_memory(w["memory_bytes"]), pinned=WORKLOAD_PINNED_SUFFIX if w["pinned"] else "")
        for w in finding["workloads"]
    ) or NO_WORKLOADS
    pinned = ""
    if finding["pinned"]["cpu_m"] or finding["pinned"]["memory_bytes"]:
        pinned = PINNED_TEXT.format(
            cpu=format_cpu(finding["pinned"]["cpu_m"]),
            memory=format_memory(finding["pinned"]["memory_bytes"]),
            room_cpu=format_cpu(finding["room_pool"]["cpu_m"]),
            room_memory=format_memory(finding["room_pool"]["memory_bytes"]),
        )
    return FINDING_TEXT.format(
        summary=finding["summary"],
        node=finding["node"],
        cpu=format_cpu(finding["displaced"]["cpu_m"]),
        memory=format_memory(finding["displaced"]["memory_bytes"]),
        room_cpu=format_cpu(finding["room_elsewhere"]["cpu_m"]),
        room_memory=format_memory(finding["room_elsewhere"]["memory_bytes"]),
        pinned=pinned,
        workloads=workloads,
    )
