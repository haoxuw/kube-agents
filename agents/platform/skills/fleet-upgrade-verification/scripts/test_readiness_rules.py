#!/usr/bin/env python3
"""Unit tests for readiness_rules/: the contract every registered rule meets, the helpers
the rules share, the `finding` helpers, the shape tables' sources, and the rules registered
here, on canned cluster records and kubectl objects."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
import upgrade_readiness as r  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from fleet_upgrade_report import parse_version  # noqa: E402
from readiness_rules import finding, zonal_control_plane  # noqa: E402

TARGET = parse_version("1.35.1-gke.1000")
LAGGING = "1.34.11-gke.1000"
CURRENT = "1.35.1-gke.1000"
POOL_LABEL = "cloud.google.com/gke-nodepool"


def pool(name="default-pool", version="1.33.4-gke.1000", autoscaling=None, **config):
    return {"name": name, "version": version, "parsed": parse_version(version), "config": config, "autoscaling": autoscaling or {}}


def cluster(name="seeded-b", location="us-central1-a", pools=(), **extra):
    record = {"name": name, "location": location, "status": "RUNNING", "currentMasterVersion": LAGGING, "nodePools": list(pools), "locations": [location]}
    record.update(extra)
    return record


def read(items=None, error=None, read_errors=None):
    """A `read` dict as fleet_upgrade_report.read_cluster_objects returns it."""
    errors = {kind: None for kind in shared.READ_KINDS}
    errors.update(read_errors or {})
    return {"kubeconfig": "k", "items": items, "error": error, "read_errors": errors}


def node(name, pool_name, cpu="940m", memory="2Gi", labels=None, taints=None, ready=True, unschedulable=False, runtime="containerd://1.7.34"):
    record = {
        "kind": "Node",
        "metadata": {"name": name, "labels": {POOL_LABEL: pool_name, **(labels or {})}},
        "spec": {"taints": taints or []},
        "status": {
            "allocatable": {"cpu": cpu, "memory": memory},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
            "nodeInfo": {"containerRuntimeVersion": runtime},
        },
    }
    if unschedulable:
        record["spec"]["unschedulable"] = True
    return record


def pod(namespace, name, node_name, cpu="100m", memory="128Mi", owner=None, labels=None, volumes=None, phase="Running", annotations=None):
    meta = {"namespace": namespace, "name": name, "labels": labels or {}}
    if owner is not None:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1]}]
    if annotations:
        meta["annotations"] = annotations
    spec = {"nodeName": node_name, "containers": [{"name": "c", "resources": {"requests": {"cpu": cpu, "memory": memory}}}]}
    if volumes:
        spec["volumes"] = volumes
    return {"kind": "Pod", "metadata": meta, "spec": spec, "status": {"phase": phase}}


def deployment_pod(namespace, deployment, node_name, **kw):
    """A pod as a Deployment's ReplicaSet creates it: owner `<deployment>-<hash>`, hash label."""
    labels = {"app": deployment, "pod-template-hash": "7d9f"}
    return pod(namespace, f"{deployment}-7d9f-x1", node_name, owner=("ReplicaSet", f"{deployment}-7d9f"), labels=labels, **kw)


def template(kind, name, spec, namespace="seeded-shapes", workload_spec=None):
    pod_template = {"metadata": {"labels": {"app": name}}, "spec": spec}
    if kind == "CronJob":
        return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, "spec": {"suspend": True, "jobTemplate": {"spec": {"template": pod_template}}}}
    return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, "spec": {"replicas": 1, "template": pod_template, **(workload_spec or {})}}


def affinity(key, operator, values=None, required=True):
    expr = {"key": key, "operator": operator, **({"values": values} if values else {})}
    if required:
        return {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [expr]}]}}}}
    return {"affinity": {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [{"weight": 1, "preference": {"matchExpressions": [expr]}}]}}}


def claim_volume(claim):
    return {"name": "data", "persistentVolumeClaim": {"claimName": claim}}


def socket_volume(path="/run/containerd/containerd.sock"):
    return {"name": "sock", "hostPath": {"path": path, "type": "Socket"}}


