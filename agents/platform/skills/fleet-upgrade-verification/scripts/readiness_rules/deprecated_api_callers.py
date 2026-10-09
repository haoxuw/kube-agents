#!/usr/bin/env python3
"""
deprecated_api_callers.py — catalogue entry 9: a client calls an API that is deprecated
and still served.

The API server stamps every request for a deprecated API version with the audit
annotation `k8s.io/deprecated=true`, a Cloud Logging entry label; an API with a removal
scheduled also carries `k8s.io/removed-release`, and those callers are the removed-API
rule's. This rule reads the shared audit-log read (one `gcloud logging read` per member,
last seven days) and files each operator-owned caller of a deprecated API as a risk,
named with its principal, its user agent and the API, with the successor where the
tables name one: nothing breaks on upgrade day, and the count is what a later removal
turns into an outage. GKE's own callers (kube-system's endpoint-controller is stamped on
every Service's Endpoints it writes) are counted in the note, not filed.

A read that fails or times out, and a page the limit cut, are `unknown` with the reason.
"""

import upgrade_shape_tables as tables
from readiness_rules import audit_log, finding

RULE_ID = "deprecated-api-callers"
CATALOGUE_ENTRY = 9

AUDIT_READ_FAILED = "deprecated-API callers not read: {reason}"
DEPRECATED_DETAIL = "deprecated and still served; the next removal is the outage, so plan the migration now"
NOTE_PLATFORM_CALLERS = "{count} GKE-managed caller{plural} of deprecated APIs not filed ({callers})"
NOTE_PLATFORM_MORE = ", and {more} more"
NOTE_PLATFORM_SHOWN = 3
NOTE_NONE = "no operator-owned caller of a deprecated API in {days}d"
PLATFORM_CALLER_FORMAT = "{principal} on {api}"
CALLER_PLURAL = "s"
LIST_SEPARATOR = ", "
DESCRIBE_FORMAT = "{principal} via {user_agent} calls {api} ({count} call{plural} in {days}d, last {last_seen}): {detail}"
SUCCESSOR_FORMAT = "; successor {successor}"


def _finding(caller: dict, log: dict) -> dict:
    return {
        "rule": RULE_ID,
        "tier": finding.TIER_RISK,
        "principal": caller["principal"],
        "user_agent": caller["user_agent"],
        "api": caller["api"],
        "successor": tables.DEPRECATED_API_SUCCESSORS.get(caller["api"]),
        "count": caller["count"],
        "first_seen": caller["first_seen"],
        "last_seen": caller["last_seen"],
        "window_days": log["window_days"],
        "detail": DEPRECATED_DETAIL,
    }


def evaluate(cluster: dict, member: dict, items: list | None, target, context: dict) -> dict:
    result = finding.empty_result()
    log = audit_log.read_callers(context)
    if log["error"]:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, AUDIT_READ_FAILED.format(reason=log["error"])))
        return result
    platform = []
    for caller in log["callers"]:
        if not caller["deprecated"] or caller["removed_release"]:
            continue
        if caller["platform"]:
            platform.append(PLATFORM_CALLER_FORMAT.format(principal=caller["principal"], api=caller["api"]))
            continue
        result[finding.RESULT_RISKS].append(_finding(caller, log))
    if log["truncated"]:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, audit_log.truncation_reason(log)))
    if platform:
        shown = LIST_SEPARATOR.join(platform[:NOTE_PLATFORM_SHOWN])
        if len(platform) > NOTE_PLATFORM_SHOWN:
            shown += NOTE_PLATFORM_MORE.format(more=len(platform) - NOTE_PLATFORM_SHOWN)
        finding.add_note(result, NOTE_PLATFORM_CALLERS.format(count=len(platform), plural="" if len(platform) == 1 else CALLER_PLURAL, callers=shown))
    if not result[finding.RESULT_RISKS] and not log["truncated"]:
        finding.add_note(result, NOTE_NONE.format(days=log["window_days"]))
    return result


def describe(item: dict) -> str:
    if item["tier"] == finding.TIER_UNKNOWN:
        return item["reason"]
    text = DESCRIBE_FORMAT.format(
        principal=item["principal"],
        user_agent=item["user_agent"],
        api=item["api"],
        count=item["count"],
        plural="" if item["count"] == 1 else CALLER_PLURAL,
        days=item["window_days"],
        last_seen=item["last_seen"],
        detail=item["detail"],
    )
    if item.get("successor"):
        text += SUCCESSOR_FORMAT.format(successor=item["successor"])
    return text
