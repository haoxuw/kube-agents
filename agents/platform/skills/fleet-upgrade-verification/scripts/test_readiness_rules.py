#!/usr/bin/env python3
"""Unit tests for the registered readiness rules on fixture JSON.

The audit-log rules run over `testdata/audit_log_sample.json`, a sample in the shape of
the entries `gcloud logging read --format=json` returns for GKE's Kubernetes audit log,
built from the two captures in bench/upgrade-scenarios/evidence (06/removed-api.txt: a
`flowcontrol/v1beta3` FlowSchema writer stamped `k8s.io/removed-release=1.32` on a 1.31
control plane; 09/final.txt: an Endpoints v1 writer stamped `k8s.io/deprecated=true`), with
kube-system's endpoint-controller, a stale kubectl, a current kubectl and a client-go
controller added beside them. No test runs gcloud: the context carries a fake runner that
answers the removed-release read and the deprecated-or-kubectl read by their filters.
"""

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(__file__))
import upgrade_readiness as readiness  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from readiness_rules import audit_log, changed_defaults, client_addon_skew, deprecated_api_callers, finding, removed_api_callers  # noqa: E402

SAMPLE_PATH = os.path.join(os.path.dirname(__file__), "testdata", "audit_log_sample.json")
TUNER = "system:serviceaccount:kubeagents-system:legacy-flowcontrol-tuner"
WRITER = "system:serviceaccount:seeded-deprecation:legacy-endpoints-writer"
GATEWAY_OPERATOR = "system:serviceaccount:mesh:gateway-operator"
STALE_KUBECTL = "system:serviceaccount:seeded-shapes:stale-kubectl-client"
ENDPOINT_CONTROLLER = "system:serviceaccount:kube-system:endpoint-controller"
TARGET_1_35 = (1, 35, 1, 1000)
TARGET_1_31 = (1, 31, 9, 1)
# Two hours after the sample's last write, so the tuner is a recent caller.
AT = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
FAIL = "FAIL"
DEPRECATED_MARKER = 'k8s.io/deprecated"="true"'
PAGE_MARKER = 'timestamp<"'


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
    """Answers the two reads by filter: canned lists, or scripted pages per read."""

    def __init__(self, removed=(), deprecated=(), rc=0, stderr="", stdout=None, pages=None):
        self.removed, self.deprecated = list(removed), list(deprecated)
        self.rc, self.stderr, self.stdout = rc, stderr, stdout
        self.pages = {k: list(v) for k, v in (pages or {}).items()}
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
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


def context_with(runner=None, at=AT, clock=None):
    context = {"project": "example-project", "location": "us-central1-a", "cluster_name": "upg-06", "run_cmd": runner or FakeRunner(), "at": at, "cache": {}}
    if clock is not None:
        context["clock"] = clock
    return context


def sample_context(at=AT):
    removed, deprecated = split_sample()
    return context_with(FakeRunner(removed, deprecated), at=at)


def member(master="1.33.5-gke.100", pools=("1.33.5-gke.100",)):
    return {
        "project": "example-project",
        "cluster": "upg-06",
        "location": "us-central1-a",
        "control_plane_version": master,
        "node_pools": [{"name": f"pool-{i}", "version": v, "status": "RUNNING"} for i, v in enumerate(pools)],
        "target_version": None,
    }


def namespace(name, **labels):
    return {"kind": "Namespace", "metadata": {"name": name, "labels": labels}}


def psa_namespace(name, enforce, version=None):
    labels = {"pod-security.kubernetes.io/enforce": enforce}
    if version is not None:
        labels["pod-security.kubernetes.io/enforce-version"] = version
    return namespace(name, **labels)