class ContractTest(unittest.TestCase):
    """Every registered module meets readiness_rules' contract, so the fold can rely on it."""

    def test_every_registered_module_meets_the_contract(self):
        ids = [m.RULE_ID for m in r.EXTRA_RULES]
        self.assertTrue(ids)
        self.assertEqual(len(ids), len(set(ids)))
        for module in r.EXTRA_RULES:
            self.assertIsInstance(module.RULE_ID, str, module)
            self.assertIsInstance(module.ENTRY, int, module.RULE_ID)
            self.assertIsInstance(module.CAN_BLOCK, bool, module.RULE_ID)
            self.assertTrue(callable(module.evaluate) and callable(module.describe), module.RULE_ID)
            record = cluster(location="us-central1", pools=[pool("p", CURRENT)])
            result = module.evaluate(record, {}, read(items=[]), TARGET, {})
            self.assertEqual(set(result), set(shared.RESULT_KEYS), module.RULE_ID)
            self.assertTrue(all(isinstance(result[key], list) for key in shared.RESULT_KEYS), module.RULE_ID)
            # A module whose findings can never block says so, and the registry agrees.
            self.assertEqual(module.CAN_BLOCK, module.RULE_ID not in ("zonal-control-plane",))

    def test_entries_are_catalogue_numbers_and_read_kinds_are_named_once(self):
        self.assertEqual([m.ENTRY for m in r.EXTRA_RULES], sorted(m.ENTRY for m in r.EXTRA_RULES))
        self.assertEqual(shared.READ_KINDS, ("daemonset", "cronjob", "pods", "pvc", "networkpolicy", "namespace", "node", "storageclass", "pv"))
        self.assertEqual(len(shared.CONTEXT_KEYS), len(set(shared.CONTEXT_KEYS)))


class ReadHelpersTest(unittest.TestCase):
    def test_read_items_and_errors(self):
        items = [node("n1", "p")]
        answered = read(items=items)
        self.assertEqual(shared.read_items(answered), items)
        self.assertIsNone(shared.read_error(answered, "node"))
        self.assertEqual(shared.unread_kinds(answered, shared.READ_KINDS), {})
        refused = read(items=items, read_errors={"node": "nodes is forbidden", "storageclass": "sc is forbidden", "pv": "sc is forbidden"})
        self.assertEqual(shared.read_error(refused, "node"), "nodes is forbidden")
        self.assertIsNone(shared.read_error(refused, "pods"))
        self.assertEqual(shared.unread_kinds(refused, ("pods", "node", "pv")), {"node": "nodes is forbidden", "pv": "sc is forbidden"})
        # Nothing read: every kind, including the PDB read's, answers with that failure.
        nothing = read(error="get-credentials failed (1): forbidden")
        self.assertEqual(shared.read_items(nothing), [])
        self.assertEqual(shared.read_error(nothing, "deploy"), "get-credentials failed (1): forbidden")
        self.assertEqual(shared.read_error(nothing, "pods"), "get-credentials failed (1): forbidden")
        self.assertEqual(shared.read_error({"items": None}, "pods"), shared.NOTHING_READ_REASON)
        self.assertEqual(shared.read_error(None, "pods"), shared.NOTHING_READ_REASON)
        self.assertEqual(shared.items_of_kind(items, "Node"), items)
        self.assertEqual(shared.items_of_kind(None, "Node"), [])


