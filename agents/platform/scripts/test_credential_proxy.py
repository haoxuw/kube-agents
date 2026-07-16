import json
import queue
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from credential_proxy import (
    AgentAPIProxyHandler,
    CommandExecutor,
    GoogleChatRelay,
    Policy,
    SlackRelay,
)


class AgentAPIProxyTest(unittest.TestCase):
    def setUp(self):
        self.received_authorization = ""
        owner = self

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                owner.received_authorization = self.headers.get("Authorization", "")
                body = b"proxied"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _message, *_args):
                return

        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        AgentAPIProxyHandler.external_key = "external-secret"
        AgentAPIProxyHandler.upstream_key = "internal-sentinel"
        AgentAPIProxyHandler.upstream_port = self.upstream.server_port
        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), AgentAPIProxyHandler)
        for server in (self.upstream, self.proxy):
            threading.Thread(target=server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.proxy.shutdown()
        self.upstream.shutdown()
        self.proxy.server_close()
        self.upstream.server_close()

    def test_replaces_external_api_key_before_forwarding(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.server_port}/health",
            headers={"Authorization": "Bearer external-secret"},
        )
        with urllib.request.urlopen(request) as response:
            self.assertEqual(b"proxied", response.read())
        self.assertEqual("Bearer internal-sentinel", self.received_authorization)

    def test_rejects_invalid_external_api_key(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.proxy.server_port}/health",
            headers={"Authorization": "Bearer wrong"},
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request)
        self.assertEqual(401, raised.exception.code)
        self.assertEqual("", self.received_authorization)


class PolicyTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.policy_path = Path(self.temp_dir.name) / "policy.json"
        self.policy_path.write_text(
            json.dumps(
                {
                    "blockedMessage": "Command blocked for security reasons.",
                    "rules": [
                        {
                            "id": "gcp.access-token-disclosure",
                            "pattern": r"\bgcloud\s+auth\s+print-access-token\b",
                        },
                        {
                            "id": "github.token-disclosure",
                            "pattern": r"\bgh\s+auth\s+token\b",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.policy = Policy.load(str(self.policy_path))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_blocks_configured_command(self):
        rule = self.policy.blocked_by(["gcloud", "auth", "print-access-token"])
        self.assertIsNotNone(rule)
        self.assertEqual("gcp.access-token-disclosure", rule.rule_id)

    def test_allows_supported_command(self):
        self.assertIsNone(self.policy.blocked_by(["kubectl", "get", "pods"]))


class CommandExecutorTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp_dir.cleanup()

    def executor(self, timeout_seconds=5):
        return CommandExecutor(
            timeout_seconds=timeout_seconds,
            max_output_bytes=1024,
            state_dir=self.temp_dir.name,
        )

    def test_rejects_unsupported_executable(self):
        with self.assertRaisesRegex(ValueError, "not supported"):
            self.executor().execute(["env"])

    def test_rejects_shell_command_string(self):
        with self.assertRaisesRegex(ValueError, "list of strings"):
            self.executor().execute("gcloud auth list")

    def test_rejects_working_directory_outside_shared_workspace(self):
        with self.assertRaisesRegex(ValueError, "outside the shared workspace"):
            self.executor().execute(["git", "status"], cwd="/")

    def test_timeout_kills_command(self):
        result = self.executor(timeout_seconds=1).execute_internal(["/bin/sleep", "10"])
        self.assertTrue(result.timed_out)
        self.assertEqual(124, result.exit_code)

    def test_command_environment_excludes_sidecar_tokens(self):
        import os

        previous = os.environ.get("SLACK_BOT_TOKEN")
        os.environ["SLACK_BOT_TOKEN"] = "must-not-be-forwarded"
        try:
            executor = self.executor()
        finally:
            if previous is None:
                del os.environ["SLACK_BOT_TOKEN"]
            else:
                os.environ["SLACK_BOT_TOKEN"] = previous
        self.assertNotIn("SLACK_BOT_TOKEN", executor.environment)
        self.assertEqual(str(Path(self.temp_dir.name) / "home"), executor.environment["HOME"])

    def test_bootstrap_prepares_profile_for_later_commands(self):
        executor = self.executor()
        executor.bootstrap("printf ready > \"$HOME/bootstrap-state\"")
        self.assertTrue((Path(self.temp_dir.name) / "home" / "bootstrap-state").exists())

    def test_bootstrap_failure_does_not_return_command_output(self):
        with self.assertRaisesRegex(RuntimeError, "exit code 9") as raised:
            self.executor().bootstrap("printf secret >&2; exit 9")
        self.assertNotIn("secret", str(raised.exception))


class GoogleChatRelayTest(unittest.TestCase):
    class FakeRequest:
        def __init__(self, response):
            self.response = response

        def execute(self):
            return self.response

    class FakeResource:
        def __init__(self, calls, path=()):
            self.calls = calls
            self.path = path

        def __getattr__(self, name):
            def invoke(**arguments):
                if not arguments:
                    return GoogleChatRelayTest.FakeResource(
                        self.calls, (*self.path, name)
                    )
                self.calls.append((self.path, name, arguments))
                return GoogleChatRelayTest.FakeRequest(
                    {"path": self.path, "method": name, "arguments": arguments}
                )

            return invoke

    def test_forwards_unknown_resource_method_and_body_unchanged(self):
        calls = []
        relay = GoogleChatRelay.__new__(GoogleChatRelay)
        relay.chat = self.FakeResource(calls)
        arguments = {"body": {"futureSchema": {"nested": [1, 2, 3]}}}

        result = relay.api_call(
            ["futureResource", "messages"], "futureMethod", arguments
        )

        self.assertEqual(
            [(("futureResource", "messages"), "futureMethod", arguments)], calls
        )
        self.assertEqual(arguments, result["arguments"])


class SlackRelayTest(unittest.TestCase):
    class FakeClient:
        token = "xoxb-not-returned"

        def api_call(self, method, **arguments):
            return {"ok": True, "method": method, "arguments": arguments}

    def relay(self):
        relay = SlackRelay.__new__(SlackRelay)
        relay.primary_client = self.FakeClient()
        relay.clients = {"T123": relay.primary_client}
        relay.workspaces = [{"teamId": "T123", "botUserId": "U123", "botName": "agent"}]
        relay._events = queue.Queue()
        relay._receipts = {}
        import threading

        relay._lock = threading.Lock()
        return relay

    def test_forwards_unknown_web_api_method_and_arguments_unchanged(self):
        arguments = {"json": {"futureSchema": {"nested": [1, 2, 3]}}}
        result = self.relay().api_call(
            "T123", "future.method", arguments
        )
        self.assertTrue(result["ok"])
        self.assertEqual("future.method", result["method"])
        self.assertEqual(arguments, result["arguments"])
        self.assertNotIn("token", json.dumps(result))

    def test_nack_requeues_event(self):
        relay = self.relay()
        relay._events.put({"type": "events_api", "payload": {"event": {}}})
        event = relay.pull(timeout_seconds=1)
        self.assertTrue(relay.settle(event["receipt"], acknowledge=False))
        retried = relay.pull(timeout_seconds=1)
        self.assertEqual("events_api", retried["type"])


if __name__ == "__main__":
    unittest.main()
