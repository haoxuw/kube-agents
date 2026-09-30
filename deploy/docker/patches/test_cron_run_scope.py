"""Unit tests for the cron-run scope installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import ast
import concurrent.futures
import contextvars
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

import apply_cron_run_scope
from cron_run_scope import (
    CRON_RESPONSE_LIMIT,
    CRON_RISK_ENV,
    CRON_RUN_ENV,
    KANBAN_DB_NAME,
    KANBAN_HOME_ENV,
    ORIGIN_THREADS_LIMIT,
    WORKER_DB_ENV,
    WORKER_TASK_ENV,
    clip_cron_response,
    cron_ownership_violation,
    cron_run_scope,
    current_cron_job,
    current_cron_risk,
    missing_task_id_error,
    worker_origin,
)

# The card the kanban dispatcher scoped the worker to, and the job the worker
# then dispatched with cronjob(action='run') — the 2026-08-04 shape.
CALLER_CARD = "t_a1b2c3d4"
JOB_ID = "fleet-wide-cost-analysis"
DISPATCH_ENV = {WORKER_TASK_ENV: CALLER_CARD, CRON_RUN_ENV: JOB_ID}
WORKER_ENV = {WORKER_TASK_ENV: CALLER_CARD}

# The subscription table as hermes_cli/kanban_db.py declares it at v2026.9.14.
# Copied rather than imported: the unit suite runs without a Hermes tree, and
# what worker_origin depends on is these column names, which is what the copy
# pins. verify_cron_run_scope.py runs the same lookup against upstream's real
# SCHEMA_SQL in the image, so a drift between the two fails the build.
KANBAN_NOTIFY_SUBS_SCHEMA = """
CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       TEXT NOT NULL,
    platform      TEXT NOT NULL,
    chat_id       TEXT NOT NULL,
    thread_id     TEXT NOT NULL DEFAULT '',
    user_id       TEXT,
    user_id_alt   TEXT,
    chat_type     TEXT,
    notifier_profile TEXT,
    delivery_mode TEXT NOT NULL DEFAULT 'notify',
    delivery_metadata TEXT,
    created_at    INTEGER NOT NULL,
    last_event_id INTEGER NOT NULL DEFAULT 0,
    last_ping_event_id INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);
