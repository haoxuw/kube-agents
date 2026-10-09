#!/usr/bin/env python3
"""
finding.py — the shape every readiness rule returns and the two helpers that build it.

Not a rule. A rule's `evaluate` returns `empty_result()` filled in: three lists of
findings, one per tier, plus a `note` the report appends to the member's note column for
what the rule read but did not file (GKE's own callers, namespaces pinned to a version).
Every finding carries `rule` and `tier`; an `unknown` finding carries `reason`.
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


def empty_result() -> dict:
    return {RESULT_BLOCKING: [], RESULT_RISKS: [], RESULT_UNKNOWN: [], RESULT_NOTE: ""}


def unknown(rule_id: str, reason: str) -> dict:
    return {"rule": rule_id, "tier": TIER_UNKNOWN, "reason": reason}


def add_note(result: dict, note: str) -> None:
    """Appends one clause to the result's note; empty clauses are dropped."""
    if not note:
        return
    result[RESULT_NOTE] = NOTE_SEPARATOR.join(part for part in (result.get(RESULT_NOTE, ""), note) if part)
