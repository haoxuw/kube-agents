#!/usr/bin/env python3
"""Unit tests for the registered readiness rules on fixture JSON.

The audit-log rules run over `testdata/audit_log_sample.json`, a sample in the shape of
the entries `gcloud logging read --format=json` returns for GKE's Kubernetes audit log,
built from the two captures in bench/upgrade-scenarios/evidence (06/removed-api.txt: a
`flowcontrol/v1beta3` FlowSchema writer stamped `k8s.io/removed-release=1.32` on a 1.31
control plane; 09/final.txt: an Endpoints v1 writer stamped `k8s.io/deprecated=true`), with
kube-system's endpoint-controller, a stale kubectl, a current kubectl and a client-go
controller added beside them. No test runs gcloud: the context carries a fake runner.
"""

import json
import os
import sys
import unittest
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


def sample_entries():
    with open(SAMPLE_PATH, encoding="utf-8") as f:
        return json.load(f)


def context_with(entries=None, rc=0, stdout=None, stderr="", calls=None):
    """A readiness context whose runner answers every command with the canned result."""
    calls = calls if calls is not None else []

    def run(cmd):
        calls.append(cmd)
        if stdout is not None:
            return rc, stdout, stderr
        return rc, json.dumps([] if entries is None else entries), stderr

    return {"project": "example-project", "location": "us-central1-a", "cluster_name": "upg-06", "run_cmd": run, "cache": {}, "calls": calls}


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