def workload(kind, ns, name, image, volumes=None):
    pod_spec = {"containers": [{"name": "c", "image": image}]}
    if volumes:
        pod_spec["volumes"] = volumes
    template = {"metadata": {"labels": {"app": name}}, "spec": pod_spec}
    if kind == "CronJob":
        return {"kind": kind, "metadata": {"namespace": ns, "name": name}, "spec": {"schedule": "*/10 * * * *", "jobTemplate": {"spec": {"template": template}}}}
    return {"kind": kind, "metadata": {"namespace": ns, "name": name}, "spec": {"replicas": 1, "template": template}}


def with_principal(entry, principal):
    copy = json.loads(json.dumps(entry))
    copy["protoPayload"]["authenticationInfo"]["principalEmail"] = principal
    return copy


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


class RemovedApiCallersTest(unittest.TestCase):
    def test_a_recent_operator_caller_of_a_removed_api_blocks(self):
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, sample_context())
        self.assertEqual(len(result["blocking"]), 1)
        item = result["blocking"][0]
        self.assertEqual((item["principal"], item["caller_class"], item["removed_release"], item["replacement"]), (TUNER, audit_log.CLASS_OPERATOR, "1.32", "flowcontrol.apiserver.k8s.io/v1"))
        self.assertEqual(item["api"], "flowcontrol.apiserver.k8s.io/v1beta3 flowschemas")
        self.assertEqual((result["risks"], result["unknown"]), ([], []))
        text = removed_api_callers.describe(item)
        self.assertIn(f"{TUNER} via legacy-flowcontrol-tuner/0.3 writes flowcontrol.apiserver.k8s.io/v1beta3 flowschemas, removed in 1.32 (2 writes in 7d, last 2026-09-29T14:04:19.112Z)", text)
        self.assertIn("removed at or before the target 1.35, last write 2026-09-29T14:04:19.112Z; this caller fails", text)
        self.assertIn("replacement flowcontrol.apiserver.k8s.io/v1", text)
        self.assertEqual(result["note"], "2 removed-release entries read over 7d (writes only; a caller that only reads is not in this log)")

    def test_a_caller_silent_for_more_than_48_hours_is_a_risk_naming_its_last_write(self):
        later = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, sample_context(at=later))
        self.assertEqual(result["blocking"], [])
        self.assertEqual([r["principal"] for r in result["risks"]], [TUNER])
        self.assertIn("the last write was 2026-09-29T14:04:19.112Z, more than 48h before 2026-10-05T16:00:00Z", result["risks"][0]["detail"])
        exactly = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, sample_context(at=datetime(2026, 10, 1, 14, 4, 19, 112000, tzinfo=timezone.utc)))
        self.assertEqual(len(exactly["blocking"]), 1)

    def test_removal_after_the_target_is_a_risk_not_a_blocker(self):
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_31, sample_context())
        self.assertEqual(result["blocking"], [])
        self.assertIn("removed after the target 1.31", result["risks"][0]["detail"])

    def test_without_a_target_callers_are_risks_and_the_rule_notes_it(self):
        result = removed_api_callers.evaluate({}, member(), [], None, sample_context())
        self.assertEqual((result["blocking"], result["unknown"]), ([], []))
        self.assertEqual(len(result["risks"]), 1)
        self.assertIn(removed_api_callers.NOTE_NO_TARGET, result["note"])

    def test_provider_and_unplaced_principals_never_block(self):
        removed, _ = split_sample()
        provider = with_principal(removed[0], "system:cloud-controller-manager")
        unplaced = with_principal(removed[0], "legacy-flowcontrol-tuner")
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(FakeRunner([provider, unplaced])))
        self.assertEqual(result["blocking"], [])
        by_principal = {r["principal"]: r for r in result["risks"]}
        self.assertEqual(by_principal["system:cloud-controller-manager"]["detail"], removed_api_callers.PROVIDER_DETAIL)
        self.assertIn("a class this rule cannot place (legacy-flowcontrol-tuner)", by_principal["legacy-flowcontrol-tuner"]["detail"])

    def test_a_failed_read_is_unknown_only(self):
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(FakeRunner(rc=-1, stderr="timed out after 60 seconds")))
        self.assertEqual((result["blocking"], result["risks"]), ([], []))
        self.assertEqual(len(result["unknown"]), 1)
        self.assertIn("timed out after 60 seconds", result["unknown"][0]["reason"])
        self.assertEqual(removed_api_callers.describe(result["unknown"][0]), result["unknown"][0]["reason"])
        self.assertEqual(result["note"], "")

    def test_a_failed_later_page_keeps_the_blocker_and_adds_an_unknown(self):
        removed, _ = split_sample()
        runner = FakeRunner(pages={audit_log.READ_REMOVED: [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT), FAIL]})
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(runner))
        self.assertEqual(len(result["blocking"]), 1)
        self.assertIn("page 2", result["unknown"][0]["reason"])

    def test_a_sampled_read_is_graded_and_noted_not_unknown(self):
        removed, _ = split_sample()
        pages = [full_page(removed[0], tables.AUDIT_LOG_PAGE_LIMIT, start=datetime(2026, 9, 29, 12 - i, 0, tzinfo=timezone.utc)) for i in range(3)]
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(FakeRunner(pages={audit_log.READ_REMOVED: pages})))
        self.assertEqual(len(result["blocking"]), 1)
        self.assertEqual(result["unknown"], [])
        self.assertIn("sampled 3000 entries over 3 page(s) of the removed read; more callers possible", result["note"])

    def test_a_chatty_deprecated_writer_cannot_crowd_out_the_removed_read(self):
        # The seeded fixture's rate: a write every ten minutes is 1,008 stamped entries in
        # seven days, more than one page, all of them on the deprecated read.
        _, deprecated = split_sample()
        writer = next(e for e in deprecated if e["protoPayload"]["authenticationInfo"]["principalEmail"] == WRITER)
        chatty = full_page(writer, 1008)
        context = context_with(FakeRunner(removed=[], deprecated=chatty))
        clean = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context)
        self.assertEqual((clean["blocking"], clean["risks"], clean["unknown"]), ([], [], []))
        self.assertEqual(clean["note"], "0 removed-release entries read over 7d (writes only; a caller that only reads is not in this log)")
        noisy = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, context)
        self.assertEqual([(r["principal"], r["count"]) for r in noisy["risks"]], [(WRITER, 1008)])
        self.assertEqual(noisy["unknown"], [])

    def test_a_clean_log_reads_clean(self):
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(FakeRunner()))
        self.assertEqual((result["blocking"], result["risks"], result["unknown"]), ([], [], []))
        self.assertEqual(result["note"], "0 removed-release entries read over 7d (writes only; a caller that only reads is not in this log)")