class SharedHelpersTest(unittest.TestCase):
    def test_quantities(self):
        self.assertEqual(shared.cpu_millis("250m"), 250)
        self.assertEqual(shared.cpu_millis("2"), 2000)
        self.assertEqual(shared.cpu_millis("0.5"), 500)
        self.assertEqual(shared.cpu_millis(1), 1000)
        self.assertEqual(shared.memory_bytes("256Mi"), 256 * 1024**2)
        self.assertEqual(shared.memory_bytes("1Gi"), 1024**3)
        self.assertEqual(shared.memory_bytes("1e3"), 1000)
        self.assertEqual(shared.memory_bytes("2868948Ki"), 2868948 * 1024)
        self.assertIsNone(shared.cpu_millis("lots"))
        self.assertIsNone(shared.cpu_millis(True))
        self.assertEqual(shared.format_cpu(250), "250m")
        self.assertEqual(shared.format_memory(256 * 1024**2), "256Mi")

    def test_pod_requests_take_the_larger_of_containers_and_init(self):
        spec = {
            "containers": [{"resources": {"requests": {"cpu": "100m", "memory": "64Mi"}}}, {"resources": {"requests": {"cpu": "100m"}}}],
            "initContainers": [{"resources": {"requests": {"cpu": "500m", "memory": "32Mi"}}}],
        }
        self.assertEqual(shared.pod_requests(spec), (500, 64 * 1024**2))
        self.assertEqual(shared.pod_requests({"containers": [{"resources": {"requests": {"cpu": "bad"}}}]}), (None, 0))
        self.assertEqual(shared.pod_requests({"containers": [{}]}), (0, 0))

    def test_pod_owner_names_the_deployment_behind_a_replicaset(self):
        self.assertEqual(shared.pod_owner(deployment_pod("ns", "web", "n1")), {"kind": "Deployment", "namespace": "ns", "name": "web"})
        bare = pod("ns", "job-x", "n1", owner=("Job", "job"))
        self.assertEqual(shared.owner_label(shared.pod_owner(bare)), "Job ns/job")
        self.assertEqual(shared.owner_label(shared.pod_owner(pod("ns", "solo", "n1"))), "Pod ns/solo")
        orphan_rs = pod("ns", "p", "n1", owner=("ReplicaSet", "web-abc"))  # no hash label: stays a ReplicaSet
        self.assertEqual(shared.pod_owner(orphan_rs)["kind"], "ReplicaSet")

    def test_node_helpers(self):
        self.assertTrue(shared.node_schedulable(node("n", "p")))
        self.assertFalse(shared.node_schedulable(node("n", "p", ready=False)))
        self.assertFalse(shared.node_schedulable(node("n", "p", unschedulable=True)))
        self.assertEqual(shared.node_pool(node("n", "idle-pool")), "idle-pool")
        self.assertEqual(shared.node_allocatable(node("n", "p", cpu="940m", memory="1Gi")), (940, 1024**3))

    def test_active_pods_and_volumes(self):
        items = [pod("a", "done", "n", phase="Succeeded"), pod("a", "live", "n", volumes=[socket_volume(), claim_volume("data")])]
        active = shared.active_pods(items)
        self.assertEqual([p["metadata"]["name"] for p in active], ["live"])
        self.assertEqual(shared.hostpath_volumes(active[0]["spec"]), ["/run/containerd/containerd.sock"])
        self.assertEqual(shared.claim_names(active[0]["spec"]), ["data"])
        self.assertTrue(shared.is_mirror_pod(pod("kube-system", "etcd", "n", annotations={"kubernetes.io/config.mirror": "x"})))
        self.assertTrue(shared.is_daemonset_pod(pod("a", "ds-x", "n", owner=("DaemonSet", "ds"))))

    def test_templates_walk_every_kind_once_and_skip_system_namespaces_unless_asked(self):
        items = [
            template("Deployment", "web", {"containers": [{"name": "c", "image": "img"}]}),
            template("CronJob", "job", {"containers": [{"name": "c", "image": "img"}]}),
            template("DaemonSet", "agent", {"containers": [{"name": "c", "image": "img"}]}, namespace="kube-system"),
            {"kind": "Pod", "metadata": {"namespace": "x", "name": "p"}, "spec": {}},
            {"kind": "Deployment", "metadata": {"namespace": "x", "name": "no-template"}, "spec": {}},
        ]
        self.assertEqual([(obj["kind"], obj["object"]) for obj, _, _ in shared.templates(items)], [("Deployment", "seeded-shapes/web"), ("CronJob", "seeded-shapes/job")])
        self.assertEqual([obj["kind"] for obj, _ in shared.template_specs(items)], ["Deployment", "CronJob", "DaemonSet"])
        self.assertEqual([obj["name"] for obj, _, _ in shared.templates(items, ("CronJob",))], ["job"])
        self.assertEqual(shared.pod_template_spec(items[1])["containers"][0]["image"], "img")
        self.assertIsNone(shared.pod_template_spec(items[3]))
        self.assertIsNone(shared.pod_template_spec("not a dict"))
        self.assertEqual(shared.containers({"containers": [{"name": "a"}], "initContainers": [{"name": "b"}, "junk"]}), [{"name": "a"}, {"name": "b"}])

    def test_selectors_keep_operators_and_skip_preferred_terms(self):
        spec = {"nodeSelector": {"a": "1"}, **affinity("b", "NotIn", ["x", "y"])}
        self.assertEqual(shared.selectors(spec), [{"key": "a", "operator": "In", "values": ["1"], "where": "nodeSelector"}, {"key": "b", "operator": "NotIn", "values": ["x", "y"], "where": "nodeAffinity"}])
        self.assertEqual(shared.selectors(affinity("c", "In", ["z"], required=False)), [])
        self.assertEqual(shared.selected_values({**affinity("b", "NotIn", ["x"]), "nodeSelector": {"b": "w"}}, "b"), ["w"])

    def test_pools_for_template_follows_the_nodepool_selector(self):
        pools = [pool("a"), pool("b")]
        self.assertEqual([p["name"] for p in shared.pools_for_template({"nodeSelector": {tables.NODEPOOL_LABEL: "b"}}, pools)], ["b"])
        self.assertEqual([p["name"] for p in shared.pools_for_template(affinity(tables.NODEPOOL_LABEL, "In", ["a"]), pools)], ["a"])
        self.assertEqual(len(shared.pools_for_template({}, pools)), 2)

    def test_pool_helpers(self):
        self.assertTrue(shared.pool_scales_from_zero(pool(autoscaling={"enabled": True, "minNodeCount": 0, "maxNodeCount": 3})))
        self.assertFalse(shared.pool_scales_from_zero(pool(autoscaling={"enabled": True, "minNodeCount": 1})))
        self.assertFalse(shared.pool_scales_from_zero(pool(autoscaling={"enabled": True, "totalMinNodeCount": 2})))
        self.assertFalse(shared.pool_scales_from_zero(pool()))
        self.assertEqual(shared.pool_config({"config": "junk"}), {})
        self.assertEqual(shared.minor_text(TARGET), "1.35")
        self.assertIsNone(shared.minor_text(None))
        self.assertTrue(shared.pool_below_target(pool(), TARGET))
        self.assertFalse(shared.pool_below_target(pool(version=CURRENT), TARGET))
        self.assertFalse(shared.pool_below_target(pool(version="junk"), TARGET))
        self.assertEqual(shared.pools_below_target([pool("a"), pool("b", CURRENT)], TARGET), ["a"])

    def test_cgroup_transition(self):
        v1 = pool(effectiveCgroupMode=tables.CGROUP_V1_MODE)
        self.assertEqual(shared.cgroup_transition(v1, TARGET), ("moves", "GKE migrates cgroup v1 pools to v2 from 1.33"))
        self.assertEqual(shared.cgroup_transition(v1, parse_version("1.32.0-gke.1"))[0], "stays-v1")
        self.assertEqual(shared.cgroup_transition(v1, None), ("unknown", shared.CGROUP_REASON_NO_TARGET))
        pinned = pool(effectiveCgroupMode=tables.CGROUP_V1_MODE, linuxNodeConfig={"cgroupMode": tables.CGROUP_MODE_V1_PIN})
        self.assertEqual(shared.cgroup_transition(pinned, parse_version("1.34.0-gke.1"))[0], "stays-v1")
        self.assertEqual(shared.cgroup_transition(pinned, TARGET), ("moves", "the pool is pinned to cgroup v1 and GKE removes cgroup v1 at 1.35"))
        self.assertEqual(shared.cgroup_transition(pool(effectiveCgroupMode=tables.CGROUP_V2_MODE), TARGET), ("already-v2", shared.CGROUP_REASON_ALREADY))
        self.assertEqual(shared.cgroup_transition(pool(), TARGET), ("unknown", shared.CGROUP_REASON_UNREAD))

    def test_image_repository_and_tag(self):
        self.assertEqual(shared.image_repository_and_tag("docker.io/library/eclipse-temurin:8u302-b08-jre"), ("eclipse-temurin", "8u302-b08-jre"))
        self.assertEqual(shared.image_repository_and_tag("localhost:5000/openjdk"), ("openjdk", ""))
        self.assertEqual(shared.image_repository_and_tag("openjdk:11@sha256:abc"), ("openjdk", "11"))

    def test_system_namespaces_are_the_collectors_set_plus_the_two_prefixes(self):
        for namespace in ("kube-system", "kube-public", "gmp-system", "gke-managed-cim", "gke-gmp-system", "cnrm-system", "istio-system", "gatekeeper-system", "config-management-system"):
            self.assertTrue(shared.is_system_namespace(namespace), namespace)
        for namespace in ("seeded-shapes", "default", "istio-ingress", "gkeapp"):
            self.assertFalse(shared.is_system_namespace(namespace), namespace)

    def test_object_findings_and_their_description(self):
        obj = {"kind": "Deployment", "namespace": "shop", "name": "web", "object": "shop/web"}
        entry = shared.new_finding("x", 4, shared.GRADE_RISK, obj, "keeps data on the node", volumes=["data"])
        self.assertEqual(entry, {"rule": "x", "entry": 4, "grade": "risk", "kind": "Deployment", "namespace": "shop", "name": "web", "object": "shop/web", "detail": "keeps data on the node", "volumes": ["data"]})
        self.assertEqual(shared.describe(entry), "Deployment shop/web: keeps data on the node")
        self.assertEqual(shared.describe(shared.rule_unknown("x", None, "why")), "x: why")
        self.assertEqual(shared.new_result(), {"blocking": [], "risks": [], "unknown": [], "notes": []})