class AuditLogReadTest(unittest.TestCase):
    def test_command_has_the_filter_window_limit_and_json(self):
        cmd = audit_log.build_command("example-project", "seeded-a", "us-central1-a")
        self.assertEqual(cmd[:3], ["gcloud", "logging", "read"])
        self.assertIn('resource.type="k8s_cluster"', cmd[3])
        self.assertIn('resource.labels.cluster_name="seeded-a"', cmd[3])
        self.assertIn('resource.labels.location="us-central1-a"', cmd[3])
        self.assertIn('labels."k8s.io/removed-release":*', cmd[3])
        self.assertIn('labels."k8s.io/deprecated"="true"', cmd[3])
        self.assertIn('protoPayload.requestMetadata.callerSuppliedUserAgent:"kubectl/"', cmd[3])
        self.assertEqual(cmd[4:], ["--project=example-project", "--freshness=7d", "--limit=1000", "--format=json"])
        self.assertEqual(tables.AUDIT_LOG_WINDOW_DAYS, 7)

    def test_a_quote_in_a_name_cannot_close_the_filter(self):
        self.assertIn('cluster_name="a\\"b"', audit_log.build_filter('a"b', "loc"))

    def test_api_from_resource_name_and_from_method_name(self):
        self.assertEqual(audit_log.api_of("core/v1/namespaces/seeded-deprecation/endpoints/legacy-endpoints-lane", None), "core/v1 endpoints")
        self.assertEqual(audit_log.api_of("flowcontrol.apiserver.k8s.io/v1beta3/flowschemas/legacy-batch-lane", None), "flowcontrol.apiserver.k8s.io/v1beta3 flowschemas")
        self.assertEqual(audit_log.api_of("apps/v1/namespaces/shop/deployments/web/status", None), "apps/v1 deployments")
        self.assertEqual(audit_log.api_of("core/v1/nodes/n1", None), "core/v1 nodes")
        self.assertEqual(audit_log.api_of("core/v1/namespaces/foo", None), "core/v1 namespaces")
        self.assertEqual(audit_log.api_of(None, "io.k8s.core.v1.endpoints.patch"), "core/v1 endpoints")
        self.assertEqual(audit_log.api_of(None, "io.k8s.apiserver.flowcontrol.v1beta3.flowschemas.create"), "flowcontrol.apiserver/v1beta3 flowschemas")
        self.assertEqual(audit_log.api_of(None, ""), audit_log.UNKNOWN_API)

    def test_platform_callers(self):
        for principal in (ENDPOINT_CONTROLLER, "system:kube-controller-manager", "system:node:gke-a-1", "system:serviceaccount:gke-managed-system:x", "service-123@container-engine-robot.iam.gserviceaccount.com", "service-123@gcp-sa-gkenode.iam.gserviceaccount.com"):
            self.assertTrue(audit_log.is_platform_caller(principal), principal)
        for principal in (TUNER, WRITER, "operator@example.com", "system:serviceaccount:kubeagents-system:platform"):
            self.assertFalse(audit_log.is_platform_caller(principal), principal)

    def test_sample_groups_by_caller_and_api(self):
        callers = {(c["principal"], c["api"]): c for c in audit_log.caller_records(sample_entries())}
        tuner = callers[(TUNER, "flowcontrol.apiserver.k8s.io/v1beta3 flowschemas")]
        self.assertEqual(tuner["count"], 2)
        self.assertEqual(tuner["removed_release"], "1.32")
        self.assertTrue(tuner["deprecated"])
        self.assertEqual(tuner["user_agent"], "legacy-flowcontrol-tuner/0.3")
        self.assertEqual(tuner["first_seen"], "2026-09-29T13:54:19.378013Z")
        self.assertEqual(tuner["last_seen"], "2026-09-29T14:04:19.112Z")
        self.assertFalse(tuner["platform"])
        writer = callers[(WRITER, "core/v1 endpoints")]
        self.assertEqual(writer["count"], 2)
        self.assertIsNone(writer["removed_release"])
        self.assertTrue(writer["deprecated"])
        self.assertTrue(callers[(ENDPOINT_CONTROLLER, "core/v1 endpoints")]["platform"])
        self.assertTrue(callers[(STALE_KUBECTL, "core/v1 configmaps")]["kubectl"])
        self.assertFalse(callers[(STALE_KUBECTL, "core/v1 configmaps")]["deprecated"])

    def test_read_once_per_context_and_cached_for_every_rule(self):
        context = context_with(sample_entries())
        first = audit_log.read_callers(context)
        second = audit_log.read_callers(context)
        self.assertIs(first, second)
        self.assertEqual(len(context["calls"]), 1)
        self.assertEqual(first["entries"], 8)
        self.assertFalse(first["truncated"])
        self.assertIsNone(first["error"])
        self.assertIn("gcloud logging read", first["command"])

    def test_failed_timed_out_and_unparsable_reads_are_errors_not_findings(self):
        failed = audit_log.read_callers(context_with(rc=1, stderr="ERROR: (gcloud.logging.read) PERMISSION_DENIED"))
        self.assertIn("failed (rc=1)", failed["error"])
        self.assertIn("PERMISSION_DENIED", failed["error"])
        timed_out = audit_log.read_callers(context_with(rc=-1, stderr="timed out after 60 seconds"))
        self.assertIn("timed out after 60 seconds", timed_out["error"])
        garbled = audit_log.read_callers(context_with(stdout="{not json"))
        self.assertEqual(garbled["error"], audit_log.UNPARSABLE_REASON)
        not_a_list = audit_log.read_callers(context_with(stdout=json.dumps({"entries": []})))
        self.assertEqual(not_a_list["error"], audit_log.UNPARSABLE_REASON)
        for result in (failed, timed_out, garbled, not_a_list):
            self.assertEqual(result["callers"], [])

    def test_empty_output_is_a_clean_window_and_a_full_page_is_truncated(self):
        clean = audit_log.read_callers(context_with(stdout=""))
        self.assertIsNone(clean["error"])
        self.assertEqual(clean["entries"], 0)
        page = [sample_entries()[0]] * tables.AUDIT_LOG_LIMIT
        full = audit_log.read_callers(context_with(page))
        self.assertTrue(full["truncated"])
        self.assertIn("full page of 1000 entries", audit_log.truncation_reason(full))

    def test_no_runner_or_scope_is_an_error(self):
        self.assertEqual(audit_log.read_callers({"project": "p", "location": "l", "cluster_name": "c"})["error"], audit_log.NO_RUNNER_REASON)
        self.assertEqual(audit_log.read_callers({"run_cmd": lambda cmd: (0, "[]", ""), "project": "p"})["error"], audit_log.NO_SCOPE_REASON)


