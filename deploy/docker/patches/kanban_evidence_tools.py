"""Typed evidence and artifact records for delegated kanban tasks.

Installed into the image at ``/opt/hermes/tools/kanban_evidence_tools.py`` and
registered into ``tools/kanban_tools.py`` by ``apply_kanban_evidence_tools.py``.

Why
---
Specialist workers already close their card with a prose report in
``tasks.result``, but a report is not evidence: the admin portal's CUJ
contract (#804) scores typed, provenance-carrying records — "which API was
called, with what request, and what did it return" — and structured
artifacts (a ComputeClass manifest, a Node Auto-Provisioning spec) rather
than claims inside answer text. Nothing in the runtime could carry those, so
the portal's task projection had nothing to show and every evidence-scored
criterion failed with "portal task projection omits evidence/artifacts".

These two tools give a worker a place to put them. Records land in two
tables beside the task itself in ``kanban.db``; the admin console's in-pod
reader projects them verbatim (``admin_console/agent_runtime.py``), and both
sides tolerate the other's absence: an older image simply projects empty
lists, an older console simply does not read the tables.

The recorder does not manufacture provenance. ``execution_ref`` is the
caller-supplied pointer at the raw execution that produced the analysis (a
credential-proxy execution id, a log path); evaluators and reviewers decide
what a missing reference means. What the recorder does enforce is shape:
known types, real dictionaries, bounded sizes, and a task that exists and —
for a dispatcher-spawned worker — is the worker's own.
"""

from __future__ import annotations

import json
import os
import time

EVIDENCE_TYPES = frozenset(
    {
        "quota_check",
        "advice_service_capacity",
        "computeclass_server_dry_run",
    }
)
ARTIFACT_TYPES = frozenset(
    {
        "computeclass",
        "node_auto_provisioning",
        "provisioning_request",
        "local_queue",
    }
)
EVIDENCE_STATUSES = frozenset({"completed", "failed"})

#: One serialized request/analysis/manifest may not exceed this. Large enough
#: for a full ComputeClass or a multi-zone capacity analysis, small enough
#: that a runaway worker cannot turn the board into a blob store.
MAX_OBJECT_BYTES = 65536

_DDL = """
CREATE TABLE IF NOT EXISTS task_evidence (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL,
    api_method TEXT NOT NULL DEFAULT '',
    request_json TEXT NOT NULL DEFAULT '{}',
    analysis_json TEXT NOT NULL DEFAULT '{}',
    execution_ref TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_evidence_task ON task_evidence(task_id);
CREATE TABLE IF NOT EXISTS task_artifacts (
    id INTEGER PRIMARY KEY,
    task_id TEXT NOT NULL,
    type TEXT NOT NULL,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    pair_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_artifacts_task ON task_artifacts(task_id);
"""

RECORD_EVIDENCE_SCHEMA = {
    "name": "record_evidence",
    "description": (
        "Record one typed, machine-readable evidence entry on your current "
        "task: which API or command you executed (``api_method``), the "
        "request you made, and the analysis you derived from its real "
        "output. The admin portal projects these records verbatim, so they "
        "are how a reviewer verifies your work happened — claims that exist "
        "only in your prose report are not evidence. Pass ``execution_ref`` "
        "pointing at the raw execution (a credential-proxy execution id or "
        "log path) whenever you have one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task to attach the record to; defaults to your own "
                    "task when running under the dispatcher."
                ),
            },
            "type": {
                "type": "string",
                "enum": sorted(EVIDENCE_TYPES),
                "description": "The evidence contract this record satisfies.",
            },
            "status": {
                "type": "string",
                "enum": sorted(EVIDENCE_STATUSES),
                "description": "Whether the underlying check succeeded.",
            },
            "api_method": {
                "type": "string",
                "description": (
                    "Canonical method behind the record, e.g. "
                    "compute.beta.AdviceService.Capacity."
                ),
            },
            "request": {
                "type": "object",
                "description": "The request you actually made, as an object.",
            },
            "analysis": {
                "type": "object",
                "description": (
                    "Structured findings derived from the real response "
                    "(quantities, zones, provisioning models)."
                ),
            },
            "execution_ref": {
                "type": "string",
                "description": "Pointer at the raw execution that produced this.",
            },
        },
        "required": ["type", "analysis"],
    },
}

ATTACH_ARTIFACT_SCHEMA = {
    "name": "attach_artifact",
    "description": (
        "Attach one structured artifact you produced to your current task: "
        "a ComputeClass manifest, a Node Auto-Provisioning spec, a "
        "ProvisioningRequest, or a Kueue LocalQueue. Pass the parsed "
        "manifest as an object, not YAML text. Artifacts that belong "
        "together (a ProvisioningRequest and the LocalQueue targeting the "
        "same window) share a ``pair_id``."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": (
                    "Task to attach the artifact to; defaults to your own "
                    "task when running under the dispatcher."
                ),
            },
            "type": {
                "type": "string",
                "enum": sorted(ARTIFACT_TYPES),
                "description": "What kind of artifact this is.",
            },
            "manifest": {
                "type": "object",
                "description": "The artifact itself, as a parsed object.",
            },
            "pair_id": {
                "type": "string",
                "description": "Shared id linking artifacts produced together.",
            },
        },
        "required": ["type", "manifest"],
    },
}