class DeprecatedApiCallersTest(unittest.TestCase):
    def test_operator_callers_are_risks_and_provider_callers_are_a_note(self):
        result = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, sample_context())
        self.assertEqual(result["blocking"], [])
        self.assertEqual([r["principal"] for r in result["risks"]], [GATEWAY_OPERATOR, WRITER])
        writer = result["risks"][1]
        self.assertEqual((writer["api"], writer["count"], writer["caller_class"]), ("core/v1 endpoints", 2, audit_log.CLASS_OPERATOR))
        self.assertIn("EndpointSlice", writer["successor"])
        text = deprecated_api_callers.describe(writer)
        self.assertIn(f"{WRITER} via Python-urllib/3.12 writes core/v1 endpoints (2 writes in 7d, last 2026-09-29T14:10:02.950Z)", text)
        self.assertIn("successor discovery.k8s.io/v1 EndpointSlice", text)
        self.assertIn(f"1 provider caller of deprecated APIs not filed ({ENDPOINT_CONTROLLER} on core/v1 endpoints)", result["note"])
        self.assertNotIn(TUNER, [r["principal"] for r in result["risks"]])
        self.assertEqual(result["unknown"], [])

    def test_an_unplaced_principal_is_a_risk_that_names_it(self):
        _, deprecated = split_sample()
        writer = next(e for e in deprecated if e["protoPayload"]["authenticationInfo"]["principalEmail"] == WRITER)
        result = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(FakeRunner(deprecated=[with_principal(writer, "legacy-writer")])))
        self.assertEqual([(r["principal"], r["caller_class"]) for r in result["risks"]], [("legacy-writer", audit_log.CLASS_UNPLACED)])
        self.assertEqual(result["risks"][0]["detail"], deprecated_api_callers.UNPLACED_DETAIL)

    def test_failed_read_and_sampled_read(self):
        failed = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(FakeRunner(rc=1, stderr="denied")))
        self.assertEqual(failed["risks"], [])
        self.assertIn("denied", failed["unknown"][0]["reason"])
        _, deprecated = split_sample()
        writer = next(e for e in deprecated if e["protoPayload"]["authenticationInfo"]["principalEmail"] == WRITER)
        pages = [full_page(writer, tables.AUDIT_LOG_PAGE_LIMIT, start=datetime(2026, 9, 29, 12 - i, 0, tzinfo=timezone.utc)) for i in range(3)]
        sampled = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(FakeRunner(pages={audit_log.READ_DEPRECATED: pages})))
        self.assertEqual(len(sampled["risks"]), 1)
        self.assertEqual(sampled["unknown"], [])
        self.assertIn("sampled 3000 entries over 3 page(s) of the deprecated read", sampled["note"])

    def test_clean_log_notes_it(self):
        result = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(FakeRunner()))
        self.assertEqual(result["risks"], [])
        self.assertEqual(result["note"], "no operator caller of a deprecated API in 7d (writes only; a caller that only reads is not in this log)")