class FindingHelpersTest(unittest.TestCase):
    def test_tiers_notes_and_labels(self):
        result = finding.empty_result()
        self.assertEqual(result, shared.new_result())
        finding.add_note(result, "")
        finding.add_note(result, "first")
        finding.add_note(result, "second")
        self.assertEqual(result["notes"], ["first", "second"])
        self.assertEqual(finding.unknown("x", "no runner"), {"rule": "x", "tier": "unknown", "reason": "no runner"})
        cron = template("CronJob", "job", {"containers": [{"name": "c", "image": "img"}]}, namespace="ns")
        self.assertEqual(finding.pod_spec(cron)["containers"][0]["image"], "img")
        self.assertIsNone(finding.pod_spec({"kind": "Pod", "spec": {}}))
        self.assertEqual(finding.workload_label(cron), "CronJob ns/job")
        self.assertEqual((finding.TIER_BLOCKING, finding.TIER_RISK, finding.TIER_UNKNOWN), ("blocking", "risk", "unknown"))
        self.assertEqual((finding.RESULT_BLOCKING, finding.RESULT_RISKS, finding.RESULT_UNKNOWN, finding.RESULT_NOTES), shared.RESULT_KEYS)


class ZonalControlPlaneRuleTest(unittest.TestCase):
    def test_zone_is_a_risk_region_is_clean_garbage_is_unknown(self):
        zonal = zonal_control_plane.evaluate(cluster(location="us-central1-a"), {}, None, None, {})
        self.assertEqual(zonal["blocking"], [])
        self.assertEqual(zonal["risks"][0], {"location": "us-central1-a", "node_locations": ["us-central1-a"]})
        text = zonal_control_plane.describe(zonal["risks"][0])
        self.assertIn("control plane is zonal (us-central1-a)", text)
        self.assertIn("retry with backoff", text)
        self.assertIn("running pods are unaffected", text)
        self.assertEqual(zonal_control_plane.evaluate(cluster(location="us-central1"), {}, None, None, {}), shared.new_result())
        self.assertEqual(zonal_control_plane.evaluate(cluster(location="northamerica-northeast1-b"), {}, None, None, {})["risks"][0]["location"], "northamerica-northeast1-b")
        odd = zonal_control_plane.evaluate(cluster(location="everywhere"), {}, None, None, {})
        self.assertIn("neither a zone nor a region", odd["unknown"][0])
        self.assertFalse(zonal_control_plane.CAN_BLOCK)
        self.assertEqual((zonal_control_plane.RULE_ID, zonal_control_plane.ENTRY), ("zonal-control-plane", 11))

    def test_multi_zonal_reads_as_zonal_and_the_zone_key_is_a_fallback(self):
        record = cluster(location="us-central1-a", locations=["us-central1-a", "us-central1-b"])
        self.assertEqual(zonal_control_plane.evaluate(record, {}, None, None, {})["risks"][0]["node_locations"], ["us-central1-a", "us-central1-b"])
        record = cluster(location=None)
        del record["location"]
        record["zone"] = "europe-west4-b"
        self.assertEqual(zonal_control_plane.evaluate(record, {}, None, None, {})["risks"][0]["location"], "europe-west4-b")

    def test_the_read_does_not_matter(self):
        record = cluster(location="us-central1-a")
        self.assertEqual(zonal_control_plane.evaluate(record, {}, read(error="forbidden"), TARGET, {}), zonal_control_plane.evaluate(record, {}, read(items=[]), TARGET, {}))


