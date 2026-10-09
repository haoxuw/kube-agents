#!/usr/bin/env python3
"""
audit_log.py — the one Cloud Logging read the audit-log rules share.

Not a rule. `read_callers(context)` runs `gcloud logging read` once per member over the
last AUDIT_LOG_WINDOW_DAYS for the Kubernetes audit entries that carry the API server's
deprecation annotations (`k8s.io/deprecated` and `k8s.io/removed-release`, which Cloud
Logging stores as entry labels) or a kubectl user agent, and groups them by caller
(principal and user agent) and API. The result is cached in the context, so the
removed-API rule (catalogue entry 6), the deprecated-API rule (entry 9) and the client-skew
rule (entry 10) read the log once between them.

The read is bounded twice: `--limit` caps the entries, and the runner the context carries
is the report's `run_cmd`, whose default timeout is the report's 60-second cap on every
gcloud call. A read that fails, times out or returns something that is not a JSON list is
an error the rules report as `unknown` with the reason, never as a blocker. A full page
means the window was cut, and the result says so, because a rule that saw no caller on a
cut window has not shown there is none.

`gcloud logging read` is on the agent's gcloud read allowlist
(agents/platform/scripts/command_policy.py), with `--freshness`, `--limit`, `--project`
and `--format` among the flags it admits.
"""

import json
import re

import upgrade_shape_tables as tables

# The command and its filter. Cloud Logging keeps GKE's Kubernetes audit entries under
# resource type k8s_cluster, with the cluster and location as resource labels and the
# API server's audit annotations as entry labels; the user agent is the AuditLog's
# callerSuppliedUserAgent. The three alternatives are the three rules' inputs.
GCLOUD = "gcloud"
LOGGING_READ = ("logging", "read")
RESOURCE_TYPE = "k8s_cluster"
LABEL_DEPRECATED = "k8s.io/deprecated"
LABEL_REMOVED_RELEASE = "k8s.io/removed-release"
DEPRECATED_TRUE = "true"
USER_AGENT_FIELD = "protoPayload.requestMetadata.callerSuppliedUserAgent"
KUBECTL_USER_AGENT_PREFIX = "kubectl/"
FILTER_FORMAT = (
    'resource.type="{resource_type}" AND resource.labels.cluster_name="{cluster}" '
    'AND resource.labels.location="{location}" '
    'AND (labels."{removed}":* OR labels."{deprecated}"="{deprecated_true}" '
    'OR {user_agent_field}:"{kubectl}")'
)
PROJECT_FLAG = "--project={project}"
FRESHNESS_FLAG = "--freshness={days}d"
LIMIT_FLAG = "--limit={limit}"
JSON_FORMAT_FLAG = "--format=json"
# Characters escaped inside a quoted filter string, so a cluster name read from the API
# can never close the quote. Real GKE names are lowercase alphanumerics and hyphens.
FILTER_ESCAPE_RE = re.compile(r'(["\\])')
FILTER_ESCAPE_REPLACEMENT = r"\\\1"

# Where one member's read is cached in the context, and the reasons a read is not made.
CACHE_KEY = "audit_log"
CONTEXT_RUNNER_KEY = "run_cmd"
CONTEXT_CACHE_KEY = "cache"
CONTEXT_PROJECT_KEY = "project"
CONTEXT_LOCATION_KEY = "location"
CONTEXT_CLUSTER_KEY = "cluster_name"
NO_RUNNER_REASON = "no command runner in the readiness context; the audit log was not read"
NO_SCOPE_REASON = "the readiness context carries no project, location or cluster name for the log filter"
READ_FAILED_REASON = "`gcloud logging read` failed (rc={rc}): {stderr}"
UNPARSABLE_REASON = "`gcloud logging read` returned output that is not a JSON list of entries (rc=0)"
TRUNCATED_REASON = (
    "the audit-log read returned a full page of {limit} entries, so older entries in the "
    "{days}-day window were not read; a caller absent from this page may still exist"
)
STDERR_EXCERPT_CHARS = 300
NO_STDERR = "no stderr"