"""

# The thread a person asked in — a Google Chat space and the message thread
# under it — and a second subscriber on Slack with no thread (a channel).
ASKING_THREAD = {
    "platform": "google_chat",
    "chat_id": "spaces/AAAA1234",
    "thread_id": "spaces/AAAA1234/threads/BBBB5678",
}
CHANNEL_SUB = {"platform": "slack", "chat_id": "C0123456789", "thread_id": ""}


def _board(rows, schema=KANBAN_NOTIFY_SUBS_SCHEMA, task_id=CALLER_CARD):
    """A throwaway board holding ``rows`` as subscriptions to ``task_id``."""
    path = os.path.join(tempfile.mkdtemp(), KANBAN_DB_NAME)
    conn = sqlite3.connect(path)
    conn.executescript(schema)
    for order, row in enumerate(rows):
        conn.execute(
            "INSERT INTO kanban_notify_subs "
            "(task_id, platform, chat_id, thread_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (
                row.get("task_id", task_id),
                row["platform"],
                row["chat_id"],
                row["thread_id"],
                1000 + order,
            ),
        )
    conn.commit()
    conn.close()
    return path


class CronRunScopeTest(unittest.TestCase):
    """The marker must cover the run, and reach nothing else."""

    def setUp(self):
        self.addCleanup(os.environ.pop, CRON_RUN_ENV, None)
        self.addCleanup(os.environ.pop, CRON_RISK_ENV, None)
        os.environ.pop(CRON_RUN_ENV, None)
        os.environ.pop(CRON_RISK_ENV, None)

    def test_the_marker_is_set_during_the_run_and_cleared_after(self):
        self.assertEqual(current_cron_job(), "")
        self.assertEqual(current_cron_risk(), "high")
        with cron_run_scope(JOB_ID, risk="low"):
            self.assertEqual(current_cron_job(), JOB_ID)
            self.assertEqual(current_cron_risk(), "low")
        self.assertEqual(current_cron_job(), "")
        self.assertEqual(current_cron_risk(), "high")

    def test_default_risk_is_high(self):
        with cron_run_scope(JOB_ID):
            self.assertEqual(current_cron_risk(), "high")

    def test_the_marker_is_cleared_when_the_run_raises(self):
        with self.assertRaises(RuntimeError):
            with cron_run_scope(JOB_ID):
                raise RuntimeError("job blew up")
        self.assertEqual(current_cron_job(), "")

    def test_the_scope_does_not_touch_the_environment(self):
        """The env half is gone, and its absence is the fix.

        os.environ is process-global; a cron run is not. Writing the marker
        there bled it into unrelated threads, was inherited for life by workers
        the dispatcher spawned mid-run, and — see the next test — leaked
        permanently under a non-nested interleaving.
        """
        with cron_run_scope(JOB_ID):
            self.assertNotIn(CRON_RUN_ENV, os.environ)
        self.assertNotIn(CRON_RUN_ENV, os.environ)

    def test_two_overlapping_runs_leave_nothing_behind(self):
        """A in, B in, A out, B out — the interleaving that used to wedge.

        Two `cronjob(action='run')` calls in one assistant turn reach this:
        agent/tool_executor.py runs tool calls on a pool and `cronjob` is not on
        its serial list. With the save/restore env pair, A popped a variable it
        never set and B then restored A's marker with no run active at all —
        after which every worker in the process was told it was a run of job A,
        so it could neither default to its own card nor name it explicitly, and
        the guardrail backstop was suppressed too. The card sat `running` with
        no result and no failure, forever.

        Held in a ContextVar the overlap is structurally impossible: each pool
        thread starts from its own context, so neither scope can observe or
        restore the other's value. Interleaved with real barriers rather than
        by calling __enter__/__exit__ by hand — out-of-order resets in ONE
        context would leak a ContextVar too, and `with` is what rules that out.
        """
        a_in, b_in, a_out = (threading.Event() for _ in range(3))
        seen = {}

        def run_a():
            with cron_run_scope("job-a"):
                a_in.set()
                b_in.wait(5)
                seen["a"] = current_cron_job()
            a_out.set()

        def run_b():
            a_in.wait(5)
            with cron_run_scope("job-b"):
                b_in.set()
                a_out.wait(5)
                # A has fully exited by now and must not have disturbed B.
                seen["b"] = current_cron_job()

        threads = [threading.Thread(target=run_a), threading.Thread(target=run_b)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)

        self.assertEqual(seen, {"a": "job-a", "b": "job-b"})
        self.assertEqual(current_cron_job(), "")
        self.assertNotIn(CRON_RUN_ENV, os.environ)

    def test_the_workers_task_env_survives_the_scope(self):
        # heartbeat_current_worker_from_env() reads HERMES_KANBAN_TASK on every
        # call to hold the dispatcher's 15-minute claim; a 20-minute compliance
        # run would lose the card if the scope stripped it.
        os.environ[WORKER_TASK_ENV] = CALLER_CARD
        self.addCleanup(os.environ.pop, WORKER_TASK_ENV, None)
        with cron_run_scope(JOB_ID):
            self.assertEqual(os.environ[WORKER_TASK_ENV], CALLER_CARD)
        self.assertEqual(os.environ[WORKER_TASK_ENV], CALLER_CARD)

    def test_an_empty_job_id_still_marks_the_run(self):
        with cron_run_scope(""):
            self.assertTrue(current_cron_job())

    def test_the_marker_reaches_the_cron_agents_worker_thread(self):
        # cron/scheduler.py:run_job submits agent.run_conversation to a
        # single-worker pool through contextvars.copy_context(). Reproduce that
        # exact hop: the marker must survive it, or the kanban tools the cron
        # agent calls would not see it.
        with cron_run_scope(JOB_ID):
            context = contextvars.copy_context()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                seen = pool.submit(context.run, current_cron_job).result()
        self.assertEqual(seen, JOB_ID)

    def test_the_marker_does_not_leak_into_a_sibling_context(self):
        """An unrelated concurrent session must still resolve to no cron run.

        Reads the REAL os.environ, not an empty stand-in. Passing environ={}
        here was the whole defect: it isolated the assertion from the
        process-global half that was doing the leaking, so the test named after
        the leak could not observe it.
        """
        sibling = contextvars.copy_context()
        with cron_run_scope(JOB_ID):
            self.assertEqual(sibling.run(current_cron_job), "")

    def test_a_thread_outside_the_scope_sees_no_run(self):
        """The same claim across a real thread, which is how it happened.

        Under dispatch_in_gateway a second worker runs concurrently with
        somebody else's dispatch. It never entered the scope, so it must not
        read the marker — and while the marker lived in os.environ, it did.
        """
        with cron_run_scope(JOB_ID):
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                # No copy_context(): a bare pool thread starts from an empty
                # context, exactly like an unrelated worker.
                seen = pool.submit(current_cron_job).result()
        self.assertEqual(seen, "")

    def test_the_context_var_wins_over_a_stale_env_value(self):
        os.environ[CRON_RUN_ENV] = "stale-job"
        with cron_run_scope(JOB_ID):
            self.assertEqual(current_cron_job(), JOB_ID)
        self.assertEqual(os.environ[CRON_RUN_ENV], "stale-job")

    def test_the_env_fallback_still_answers_when_something_sets_it(self):
        # Nothing in the image writes it today. Kept because it is the one
        # thing a ContextVar cannot do — cross a fork — so a process launched
        # with the marker already in its environment is still recognised.
        self.assertEqual(current_cron_job(DISPATCH_ENV), JOB_ID)
        self.assertEqual(current_cron_job({}), "")


class CronOwnershipViolationTest(unittest.TestCase):
    """A cron run naming its caller's card explicitly is rejected too."""

    def test_the_callers_card_is_refused(self):
        err = cron_ownership_violation(CALLER_CARD, DISPATCH_ENV)
        self.assertIsNotNone(err)
        self.assertIn(JOB_ID, err)
        self.assertIn(CALLER_CARD, err)

    def test_another_task_is_left_to_the_existing_rules(self):
        self.assertIsNone(cron_ownership_violation("t_sibling", DISPATCH_ENV))

    def test_a_worker_outside_a_cron_run_is_unaffected(self):
        self.assertIsNone(cron_ownership_violation(CALLER_CARD, WORKER_ENV))

    def test_a_cron_run_with_no_ambient_card_is_unaffected(self):
        self.assertIsNone(
            cron_ownership_violation(CALLER_CARD, {CRON_RUN_ENV: JOB_ID})
        )

    def test_a_missing_task_id_is_not_a_violation(self):
        # _default_task_id already returned None; the handler's own
        # "task_id is required" guard owns that path.
        self.assertIsNone(cron_ownership_violation(None, DISPATCH_ENV))


