#!/usr/bin/env python3
"""
audit_log.py — the two Cloud Logging reads the audit-log rules share.

Not a rule. `read(context, which)` runs `gcloud logging read` for one member over the seven
days ending at the evaluation instant, pages it by timestamp bound, groups the entries by
caller (principal and user agent) and API, and caches the result in the context, so the
removed-API rule (catalogue entry 6), the deprecated-API rule (entry 9) and the client-skew
rule (entry 10) read the log twice between them rather than once each:

- the removed-release read: entries the API server stamped `k8s.io/removed-release`;
- the deprecated-or-kubectl read: entries stamped `k8s.io/deprecated` with no removal
  release, and every write whose user agent is kubectl's.

Two reads rather than one, so a chatty deprecated writer (the seeded fixture writes every
ten minutes) cannot fill the page a removed-API caller would have been on. The provider's
own principals are excluded in the filter where the query language can say it, and
`caller_class` places every caller that comes back: the operator's (user accounts, service
accounts, and Kubernetes service accounts outside the system namespaces), the provider's
(every other `system:` principal, the node identities, Google-managed service agents), or
unplaced, which a rule names and never blocks on.

The log is the Admin Activity audit log, which carries writes only: a caller that only
reads an API is not in it, and GKE Deprecation Insights are the cross-check. A read that
fails or times out is an error the rules report as `unknown`, never as a blocker; a read
that still fills its last page is graded from what it saw and says so (`sampled`).

The context the reads run through is the per-member one readiness_rules documents:
`run_cmd` with `timeout_seconds`, `project`, `location`, `cluster_name`, `at` and `cache`.

`gcloud logging read` is on the agent's gcloud read allowlist
(agents/platform/scripts/command_policy.py) with `--limit`, `--project` and `--format`
among the flags it admits. The command has no `--page-size`, so a page is `--limit` and the
next page is a `timestamp<` bound on the oldest entry seen; `--freshness` is not used,
because gcloud refuses it beside a timestamp in the filter, and the window is in the filter.
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone

import upgrade_shape_tables as tables
from readiness_rules import CONTEXT_AT, CONTEXT_CACHE, CONTEXT_CLOCK, CONTEXT_CLUSTER_NAME, CONTEXT_LOCATION, CONTEXT_PROJECT, CONTEXT_RUN_CMD, CONTEXT_TIMEOUT_SECONDS

# The command. Cloud Logging keeps GKE's Kubernetes audit entries under resource type
# k8s_cluster, with the cluster and location as resource labels, the API server's audit
# annotations as entry labels, and the user agent and principal in the AuditLog payload.
GCLOUD = "gcloud"
LOGGING_READ = ("logging", "read")
RESOURCE_TYPE = "k8s_cluster"
LABEL_DEPRECATED = "k8s.io/deprecated"
LABEL_REMOVED_RELEASE = "k8s.io/removed-release"
DEPRECATED_TRUE = "true"
USER_AGENT_FIELD = "protoPayload.requestMetadata.callerSuppliedUserAgent"
PRINCIPAL_FIELD = "protoPayload.authenticationInfo.principalEmail"
KUBECTL_USER_AGENT_PREFIX = "kubectl/"
PROJECT_FLAG = "--project={project}"
LIMIT_FLAG = "--limit={limit}"
JSON_FORMAT_FLAG = "--format=json"

# The two reads and their filters.
READ_REMOVED = "removed"
READ_DEPRECATED = "deprecated"
READS = (READ_REMOVED, READ_DEPRECATED)
SCOPE_CLAUSE = 'resource.type="{resource_type}" AND resource.labels.cluster_name="{cluster}" AND resource.labels.location="{location}"'
REMOVED_CLAUSE = 'labels."{removed}":*'
DEPRECATED_CLAUSE = '((labels."{deprecated}"="{deprecated_true}" AND NOT labels."{removed}":*) OR {user_agent_field}:"{kubectl}")'
WINDOW_CLAUSE = 'timestamp>="{start}" AND timestamp<="{end}"'
PAGE_CLAUSE = 'timestamp<"{before}"'
CLAUSE_SEPARATOR = " AND "
RFC3339_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

# Who a principal is. The operator's callers are user accounts, service accounts, and
# Kubernetes service accounts outside the namespaces GKE and its Fleet features manage;
# every other `system:` principal (the control plane's components, the node identities,
# the anonymous and unsecured users), the service accounts in those namespaces, and
# Google-managed service agents are the provider's, and GKE moves them with the version.
# A principal of no recognisable shape is unplaced: named, never blocked on.
CLASS_OPERATOR = "operator"
CLASS_PROVIDER = "provider"
CLASS_UNPLACED = "unplaced"
SYSTEM_PREFIX = "system:"
SERVICEACCOUNT_PREFIX = "system:serviceaccount:"
SERVICEACCOUNT_SEPARATOR = ":"
PROVIDER_NAMESPACES = ("kube-system", "kube-public", "kube-node-lease", "gatekeeper-system", "cnrm-system", "asm-system")
PROVIDER_NAMESPACE_PREFIXES = ("gke-", "gmp-", "config-management-")
EMAIL_MARKER = "@"
GOOGLE_SERVICE_AGENT_SUFFIXES = (
    "@container-engine-robot.iam.gserviceaccount.com",
    "@cloudservices.gserviceaccount.com",
    "@system.gserviceaccount.com",
)
GOOGLE_SERVICE_AGENT_MARKER = "@gcp-sa-"
GOOGLE_SERVICE_AGENT_IAM_SUFFIX = ".iam.gserviceaccount.com"
# The same classes as the query language can state them, so the provider's callers never
# fill a page. RE2 has no lookahead, so "every system: principal except a service account"
# is two clauses; dots are character classes because a backslash inside a quoted filter
# string would need an escape of its own. `caller_class` is still applied to what comes
# back, so a principal the regexes do not express is placed on the way in.
PROVIDER_EXCLUSION_CLAUSE = '({principal}!~"{system_re}" OR {principal}=~"{serviceaccount_re}") AND {principal}!~"{system_namespaces_re}" AND {principal}!~"{service_agents_re}"'
SYSTEM_PRINCIPAL_RE = "^system:"
SERVICEACCOUNT_RE = "^system:serviceaccount:"
NAMESPACE_PREFIX_TAIL_RE = "[a-z0-9-]*"
SYSTEM_NAMESPACES_RE_FORMAT = "^system:serviceaccount:({namespaces}):"
SERVICE_AGENTS_RE = "@(gcp-sa-[a-z0-9-]+[.]iam|container-engine-robot[.]iam|cloudservices|system)[.]gserviceaccount[.]com$"
RE_ALTERNATIVE = "|"
# Characters escaped inside a quoted filter string, so a cluster name read from the API
# can never close the quote. Real GKE names are lowercase alphanumerics and hyphens.
FILTER_ESCAPE_RE = re.compile(r'(["\\])')
FILTER_ESCAPE_REPLACEMENT = r"\\\1"

# Where one member's reads are cached in the context (the keys read are the package's
# CONTEXT_* names), and the reasons a read produced nothing usable.
CACHE_KEY = "audit_log"
NO_RUNNER_REASON = "no command runner in the readiness context; the audit log was not read"
NO_SCOPE_REASON = "the readiness context carries no project, location or cluster name for the log filter"
READ_FAILED_REASON = "`gcloud logging read` ({read} read, page {page}) failed (rc={rc}): {stderr}"
UNPARSABLE_REASON = "`gcloud logging read` ({read} read, page {page}) returned output that is not a JSON list of entries (rc=0)"
SAMPLED_NOTE = "sampled {entries} entries over {pages} page(s) of the {read} read; more callers possible"
WRITES_ONLY_NOTE = "writes only; a caller that only reads is not in this log"
STDERR_EXCERPT_CHARS = 300
STDERR_JOIN = " "
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
# Cloud Logging timestamps: RFC 3339 with up to nine fractional digits; Python keeps six.
TIMESTAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:\d{2})$")
MICROSECOND_DIGITS = 6
UTC_OFFSET = "+00:00"
ZULU = "Z"


def _escape(value) -> str:
    return FILTER_ESCAPE_RE.sub(FILTER_ESCAPE_REPLACEMENT, str(value or ""))


def format_rfc3339(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime(RFC3339_FORMAT)


def parse_timestamp(text) -> datetime | None:
    """A Cloud Logging timestamp as an aware UTC datetime; None when it does not parse."""
    m = TIMESTAMP_RE.match(str(text or "").strip())
    if not m:
        return None
    base, fraction, zone = m.groups()
    micro = int((fraction or "0")[:MICROSECOND_DIGITS].ljust(MICROSECOND_DIGITS, "0"))
    try:
        parsed = datetime.fromisoformat(base + (UTC_OFFSET if zone == ZULU else zone))
    except ValueError:
        return None
    return parsed.replace(microsecond=micro).astimezone(timezone.utc)


def provider_exclusion_clause() -> str:
    namespaces = [re.escape(n) for n in PROVIDER_NAMESPACES] + [p + NAMESPACE_PREFIX_TAIL_RE for p in PROVIDER_NAMESPACE_PREFIXES]
    return PROVIDER_EXCLUSION_CLAUSE.format(
        principal=PRINCIPAL_FIELD,
        system_re=SYSTEM_PRINCIPAL_RE,
        serviceaccount_re=SERVICEACCOUNT_RE,
        system_namespaces_re=SYSTEM_NAMESPACES_RE_FORMAT.format(namespaces=RE_ALTERNATIVE.join(namespaces)),
        service_agents_re=SERVICE_AGENTS_RE,
    )


def build_filter(which: str, cluster: str, location: str, start: str, end: str) -> str:
    """The Cloud Logging filter of one read for one cluster over [start, end]."""
    population = REMOVED_CLAUSE.format(removed=LABEL_REMOVED_RELEASE)
    if which == READ_DEPRECATED:
        population = DEPRECATED_CLAUSE.format(
            deprecated=LABEL_DEPRECATED, deprecated_true=DEPRECATED_TRUE, removed=LABEL_REMOVED_RELEASE, user_agent_field=USER_AGENT_FIELD, kubectl=KUBECTL_USER_AGENT_PREFIX
        )
    return CLAUSE_SEPARATOR.join(
        (
            SCOPE_CLAUSE.format(resource_type=RESOURCE_TYPE, cluster=_escape(cluster), location=_escape(location)),
            population,
            WINDOW_CLAUSE.format(start=start, end=end),
            provider_exclusion_clause(),
        )
    )


def build_command(project: str, filter_text: str) -> list[str]:
    return [GCLOUD, *LOGGING_READ, filter_text, PROJECT_FLAG.format(project=project), LIMIT_FLAG.format(limit=tables.AUDIT_LOG_PAGE_LIMIT), JSON_FORMAT_FLAG]


def is_google_service_agent(principal: str) -> bool:
    text = str(principal or "")
    if text.endswith(GOOGLE_SERVICE_AGENT_SUFFIXES):
        return True
    return GOOGLE_SERVICE_AGENT_MARKER in text and text.endswith(GOOGLE_SERVICE_AGENT_IAM_SUFFIX)


def caller_class(principal) -> str:
    """CLASS_OPERATOR, CLASS_PROVIDER or CLASS_UNPLACED for one principal (see the module doc)."""
    text = str(principal or "").strip()
    if not text:
        return CLASS_UNPLACED
    if text.startswith(SERVICEACCOUNT_PREFIX):
        rest = text[len(SERVICEACCOUNT_PREFIX) :]
        namespace, separator, name = rest.partition(SERVICEACCOUNT_SEPARATOR)
        if not namespace or not separator or not name:
            return CLASS_UNPLACED
        if namespace in PROVIDER_NAMESPACES or namespace.startswith(PROVIDER_NAMESPACE_PREFIXES):
            return CLASS_PROVIDER
        return CLASS_OPERATOR
    if text.startswith(SYSTEM_PREFIX):
        return CLASS_PROVIDER
    if EMAIL_MARKER in text:
        return CLASS_PROVIDER if is_google_service_agent(text) else CLASS_OPERATOR
    return CLASS_UNPLACED


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
    """Entries grouped by (principal, user agent, API), sorted by those three.

    Each record carries the deprecation labels seen on the group: `removed_release` is the
    release the API server stamped (the same on every entry of one API), `deprecated` is
    whether any entry carried `k8s.io/deprecated=true`, `kubectl` whether the user agent is
    kubectl's, `caller_class` who the principal is. Timestamps are the RFC 3339 strings
    Cloud Logging wrote, compared as text, which orders correctly within one logger's format.
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
                "caller_class": caller_class(None if principal == UNKNOWN_PRINCIPAL else principal),
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