class ClientAddonSkewTest(unittest.TestCase):
    CLUSTER = {
        "addonsConfig": {
            "httpLoadBalancing": {},
            "horizontalPodAutoscaling": {},
            "kubernetesDashboard": {"disabled": True},
            "networkPolicyConfig": {"disabled": True},
            "gcePersistentDiskCsiDriverConfig": {"enabled": True},
        }
    }
    ITEMS = [
        workload("CronJob", "seeded-shapes", "stale-kubectl-client", "registry.k8s.io/kubectl:v1.29.0"),
        workload("Deployment", "cert-manager", "cert-manager", "quay.io/jetstack/cert-manager-controller:v1.21.0"),
        workload("Deployment", "istio-system", "istiod", "docker.io/istio/pilot:1.24.2"),
        workload("Deployment", "shop", "web", "registry.k8s.io/pause:3.9"),
    ]
    CLEAR = {"blocking_exclusions": [], "undecided_exclusions": []}
    NO_SKEW = {"blocking": [], "unknown": []}

    def test_kubectl_outside_one_minor_is_a_risk_from_the_log_and_from_images(self):
        result = client_addon_skew.evaluate(self.CLUSTER, member(), self.ITEMS, TARGET_1_35, sample_context())
        self.assertEqual(result["blocking"], [])
        clients = [r for r in result["risks"] if r["kind"] == client_addon_skew.KIND_CLIENT]
        self.assertEqual(len(clients), 2)
        from_log = next(c for c in clients if "principal" in c["where"])
        self.assertEqual((from_log["version"], from_log["gap_minors"]), ("v1.29", 6))
        self.assertIn(STALE_KUBECTL, from_log["where"])
        self.assertIn("kubectl is supported within 1 minor of kube-apiserver", from_log["detail"])
        self.assertIn(tables.KUBECTL_SKEW_SOURCE, from_log["detail"])
        from_image = next(c for c in clients if "image" in c["where"])
        self.assertEqual(from_image["where"], "CronJob seeded-shapes/stale-kubectl-client (image registry.k8s.io/kubectl:v1.29.0)")
        self.assertIn("kubectl v1.29 from CronJob seeded-shapes/stale-kubectl-client", client_addon_skew.describe(from_image))
        self.assertIn("1 kubectl client within one minor of the target", result["note"])
        self.assertIn("1 client-go caller whose user agent carries the binary's version", result["note"])

    def test_an_addon_without_a_table_is_a_note_and_the_verdict_stays(self):
        result = client_addon_skew.evaluate(self.CLUSTER, member(), self.ITEMS, TARGET_1_35, context_with())
        self.assertEqual([r for r in result["risks"] if r.get("kind") == client_addon_skew.KIND_ADDON], [])
        self.assertEqual(result["unknown"], [])
        self.assertIn("istio v1.24 at Deployment istio-system/istiod (image docker.io/istio/pilot:1.24.2): version not graded (no support table with a source here; check the vendor's matrix against 1.35)", result["note"])
        self.assertIn("cert-manager v1.21 supports the target 1.35 (https://cert-manager.io/docs/releases/)", result["note"])
        self.assertIn("GKE add-ons on: gcePersistentDiskCsiDriverConfig, horizontalPodAutoscaling, httpLoadBalancing (GKE-managed", result["note"])
        self.assertEqual(readiness.readiness_status({"blocking": []}, self.CLEAR, self.NO_SKEW, True, {client_addon_skew.RULE_ID: result}), "ready")

    def test_addon_outside_its_supported_range_is_a_risk_and_an_unlisted_release_a_note(self):
        result = client_addon_skew.evaluate({}, member(), self.ITEMS[1:2], (1, 37, 0, 0), context_with())
        addon = result["risks"][0]
        self.assertEqual((addon["addon"], addon["version"], addon["supported"]), ("cert-manager", "v1.21", ("1.33", "1.36")))
        self.assertIn("cert-manager v1.21 supports Kubernetes 1.33 to 1.36", client_addon_skew.describe(addon))
        self.assertIn("the target 1.37 is outside it", addon["detail"])
        old = workload("Deployment", "cert-manager", "cert-manager", "quay.io/jetstack/cert-manager-controller:v1.12.0")
        result = client_addon_skew.evaluate({}, member(), [old], TARGET_1_35, context_with())
        self.assertEqual((result["risks"], result["unknown"]), ([], []))
        self.assertIn("cert-manager v1.12 at Deployment cert-manager/cert-manager (image quay.io/jetstack/cert-manager-controller:v1.12.0): version not graded (release not in the table read on 2026-10-09", result["note"])

    def test_unread_objects_are_unknown_and_a_missing_target_is_a_note(self):
        unread = client_addon_skew.evaluate(self.CLUSTER, member(), None, TARGET_1_35, sample_context())
        self.assertEqual([u["reason"] for u in unread["unknown"]], [client_addon_skew.OBJECTS_NOT_READ])
        self.assertEqual(len(unread["risks"]), 1)
        no_target = client_addon_skew.evaluate(self.CLUSTER, member(), self.ITEMS, None, sample_context())
        self.assertEqual((no_target["risks"], no_target["unknown"]), ([], []))
        self.assertIn("no target; 3 kubectl clients not graded against a skew window", no_target["note"])

    def test_failed_log_read_still_grades_images(self):
        result = client_addon_skew.evaluate({}, member(), self.ITEMS[:1], TARGET_1_35, context_with(FakeRunner(rc=-1, stderr="timed out after 60 seconds")))
        self.assertEqual(len(result["risks"]), 1)
        self.assertIn("image registry.k8s.io/kubectl:v1.29.0", result["risks"][0]["where"])
        self.assertIn("timed out", result["unknown"][0]["reason"])

    def test_gke_addon_blocks_read_enabled_and_disabled_as_the_proto_spells_them(self):
        config = {"addonsConfig": {"dnsCacheConfig": {}, "cloudRunConfig": {"disabled": True}, "gkeBackupAgentConfig": {"enabled": True}, "configConnectorConfig": {"enabled": False}, "httpLoadBalancing": {}}}
        self.assertEqual(client_addon_skew.enabled_gke_addons(config), ["gkeBackupAgentConfig", "httpLoadBalancing"])
        self.assertEqual(client_addon_skew.enabled_gke_addons({"addonsConfig": {"dnsCacheConfig": {"enabled": True}}}), ["dnsCacheConfig"])
        self.assertEqual(client_addon_skew.enabled_gke_addons({}), [])