class MissingTaskIdErrorTest(unittest.TestCase):
    """The stock advice is wrong inside a cron run — the env var is the trap."""

    def test_outside_a_cron_run_the_stock_message_is_unchanged(self):
        self.assertEqual(
            missing_task_id_error(WORKER_ENV),
            "task_id is required (or set HERMES_KANBAN_TASK in the env)",
        )
        self.assertIn("HERMES_KANBAN_TASK", missing_task_id_error({}))

    def test_inside_a_cron_run_it_names_the_job_and_points_at_the_response(self):
        msg = missing_task_id_error(DISPATCH_ENV)
        self.assertIn(JOB_ID, msg)
        self.assertIn("final response", msg)
        # Must not tell the run to set the very env var that caused the bug.
        self.assertNotIn("set HERMES_KANBAN_TASK", msg)


class WorkerOriginTest(unittest.TestCase):
    """A worker's job remembers the thread that asked; nothing else does."""

    def setUp(self):
        for var in (WORKER_TASK_ENV, WORKER_DB_ENV, KANBAN_HOME_ENV, CRON_RUN_ENV):
            self.addCleanup(os.environ.pop, var, None)
            os.environ.pop(var, None)

    def test_outside_a_worker_there_is_no_origin(self):
        self.assertEqual(worker_origin({}), ("", []))
        # The real environment, with the card scrubbed by setUp: the cron
        # ticker's own process and an interactive session look like this.
        self.assertEqual(worker_origin(), ("", []))
        # A blank card id is no card.
        self.assertEqual(worker_origin({WORKER_TASK_ENV: "   "}), ("", []))

    def test_a_worker_reads_its_cards_subscriptions(self):
        db = _board([ASKING_THREAD, CHANNEL_SUB])
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: db}
        self.assertEqual(
            worker_origin(env), (CALLER_CARD, [ASKING_THREAD, CHANNEL_SUB])
        )

    def test_the_pinned_board_path_is_what_the_dispatcher_injected(self):
        # HERMES_KANBAN_DB is set on every spawn by kanban_db_dispatch; the
        # explicit argument exists for the verifier and wins over it.
        pinned = _board([ASKING_THREAD])
        explicit = _board([CHANNEL_SUB])
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: pinned}
        self.assertEqual(worker_origin(env)[1], [ASKING_THREAD])
        self.assertEqual(worker_origin(env, db_path=explicit)[1], [CHANNEL_SUB])

    def test_without_a_pin_the_default_board_under_the_kanban_home_is_read(self):
        db = _board([ASKING_THREAD])
        env = {WORKER_TASK_ENV: CALLER_CARD, KANBAN_HOME_ENV: os.path.dirname(db)}
        self.assertEqual(worker_origin(env), (CALLER_CARD, [ASKING_THREAD]))

    def test_the_real_environment_is_read_when_none_is_given(self):
        # create_job calls worker_origin() bare, so the default must be the
        # process environment the dispatcher populated, not an empty mapping.
        db = _board([ASKING_THREAD])
        os.environ[WORKER_TASK_ENV] = CALLER_CARD
        os.environ[WORKER_DB_ENV] = db
        self.assertEqual(worker_origin(), (CALLER_CARD, [ASKING_THREAD]))

    def test_another_cards_subscriptions_are_not_this_workers(self):
        db = _board([dict(ASKING_THREAD, task_id="t_someone_else")])
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: db}
        self.assertEqual(worker_origin(env), (CALLER_CARD, []))

    def test_a_card_nobody_subscribed_to_is_still_a_card(self):
        # The distinction the stamp relies on: origin_task set with an empty
        # origin_threads means "created on request, but no thread to answer
        # in", which the relay falls back from; no origin_task at all means
        # the job was not created by a worker.
        db = _board([])
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: db}
        self.assertEqual(worker_origin(env), (CALLER_CARD, []))

    def test_a_row_with_no_chat_id_is_unaddressable_and_dropped(self):
        blank = {"platform": "slack", "chat_id": "", "thread_id": "1700000000.000100"}
        db = _board([blank, ASKING_THREAD])
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: db}
        self.assertEqual(worker_origin(env)[1], [ASKING_THREAD])

    def test_an_empty_thread_id_is_a_channel_and_kept(self):
        db = _board([CHANNEL_SUB])
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: db}
        self.assertEqual(worker_origin(env)[1], [CHANNEL_SUB])

    def test_duplicate_triples_collapse_to_one(self):
        # Upstream's primary key already makes two identical rows for one card
        # impossible on a current board, so the dedupe is exercised against
        # the same table without the key — the shape of a board whose schema
        # is not the one this suite pins, which is the case the dedupe is for.
        keyless = KANBAN_NOTIFY_SUBS_SCHEMA.replace(
            ",\n    PRIMARY KEY (task_id, platform, chat_id, thread_id)", ""
        )
        self.assertNotIn("PRIMARY KEY", keyless)
        db = _board([ASKING_THREAD, ASKING_THREAD, CHANNEL_SUB, ASKING_THREAD], keyless)
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: db}
        self.assertEqual(worker_origin(env)[1], [ASKING_THREAD, CHANNEL_SUB])

    def test_the_list_is_capped_oldest_first(self):
        rows = [
            {"platform": "slack", "chat_id": f"C{i:010d}", "thread_id": ""}
            for i in range(ORIGIN_THREADS_LIMIT + 4)
        ]
        db = _board(rows)
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: db}
        threads = worker_origin(env)[1]
        self.assertEqual(len(threads), ORIGIN_THREADS_LIMIT)
        self.assertEqual(threads, rows[:ORIGIN_THREADS_LIMIT])

    def test_a_cron_run_borrowing_the_worker_env_has_no_origin(self):
        # A job a cron run creates was not asked for in the caller's thread.
        # Both markers, because both exist: the ContextVar the scope sets and
        # the env fallback a forked process would carry.
        db = _board([ASKING_THREAD])
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: db}
        with cron_run_scope(JOB_ID):
            self.assertEqual(worker_origin(env), ("", []))
        self.assertEqual(worker_origin(dict(env, **{CRON_RUN_ENV: JOB_ID})), ("", []))
        # And the moment the run is over the worker's own creates see it again.
        self.assertEqual(worker_origin(env), (CALLER_CARD, [ASKING_THREAD]))

    def test_an_unreadable_board_stamps_nothing_and_never_raises(self):
        missing = os.path.join(tempfile.mkdtemp(), KANBAN_DB_NAME)
        garbage = os.path.join(tempfile.mkdtemp(), KANBAN_DB_NAME)
        Path(garbage).write_bytes(b"this is not a database")
        # An older board that predates the subscription table entirely.
        old = os.path.join(tempfile.mkdtemp(), KANBAN_DB_NAME)
        sqlite3.connect(old).close()
        for db in (missing, garbage, old):
            env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: db}
            self.assertEqual(worker_origin(env), ("", []), db)
        # Read-only through a URI: the lookup must not have created the board.
        self.assertFalse(os.path.exists(missing))

    def test_an_unreadable_board_is_logged_and_still_stamps_nothing(self):
        # Fail-soft is not fail-silent: a create that quietly lost its origin
        # would be indistinguishable from one nobody asked for in a thread, so
        # the swallowed exception leaves a warning naming the board and what
        # went wrong, and the return value is exactly the no-origin one.
        garbage = os.path.join(tempfile.mkdtemp(), KANBAN_DB_NAME)
        Path(garbage).write_bytes(b"this is not a database")
        env = {WORKER_TASK_ENV: CALLER_CARD, WORKER_DB_ENV: garbage}
        with self.assertLogs(worker_origin.__module__, level="WARNING") as captured:
            self.assertEqual(worker_origin(env), ("", []))
        self.assertEqual(len(captured.records), 1)
        record = captured.records[0]
        self.assertEqual(record.levelname, "WARNING")
        self.assertIn(garbage, record.getMessage())
        self.assertIn(sqlite3.DatabaseError.__name__, record.getMessage())


