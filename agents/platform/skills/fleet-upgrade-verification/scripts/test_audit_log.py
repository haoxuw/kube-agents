#!/usr/bin/env python3
"""Unit tests for readiness_rules/audit_log.py, the two paged Cloud Logging reads the
audit-log rules share, over `testdata/audit_log_sample.json`.

The sample is in the shape of the entries `gcloud logging read --format=json` returns for
GKE's Kubernetes audit log, built from the two captures in bench/upgrade-scenarios/evidence
(06/removed-api.txt: a `flowcontrol/v1beta3` FlowSchema writer stamped
`k8s.io/removed-release=1.32` on a 1.31 control plane; 09/final.txt: an Endpoints v1 writer
stamped `k8s.io/deprecated=true`), with kube-system's endpoint-controller, a stale kubectl,
a current kubectl and a client-go controller added beside them. No test runs gcloud: the
context carries a fake runner that answers the removed-release read and the
deprecated-or-kubectl read by their filters.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from readiness_rules import audit_log  # noqa: E402

SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "testdata", "audit_log_sample.json")
TUNER = "system:serviceaccount:kubeagents-system:legacy-flowcontrol-tuner"
WRITER = "system:serviceaccount:seeded-deprecation:legacy-endpoints-writer"
STALE_KUBECTL = "system:serviceaccount:seeded-shapes:stale-kubectl-client"
ENDPOINT_CONTROLLER = "system:serviceaccount:kube-system:endpoint-controller"
# Two hours after the sample's last write.
AT = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
FAIL = "FAIL"
DEPRECATED_MARKER = 'k8s.io/deprecated"="true"'
PAGE_MARKER = 'timestamp<"'
TIMEOUT = 45


def sample_entries():
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        return json.load(f)


def split_sample():
    entries = sample_entries()
    removed = [e for e in entries if audit_log.LABEL_REMOVED_RELEASE in e["labels"]]
    return removed, [e for e in entries if e not in removed]


def full_page(entry, count, start=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc)):
    """`count` copies of `entry` with distinct, descending timestamps from `start`."""
    page = []
    for i in range(count):
        copy = json.loads(json.dumps(entry))
        copy["timestamp"] = (start - timedelta(seconds=i)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        page.append(copy)
    return page


class FakeRunner:
    """Answers the two reads by filter: canned lists, or scripted pages per read. Takes
    the timeout the context passes, as the report's `run_cmd` does, and records it."""

    def __init__(self, removed=(), deprecated=(), rc=0, stderr="", stdout=None, pages=None):
        self.removed, self.deprecated = list(removed), list(deprecated)
        self.rc, self.stderr, self.stdout = rc, stderr, stdout
        self.pages = {k: list(v) for k, v in (pages or {}).items()}
        self.calls = []
        self.timeouts = []

    def __call__(self, cmd, timeout=None, env=None):
        self.calls.append(cmd)
        self.timeouts.append(timeout)
        filter_text = cmd[3]
        which = audit_log.READ_DEPRECATED if DEPRECATED_MARKER in filter_text else audit_log.READ_REMOVED
        if self.stdout is not None:
            return self.rc, self.stdout, self.stderr
        if self.rc != 0:
            return self.rc, "", self.stderr
        if which in self.pages:
            queue = self.pages[which]
            page = queue.pop(0) if queue else []
            if page == FAIL:
                return 1, "", "ERROR: (gcloud.logging.read) boom"
            return 0, json.dumps(page), ""
        if PAGE_MARKER in filter_text:
            return 0, "[]", ""
        return 0, json.dumps(self.removed if which == audit_log.READ_REMOVED else self.deprecated), ""


def context_with(runner=None, at=AT, clock=None, timeout=TIMEOUT):
    context = {
        shared.CONTEXT_PROJECT: "example-project",
        shared.CONTEXT_LOCATION: "us-central1-a",
        shared.CONTEXT_CLUSTER_NAME: "upg-06",
        shared.CONTEXT_RUN_CMD: runner or FakeRunner(),
        shared.CONTEXT_AT: at,
        shared.CONTEXT_CACHE: {},
    }
    if clock is not None:
        context[shared.CONTEXT_CLOCK] = clock
    if timeout is not None:
        context[shared.CONTEXT_TIMEOUT_SECONDS] = timeout
    return context


def sample_context(at=AT):
    removed, deprecated = split_sample()
    return context_with(FakeRunner(removed, deprecated), at=at)