class RemovedApiCallersTest(unittest.TestCase):
    def test_caller_of_an_api_removed_at_or_before_the_target_blocks(self):
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(sample_entries()))
        self.assertEqual(len(result["blocking"]), 1)
        item = result["blocking"][0]
        self.assertEqual(item["principal"], TUNER)
        self.assertEqual(item["api"], "flowcontrol.apiserver.k8s.io/v1beta3 flowschemas")
        self.assertEqual(item["removed_release"], "1.32")
        self.assertEqual(item["replacement"], "flowcontrol.apiserver.k8s.io/v1")
        self.assertEqual(result["risks"], [])
        self.assertEqual(result["unknown"], [])
        text = removed_api_callers.describe(item)
        self.assertIn(f"{TUNER} via legacy-flowcontrol-tuner/0.3 calls flowcontrol.apiserver.k8s.io/v1beta3 flowschemas, removed in 1.32 (2 calls in 7d, last 2026-09-29T14:04:19.112Z)", text)
        self.assertIn("removed at or before the target 1.35", text)
        self.assertIn("replacement flowcontrol.apiserver.k8s.io/v1", text)
        self.assertEqual(result["note"], "8 stamped audit entries read over 7d")

    def test_removal_after_the_target_is_a_risk_not_a_blocker(self):
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_31, context_with(sample_entries()))
        self.assertEqual(result["blocking"], [])
        self.assertEqual([r["principal"] for r in result["risks"]], [TUNER])
        self.assertIn("removed after the target 1.31", result["risks"][0]["detail"])

    def test_without_a_target_the_caller_is_a_risk_and_the_rule_is_unknown(self):
        result = removed_api_callers.evaluate({}, member(), [], None, context_with(sample_entries()))
        self.assertEqual(result["blocking"], [])
        self.assertEqual(len(result["risks"]), 1)
        self.assertEqual([u["reason"] for u in result["unknown"]], [removed_api_callers.NO_TARGET_REASON])

    def test_a_platform_caller_is_a_risk(self):
        entry = sample_entries()[0]
        entry["protoPayload"]["authenticationInfo"]["principalEmail"] = ENDPOINT_CONTROLLER
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with([entry]))
        self.assertEqual(result["blocking"], [])
        self.assertIn("GKE-managed caller", result["risks"][0]["detail"])

    def test_a_failed_read_is_unknown_only(self):
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(rc=-1, stderr="timed out after 60 seconds"))
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["risks"], [])
        self.assertEqual(len(result["unknown"]), 1)
        self.assertIn("timed out after 60 seconds", result["unknown"][0]["reason"])
        self.assertEqual(removed_api_callers.describe(result["unknown"][0]), result["unknown"][0]["reason"])

    def test_a_cut_window_adds_an_unknown_beside_what_was_read(self):
        page = [sample_entries()[0]] * tables.AUDIT_LOG_LIMIT
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(page))
        self.assertEqual(len(result["blocking"]), 1)
        self.assertIn("full page", result["unknown"][0]["reason"])

    def test_a_clean_log_reads_clean(self):
        result = removed_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with([]))
        self.assertEqual((result["blocking"], result["risks"], result["unknown"]), ([], [], []))
        self.assertEqual(result["note"], "0 stamped audit entries read over 7d")