class ClipCronResponseTest(unittest.TestCase):
    """The report carried back must keep its ledger URL clickable."""

    LEDGER_URL = "https://github.com/gke-agentic/adamparco-infra/issues/27"

    def test_a_real_report_survives_whole(self):
        report = (
            "Fleet-wide cost analysis complete: 12 findings (3 major, 9 minor) "
            "across 3 clusters. Ledger updated at " + self.LEDGER_URL
        )
        self.assertEqual(clip_cron_response(report), report)

    def test_an_oversized_report_never_severs_the_url(self):
        filler = " ".join(f"finding{i}" for i in range(2000))
        clipped = clip_cron_response(filler + " " + self.LEDGER_URL)
        self.assertLessEqual(len(clipped), CRON_RESPONSE_LIMIT)
        self.assertNotIn("https://github.com/gke-agentic/adamparco-infra/is", clipped)

    def test_an_empty_report_is_an_empty_string(self):
        self.assertEqual(clip_cron_response(None), "")
        self.assertEqual(clip_cron_response("   "), "")

    def test_the_budget_is_roomier_than_the_chat_handoff(self):
        self.assertGreaterEqual(CRON_RESPONSE_LIMIT, 4000)


# The applier is exercised against a miniature of the four real files. The
# real anchors are asserted against the shipped image by
# verify_cron_run_scope.py; what these miniatures carry is the SHAPE upstream
# has at v2026.9.14, which is the thing a version bump moves.
#
# The wrapper/body split (v2026.8.19) is the whole reason this test exists.
# `run_one_job` stopped being the execute→deliver→mark body and became a wrapper
# that delegates through `_run_with_fire_claim_heartbeat`; putting the out-param
# on the wrapper alone leaves both anchors matched, the file parsing, and the
# body raising NameError on the first real tick. v2026.9.14 then split the body
# itself: save/deliver moved into `_save_compose_deliver` over a `_RunDelivery`
# record and the bookkeeping tail into `_finish_completed_run`, so the report is
# written from the seam between them and the saved path rides on the record.
SCHEDULER_STUB = '''from dataclasses import dataclass
from typing import Optional


def run_one_job(
    job: dict, *, adapters=None, loop=None, verbose: bool = False,
    extra_prompt: Optional[str] = None, cancel_event=None,
) -> bool:
    """Register the fire owner, then delegate."""
    execution_token = object()
    try:
        return _run_with_fire_claim_heartbeat(
            job,
            lambda lost_ownership: _run_one_job_body(
                job,
                adapters=adapters,
                loop=loop,
                verbose=verbose,
                extra_prompt=extra_prompt,
                fire_claim_lost=lost_ownership,
                execution_token=execution_token))
    finally:
        pass


@dataclass
class _RunDelivery:
    job: dict
    success: bool
    error: Optional[str]
    delivery_error: Optional[str] = None
    side_effect_ownership_lost: bool = False


def _save_compose_deliver(d, fence, final_response, output, *, adapters, loop, verbose, execution_token):
    job = d.job
    with fence.side_effect_fence() as owns_output:
        if not owns_output:
            raise RuntimeError
        output_file = save_job_output(job["id"], output)
    if verbose:
        logger.info("Output saved to: %s", output_file)
    d.delivery_error = None


def _finish_completed_run(d, fire_owner, execution_id) -> bool:
    finish_execution(execution_id, success=d.success, error=d.error)
    return True


def _run_one_job_body(
    job: dict, *, adapters=None, loop=None, verbose: bool = False,
    extra_prompt: Optional[str] = None, fire_claim_lost=None,
    execution_token: Optional[object] = None,
) -> bool:
    execution_id = "x"
    delivery_error = None
    try:
        _deferred_agents: list = []
        _run_kwargs = {
            "defer_agent_teardown": _deferred_agents,
            "extra_prompt": extra_prompt,
            "execution_id": execution_id}
        if fire_claim_lost is not None:
            _run_kwargs["cancel_event"] = fire_claim_lost
        try:
            success, output, final_response, error = run_job(job, **_run_kwargs)
        except BaseException:
            raise
        d = _RunDelivery(job=job, success=success, error=error)
        try:
            _save_compose_deliver(
                d, None, final_response, output, adapters=adapters, loop=loop, verbose=verbose,
                execution_token=execution_token)
        finally:
            delivery_error = d.delivery_error
        fire_owner = None
        return _finish_completed_run(d, fire_owner, execution_id)
    except Exception:
        finish_execution(execution_id, success=False)
        return False
'''