class AuditLogReadTest(unittest.TestCase):
    def test_filters_carry_the_population_the_window_and_the_provider_exclusions(self):
        removed = audit_log.build_filter(audit_log.READ_REMOVED, "seeded-a", "us-central1-a", "2026-09-22T16:00:00Z", "2026-09-29T16:00:00Z")
        deprecated = audit_log.build_filter(audit_log.READ_DEPRECATED, "seeded-a", "us-central1-a", "2026-09-22T16:00:00Z", "2026-09-29T16:00:00Z")
        for text in (removed, deprecated):
            self.assertIn('resource.type="k8s_cluster" AND resource.labels.cluster_name="seeded-a" AND resource.labels.location="us-central1-a"', text)
            self.assertIn('timestamp>="2026-09-22T16:00:00Z" AND timestamp<="2026-09-29T16:00:00Z"', text)
            self.assertIn('(protoPayload.authenticationInfo.principalEmail!~"^system:" OR protoPayload.authenticationInfo.principalEmail=~"^system:serviceaccount:")', text)
            self.assertIn('principalEmail!~"^system:serviceaccount:(kube\\-system|kube\\-public|kube\\-node\\-lease|gatekeeper\\-system|cnrm\\-system|asm\\-system|gke-[a-z0-9-]*|gmp-[a-z0-9-]*|config-management-[a-z0-9-]*):"', text)
            self.assertIn('principalEmail!~"@(gcp-sa-[a-z0-9-]+[.]iam|container-engine-robot[.]iam|cloudservices|system)[.]gserviceaccount[.]com$"', text)
            self.assertNotIn("\\.", text.replace("\\-", ""))
        self.assertIn(' AND labels."k8s.io/removed-release":* AND ', removed)
        self.assertNotIn("k8s.io/deprecated", removed)
        self.assertIn('((labels."k8s.io/deprecated"="true" AND NOT labels."k8s.io/removed-release":*) OR protoPayload.requestMetadata.callerSuppliedUserAgent:"kubectl/")', deprecated)
        cmd = audit_log.build_command("example-project", removed)
        self.assertEqual(cmd[:3], ["gcloud", "logging", "read"])
        self.assertEqual(cmd[4:], ["--project=example-project", "--limit=1000", "--format=json"])
        self.assertFalse(any(flag.startswith("--freshness") or flag.startswith("--page-size") for flag in cmd))
        self.assertEqual((tables.AUDIT_LOG_WINDOW_DAYS, tables.AUDIT_LOG_PAGE_LIMIT, tables.AUDIT_LOG_MAX_PAGES, tables.AUDIT_LOG_READ_BUDGET_SECONDS), (7, 1000, 3, 60))

    def test_a_quote_in_a_name_cannot_close_the_filter(self):
        self.assertIn('cluster_name="a\\"b"', audit_log.build_filter(audit_log.READ_REMOVED, 'a"b', "loc", "s", "e"))

    def test_api_from_resource_name_and_from_method_name(self):
        self.assertEqual(audit_log.api_of("core/v1/namespaces/seeded-deprecation/endpoints/legacy-endpoints-lane", None), "core/v1 endpoints")
        self.assertEqual(audit_log.api_of("flowcontrol.apiserver.k8s.io/v1beta3/flowschemas/legacy-batch-lane", None), "flowcontrol.apiserver.k8s.io/v1beta3 flowschemas")
        self.assertEqual(audit_log.api_of("apps/v1/namespaces/shop/deployments/web/status", None), "apps/v1 deployments")
        self.assertEqual(audit_log.api_of("core/v1/nodes/n1", None), "core/v1 nodes")
        self.assertEqual(audit_log.api_of("core/v1/namespaces/foo", None), "core/v1 namespaces")
        self.assertEqual(audit_log.api_of(None, "io.k8s.core.v1.endpoints.patch"), "core/v1 endpoints")
        self.assertEqual(audit_log.api_of(None, "io.k8s.apiserver.flowcontrol.v1beta3.flowschemas.create"), "flowcontrol.apiserver/v1beta3 flowschemas")
        self.assertEqual(audit_log.api_of(None, ""), audit_log.UNKNOWN_API)

    def test_caller_class_places_the_operator_the_provider_and_the_rest(self):
        operator = (TUNER, WRITER, "operator@example.com", "deployer@my-project.iam.gserviceaccount.com", "system:serviceaccount:kubeagents-system:platform")
        provider = (
            ENDPOINT_CONTROLLER,
            "system:kube-controller-manager",
            "system:cloud-controller-manager",
            "system:l7-lb-controller",
            "system:node:gke-a-1",
            "system:anonymous",
            "system:serviceaccount:gke-managed-system:x",
            "system:serviceaccount:gmp-system:collector",
            "system:serviceaccount:config-management-system:reconciler",
            "system:serviceaccount:gatekeeper-system:gatekeeper-admin",
            "service-123@container-engine-robot.iam.gserviceaccount.com",
            "service-123@gcp-sa-gkenode.iam.gserviceaccount.com",
            "123@cloudservices.gserviceaccount.com",
            "x@system.gserviceaccount.com",
        )
        unplaced = ("", None, "legacy-flowcontrol-tuner", "system:serviceaccount:", "system:serviceaccount:ns-only")
        for principal in operator:
            self.assertEqual(audit_log.caller_class(principal), audit_log.CLASS_OPERATOR, principal)
        for principal in provider:
            self.assertEqual(audit_log.caller_class(principal), audit_log.CLASS_PROVIDER, principal)
        for principal in unplaced:
            self.assertEqual(audit_log.caller_class(principal), audit_log.CLASS_UNPLACED, principal)

    def test_sample_groups_by_caller_and_api(self):
        callers = {(c["principal"], c["api"]): c for c in audit_log.caller_records(sample_entries())}
        tuner = callers[(TUNER, "flowcontrol.apiserver.k8s.io/v1beta3 flowschemas")]
        self.assertEqual(tuner["count"], 2)
        self.assertEqual(tuner["removed_release"], "1.32")
        self.assertTrue(tuner["deprecated"])
        self.assertEqual(tuner["user_agent"], "legacy-flowcontrol-tuner/0.3")
        self.assertEqual(tuner["first_seen"], "2026-09-29T13:54:19.378013Z")
        self.assertEqual(tuner["last_seen"], "2026-09-29T14:04:19.112Z")
        self.assertEqual(tuner["caller_class"], audit_log.CLASS_OPERATOR)
        writer = callers[(WRITER, "core/v1 endpoints")]
        self.assertEqual(writer["count"], 2)
        self.assertIsNone(writer["removed_release"])
        self.assertTrue(writer["deprecated"])
        self.assertEqual(callers[(ENDPOINT_CONTROLLER, "core/v1 endpoints")]["caller_class"], audit_log.CLASS_PROVIDER)
        self.assertTrue(callers[(STALE_KUBECTL, "core/v1 configmaps")]["kubectl"])
        self.assertFalse(callers[(STALE_KUBECTL, "core/v1 configmaps")]["deprecated"])

    def test_each_read_runs_once_per_context_and_is_cached(self):
        context = sample_context()
        first = audit_log.read_removed(context)
        self.assertIs(first, audit_log.read_removed(context))
        second = audit_log.read_deprecated(context)
        self.assertIs(second, audit_log.read_deprecated(context))
        self.assertEqual(len(context["run_cmd"].calls), 2)
        self.assertEqual((first["entries"], first["pages"], first["sampled"], first["error"]), (2, 1, False, None))
        self.assertEqual((second["entries"], second["pages"], second["sampled"], second["error"]), (6, 1, False, None))
        self.assertEqual((first["window_start"], first["window_end"]), ("2026-09-22T16:00:00Z", "2026-09-29T16:00:00Z"))
        self.assertIn('timestamp>="2026-09-22T16:00:00Z"', first["commands"][0])

    def test_failed_timed_out_and_unparsable_reads_are_errors_on_one_line(self):
        two_lines = "ERROR: (gcloud.logging.read) PERMISSION_DENIED: Cloud Logging API has not been used\n- '@type': type.googleapis.com/google.rpc.ErrorInfo\n  reason: SERVICE_DISABLED"
        failed = audit_log.read_removed(context_with(FakeRunner(rc=1, stderr=two_lines)))
        self.assertIn("(removed read, page 1) failed (rc=1)", failed["error"])
        self.assertIn("PERMISSION_DENIED", failed["error"])
        self.assertIn("reason: SERVICE_DISABLED", failed["error"])
        self.assertNotIn("\n", failed["error"])
        timed_out = audit_log.read_deprecated(context_with(FakeRunner(rc=-1, stderr="timed out after 60 seconds")))
        self.assertIn("(deprecated read, page 1) failed (rc=-1): timed out after 60 seconds", timed_out["error"])
        garbled = audit_log.read_removed(context_with(FakeRunner(stdout="{not json")))
        self.assertIn("not a JSON list of entries", garbled["error"])
        not_a_list = audit_log.read_removed(context_with(FakeRunner(stdout=json.dumps({"entries": []}))))
        self.assertIn("not a JSON list of entries", not_a_list["error"])
        for result in (failed, timed_out, garbled, not_a_list):
            self.assertEqual(result["callers"], [])
            self.assertEqual(result["pages"], 1)

    def test_empty_output_is_a_clean_window(self):
        clean = audit_log.read_removed(context_with(FakeRunner(stdout="")))
        self.assertIsNone(clean["error"])
        self.assertEqual((clean["entries"], clean["pages"], clean["sampled"]), (0, 1, False))

    def test_pages_follow_the_oldest_timestamp_until_a_short_page(self):
        removed, _ = split_sample()
        first = full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT)
        second = full_page(removed[0], 400, start=datetime(2026, 9, 29, 11, 0, tzinfo=timezone.utc))
        runner = FakeRunner(pages={audit_log.READ_REMOVED: [first, second]})
        result = audit_log.read_removed(context_with(runner))
        self.assertEqual((result["entries"], result["pages"], result["sampled"], result["error"]), (1400, 2, False, None))
        self.assertNotIn(PAGE_MARKER, runner.calls[0][3])
        self.assertIn(' AND timestamp<"' + first[-1]["timestamp"] + '"', runner.calls[1][3])
        self.assertIn('timestamp>="2026-09-22T16:00:00Z"', runner.calls[1][3])
        self.assertEqual(result["callers"][0]["count"], 1400)

    def test_page_ceiling_and_time_budget_sample_rather_than_fail(self):
        removed, _ = split_sample()
        pages = [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT, start=datetime(2026, 9, 29, 12 - i, 0, tzinfo=timezone.utc)) for i in range(4)]
        ceiling = audit_log.read_removed(context_with(FakeRunner(pages={audit_log.READ_REMOVED: pages})))
        self.assertEqual((ceiling["entries"], ceiling["pages"], ceiling["sampled"], ceiling["error"]), (3000, 3, True, None))
        self.assertEqual(audit_log.sampled_note(ceiling), "sampled 3000 entries over 3 page(s) of the removed read; more callers possible")
        ticks = iter([0, 61, 61, 61])
        budget = audit_log.read_removed(context_with(FakeRunner(pages={audit_log.READ_REMOVED: [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT)]}), clock=lambda: next(ticks)))
        self.assertEqual((budget["entries"], budget["pages"], budget["sampled"]), (1000, 1, True))

    def test_a_failed_later_page_keeps_the_pages_before_it(self):
        removed, _ = split_sample()
        runner = FakeRunner(pages={audit_log.READ_REMOVED: [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT), FAIL]})
        result = audit_log.read_removed(context_with(runner))
        self.assertIn("(removed read, page 2) failed (rc=1): ERROR: (gcloud.logging.read) boom", result["error"])
        self.assertEqual((result["entries"], result["pages"]), (1000, 2))
        self.assertEqual(result["callers"][0]["principal"], TUNER)

    def test_no_runner_or_scope_is_an_error(self):
        self.assertEqual(audit_log.read_removed({"project": "p", "location": "l", "cluster_name": "c"})["error"], audit_log.NO_RUNNER_REASON)
        self.assertEqual(audit_log.read_removed({"run_cmd": lambda cmd: (0, "[]", ""), "project": "p"})["error"], audit_log.NO_SCOPE_REASON)

    def test_parse_timestamp(self):
        self.assertEqual(audit_log.parse_timestamp("2026-09-29T14:04:19.112Z"), datetime(2026, 9, 29, 14, 4, 19, 112000, tzinfo=timezone.utc))
        self.assertEqual(audit_log.parse_timestamp("2026-09-29T13:54:19.378013123Z"), datetime(2026, 9, 29, 13, 54, 19, 378013, tzinfo=timezone.utc))
        self.assertEqual(audit_log.parse_timestamp("2026-09-29T15:00:00+01:00"), datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc))
        for bad in ("2026-09-29", "yesterday", "", None):
            self.assertIsNone(audit_log.parse_timestamp(bad), bad)

    def test_the_context_timeout_is_the_per_call_cap(self):
        runner = FakeRunner()
        audit_log.read_removed(context_with(runner))
        self.assertEqual(runner.timeouts, [TIMEOUT])
        # Without one the runner's own default applies: the call carries no timeout.
        bare = FakeRunner()
        audit_log.read_removed(context_with(bare, timeout=None))
        self.assertEqual(bare.timeouts, [None])


if __name__ == "__main__":
    unittest.main()
