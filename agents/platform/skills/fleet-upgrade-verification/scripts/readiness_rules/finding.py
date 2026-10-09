#!/usr/bin/env python3
"""
finding.py — the shape every readiness rule returns and the helpers the rules share.

Not a rule. A rule's `evaluate` returns `empty_result()` filled in: three lists of
findings, one per tier, plus a `note` the report appends to the member's note column for
what the rule read but did not file (the provider's callers, namespaces pinned to a
version, a sampled read). Every finding carries `rule` and `tier`; an `unknown` finding
carries `reason`. `pod_spec` is the one pod-template walk, so a kind added for one rule is
read by every rule.
"""

TIER_BLOCKING = "blocking"
TIER_RISK = "risk"
TIER_UNKNOWN = "unknown"
# The result keys, in the order the report renders them.
RESULT_BLOCKING = "blocking"
RESULT_RISKS = "risks"
RESULT_UNKNOWN = "unknown"
RESULT_NOTE = "note"
RESULT_KEYS = (RESULT_BLOCKING, RESULT_RISKS, RESULT_UNKNOWN)
NOTE_SEPARATOR = "; "

# Where a workload's pod template sits, per kind the readiness read returns.
TEMPLATE_PATHS = {
    "Deployment": ("spec", "template"),
    "StatefulSet": ("spec", "template"),
    "DaemonSet": ("spec", "template"),
    "CronJob": ("spec", "jobTemplate", "spec", "template"),
}
POD_SPEC_KEY = "spec"
KIND_KEY = "kind"
METADATA_KEY = "metadata"
NAMESPACE_KEY = "namespace"
NAME_KEY = "name"
WORKLOAD_FORMAT = "{kind} {namespace}/{name}"


def empty_result() -> dict:
    return {RESULT_BLOCKING: [], RESULT_RISKS: [], RESULT_UNKNOWN: [], RESULT_NOTE: ""}


def unknown(rule_id: str, reason: str) -> dict:
    return {"rule": rule_id, "tier": TIER_UNKNOWN, "reason": reason}


def add_note(result: dict, note: str) -> None:
    """Appends one clause to the result's note; empty clauses are dropped."""
    if not note:
        return
    result[RESULT_NOTE] = NOTE_SEPARATOR.join(part for part in (result.get(RESULT_NOTE, ""), note) if part)


def pod_spec(item) -> dict | None:
    """The pod spec of a workload the read returns, or None for a kind without a template."""
    if not isinstance(item, dict):
        return None
    path = TEMPLATE_PATHS.get(item.get(KIND_KEY))
    if path is None:
        return None
    node = item
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    spec = (node or {}).get(POD_SPEC_KEY) if isinstance(node, dict) else None
    return spec if isinstance(spec, dict) else None


def workload_label(item: dict) -> str:
    """`Deployment ns/name`, how every rule names a workload."""
    meta = item.get(METADATA_KEY) or {}
    return WORKLOAD_FORMAT.format(kind=item.get(KIND_KEY), namespace=meta.get(NAMESPACE_KEY, ""), name=meta.get(NAME_KEY, ""))