class ChangedDefaultsTest(unittest.TestCase):
    def test_namespace_following_latest_is_a_risk_when_a_crossed_minor_tightens(self):
        items = [psa_namespace("seeded-defaults", "baseline", "latest"), psa_namespace("unset", "restricted"), psa_namespace("pinned", "baseline", "v1.33"), psa_namespace("priv", "privileged", "latest"), namespace("plain")]
        result = changed_defaults.evaluate({}, member("1.33.5-gke.100"), items, TARGET_1_35, {})
        self.assertEqual(result["blocking"], [])
        self.assertEqual([r["namespace"] for r in result["risks"]], ["seeded-defaults", "unset"])
        item = result["risks"][0]
        self.assertEqual(item["setting"], "pod-security.kubernetes.io/enforce=baseline, pod-security.kubernetes.io/enforce-version=latest")
        self.assertEqual([c["minor"] for c in item["changes"]], ["1.34"])
        self.assertIn("host field of probes and lifecycle hooks must be unset", item["changes"][0]["change"])
        self.assertEqual(item["changes"][0]["source"], tables.POD_SECURITY_STANDARDS_SOURCE)
        text = changed_defaults.describe(item)
        self.assertTrue(text.startswith("namespace seeded-defaults: pod-security.kubernetes.io/enforce=baseline"))
        self.assertIn("follows the running minor, so the enforced baseline rule set changes at 1.34", text)
        self.assertEqual(result["risks"][1]["version_label"], "unset (latest)")
        self.assertIn("1 namespace pinned to a Pod Security version", result["note"])
        self.assertIn("1 namespace enforces privileged", result["note"])
        self.assertEqual(result["unknown"], [])

    def test_no_check_changes_between_the_minors_is_a_note_not_a_risk(self):
        result = changed_defaults.evaluate({}, member("1.34.2-gke.1"), [psa_namespace("seeded-defaults", "baseline", "latest")], TARGET_1_35, {})
        self.assertEqual(result["risks"], [])
        self.assertIn("1 namespace follows latest with no enforced check changing between 1.34 and 1.35 per the table", result["note"])

    def test_a_widened_allowlist_is_a_note(self):
        result = changed_defaults.evaluate({}, member("1.26.0-gke.1", ("1.26.0-gke.1",)), [psa_namespace("x", "baseline", "latest")], (1, 27, 0, 0), {})
        self.assertEqual(result["risks"], [])
        self.assertIn("x follows latest; 1.27 only widens the baseline allowlist, which rejects nothing new", result["note"])

    def test_restricted_only_rows_do_not_reach_a_baseline_namespace(self):
        items = [psa_namespace("base", "baseline", "latest"), psa_namespace("strict", "restricted", "latest")]
        result = changed_defaults.evaluate({}, member("1.22.0-gke.1", ("1.22.0-gke.1",)), items, (1, 23, 0, 0), {})
        self.assertEqual([r["namespace"] for r in result["risks"]], ["strict"])
        self.assertIn("runAsUser to 0", result["risks"][0]["changes"][0]["change"])

    def test_gitrepo_volume_is_graded_from_the_node_pools(self):
        volumes = [{"name": "src", "gitRepo": {"repository": "https://example.com/repo.git"}}]
        items = [workload("Deployment", "legacy", "git-sync", "registry.k8s.io/pause:3.9", volumes)]
        crossing = changed_defaults.evaluate({}, member("1.33.0-gke.1", ("1.32.0-gke.1",)), items, (1, 33, 0, 0), {})
        self.assertEqual((crossing["risks"][0]["subject"], crossing["risks"][0]["setting"]), ("Deployment legacy/git-sync", "gitRepo volume src"))
        self.assertEqual([c["minor"] for c in crossing["risks"][0]["changes"]], ["1.33"])
        self.assertIn("GitRepoVolumeDriver", crossing["risks"][0]["changes"][0]["change"])
        self.assertEqual(crossing["risks"][0]["changes"][0]["source"], tables.KUBERNETES_1_33_CHANGELOG_SOURCE)
        already_past = changed_defaults.evaluate({}, member("1.34.0-gke.1", ("1.34.0-gke.1",)), items, (1, 35, 0, 0), {})
        self.assertEqual(already_past["risks"], [])
        permanent = changed_defaults.evaluate({}, member("1.35.0-gke.1", ("1.35.0-gke.1",)), items, (1, 36, 0, 0), {})
        self.assertIn("permanently disabled", permanent["risks"][0]["changes"][0]["change"])

    def test_a_target_past_the_table_is_graded_on_the_covered_minors_with_a_note(self):
        items = [psa_namespace("seeded-defaults", "baseline", "latest")]
        result = changed_defaults.evaluate({}, member("1.33.5-gke.100"), items, (1, 37, 0, 0), {})
        self.assertEqual(result["unknown"], [])
        self.assertEqual([c["minor"] for c in result["risks"][0]["changes"]], ["1.34"])
        self.assertIn("the defaults table ends at 1.36 (read 2026-10-09); minors after it up to the target 1.37 are not graded", result["note"])

    def test_no_target_is_a_note_and_unread_objects_or_a_bad_master_are_unknown(self):
        items = [psa_namespace("seeded-defaults", "baseline", "latest")]
        no_target = changed_defaults.evaluate({}, member(), items, None, {})
        self.assertEqual((no_target["risks"], no_target["unknown"]), ([], []))
        self.assertEqual(no_target["note"], changed_defaults.NOTE_NO_TARGET)
        self.assertEqual(changed_defaults.evaluate({}, member(), None, TARGET_1_35, {})["unknown"][0]["reason"], changed_defaults.OBJECTS_NOT_READ)
        bad = changed_defaults.evaluate({}, member("latest"), items, TARGET_1_35, {})
        self.assertIn("unparsable", bad["unknown"][0]["reason"])

    def test_crossed_minors(self):
        self.assertEqual(changed_defaults.crossed_minors((1, 33), (1, 35)), [(1, 34)])
        self.assertEqual(changed_defaults.crossed_minors((1, 32), (1, 33)), [(1, 33)])
        self.assertEqual(changed_defaults.crossed_minors((1, 35), (1, 35)), [])
        self.assertEqual(changed_defaults.crossed_minors(None, (1, 23)), [(1, 23)])