class ShapeTablesTest(unittest.TestCase):
    def test_containerd_major_by_minor_and_image_family(self):
        self.assertEqual(tables.containerd_major_at((1, 32), tables.NODE_OS_LINUX), 1)
        self.assertEqual(tables.containerd_major_at((1, 33), tables.NODE_OS_LINUX), 2)
        self.assertEqual(tables.containerd_major_at((1, 34), tables.NODE_OS_WINDOWS), 1)
        self.assertEqual(tables.containerd_major_at((1, 35), tables.NODE_OS_WINDOWS), 2)
        self.assertEqual(tables.node_image_os_family("ubuntu_containerd"), tables.NODE_OS_LINUX)
        self.assertEqual(tables.node_image_os_family("WINDOWS_SAC_CONTAINERD"), tables.NODE_OS_WINDOWS)
        self.assertEqual(tables.node_image_os_family(""), tables.NODE_OS_LINUX)

    def test_cgroup_floors_are_the_sourced_values(self):
        self.assertEqual(tables.DOTNET_CGROUP_V2_VERSION, (5, 0))
        self.assertEqual((tables.JDK8_CGROUP_V2_UPDATE, tables.JDK11_CGROUP_V2_PATCH, tables.JDK_FIRST_MAJOR_WITH_CGROUP_V2), (372, (0, 16), 15))
        self.assertEqual((tables.CGROUP_V2_MIGRATION_MINOR, tables.CGROUP_V1_REMOVAL_MINOR, tables.GROUP_OOM_KILL_KUBELET_MINOR), (33, 35, 28))

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

    def test_driver_tables_and_dropped_labels_carry_their_shape(self):
        for image_type, by_minor in tables.GKE_GPU_DRIVERS_BY_IMAGE_TYPE.items():
            self.assertIn(image_type, tables.NODE_IMAGE_OS_FAMILY)
            for minor, (default, latest) in by_minor.items():
                self.assertLessEqual(default, latest, (image_type, minor))
        for major, floor in tables.CUDA_MAJOR_MIN_DRIVER.items():
            self.assertEqual(floor, min(d for (m, _), d in tables.CUDA_TOOLKIT_MIN_DRIVER.items() if m == major), major)
        for label, value, minor, source in tables.DROPPED_NODE_LABELS:
            self.assertTrue(label and isinstance(minor, tuple) and source, label)
        self.assertEqual(set(tables.RETIRED_IMAGE_HOSTS), set(tables.RETIRED_IMAGE_HOST_NOTES))

    def test_replacement_comes_from_the_scan_table(self):
        self.assertEqual(tables.replacement_for("flowcontrol.apiserver.k8s.io/v1beta3 flowschemas"), "flowcontrol.apiserver.k8s.io/v1")
        self.assertEqual(tables.replacement_for("policy/v1beta1 podsecuritypolicies"), "none; Pod Security Admission or a policy controller")
        self.assertIsNone(tables.replacement_for("core/v1 endpoints"))
        self.assertIsNone(tables.replacement_for("nonsense"))

    def test_parse_and_format_minor(self):
        self.assertEqual(tables.parse_minor("1.35.1-gke.1000"), (1, 35))
        self.assertEqual(tables.parse_minor("v1.29.0"), (1, 29))
        self.assertEqual(tables.parse_minor("1.32"), (1, 32))
        self.assertIsNone(tables.parse_minor("latest"))
        self.assertIsNone(tables.parse_minor(None))
        self.assertEqual(tables.format_minor((1, 35)), "1.35")
        self.assertEqual(tables.format_minor(TARGET), "1.35")

    def test_every_table_section_names_a_source(self):
        # Each table block cites its source in the comment beside it: a page, an issue, or
        # the file in this repository it is read from. A section with none is a table a
        # reader cannot check.
        with open(tables.__file__, encoding="utf-8") as f:
            text = f.read()
        sections = text.split("# ----")[1:]
        self.assertGreater(len(sections), 10)
        for section in sections:
            title = section.splitlines()[0].strip("- ")
            self.assertTrue(any(marker in section for marker in ("Source:", "https://", ".com/", ".io/", "kubernetes/kubernetes#", "dotnet/runtime#", ".json")), title)


if __name__ == "__main__":
    unittest.main()