CRONJOB_STUB = '''import json
from typing import Any, Dict, Optional


def _dumps(payload):
    return json.dumps(payload, indent=2)


def _notify_provider_jobs_changed_safe() -> None:
    pass


def _run_claimed_job(job, extra_prompt=None):
    job_id = job["id"]
    try:
        from cron.scheduler import release_running_job, run_one_job, try_register_running_job
        try:
            with _run_heartbeat(str(job.get("name") or job_id)):
                processed = run_one_job(job, adapters=adapters, loop=gateway_loop, extra_prompt=extra_prompt)
        finally:
            release_running_job(job_id)
        refreshed = get_job(job_id) or {}
        ok = refreshed.get("last_status") == "ok"
        run_error = refreshed.get("last_error")
        return {"claimed": True, "success": bool(processed and ok), "error": run_error}
    except Exception as e:
        return {"claimed": True, "success": False, "error": str(e)}


def _action_create(a):
    try:
        job = create_job_with_scheduler_registration(
            prompt=a["prompt"] or "",
            reasoning_effort=a["reasoning_effort"],
            failure_deliver=a["failure_deliver"])
    except Exception:
        pass


def _action_run(job, a):
    exec_result = a["exec_result"]
    result = a["result"]
    claimed = exec_result.get("claimed", False)
    if not claimed:
        result["execution_skipped"] = exec_result.get("error")
    elif exec_result.get("error"):
        result["execution_error"] = exec_result["error"]
    return _dumps({"success": True, "job": result})


def cronjob(
    action: str,
    job_id: Optional[str] = None,
    prompt: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
    failure_deliver: Optional[str] = None,
    result: Optional[dict] = None,
    exec_result: Optional[dict] = None,
    task_id: str = None,
    session_id: Optional[str] = None,
    paused: bool = False) -> str:
    a = dict(locals())
    del a["task_id"]
    if action == "create":
        return _action_create(a)
    if action == "run":
        return _action_run({"id": job_id}, a)
'''