# Audit entry fields.
PROTO_PAYLOAD = "protoPayload"
LABELS = "labels"
TIMESTAMP = "timestamp"
AUTHENTICATION_INFO = "authenticationInfo"
PRINCIPAL_EMAIL = "principalEmail"
REQUEST_METADATA = "requestMetadata"
CALLER_SUPPLIED_USER_AGENT = "callerSuppliedUserAgent"
RESOURCE_NAME = "resourceName"
METHOD_NAME = "methodName"
# `<group>/<version>[/namespaces/<ns>]/<resource>[/<name>[/<subresource>]]`, the AuditLog
# resourceName GKE writes (`core/v1/namespaces/x/endpoints/y`). The methodName is the
# fallback: `io.k8s.<group, reverse-DNS>.<version>.<resource>.<verb>`, where the group
# cannot always be read back exactly (`io.k8s.apps` is `apps`, `io.k8s.apiserver.flowcontrol`
# is `flowcontrol.apiserver.k8s.io`), so the fallback names it as the method spells it.
VERSION_TOKEN_RE = re.compile(r"^v\d+(?:(?:alpha|beta)\d+)?$")
NAMESPACES_SEGMENT = "namespaces"
NAMESPACED_PREFIX_LENGTH = 2
METHOD_PREFIX = ("io", "k8s")
CORE_GROUP = "core"
API_FORMAT = "{group}/{version} {resource}"
UNKNOWN_API = "(api not named in the entry)"
UNKNOWN_PRINCIPAL = "(no principal in the entry)"
UNKNOWN_USER_AGENT = "(no user agent)"

# Callers that are GKE's or Kubernetes' own: the control plane's components, the node
# identities, workloads in the namespaces GKE manages, and Google's service agents. A
# deprecated or removed API such a caller uses is GKE's to move with the control plane,
# so the rules list these callers rather than file them as the operator's finding;
# kube-system's endpoint-controller is the standing example, stamped `k8s.io/deprecated`
# on every Service's Endpoints it writes.
PLATFORM_PRINCIPAL_PREFIXES = (
    "system:kube-",
    "system:apiserver",
    "system:addon-manager",
    "system:node:",
    "system:serviceaccount:kube-system:",
    "system:serviceaccount:kube-public:",
    "system:serviceaccount:kube-node-lease:",
    "system:serviceaccount:gke-",
    "system:serviceaccount:gmp-",
)
GOOGLE_SERVICE_AGENT_SUFFIXES = (
    "@container-engine-robot.iam.gserviceaccount.com",
    "@system.gserviceaccount.com",
)
GOOGLE_SERVICE_AGENT_MARKERS = ("@gcp-sa-",)


def _escape(value) -> str:
    return FILTER_ESCAPE_RE.sub(FILTER_ESCAPE_REPLACEMENT, str(value or ""))


def build_filter(cluster: str, location: str) -> str:
    """The Cloud Logging filter for one cluster's stamped audit entries and kubectl calls."""
    return FILTER_FORMAT.format(
        resource_type=RESOURCE_TYPE,
        cluster=_escape(cluster),
        location=_escape(location),
        removed=LABEL_REMOVED_RELEASE,
        deprecated=LABEL_DEPRECATED,
        deprecated_true=DEPRECATED_TRUE,
        user_agent_field=USER_AGENT_FIELD,
        kubectl=KUBECTL_USER_AGENT_PREFIX,
    )


def build_command(project: str, cluster: str, location: str) -> list[str]:
    return [
        GCLOUD,
        *LOGGING_READ,
        build_filter(cluster, location),
        PROJECT_FLAG.format(project=project),
        FRESHNESS_FLAG.format(days=tables.AUDIT_LOG_WINDOW_DAYS),
        LIMIT_FLAG.format(limit=tables.AUDIT_LOG_LIMIT),
        JSON_FORMAT_FLAG,
    ]


def is_platform_caller(principal) -> bool:
    """Whether a principal is GKE's or Kubernetes' own rather than the operator's."""
    text = str(principal or "")
    if text.startswith(PLATFORM_PRINCIPAL_PREFIXES):
        return True
    if text.endswith(GOOGLE_SERVICE_AGENT_SUFFIXES):
        return True
    return any(marker in text for marker in GOOGLE_SERVICE_AGENT_MARKERS)


def api_of(resource_name, method_name) -> str:
    """`core/v1 endpoints` from the entry's resourceName, or from its methodName."""
    parts = [p for p in str(resource_name or "").split("/") if p]
    if len(parts) >= 3 and VERSION_TOKEN_RE.match(parts[1]):
        rest = parts[2:]
        if rest[0] == NAMESPACES_SEGMENT and len(rest) > NAMESPACED_PREFIX_LENGTH:
            rest = rest[NAMESPACED_PREFIX_LENGTH:]
        return API_FORMAT.format(group=parts[0], version=parts[1], resource=rest[0])
    tokens = str(method_name or "").split(".")
    for index, token in enumerate(tokens):
        if VERSION_TOKEN_RE.match(token) and index + 1 < len(tokens):
            group_tokens = tokens[:index]
            if tuple(group_tokens[: len(METHOD_PREFIX)]) == METHOD_PREFIX:
                group_tokens = group_tokens[len(METHOD_PREFIX) :]
            group = CORE_GROUP if group_tokens == [CORE_GROUP] else ".".join(reversed(group_tokens))
            return API_FORMAT.format(group=group or CORE_GROUP, version=token, resource=tokens[index + 1])
    return UNKNOWN_API


