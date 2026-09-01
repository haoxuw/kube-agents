"""Unit tests for the typed evidence recorder installed by deploy/docker/Dockerfile.

Run: python3 -m unittest discover -s deploy/docker/patches -p 'test_*.py' -t deploy/docker/patches
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import kanban_evidence_tools as ket

REPO_ROOT = Path(__file__).resolve().parents[3]


def tool_error(message: str) -> str:
    return f"ERROR: {message}"


class RecorderFixture(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.db_path = Path(self._directory.name) / "kanban.db"
        with sqlite3.connect(self.db_path) as connection:
            connection.executescript(
                """
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT,
                    session_id TEXT, created_at REAL, started_at REAL,
                    completed_at REAL, last_heartbeat_at REAL,
                    last_failure_error TEXT, result TEXT
                );
                CREATE TABLE task_runs (
                    id INTEGER PRIMARY KEY, task_id TEXT, summary TEXT, error TEXT
                );
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY, task_id TEXT, kind TEXT, created_at REAL
                );
                """
            )
            connection.execute(
                "INSERT INTO tasks VALUES ('t_1', 'Design', 'platform', 'done', "
                "'portal_session', 1, NULL, 2, NULL, '', 'Full report.')"
            )
        self._original_connect = ket._connect
        ket._connect = lambda: sqlite3.connect(self.db_path)
        self.addCleanup(setattr, ket, "_connect", self._original_connect)
        self._original_env = os.environ.get("HERMES_KANBAN_TASK")
        os.environ["HERMES_KANBAN_TASK"] = "t_1"
        self.addCleanup(self._restore_env)
        self.record, self.attach = ket.make_handlers(tool_error)

    def _restore_env(self) -> None:
        if self._original_env is None:
            os.environ.pop("HERMES_KANBAN_TASK", None)
        else:
            os.environ["HERMES_KANBAN_TASK"] = self._original_env

    def rows(self, table: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


class RecordEvidenceTest(RecorderFixture):
    def test_records_a_typed_entry_on_the_workers_own_task(self) -> None:
        reply = self.record(
            {
                "type": "advice_service_capacity",
                "api_method": "compute.beta.AdviceService.Capacity",
                "request": {
                    "region": "us-central1",
                    "acceleratorType": "nvidia-a100",
                    "acceleratorCount": 32,
                    "size": 8,
                },
                "analysis": {"availableQuantity": 8},
                "execution_ref": "exec-7",
            }
        )
        self.assertEqual(reply, "recorded advice_service_capacity evidence on t_1")
        (row,) = self.rows("task_evidence")
        self.assertEqual(row["task_id"], "t_1")
        self.assertEqual(row["status"], "completed")
        self.assertEqual(json.loads(row["analysis_json"]), {"availableQuantity": 8})

    def test_completed_capacity_evidence_must_name_what_it_asked_for(self) -> None:
        # Observed live: a worker recorded the right apiMethod with an empty
        # request, leaving a claim no reader could check.
        reply = self.record(
            {
                "type": "advice_service_capacity",
                "api_method": "compute.beta.AdviceService.Capacity",
                "request": {},
                "analysis": {"availableQuantity": 8},
            }
        )
        self.assertIn("must name region, acceleratorType, acceleratorCount", reply)
        reply = self.record(
            {
                "type": "advice_service_capacity",
                "api_method": "compute.beta.AdviceService.Capacity",
                "request": {
                    "region": "us-central1",
                    "acceleratorType": "nvidia-a100",
                    "acceleratorCount": 32,
                },
                "analysis": {"availableQuantity": 8},
            }
        )
        self.assertNotIn("ERROR", reply)

    def test_a_failed_probe_may_be_recorded_without_a_full_request(self) -> None:
        # A probe that could not run has nothing to report but the attempt,
        # and hiding it would be worse than recording it thinly.
        reply = self.record(
            {
                "type": "advice_service_capacity",
                "status": "failed",
                "api_method": "compute.beta.AdviceService.Capacity",
                "analysis": {"notes": "FLEX_START rejected by the SDK"},
            }
        )
        self.assertNotIn("ERROR", reply)

    def test_rejects_types_outside_the_evidence_contract(self) -> None:
        reply = self.record({"type": "vibes", "analysis": {}})
        self.assertIn("unknown evidence type", reply)
        with sqlite3.connect(self.db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("task_evidence", tables, "rejections must not write")

    def test_refuses_to_write_onto_another_workers_task(self) -> None:
        reply = self.record(
            {"type": "quota_check", "analysis": {}, "task_id": "t_other"}
        )
        self.assertIn("scoped to task t_1", reply)

    def test_refuses_a_task_that_does_not_exist(self) -> None:
        os.environ["HERMES_KANBAN_TASK"] = "t_ghost"
        reply = self.record(
            {
                "type": "quota_check",
                "request": {"region": "us-central1"},
                "analysis": {},
            }
        )
        self.assertIn("does not exist", reply)

    def test_bounds_the_analysis_payload(self) -> None:
        oversized = {"zones": ["z" * 100] * 1000}
        reply = self.record(
            {
                "type": "quota_check",
                "request": {"region": "us-central1"},
                "analysis": oversized,
            }
        )
        self.assertIn("exceeds", reply)


class AttachArtifactTest(RecorderFixture):
    def test_attaches_a_structured_manifest(self) -> None:
        reply = self.attach(
            {
                "type": "computeclass",
                "manifest": {"kind": "ComputeClass", "apiVersion": "cloud.google.com/v1"},
                "pair_id": "design-1",
            }
        )
        self.assertEqual(reply, "attached computeclass artifact to t_1")
        (row,) = self.rows("task_artifacts")
        self.assertEqual(json.loads(row["manifest_json"])["kind"], "ComputeClass")
        self.assertEqual(row["pair_id"], "design-1")

    def test_rejects_manifest_text_that_is_not_an_object(self) -> None:
        reply = self.attach({"type": "computeclass", "manifest": "kind: ComputeClass"})
        self.assertIn("must be an object", reply)

    def test_rejects_manifest_whose_sections_collapsed_to_scalars(self) -> None:
        # Observed live: {"metadata": 1, "spec": 1} attached before the worker
        # retried with the real manifest; the broken copy must not be stored.
        reply = self.attach(
            {
                "type": "computeclass",
                "manifest": {"kind": "ComputeClass", "metadata": 1, "spec": 1},
            }
        )
        self.assertIn("manifest.metadata must be an object", reply)
        with sqlite3.connect(self.db_path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("task_artifacts", tables, "rejections must not write")


class ProjectionRoundTripTest(RecorderFixture):
    """The recorder's rows must project through the admin console verbatim.

    This is the seam #804 is about: the worker writes with these tools, the
    portal reads with admin_console's embedded script, and the CUJ evaluators
    score exactly what comes out the far end. One test holds both sides to
    the same schema so they cannot drift apart silently.
    """

    def test_recorded_evidence_and_artifacts_reach_the_portal_projection(self) -> None:
        self.record(
            {
                "type": "advice_service_capacity",
                "api_method": "compute.beta.AdviceService.Capacity",
                "request": {
                    "region": "us-central1",
                    "acceleratorType": "nvidia-a100",
                    "acceleratorCount": 32,
                },
                "analysis": {"availableQuantity": 8},
                "execution_ref": "exec-7",
            }
        )
        self.attach(
            {
                "type": "computeclass",
                "manifest": {"kind": "ComputeClass"},
                "pair_id": "pair-1",
            }
        )

        sys.path.insert(0, str(REPO_ROOT))
        try:
            from admin_console.agent_runtime import _READ_SCRIPT
        finally:
            sys.path.pop(0)
        script = _READ_SCRIPT.replace(
            'Path("/opt/data/kanban.db")', f"Path({str(self.db_path)!r})"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, "tasks", "portal_session", "10"],
            check=True,
            capture_output=True,
            text=True,
        )
        row = json.loads(completed.stdout)["tasks"][0]

        self.assertEqual(row["result"], "Full report.")
        self.assertEqual(
            row["evidence"],
            [
                {
                    "type": "advice_service_capacity",
                    "status": "completed",
                    "details": {
                        "apiMethod": "compute.beta.AdviceService.Capacity",
                        "region": "us-central1",
                        "request": {
                            "acceleratorCount": 32,
                            "acceleratorType": "nvidia-a100",
                            "region": "us-central1",
                        },
                        "analysis": {"availableQuantity": 8},
                        "executionRef": "exec-7",
                    },
                }
            ],
        )
        self.assertEqual(
            row["artifacts"],
            [
                {
                    "type": "computeclass",
                    "manifest": {"kind": "ComputeClass"},
                    "pairId": "pair-1",
                }
            ],
        )


class RegistrationTest(unittest.TestCase):
    def test_register_wires_both_tools_behind_the_given_gate(self) -> None:
        calls = []

        class Registry:
            def register(self, **kwargs):
                calls.append(kwargs)

        gate = object()
        ket.register(Registry(), gate, tool_error)

        self.assertEqual(
            [call["name"] for call in calls], ["record_evidence", "attach_artifact"]
        )
        for call in calls:
            self.assertEqual(call["toolset"], "kanban")
            self.assertIs(call["check_fn"], gate)
            self.assertIn("parameters", call["schema"])


if __name__ == "__main__":
    unittest.main()
