"""
Entry 12, a node label removed: a template whose nodeSelector or node affinity names a
label the rebuilt nodes no longer carry can never schedule again. A label (or label
value) the target minor drops (`DROPPED_NODE_LABELS`) is `blocking`; a label no node in
the template's pools carries today is a `risk`, as is one of the deprecated beta labels
the kubelet still sets, named with its GA replacement. Without Node objects in the read
the carried-today question is `unknown`.
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "removed-node-label"
ENTRY = 12
VALUE_FORMAT = "{key}={value}"
ANY_VALUE_FORMAT = "{key}"
DROPPED_DETAIL = "selects on {label} ({where}), dropped at {minor}: {source}; the target {target} is at or past it"
DROPPED_NO_TARGET_DETAIL = "selects on {label} ({where}), dropped at {minor}: {source}; whether the target crosses it needs a target"
NOT_CARRIED_DETAIL = "selects on {label} ({where}) and no node in {scope} carries it today; the pods do not schedule now and the rebuilt nodes carry it no more"
DEPRECATED_DETAIL = "selects on {label} ({where}), deprecated since {since} in favour of {replacement}; the kubelet still sets it today, and a node image that stops setting it strands the pods"
DEPRECATED_PREFIX_DETAIL = "selects on {label} ({where}), a beta-prefixed label Kubernetes has deprecated; a node image that stops setting it strands the pods"
NO_NODES_DETAIL = "selects on {label} ({where}); no Node object was read, so whether a node carries it is unknown"
SCOPE_POOLS = "pool(s) {pools}"
SCOPE_CLUSTER = "the cluster"
DEPRECATED_SINCE_UNKNOWN = "its deprecation"


def _label_text(key: str, value) -> str:
    return VALUE_FORMAT.format(key=key, value=value) if value is not None else ANY_VALUE_FORMAT.format(key=key)


def _dropped_row(key: str, values: list | None):
    """The DROPPED_NODE_LABELS row a selector hits: the key with any value, or the key with
    the dropped value among the selector's values."""
    for label, value, minor, source in tables.DROPPED_NODE_LABELS:
        if label != key:
            continue
        if value is None or (values is not None and value in values):
            return label, value, minor, source
    return None


def _carried(key: str, values: list | None, node_list: list[dict]) -> bool:
    for node in node_list:
        labels = (node.get("metadata") or {}).get("labels") or {}
        if key not in labels:
            continue
        if values is None or labels[key] in values:
            return True
    return False


def evaluate(cluster: dict, member: dict, items: list, target, context: dict) -> dict:
    result = rules.empty_result()
    all_nodes = rules.nodes(items)
    for obj, spec, _ in rules.templates(items):
        pools = rules.pools_for_template(spec, context.get("pools") or [])
        pool_names = {p.get("name", "") for p in pools}
        named = rules.selected_values(spec, tables.NODEPOOL_LABEL)
        node_list = [n for n in all_nodes if not named or rules.node_pool_name(n) in pool_names]
        scope = SCOPE_POOLS.format(pools=rules.LIST_SEPARATOR.join(sorted(pool_names))) if named else SCOPE_CLUSTER
        seen = set()
        for selector in rules.selectors(spec):
            key, values, where = selector["key"], selector["values"], selector["where"]
            if (key, tuple(values or ())) in seen:
                continue
            seen.add((key, tuple(values or ())))
            dropped = _dropped_row(key, values)
            if dropped is not None:
                label, value, minor, source = dropped
                text = _label_text(label, value)
                minor_text = rules.minor_text(minor)
                if target is None:
                    detail = DROPPED_NO_TARGET_DETAIL.format(label=text, where=where, minor=minor_text, source=source)
                    result["unknown"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_UNKNOWN, obj, detail, label=key, value=value))
                    continue
                if target[:2] >= minor:
                    detail = DROPPED_DETAIL.format(label=text, where=where, minor=minor_text, source=source, target=rules.minor_text(target))
                    result["blocking"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_BLOCKING, obj, detail, label=key, value=value, dropped_at=minor_text))
                    continue
            text = _label_text(key, values[0] if values and len(values) == 1 else None)
            if not all_nodes:
                detail = NO_NODES_DETAIL.format(label=text, where=where)
                result["unknown"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_UNKNOWN, obj, detail, label=key))
                continue
            if not _carried(key, values, node_list):
                detail = NOT_CARRIED_DETAIL.format(label=text, where=where, scope=scope)
                result["risks"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, label=key))
                continue
            if key in tables.DEPRECATED_NODE_LABELS:
                replacement, since = tables.DEPRECATED_NODE_LABELS[key]
                detail = DEPRECATED_DETAIL.format(label=text, where=where, since=since, replacement=replacement)
                result["risks"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, label=key, replacement=replacement))
            elif key.startswith(tables.DEPRECATED_NODE_LABEL_PREFIXES):
                detail = DEPRECATED_PREFIX_DETAIL.format(label=text, where=where)
                result["risks"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, label=key))
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)