def _connect():
    """Connect to the active kanban board.

    Imported lazily, exactly like ``kanban_tools._connect``, so this module
    imports cleanly in contexts without hermes installed (unit tests, rigs).
    """
    from hermes_cli import kanban_db as kb

    return kb.connect()


def _ensure_tables(connection) -> None:
    connection.executescript(_DDL)


def _scoped_task_id(requested: object, tool_error):
    """Resolve the target task, refusing cross-task writes from a worker."""
    env_tid = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    tid = str(requested or "").strip() or env_tid
    if not tid:
        return None, tool_error(
            "no task in scope: pass task_id or run under the dispatcher"
        )
    if env_tid and tid != env_tid:
        return None, tool_error(
            f"worker is scoped to task {env_tid}; refusing to write records "
            f"onto {tid}"
        )
    return tid, None


def _bounded_json(value: object, field: str, tool_error):
    if not isinstance(value, dict):
        return None, tool_error(f"{field} must be an object")
    try:
        rendered = json.dumps(value, sort_keys=True)
    except (TypeError, ValueError):
        return None, tool_error(f"{field} must be JSON-serializable")
    if len(rendered.encode("utf-8")) > MAX_OBJECT_BYTES:
        return None, tool_error(
            f"{field} exceeds {MAX_OBJECT_BYTES} bytes; record a summary and "
            "reference the raw output instead"
        )
    return rendered, None


def _task_exists(connection, task_id: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return row is not None


def make_handlers(tool_error):
    """Build the two handlers around the caller's ``tool_error`` shape."""

    def handle_record_evidence(args: dict, **_kw) -> str:
        kind = str(args.get("type") or "")
        if kind not in EVIDENCE_TYPES:
            return tool_error(
                f"unknown evidence type {kind!r}; expected one of "
                f"{sorted(EVIDENCE_TYPES)}"
            )
        status = str(args.get("status") or "completed")
        if status not in EVIDENCE_STATUSES:
            return tool_error(
                f"unknown status {status!r}; expected one of "
                f"{sorted(EVIDENCE_STATUSES)}"
            )
        tid, err = _scoped_task_id(args.get("task_id"), tool_error)
        if err:
            return err
        request_json, err = _bounded_json(
            args.get("request") or {}, "request", tool_error
        )
        if err:
            return err
        analysis_json, err = _bounded_json(
            args.get("analysis") or {}, "analysis", tool_error
        )
        if err:
            return err
        connection = _connect()
        _ensure_tables(connection)
        if not _task_exists(connection, tid):
            return tool_error(f"task {tid} does not exist on this board")
        with connection:
            connection.execute(
                "INSERT INTO task_evidence (task_id, type, status, api_method,"
                " request_json, analysis_json, execution_ref, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    tid,
                    kind,
                    status,
                    str(args.get("api_method") or ""),
                    request_json,
                    analysis_json,
                    str(args.get("execution_ref") or ""),
                    time.time(),
                ),
            )
        return f"recorded {kind} evidence on {tid}"

    def handle_attach_artifact(args: dict, **_kw) -> str:
        kind = str(args.get("type") or "")
        if kind not in ARTIFACT_TYPES:
            return tool_error(
                f"unknown artifact type {kind!r}; expected one of "
                f"{sorted(ARTIFACT_TYPES)}"
            )
        tid, err = _scoped_task_id(args.get("task_id"), tool_error)
        if err:
            return err
        manifest_json, err = _bounded_json(
            args.get("manifest"), "manifest", tool_error
        )
        if err:
            return err
        connection = _connect()
        _ensure_tables(connection)
        if not _task_exists(connection, tid):
            return tool_error(f"task {tid} does not exist on this board")
        with connection:
            connection.execute(
                "INSERT INTO task_artifacts (task_id, type, manifest_json,"
                " pair_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    tid,
                    kind,
                    manifest_json,
                    str(args.get("pair_id") or ""),
                    time.time(),
                ),
            )
        return f"attached {kind} artifact to {tid}"

    return handle_record_evidence, handle_attach_artifact


def register(registry, check_fn, tool_error) -> None:
    """Register both tools; called from inside ``tools/kanban_tools.py``.

    The caller hands over its own ``registry``, worker-mode ``check_fn`` and
    ``tool_error`` so this module needs no import of hermes internals at
    import time and the tools gate exactly like the other worker lifecycle
    tools.
    """
    record_evidence, attach_artifact = make_handlers(tool_error)
    registry.register(
        name="record_evidence",
        toolset="kanban",
        schema=RECORD_EVIDENCE_SCHEMA,
        handler=record_evidence,
        check_fn=check_fn,
        emoji="🧾",
    )
    registry.register(
        name="attach_artifact",
        toolset="kanban",
        schema=ATTACH_ARTIFACT_SCHEMA,
        handler=attach_artifact,
        check_fn=check_fn,
        emoji="📎",
    )