def caller_records(entries: list) -> list[dict]:
    """Entries grouped by (principal, user agent, API), newest-first timestamps kept as read.

    Each record carries the deprecation labels seen on the group: `removed_release` is the
    release the API server stamped (the same on every entry of one API), `deprecated` is
    whether any entry carried `k8s.io/deprecated=true`, `kubectl` whether the user agent is
    kubectl's. Timestamps are the RFC 3339 strings Cloud Logging wrote, compared as text,
    which orders correctly within one logger's format.
    """
    grouped: dict[tuple, dict] = {}
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        payload = entry.get(PROTO_PAYLOAD) if isinstance(entry.get(PROTO_PAYLOAD), dict) else {}
        labels = entry.get(LABELS) if isinstance(entry.get(LABELS), dict) else {}
        principal = (payload.get(AUTHENTICATION_INFO) or {}).get(PRINCIPAL_EMAIL) or UNKNOWN_PRINCIPAL
        user_agent = (payload.get(REQUEST_METADATA) or {}).get(CALLER_SUPPLIED_USER_AGENT) or UNKNOWN_USER_AGENT
        api = api_of(payload.get(RESOURCE_NAME), payload.get(METHOD_NAME))
        key = (principal, user_agent, api)
        record = grouped.setdefault(
            key,
            {
                "principal": principal,
                "user_agent": user_agent,
                "api": api,
                "removed_release": None,
                "deprecated": False,
                "kubectl": str(user_agent).startswith(KUBECTL_USER_AGENT_PREFIX),
                "platform": is_platform_caller(principal),
                "count": 0,
                "first_seen": None,
                "last_seen": None,
            },
        )
        record["count"] += 1
        removed = labels.get(LABEL_REMOVED_RELEASE)
        if removed and not record["removed_release"]:
            record["removed_release"] = str(removed)
        if str(labels.get(LABEL_DEPRECATED, "")).lower() == DEPRECATED_TRUE:
            record["deprecated"] = True
        stamp = entry.get(TIMESTAMP)
        if isinstance(stamp, str) and stamp:
            if record["first_seen"] is None or stamp < record["first_seen"]:
                record["first_seen"] = stamp
            if record["last_seen"] is None or stamp > record["last_seen"]:
                record["last_seen"] = stamp
    return sorted(grouped.values(), key=lambda r: (r["principal"], r["user_agent"], r["api"]))


def read_callers(context: dict) -> dict:
    """The member's stamped audit callers, read once and cached in `context["cache"]`.

    Returns `{"command", "error", "entries", "truncated", "callers", "window_days", "limit"}`.
    `error` is the reason the read produced nothing usable (no runner, a failed or timed-out
    command, unparsable output); `truncated` says the page was full.
    """
    cache = context.setdefault(CONTEXT_CACHE_KEY, {})
    if CACHE_KEY in cache:
        return cache[CACHE_KEY]
    result = {
        "command": None,
        "error": None,
        "entries": 0,
        "truncated": False,
        "callers": [],
        "window_days": tables.AUDIT_LOG_WINDOW_DAYS,
        "limit": tables.AUDIT_LOG_LIMIT,
    }
    cache[CACHE_KEY] = result
    run = context.get(CONTEXT_RUNNER_KEY)
    project = context.get(CONTEXT_PROJECT_KEY)
    location = context.get(CONTEXT_LOCATION_KEY)
    cluster = context.get(CONTEXT_CLUSTER_KEY)
    if run is None:
        result["error"] = NO_RUNNER_REASON
        return result
    if not (project and location and cluster):
        result["error"] = NO_SCOPE_REASON
        return result
    cmd = build_command(project, cluster, location)
    result["command"] = " ".join(cmd)
    # The runner's own default timeout is the report's per-call cap; a timed-out call
    # comes back as a non-zero rc with the reason on stderr, like any failed read.
    rc, stdout, stderr = run(cmd)
    if rc != 0:
        result["error"] = READ_FAILED_REASON.format(rc=rc, stderr=(stderr or "").strip()[:STDERR_EXCERPT_CHARS] or NO_STDERR)
        return result
    if not (stdout or "").strip():
        return result
    try:
        entries = json.loads(stdout)
    except ValueError:
        result["error"] = UNPARSABLE_REASON
        return result
    if not isinstance(entries, list):
        result["error"] = UNPARSABLE_REASON
        return result
    result["entries"] = len(entries)
    result["truncated"] = len(entries) >= tables.AUDIT_LOG_LIMIT
    result["callers"] = caller_records(entries)
    return result


def truncation_reason(log: dict) -> str:
    return TRUNCATED_REASON.format(limit=log["limit"], days=log["window_days"])