def _one_line(stderr) -> str:
    """gcloud's stderr as one line, so a multi-line error never splits a table row."""
    return STDERR_JOIN.join(str(stderr or "").split())[:STDERR_EXCERPT_CHARS] or NO_STDERR


def _parse_page(stdout) -> list | None:
    if not (stdout or "").strip():
        return []
    try:
        entries = json.loads(stdout)
    except ValueError:
        return None
    return entries if isinstance(entries, list) else None


def _oldest_timestamp(page: list) -> str | None:
    stamps = [e.get(TIMESTAMP) for e in page if isinstance(e, dict) and isinstance(e.get(TIMESTAMP), str) and e.get(TIMESTAMP)]
    return min(stamps) if stamps else None


def read(context: dict, which: str) -> dict:
    """One member's callers from one read, paged, read once and cached in the context.

    Returns `{"read", "commands", "error", "entries", "pages", "sampled", "callers",
    "window_days", "page_limit", "window_start", "window_end"}`. `error` is the reason the
    read stopped producing usable pages (no runner, a failed or timed-out command, output
    that is not a list); the pages before it are kept and graded. `sampled` says the last
    page was full and no further page was read: the page count reached its ceiling, the
    read's time budget ran out, or the page carried no timestamp to bound the next one on.
    """
    cache = context.setdefault(CONTEXT_CACHE, {}).setdefault(CACHE_KEY, {})
    if which in cache:
        return cache[which]
    result = {
        "read": which,
        "commands": [],
        "error": None,
        "entries": 0,
        "pages": 0,
        "sampled": False,
        "callers": [],
        "window_days": tables.AUDIT_LOG_WINDOW_DAYS,
        "page_limit": tables.AUDIT_LOG_PAGE_LIMIT,
        "window_start": None,
        "window_end": None,
    }
    cache[which] = result
    run = context.get(CONTEXT_RUN_CMD)
    project = context.get(CONTEXT_PROJECT)
    location = context.get(CONTEXT_LOCATION)
    cluster = context.get(CONTEXT_CLUSTER_NAME)
    if run is None:
        result["error"] = NO_RUNNER_REASON
        return result
    if not (project and location and cluster):
        result["error"] = NO_SCOPE_REASON
        return result
    at = context.get(CONTEXT_AT)
    end = at.astimezone(timezone.utc) if isinstance(at, datetime) else datetime.now(timezone.utc)
    start = end - timedelta(days=tables.AUDIT_LOG_WINDOW_DAYS)
    result["window_start"], result["window_end"] = format_rfc3339(start), format_rfc3339(end)
    clock = context.get(CONTEXT_CLOCK) or time.monotonic
    timeout = context.get(CONTEXT_TIMEOUT_SECONDS)
    base = build_filter(which, cluster, location, result["window_start"], result["window_end"])
    started = clock()
    before = None
    entries: list = []
    while True:
        filter_text = base if before is None else base + CLAUSE_SEPARATOR + PAGE_CLAUSE.format(before=before)
        cmd = build_command(project, filter_text)
        result["commands"].append(" ".join(cmd))
        # The context's `timeout_seconds` is the per-call cap, the runner's own default
        # without it; a timed-out call comes back as a non-zero rc with the reason on
        # stderr, like any failed read.
        rc, stdout, stderr = run(cmd, timeout) if timeout else run(cmd)
        result["pages"] += 1
        if rc != 0:
            result["error"] = READ_FAILED_REASON.format(read=which, page=result["pages"], rc=rc, stderr=_one_line(stderr))
            break
        page = _parse_page(stdout)
        if page is None:
            result["error"] = UNPARSABLE_REASON.format(read=which, page=result["pages"])
            break
        entries.extend(page)
        if len(page) < tables.AUDIT_LOG_PAGE_LIMIT:
            break
        oldest = _oldest_timestamp(page)
        if oldest is None or result["pages"] >= tables.AUDIT_LOG_MAX_PAGES or clock() - started >= tables.AUDIT_LOG_READ_BUDGET_SECONDS:
            result["sampled"] = True
            break
        before = oldest
    result["entries"] = len(entries)
    result["callers"] = caller_records(entries)
    return result


def read_removed(context: dict) -> dict:
    return read(context, READ_REMOVED)


def read_deprecated(context: dict) -> dict:
    return read(context, READ_DEPRECATED)


def sampled_note(log: dict) -> str:
    return SAMPLED_NOTE.format(entries=log["entries"], pages=log["pages"], read=log["read"])