class DeprecatedApiCallersTest(unittest.TestCase):
    def test_operator_callers_are_risks_and_gke_callers_are_a_note(self):
        result = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(sample_entries()))
        self.assertEqual(result["blocking"], [])
        self.assertEqual([r["principal"] for r in result["risks"]], [GATEWAY_OPERATOR, WRITER])
        writer = result["risks"][1]
        self.assertEqual(writer["api"], "core/v1 endpoints")
        self.assertEqual(writer["count"], 2)
        self.assertIn("EndpointSlice", writer["successor"])
        text = deprecated_api_callers.describe(writer)
        self.assertIn(f"{WRITER} via Python-urllib/3.12 calls core/v1 endpoints (2 calls in 7d, last 2026-09-29T14:10:02.950Z)", text)
        self.assertIn("successor discovery.k8s.io/v1 EndpointSlice", text)
        self.assertIn(f"1 GKE-managed caller of deprecated APIs not filed ({ENDPOINT_CONTROLLER} on core/v1 endpoints)", result["note"])
        # The removed-release caller belongs to the removal rule, not here.
        self.assertNotIn(TUNER, [r["principal"] for r in result["risks"]])
        self.assertEqual(result["unknown"], [])

    def test_failed_read_and_cut_window(self):
        failed = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(rc=1, stderr="denied"))
        self.assertEqual(failed["risks"], [])
        self.assertIn("denied", failed["unknown"][0]["reason"])
        page = [sample_entries()[2]] * tables.AUDIT_LOG_LIMIT
        cut = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with(page))
        self.assertEqual(len(cut["risks"]), 1)
        self.assertIn("full page", cut["unknown"][0]["reason"])

    def test_clean_log_notes_it(self):
        result = deprecated_api_callers.evaluate({}, member(), [], TARGET_1_35, context_with([]))
        self.assertEqual(result["risks"], [])
        self.assertEqual(result["note"], "no operator-owned caller of a deprecated API in 7d")


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

    def test_kubectl_outside_one_minor_is_a_risk_from_the_log_and_from_images(self):
        result = client_addon_skew.evaluate(self.CLUSTER, member(), self.ITEMS, TARGET_1_35, context_with(sample_entries()))
        self.assertEqual(result["blocking"], [])
        clients = [r for r in result["risks"] if r["kind"] == "client"]
        self.assertEqual(len(clients), 2)
        from_log = next(c for c in clients if "principal" in c["where"])
        self.assertEqual(from_log["version"], "v1.29")
        self.assertEqual(from_log["gap_minors"], 6)
        self.assertIn(STALE_KUBECTL, from_log["where"])
        self.assertIn("kubectl is supported within 1 minor of kube-apiserver", from_log["detail"])
        self.assertIn(tables.KUBECTL_SKEW_SOURCE, from_log["detail"])
        from_image = next(c for c in clients if "image" in c["where"])
        self.assertEqual(from_image["where"], "CronJob seeded-shapes/stale-kubectl-client (image registry.k8s.io/kubectl:v1.29.0)")
        self.assertIn("kubectl v1.29 from CronJob seeded-shapes/stale-kubectl-client", client_addon_skew.describe(from_image))
        # The 1.35 kubectl is within the window; the client-go controller is counted, not graded.
        self.assertIn("1 kubectl client within one minor of the target", result["note"])
        self.assertIn("1 client-go caller whose user agent carries the binary's version", result["note"])

    def test_addons_graded_against_the_table_or_unknown_without_one(self):
        result = client_addon_skew.evaluate(self.CLUSTER, member(), self.ITEMS, TARGET_1_35, context_with([]))
        self.assertEqual([r for r in result["risks"] if r.get("kind") == "addon"], [])
        self.assertIn("cert-manager v1.21 supports the target 1.35 (https://cert-manager.io/docs/releases/)", result["note"])
        self.assertEqual(len(result["unknown"]), 1)
        self.assertIn("istio v1.24 at Deployment istio-system/istiod", result["unknown"][0]["reason"])
        self.assertIn("no support table with a source here", result["unknown"][0]["reason"])
        self.assertIn("GKE add-ons on: gcePersistentDiskCsiDriverConfig, horizontalPodAutoscaling, httpLoadBalancing (GKE-managed", result["note"])

    def test_addon_outside_its_supported_range_is_a_risk(self):
        result = client_addon_skew.evaluate({}, member(), self.ITEMS[1:2], (1, 37, 0, 0), context_with([]))
        addon = result["risks"][0]
        self.assertEqual((addon["addon"], addon["version"], addon["supported"]), ("cert-manager", "v1.21", ("1.33", "1.36")))
        self.assertIn("cert-manager v1.21 supports Kubernetes 1.33 to 1.36", client_addon_skew.describe(addon))
        self.assertIn("the target 1.37 is outside it", addon["detail"])
        old = workload("Deployment", "cert-manager", "cert-manager", "quay.io/jetstack/cert-manager-controller:v1.12.0")
        result = client_addon_skew.evaluate({}, member(), [old], TARGET_1_35, context_with([]))
        self.assertIn("release not in the table read on 2026-10-09", result["unknown"][0]["reason"])

    def test_unread_objects_and_missing_target_are_unknown(self):
        unread = client_addon_skew.evaluate(self.CLUSTER, member(), None, TARGET_1_35, context_with(sample_entries()))
        self.assertEqual([u["reason"] for u in unread["unknown"]], [client_addon_skew.OBJECTS_NOT_READ])
        self.assertEqual(len(unread["risks"]), 1)
        no_target = client_addon_skew.evaluate(self.CLUSTER, member(), self.ITEMS, None, context_with(sample_entries()))
        self.assertEqual(no_target["risks"], [])
        self.assertIn(client_addon_skew.NO_TARGET_REASON, [u["reason"] for u in no_target["unknown"]])

    def test_failed_log_read_still_grades_images(self):
        result = client_addon_skew.evaluate({}, member(), self.ITEMS[:1], TARGET_1_35, context_with(rc=-1, stderr="timed out after 60 seconds"))
        self.assertEqual(len(result["risks"]), 1)
        self.assertIn("image registry.k8s.io/kubectl:v1.29.0", result["risks"][0]["where"])
        self.assertIn("timed out", result["unknown"][0]["reason"])

    def test_gke_addon_blocks_read_both_spellings(self):
        self.assertEqual(client_addon_skew.enabled_gke_addons({"addonsConfig": {"dnsCacheConfig": {}, "cloudRunConfig": {"disabled": True}, "gkeBackupAgentConfig": {"enabled": True}, "configConnectorConfig": {"enabled": False}}}), ["dnsCacheConfig", "gkeBackupAgentConfig"])
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
        self.assertEqual(crossing["risks"][0]["subject"], "Deployment legacy/git-sync")
        self.assertEqual(crossing["risks"][0]["setting"], "gitRepo volume src")
        self.assertEqual([c["minor"] for c in crossing["risks"][0]["changes"]], ["1.33"])
        self.assertIn("GitRepoVolumeDriver", crossing["risks"][0]["changes"][0]["change"])
        self.assertEqual(crossing["risks"][0]["changes"][0]["source"], tables.KUBERNETES_1_33_CHANGELOG_SOURCE)
        already_past = changed_defaults.evaluate({}, member("1.34.0-gke.1", ("1.34.0-gke.1",)), items, (1, 35, 0, 0), {})
        self.assertEqual(already_past["risks"], [])
        permanent = changed_defaults.evaluate({}, member("1.35.0-gke.1", ("1.35.0-gke.1",)), items, (1, 36, 0, 0), {})
        self.assertIn("permanently disabled", permanent["risks"][0]["changes"][0]["change"])

    def test_no_target_past_table_unread_objects_and_bad_master_are_unknown(self):
        items = [psa_namespace("seeded-defaults", "baseline", "latest")]
        self.assertEqual(changed_defaults.evaluate({}, member(), items, None, {})["unknown"][0]["reason"], changed_defaults.NO_TARGET_REASON)
        past = changed_defaults.evaluate({}, member(), items, (1, 37, 0, 0), {})
        self.assertIn("target 1.37 is past the defaults table (as of 1.36", past["unknown"][0]["reason"])
        self.assertEqual(past["risks"], [])
        self.assertEqual(changed_defaults.evaluate({}, member(), None, TARGET_1_35, {})["unknown"][0]["reason"], changed_defaults.OBJECTS_NOT_READ)
        bad = changed_defaults.evaluate({}, member("latest"), items, TARGET_1_35, {})
        self.assertIn("unparsable", bad["unknown"][0]["reason"])

    def test_crossed_minors(self):
        self.assertEqual(changed_defaults.crossed_minors((1, 33), (1, 35)), [(1, 34)])
        self.assertEqual(changed_defaults.crossed_minors((1, 32), (1, 33)), [(1, 33)])
        self.assertEqual(changed_defaults.crossed_minors((1, 35), (1, 35)), [])
        self.assertEqual(changed_defaults.crossed_minors(None, (1, 23)), [(1, 23)])


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
        self.assertEqual(tables.replacement_for("policy/v1beta1 podsecuritypolicies"), tables.replacement_for("policy/v1beta1 podsecuritypolicies"))
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

    def test_every_rule_runs_once_over_one_shared_read(self):
        context = context_with(sample_entries())
        items = ClientAddonSkewTest.ITEMS + [psa_namespace("seeded-defaults", "baseline", "latest")]
        results = readiness.evaluate_extra_rules(ClientAddonSkewTest.CLUSTER, member("1.33.5-gke.100"), items, TARGET_1_35, context)
        self.assertEqual(list(results), [m.RULE_ID for m in readiness.EXTRA_RULES])
        self.assertEqual(len(context["calls"]), 1)
        for result in results.values():
            self.assertEqual(set(result) >= set(finding.RESULT_KEYS), True)
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
            results = readiness.evaluate_extra_rules({}, member(), [], TARGET_1_35, context_with([]))
            self.assertEqual(results["broken"]["unknown"][0]["reason"], "rule broken raised RuntimeError('boom'); not evaluated")
            self.assertEqual(readiness.describe_rule_finding("broken", results["broken"]["unknown"][0]), "described: rule broken raised RuntimeError('boom'); not evaluated")
            self.assertEqual(results["removed-api-callers"]["blocking"], [])
        # A finding whose rule is no longer registered still renders its reason.
        self.assertEqual(readiness.describe_rule_finding("gone", {"reason": "why"}), "why")


if __name__ == "__main__":
    unittest.main()
