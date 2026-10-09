#!/usr/bin/env python3
"""
removed_api_callers.py — catalogue entry 6: a client still calls an API version the target
minor no longer serves.

The API server stamps every request for a version it will stop serving with the audit
annotation `k8s.io/removed-release=<minor>`, which Cloud Logging keeps as an entry label.
The rule reads those entries through the shared audit-log read (one `gcloud logging read`
per member, last seven days) and grades each caller against the target: a caller of an
API removed at or before the target minor blocks the upgrade, named with its principal,
its user agent, the API and the release; a caller of an API removed after the target is
a risk for a later upgrade; a GKE-managed caller is a risk rather than a blocker, because
GKE moves its own components with the control plane. Where the GitOps scan's removal
table names a replacement for the API, the finding carries it.

A read that fails or times out, a run without a target, and a page the limit cut are each
`unknown` with the reason: the rule blocks on what it read and on nothing else.
"""

import upgrade_shape_tables as tables
from readiness_rules import audit_log, finding

RULE_ID = "removed-api-callers"
CATALOGUE_ENTRY = 6

AUDIT_READ_FAILED = "removed-API callers not read: {reason}"
NO_TARGET_REASON = "no target; whether a stamped removal falls inside this upgrade needs one"
BLOCKS_DETAIL = "removed at or before the target {target}; this caller fails the moment the control plane reaches it"
AFTER_TARGET_DETAIL = "removed after the target {target}; a later upgrade removes it"
PLATFORM_DETAIL = "GKE-managed caller; GKE moves it with the control plane, so it is not the operator's to fix"
RELEASE_UNPARSABLE_DETAIL = "the stamped release {release!r} did not parse; not graded against the target"
NO_TARGET_DETAIL = "removed in {release}; whether this upgrade crosses it needs a target"
NOTE_ENTRIES_READ = "{entries} stamped audit entr{plural} read over {days}d"
ENTRY_SINGULAR = "y"
ENTRY_PLURAL = "ies"
CALL_PLURAL = "s"
DESCRIBE_FORMAT = "{principal} via {user_agent} calls {api}, removed in {release} ({count} call{plural} in {days}d, last {last_seen}): {detail}"
REPLACEMENT_FORMAT = "; replacement {replacement}"


def _finding(caller: dict, tier: str, detail: str, log: dict) -> dict:
    return {
        "rule": RULE_ID,
        "tier": tier,
        "principal": caller["principal"],
        "user_agent": caller["user_agent"],
        "api": caller["api"],
        "removed_release": caller["removed_release"],
        "replacement": tables.replacement_for(caller["api"]),
        "platform": caller["platform"],
        "count": caller["count"],
        "first_seen": caller["first_seen"],
        "last_seen": caller["last_seen"],
        "window_days": log["window_days"],
        "detail": detail,
    }


def evaluate(cluster: dict, member: dict, items: list | None, target, context: dict) -> dict:
    result = finding.empty_result()
    log = audit_log.read_callers(context)
    if log["error"]:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, AUDIT_READ_FAILED.format(reason=log["error"])))
        return result
    target_minor = (target[0], target[1]) if target else None
    target_text = tables.format_minor(target_minor) if target_minor else None
    for caller in log["callers"]:
        release_text = caller["removed_release"]
        if not release_text:
            continue
        release = tables.parse_minor(release_text)
        if release is None:
            result[finding.RESULT_RISKS].append(_finding(caller, finding.TIER_RISK, RELEASE_UNPARSABLE_DETAIL.format(release=release_text), log))
        elif target_minor is None:
            result[finding.RESULT_RISKS].append(_finding(caller, finding.TIER_RISK, NO_TARGET_DETAIL.format(release=release_text), log))
        elif release > target_minor:
            result[finding.RESULT_RISKS].append(_finding(caller, finding.TIER_RISK, AFTER_TARGET_DETAIL.format(target=target_text), log))
        elif caller["platform"]:
            result[finding.RESULT_RISKS].append(_finding(caller, finding.TIER_RISK, PLATFORM_DETAIL, log))
        else:
            result[finding.RESULT_BLOCKING].append(_finding(caller, finding.TIER_BLOCKING, BLOCKS_DETAIL.format(target=target_text), log))
    if target_minor is None:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, NO_TARGET_REASON))
    if log["truncated"]:
        result[finding.RESULT_UNKNOWN].append(finding.unknown(RULE_ID, audit_log.truncation_reason(log)))
    finding.add_note(result, NOTE_ENTRIES_READ.format(entries=log["entries"], plural=ENTRY_SINGULAR if log["entries"] == 1 else ENTRY_PLURAL, days=log["window_days"]))
    return result


def describe(item: dict) -> str:
    if item["tier"] == finding.TIER_UNKNOWN:
        return item["reason"]
    text = DESCRIBE_FORMAT.format(
        principal=item["principal"],
        user_agent=item["user_agent"],
        api=item["api"],
        release=item["removed_release"],
        count=item["count"],
        plural="" if item["count"] == 1 else CALL_PLURAL,
        days=item["window_days"],
        last_seen=item["last_seen"],
        detail=item["detail"],
    )
    if item.get("replacement"):
        text += REPLACEMENT_FORMAT.format(replacement=item["replacement"])
    return text
