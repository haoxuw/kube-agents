"""
readiness_rules — the workload rules `upgrade_readiness.py` runs after its first three.

One module per rule, each exposing:

    RULE_ID: str      the key under `readiness.rules` in the JSON and the lead of a cell
    ENTRY: int        the catalogue entry the rule reads (docs/designs/upgrade-failure-catalogue.md)
    evaluate(cluster, member, items, target, context)
        -> {"blocking": [...], "risks": [...], "unknown": [...], "notes": [...]}
    describe(finding) -> str

`cluster` is the `clusters list` record; `member` the version row `grade_member` built;
`items` the objects the member's kubectl reads returned (PodDisruptionBudgets, Deployments,
StatefulSets, DaemonSets, CronJobs, PersistentVolumeClaims, StorageClasses and the projected
Nodes); `target` the parsed target version or None; `context` a dict with `target_text`,
`master` (parsed), `pools` (dicts with `name`, `version`, `parsed`, the pool's `config` and
`autoscaling`), `autopilot`, `at`, and `read_failures`, a dict keyed `workloads`, `storage`
and `nodes` whose value is the read's error when that read failed (see
`fleet_upgrade_report.read_cluster_objects`). A rule that needs a kind the failed read carries
records one rule-level `unknown` naming the reason and still grades what it has.

A finding is a dict with `rule`, `entry`, `grade`, `kind`, `namespace`, `name`, `object`
(`namespace/name`), `detail` and whatever else the rule records; `describe` renders it for
a table cell. `blocking` goes into the member's `blocked` verdict, `unknown` into `unknown`,
`risks` into the `risks` list the table prints; an `unknown` entry's `detail` is the reason
the rule could not decide, and a rule that could not run at all records one entry whose
`object` is None. `notes` are strings for the member's note column: shapes the rule saw and
decided are not an upgrade risk, named so a reader can check the reasoning. Every rule
reads pod templates, never Pods, and skips the namespaces GKE and its add-ons occupy
(`upgrade_shape_tables.SYSTEM_NAMESPACES` and its prefixes).

Pure functions over data the report has read; nothing here runs a command.
"""

import upgrade_shape_tables as tables

GRADE_BLOCKING = "blocking"
GRADE_RISK = "risk"
GRADE_UNKNOWN = "unknown"
RESULT_KEYS = ("blocking", "risks", "unknown", "notes")
# The three reads after the PDB read, as `context["read_failures"]` keys them.
READ_WORKLOADS = "workloads"
READ_STORAGE = "storage"
READ_NODES = "nodes"
OBJECT_FORMAT = "{namespace}/{name}"
DESCRIBE_FORMAT = "{kind} {object}: {detail}"
RULE_LEVEL_FORMAT = "{rule}: {detail}"
LIST_SEPARATOR = ", "
MINOR_FORMAT = "{major}.{minor}"
KIND_NODE = "Node"
KIND_CRONJOB = "CronJob"
KIND_STATEFULSET = "StatefulSet"
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


def empty_result() -> dict:
    return {key: [] for key in RESULT_KEYS}


def finding(rule: str, entry: int, grade: str, obj: dict, detail: str, **extra) -> dict:
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


def read_failure(context: dict, read: str) -> str | None:
    """The error of one of the reads after the PDB read, or None when it answered."""
    return (context.get("read_failures") or {}).get(read)


def is_system_namespace(namespace: str) -> bool:
    """A namespace GKE or one of its add-ons occupies."""
    return namespace in tables.SYSTEM_NAMESPACES or namespace.startswith(tables.SYSTEM_NAMESPACE_PREFIXES)


def objects_of_kind(items: list, kind: str) -> list[dict]:
    return [item for item in items or [] if isinstance(item, dict) and item.get("kind") == kind]


def pod_template_spec(workload: dict) -> dict | None:
    """The PodSpec a workload's template carries; a CronJob's sits under its jobTemplate."""
    spec = workload.get("spec") or {}
    if workload.get("kind") == KIND_CRONJOB:
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec")
    return (spec.get("template") or {}).get("spec")


def templates(items: list, kinds: tuple = tables.TEMPLATE_KINDS) -> list[tuple[dict, dict, dict]]:
    """(obj, pod_spec, workload) for every template of the kinds asked, outside the system namespaces."""
    out = []
    for item in items or []:
        if not isinstance(item, dict) or item.get("kind") not in kinds:
            continue
        meta = item.get("metadata") or {}
        namespace = meta.get("namespace", "") or ""
        if is_system_namespace(namespace):
            continue
        spec = pod_template_spec(item)
        if not isinstance(spec, dict):
            continue
        name = meta.get("name", "") or ""
        obj = {"kind": item["kind"], "namespace": namespace, "name": name, "object": OBJECT_FORMAT.format(namespace=namespace, name=name)}
        out.append((obj, spec, item))
    return out


def containers(spec: dict) -> list[dict]:
    return [c for c in (spec.get("containers") or []) + (spec.get("initContainers") or []) if isinstance(c, dict)]


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
    return MINOR_FORMAT.format(major=version[0], minor=version[1]) if version else None


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


def image_repository_and_tag(image: str) -> tuple[str, str]:
    """`docker.io/library/eclipse-temurin:8u302-b08-jre` -> (`eclipse-temurin`, `8u302-b08-jre`):
    the repository is the last path component, the tag what follows the colon."""
    name = image.partition(IMAGE_DIGEST_SEPARATOR)[0]
    path, sep, tag = name.rpartition(IMAGE_TAG_SEPARATOR)
    if not sep or PATH_SEPARATOR in tag:
        path, tag = name, ""
    return path.rsplit(PATH_SEPARATOR, 1)[-1], tag


def nodes(items: list) -> list[dict]:
    return objects_of_kind(items, KIND_NODE)


def node_pool_name(node: dict) -> str:
    return ((node.get("metadata") or {}).get("labels") or {}).get(tables.NODEPOOL_LABEL, "")