class SharedHelpersTest(unittest.TestCase):
    def test_pod_spec_walks_every_kind_once(self):
        cron = workload("CronJob", "ns", "job", "img")
        deploy = workload("Deployment", "ns", "web", "img")
        self.assertEqual(finding.pod_spec(cron)["containers"][0]["image"], "img")
        self.assertEqual(finding.pod_spec(deploy)["containers"][0]["image"], "img")
        self.assertIsNone(finding.pod_spec({"kind": "Pod", "spec": {}}))
        self.assertIsNone(finding.pod_spec("not a dict"))
        self.assertEqual(finding.workload_label(cron), "CronJob ns/job")


class ShapeTablesTest(unittest.TestCase):
    def test_every_default_change_row_names_a_source_and_a_minor_inside_as_of(self):
        for minor, rows in tables.DEFAULT_CHANGES_BY_MINOR.items():
            self.assertLessEqual(minor, tables.DEFAULT_CHANGES_AS_OF)
            for row in rows:
                self.assertTrue(row["source"].startswith("https://"), row)
                self.assertIn(row["detector"], (tables.DETECTOR_POD_SECURITY, tables.DETECTOR_GITREPO_VOLUME))
                self.assertIn(row["side"], (tables.SIDE_APISERVER, tables.SIDE_KUBELET))
                self.assertIsInstance(row["tightens"], bool)

    def test_addon_support_rows_name_a_source_and_a_date(self):
        for addon, support in tables.ADDON_SUPPORT.items():
            self.assertTrue(support["source"].startswith("https://"), addon)
            self.assertRegex(support["read_on"], r"^\d{4}-\d{2}-\d{2}$")
            for release, (low, high) in support["releases"].items():
                self.assertLessEqual(low, high, (addon, release))
        self.assertTrue(set(tables.ADDON_SUPPORT) <= set(tables.ADDON_IMAGES.values()))

    def test_replacement_comes_from_the_scan_table(self):
        self.assertEqual(tables.replacement_for("flowcontrol.apiserver.k8s.io/v1beta3 flowschemas"), "flowcontrol.apiserver.k8s.io/v1")
        self.assertEqual(tables.replacement_for("policy/v1beta1 podsecuritypolicies"), "none; Pod Security Admission or a policy controller")
        self.assertIsNone(tables.replacement_for("core/v1 endpoints"))
        self.assertIsNone(tables.replacement_for("nonsense"))

    def test_parse_minor(self):
        self.assertEqual(tables.parse_minor("1.35.1-gke.1000"), (1, 35))
        self.assertEqual(tables.parse_minor("v1.29.0"), (1, 29))
        self.assertEqual(tables.parse_minor("1.32"), (1, 32))
        self.assertIsNone(tables.parse_minor("latest"))
        self.assertIsNone(tables.parse_minor(None))