JOBS_STUB = '''from typing import Any, Dict, Optional


def create_job(
    name: str,
    schedule: str,
    prompt: str,
    paused: bool = False,
    paused_reason: Optional[str] = None,
) -> Dict[str, Any]:
    job = {"name": name, "schedule": schedule, "prompt": prompt}
    with _jobs_lock():
        save_jobs(load_jobs() + [job])
    return job
'''

KANBAN_STUB = '''import os

from hermes_cli.config import cfg_get, load_config


class _Reject(Exception):
    pass


def _check(cond, message):
    if not cond:
        raise _Reject(message)


def _default_task_id(arg):
    return arg or os.environ.get("HERMES_KANBAN_TASK") or None


def _require_task_id(args: dict) -> str:
    tid = _default_task_id(args.get("task_id"))
    _check(tid, "task_id is required (or set HERMES_KANBAN_TASK in the env)")
    return tid


def _enforce_worker_task_ownership(tid: str) -> None:
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and tid != env_tid:
        raise _Reject(
            f"worker is scoped to task {env_tid}; refusing to mutate {tid}.")
'''


class ApplierTest(unittest.TestCase):
    """The applier against the v2026.9.14 wrapper/body/phase-helper shape."""

    def _apply_all(self) -> Path:
        root = Path(tempfile.mkdtemp())
        (root / "cron").mkdir()
        (root / "tools").mkdir()
        (root / "cron" / "scheduler.py").write_text(SCHEDULER_STUB)
        (root / "cron" / "jobs.py").write_text(JOBS_STUB)
        (root / "tools" / "cronjob_tools.py").write_text(CRONJOB_STUB)
        (root / "tools" / "kanban_tools.py").write_text(KANBAN_STUB)
        apply_cron_run_scope.apply(root)
        return root

    def _apply(self) -> str:
        return (self._apply_all() / "cron" / "scheduler.py").read_text()

    @staticmethod
    def _keyword_only(source, name):
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return [arg.arg for arg in node.args.kwonlyargs]
        raise AssertionError(f"no def {name} in the patched source")

    def test_both_halves_take_the_out_param(self):
        """The wrapper is what callers name; the body is what the writes read."""
        scheduler = self._apply()
        ast.parse(scheduler)
        self.assertIn("outcome", self._keyword_only(scheduler, "run_one_job"))
        self.assertIn("outcome", self._keyword_only(scheduler, "_run_one_job_body"))

    def test_the_wrapper_forwards_it_across_the_split(self):
        """Both signatures can carry it and the body still see None.

        Nothing else notices: expect_keyword_only checks the body's parameter
        list, not its callers, and the file parses either way.
        """
        scheduler = self._apply()
        delegation = scheduler[scheduler.index("_run_one_job_body(") :]
        delegation = delegation[: delegation.index(")\n")]
        self.assertIn("outcome=outcome,", delegation)

    def test_the_writes_land_in_the_body_not_the_wrapper(self):
        scheduler = self._apply()
        body = scheduler[scheduler.index("def _run_one_job_body(") :]
        self.assertIn('outcome["response"] = final_response', body)
        self.assertIn('outcome["output_file"] = d.output_file', body)
        wrapper = scheduler[: scheduler.index("@dataclass")]
        self.assertNotIn("outcome[", wrapper)

    def test_the_saved_path_rides_the_delivery_record(self):
        """save_job_output moved out of the body in v2026.9.14; the path comes
        back on _RunDelivery, so the record must carry the field and the save
        phase must fill it as a string."""
        scheduler = self._apply()
        record = scheduler[scheduler.index("class _RunDelivery") :]
        record = record[: record.index("def _save_compose_deliver")]
        self.assertIn("output_file: Optional[str] = None", record)
        save = scheduler[scheduler.index("def _save_compose_deliver") :]
        save = save[: save.index("def _finish_completed_run")]
        self.assertIn("d.output_file = str(output_file)", save)

    def test_the_report_is_written_before_the_bookkeeping_tail(self):
        scheduler = self._apply()
        body = scheduler[scheduler.index("def _run_one_job_body(") :]
        self.assertLess(
            body.index('outcome["response"] = final_response'),
            body.index("return _finish_completed_run(d, fire_owner, execution_id)"),
        )

    def test_the_scoped_run_job_lands_in_the_body(self):
        scheduler = self._apply()
        body = scheduler[scheduler.index("def _run_one_job_body(") :]
        self.assertIn('with cron_run_scope(job["id"], risk=str(job.get("risk") or "high")):', body)

    def test_cronjob_create_accepts_risk_param(self):
        root = self._apply_all()
        cronjob = (root / "tools" / "cronjob_tools.py").read_text()
        ast.parse(cronjob)
        self.assertIn("risk: Optional[str] = None,", cronjob)
        self.assertIn('risk=a["risk"],', cronjob)

    def test_jobs_create_job_stamps_default_risk(self):
        root = self._apply_all()
        jobs = (root / "cron" / "jobs.py").read_text()
        ast.parse(jobs)
        self.assertIn("risk: Optional[str] = None,", jobs)
        self.assertIn('job["risk"] = _eff_risk', jobs)

    def test_jobs_create_job_clamps_and_validates_risk(self):
        import contextlib
        from typing import Any, Dict, Optional
        root = self._apply_all()
        jobs_code = (root / "cron" / "jobs.py").read_text()
        stored_jobs = []
        ns = {
            "load_jobs": lambda: stored_jobs,
            "save_jobs": lambda js: None,
            "_jobs_lock": contextlib.nullcontext,
            "Optional": Optional,
            "Dict": Dict,
            "Any": Any,
        }
        exec(jobs_code, ns)
        create_job = ns["create_job"]

        # Default is low
        j1 = create_job("test1", "* * * * *", "echo 1")
        self.assertEqual(j1["risk"], "low")

        # Explicit low is low
        j2 = create_job("test2", "* * * * *", "echo 2", risk="low")
        self.assertEqual(j2["risk"], "low")

        # Explicit high is high
        j3 = create_job("test3", "* * * * *", "echo 3", risk="high")
        self.assertEqual(j3["risk"], "high")

        # Invalid string fails closed to high
        j4 = create_job("test4", "* * * * *", "echo 4", risk="banana")
        self.assertEqual(j4["risk"], "high")

        # Inside high-risk cron run, low and invalid risk are clamped to high
        with cron_run_scope("watchdog-1", risk="high"):
            j5 = create_job("test5", "* * * * *", "echo 5", risk="low")
            self.assertEqual(j5["risk"], "high")
            j6 = create_job("test6", "* * * * *", "echo 6", risk="banana")
            self.assertEqual(j6["risk"], "high")

    def test_jobs_create_job_stamps_the_worker_origin_after_the_risk(self):
        root = self._apply_all()
        jobs = (root / "cron" / "jobs.py").read_text()
        ast.parse(jobs)
        self.assertIn('job["origin_task"] = _origin_task', jobs)
        self.assertIn('job["origin_threads"] = _origin_threads', jobs)
        # Both import paths, as the risk clamp has them.
        self.assertIn("from tools.cron_run_scope import worker_origin", jobs)
        self.assertIn("from cron_run_scope import worker_origin", jobs)
        # Order: risk, origin, then the save the anchor was.
        self.assertLess(
            jobs.index('job["risk"] = _eff_risk'),
            jobs.index('job["origin_task"] = _origin_task'),
        )
        self.assertLess(
            jobs.index('job["origin_threads"] = _origin_threads'),
            jobs.index("with _jobs_lock():"),
        )

    def _patched_create_job(self):
        """``create_job`` from the patched stub, with the store stubbed out."""
        import contextlib
        from typing import Any, Dict, Optional
        root = self._apply_all()
        ns = {
            "load_jobs": lambda: [],
            "save_jobs": lambda js: None,
            "_jobs_lock": contextlib.nullcontext,
            "Optional": Optional,
            "Dict": Dict,
            "Any": Any,
        }
        exec((root / "cron" / "jobs.py").read_text(), ns)
        return ns["create_job"]

    def _worker_env(self, db):
        for var in (WORKER_TASK_ENV, WORKER_DB_ENV, CRON_RUN_ENV):
            self.addCleanup(os.environ.pop, var, None)
        os.environ[WORKER_TASK_ENV] = CALLER_CARD
        os.environ[WORKER_DB_ENV] = db

    def test_a_worker_created_job_carries_its_card_and_threads(self):
        create_job = self._patched_create_job()
        self._worker_env(_board([ASKING_THREAD, CHANNEL_SUB]))
        job = create_job("recheck", "0 9 * * *", "re-check the rollout")
        self.assertEqual(job["origin_task"], CALLER_CARD)
        self.assertEqual(job["origin_threads"], [ASKING_THREAD, CHANNEL_SUB])
        # The risk stamp it follows is untouched.
        self.assertEqual(job["risk"], "low")

    def test_a_card_with_no_subscribers_stamps_the_card_and_an_empty_list(self):
        create_job = self._patched_create_job()
        self._worker_env(_board([]))
        job = create_job("recheck", "0 9 * * *", "re-check the rollout")
        self.assertEqual(job["origin_task"], CALLER_CARD)
        self.assertEqual(job["origin_threads"], [])

    def test_a_job_created_outside_a_worker_has_neither_key(self):
        create_job = self._patched_create_job()
        for var in (WORKER_TASK_ENV, WORKER_DB_ENV, CRON_RUN_ENV):
            self.addCleanup(os.environ.pop, var, None)
            os.environ.pop(var, None)
        job = create_job("recheck", "0 9 * * *", "re-check the rollout")
        self.assertNotIn("origin_task", job)
        self.assertNotIn("origin_threads", job)

    def test_a_job_a_cron_run_creates_inside_a_worker_has_neither_key(self):
        create_job = self._patched_create_job()
        self._worker_env(_board([ASKING_THREAD]))
        with cron_run_scope(JOB_ID):
            job = create_job("recheck", "0 9 * * *", "re-check the rollout")
        self.assertNotIn("origin_task", job)
        self.assertNotIn("origin_threads", job)

    def test_an_unreadable_board_does_not_fail_the_create(self):
        create_job = self._patched_create_job()
        garbage = os.path.join(tempfile.mkdtemp(), KANBAN_DB_NAME)
        Path(garbage).write_bytes(b"this is not a database")
        self._worker_env(garbage)
        job = create_job("recheck", "0 9 * * *", "re-check the rollout")
        self.assertEqual(job["name"], "recheck")
        self.assertNotIn("origin_task", job)
        self.assertNotIn("origin_threads", job)

    def test_a_wrapper_that_stopped_delegating_is_fatal_not_silent(self):
        """The shape that shipped the NameError: no lambda to forward through."""
        root = Path(tempfile.mkdtemp())
        (root / "cron").mkdir()
        (root / "tools").mkdir()
        (root / "cron" / "scheduler.py").write_text(
            SCHEDULER_STUB.replace(
                "lambda lost_ownership: _run_one_job_body(", "_run_one_job_body("
            )
        )
        (root / "cron" / "jobs.py").write_text(JOBS_STUB)
        (root / "tools" / "cronjob_tools.py").write_text(CRONJOB_STUB)
        (root / "tools" / "kanban_tools.py").write_text(KANBAN_STUB)
        with self.assertRaises(SystemExit) as ctx:
            apply_cron_run_scope.apply(root)
        self.assertIn("found 0", str(ctx.exception))

    def test_applying_twice_is_refused(self):
        """Deliberately not idempotent: the out-param is inserted, not
        substituted, so a second pass would append a second `outcome=None`."""
        root = Path(tempfile.mkdtemp())
        (root / "cron").mkdir()
        (root / "tools").mkdir()
        (root / "cron" / "scheduler.py").write_text(SCHEDULER_STUB)
        (root / "cron" / "jobs.py").write_text(JOBS_STUB)
        (root / "tools" / "cronjob_tools.py").write_text(CRONJOB_STUB)
        (root / "tools" / "kanban_tools.py").write_text(KANBAN_STUB)
        apply_cron_run_scope.apply(root)
        with self.assertRaises(SystemExit):
            apply_cron_run_scope.apply(root)


if __name__ == "__main__":
    unittest.main()