class RegistryTest(unittest.TestCase):
    def test_registry_order_ids_and_contract(self):
        self.assertEqual([m.RULE_ID for m in readiness.EXTRA_RULES], ["removed-api-callers", "deprecated-api-callers", "client-addon-skew", "changed-defaults"])
        for module in readiness.EXTRA_RULES:
            self.assertTrue(callable(module.evaluate), module)
            self.assertTrue(callable(module.describe), module)

    def test_every_rule_runs_over_two_shared_reads(self):
        context = sample_context()
        items = ClientAddonSkewTest.ITEMS + [psa_namespace("seeded-defaults", "baseline", "latest")]
        results = readiness.evaluate_extra_rules(ClientAddonSkewTest.CLUSTER, member("1.33.5-gke.100"), items, TARGET_1_35, context)
        self.assertEqual(list(results), [m.RULE_ID for m in readiness.EXTRA_RULES])
        self.assertEqual(len(context["run_cmd"].calls), 2)
        for result in results.values():
            self.assertTrue(set(result) >= set(finding.RESULT_KEYS))
        self.assertEqual(len(results["removed-api-callers"]["blocking"]), 1)
        self.assertEqual(readiness.readiness_status({"blocking": []}, {"blocking_exclusions": [], "undecided_exclusions": []}, {"blocking": [], "unknown": []}, True, results), "blocked")

    def test_a_rule_that_raises_is_its_own_unknown_cell(self):
        class Broken:
            RULE_ID = "broken"
            __name__ = "broken"

            @staticmethod
            def evaluate(*args):
                raise RuntimeError("boom")

            @staticmethod
            def describe(item):
                return "described: " + item["reason"]

        with patch.object(readiness, "EXTRA_RULES", [Broken, removed_api_callers]):
            results = readiness.evaluate_extra_rules({}, member(), [], TARGET_1_35, context_with())
            self.assertEqual(results["broken"]["unknown"][0]["reason"], "rule broken raised RuntimeError('boom'); not evaluated")
            self.assertEqual(readiness.describe_rule_finding("broken", results["broken"]["unknown"][0]), "described: rule broken raised RuntimeError('boom'); not evaluated")
            self.assertEqual(results["removed-api-callers"]["blocking"], [])
        self.assertEqual(readiness.describe_rule_finding("gone", {"reason": "why"}), "why")


if __name__ == "__main__":
    unittest.main()
