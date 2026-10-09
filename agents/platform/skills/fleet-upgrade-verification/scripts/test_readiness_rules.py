#!/usr/bin/env python3
"""Unit tests for readiness_rules/: the five per-entry rules on canned cluster records and
kubectl objects, and the folding upgrade_readiness.EXTRA_RULES gets."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
import upgrade_readiness as r  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from fleet_upgrade_report import parse_version  # noqa: E402
from readiness_rules import capacity, container_runtime, dataplane, intree_volumes, zonal_control_plane  # noqa: E402

TARGET = parse_version("1.35.1-gke.1000")
LAGGING = "1.34.11-gke.1000"
CURRENT = "1.35.1-gke.1000"
POOL_LABEL = "cloud.google.com/gke-nodepool"


def pool(name, version, upgrade=None, autoscaling=None, image="COS_CONTAINERD", locations=None):
    record = {"name": name, "version": version, "status": "RUNNING", "config": {"imageType": image}}
    if upgrade is not None:
        record["upgradeSettings"] = upgrade
    if autoscaling is not None:
        record["autoscaling"] = autoscaling
    if locations is not None:
        record["locations"] = locations
    return record


def cluster(name="seeded-b", location="us-central1-a", pools=(), **extra):
    record = {"name": name, "location": location, "status": "RUNNING", "currentMasterVersion": LAGGING, "nodePools": list(pools), "locations": [location]}
    record.update(extra)
    return record


def context(record, clusters=None):
    pools = [{"name": p["name"], "version": p["version"], "parsed": parse_version(p["version"])} for p in record.get("nodePools") or []]
    return {"clusters": clusters if clusters is not None else [record], "pools": pools, "project": "p1", "at": None}


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


def pod(namespace, name, node_name, cpu="100m", memory="128Mi", owner=None, labels=None, node_selector=None, tolerations=None, volumes=None, phase="Running", annotations=None, affinity=None):
    meta = {"namespace": namespace, "name": name, "labels": labels or {}}
    if owner is not None:
        meta["ownerReferences"] = [{"kind": owner[0], "name": owner[1]}]
    if annotations:
        meta["annotations"] = annotations
    spec = {"nodeName": node_name, "containers": [{"name": "c", "resources": {"requests": {"cpu": cpu, "memory": memory}}}]}
    if node_selector:
        spec["nodeSelector"] = node_selector
    if tolerations:
        spec["tolerations"] = tolerations
    if volumes:
        spec["volumes"] = volumes
    if affinity:
        spec["affinity"] = affinity
    return {"kind": "Pod", "metadata": meta, "spec": spec, "status": {"phase": phase}}


def deployment_pod(namespace, deployment, node_name, **kw):
    """A pod as a Deployment's ReplicaSet creates it: owner `<deployment>-<hash>`, hash label."""
    labels = {"app": deployment, "pod-template-hash": "7d9f"}
    return pod(namespace, f"{deployment}-7d9f-x1", node_name, owner=("ReplicaSet", f"{deployment}-7d9f"), labels=labels, **kw)


def template(kind, namespace, name, volumes=None, node_selector=None):
    spec = {"containers": [{"name": "c"}]}
    if volumes:
        spec["volumes"] = volumes
    if node_selector:
        spec["nodeSelector"] = node_selector
    return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, "spec": {"template": {"spec": spec}}}


def pv(name, pd_name="disk-1", claim=("seeded-shapes", "intree-pd"), phase="Bound", annotations=None, csi=False):
    spec = {"capacity": {"storage": "10Gi"}}
    if csi:
        spec["csi"] = {"driver": "pd.csi.storage.gke.io", "volumeHandle": pd_name}
    else:
        spec["gcePersistentDisk"] = {"pdName": pd_name, "fsType": "ext4"}
    if claim:
        spec["claimRef"] = {"namespace": claim[0], "name": claim[1]}
    meta = {"name": name}
    if annotations:
        meta["annotations"] = annotations
    return {"kind": "PersistentVolume", "metadata": meta, "spec": spec, "status": {"phase": phase}}


def netpol(namespace, name):
    return {"kind": "NetworkPolicy", "metadata": {"namespace": namespace, "name": name}, "spec": {"podSelector": {}}}


def claim_volume(claim):
    return {"name": "data", "persistentVolumeClaim": {"claimName": claim}}


def socket_volume(path="/run/containerd/containerd.sock"):
    return {"name": "sock", "hostPath": {"path": path, "type": "Socket"}}


NO_SURGE = {"maxUnavailable": 1, "strategy": "SURGE"}  # the API omits a zero maxSurge
PIN = {"seeded-role": "no-surge"}
TAINT = {"key": "seeded-role", "value": "no-surge", "effect": "NoSchedule"}
TOLERATION = {"key": "seeded-role", "operator": "Equal", "value": "no-surge", "effect": "NoSchedule"}


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


class CapacityRuleTest(unittest.TestCase):
    """Entry 2. The seeded shape: no-surge-pool (maxSurge 0, maxUnavailable 1, one node,
    not autoscaled) with pinned-batch-runner pinned to it by selector and taint, beside a
    default pool with room."""

    def _seeded(self, upgrade=NO_SURGE, autoscaling=None, pinned=True, default_room=True):
        record = cluster(pools=[pool("default-pool", LAGGING), pool("no-surge-pool", LAGGING, upgrade=upgrade, autoscaling=autoscaling)])
        runner_kw = {"cpu": "250m", "memory": "256Mi"}
        if pinned:
            runner_kw.update(node_selector=PIN, tolerations=[TOLERATION])
        items = [
            node("gke-b-default-1", "default-pool", cpu="940m", memory="2Gi"),
            node("gke-b-nosurge-1", "no-surge-pool", cpu="940m", memory="2Gi", labels=PIN, taints=[TAINT]),
            deployment_pod("seeded-upgrade", "pinned-batch-runner", "gke-b-nosurge-1", **runner_kw),
            deployment_pod("kube-system", "metrics-server", "gke-b-default-1", cpu="800m" if not default_room else "100m", memory="1900Mi" if not default_room else "100Mi"),
            pod("kube-system", "fluentbit-x", "gke-b-nosurge-1", cpu="100m", memory="100Mi", owner=("DaemonSet", "fluentbit")),
        ]
        return record, items

    def test_pinned_pod_with_no_room_on_the_pool_blocks(self):
        record, items = self._seeded()
        out = capacity.evaluate(record, {}, items, TARGET, context(record))
        self.assertEqual(out["risks"], [])
        self.assertEqual(out["unknown"], [])
        self.assertEqual(len(out["blocking"]), 1)
        finding = out["blocking"][0]
        self.assertEqual(finding["pool"], "no-surge-pool")
        self.assertEqual((finding["max_surge"], finding["max_unavailable"], finding["autoscaled"], finding["at_ceiling"]), (0, 1, False, True))
        self.assertEqual(finding["node"], "gke-b-nosurge-1")
        # The DaemonSet pod is not displaced; the runner is, and it is pinned.
        self.assertEqual(finding["displaced"], {"cpu_m": 250, "memory_bytes": 256 * 1024**2})
        self.assertEqual(finding["pinned"], {"cpu_m": 250, "memory_bytes": 256 * 1024**2})
        self.assertEqual(finding["room_pool"], {"cpu_m": 0, "memory_bytes": 0})
        self.assertEqual(finding["workloads"], [{"owner": "Deployment seeded-upgrade/pinned-batch-runner", "cpu_m": 250, "memory_bytes": 256 * 1024**2, "pinned": True}])
        text = capacity.describe(finding)
        self.assertIn("pool no-surge-pool (maxSurge 0, maxUnavailable 1; 1 node(s), not autoscaled) removes a node before its replacement exists", text)
        self.assertIn("250m CPU / 256Mi", text)
        self.assertIn("Deployment seeded-upgrade/pinned-batch-runner (250m / 256Mi, pinned to the pool)", text)

    def test_free_pod_with_room_elsewhere_is_a_note_not_a_finding(self):
        record, items = self._seeded(pinned=False)
        out = capacity.evaluate(record, {}, items, TARGET, context(record))
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(len(out["notes"]), 1)
        self.assertIn("the room elsewhere covers its largest node's pods", out["notes"][0])

    def test_free_pod_with_no_room_anywhere_blocks(self):
        record, items = self._seeded(pinned=False, default_room=False)
        out = capacity.evaluate(record, {}, items, TARGET, context(record))
        self.assertEqual(len(out["blocking"]), 1)
        self.assertEqual(out["blocking"][0]["pinned"], {"cpu_m": 0, "memory_bytes": 0})
        self.assertNotIn("can run only on this pool", capacity.describe(out["blocking"][0]))

    def test_surge_left_or_a_growable_autoscaler_is_a_risk(self):
        record, items = self._seeded(upgrade={"maxSurge": 1, "maxUnavailable": 1, "strategy": "SURGE"})
        out = capacity.evaluate(record, {}, items, TARGET, context(record))
        self.assertEqual(out["blocking"], [])
        self.assertEqual(out["risks"][0]["max_surge"], 1)

        record, items = self._seeded(autoscaling={"enabled": True, "minNodeCount": 1, "maxNodeCount": 3})
        out = capacity.evaluate(record, {}, items, TARGET, context(record))
        self.assertEqual(out["blocking"], [])
        self.assertEqual((out["risks"][0]["autoscaled"], out["risks"][0]["ceiling"], out["risks"][0]["at_ceiling"]), (True, 3, False))
        self.assertIn("autoscaler can grow to 3", capacity.describe(out["risks"][0]))

    def test_autoscaler_at_its_ceiling_blocks(self):
        record, items = self._seeded(autoscaling={"enabled": True, "minNodeCount": 1, "maxNodeCount": 1})
        out = capacity.evaluate(record, {}, items, TARGET, context(record))
        self.assertEqual(len(out["blocking"]), 1)
        self.assertIn("autoscaler at its ceiling of 1", out["blocking"][0]["summary"])

    def test_autoscaled_blue_green_is_a_risk_never_a_block(self):
        upgrade = {"strategy": "BLUE_GREEN", "blueGreenSettings": {"autoscaledRolloutPolicy": {}}}
        record, items = self._seeded(upgrade=upgrade)
        out = capacity.evaluate(record, {}, items, TARGET, context(record))
        self.assertEqual(out["blocking"], [])
        self.assertEqual(out["risks"][0]["strategy"], "BLUE_GREEN")
        self.assertIn("autoscaled blue-green, whose green pool starts empty", capacity.describe(out["risks"][0]))
        standard = {"strategy": "BLUE_GREEN", "blueGreenSettings": {"standardRolloutPolicy": {"batchNodeCount": 1}}}
        record, items = self._seeded(upgrade=standard)
        self.assertEqual(capacity.evaluate(record, {}, items, TARGET, context(record)), shared.new_result())

    def test_default_settings_and_pools_at_the_target_are_clean(self):
        record, items = self._seeded(upgrade=None)
        self.assertEqual(capacity.evaluate(record, {}, items, TARGET, context(record)), shared.new_result())
        record, items = self._seeded()
        record["nodePools"][1]["version"] = CURRENT
        self.assertEqual(capacity.evaluate(record, {}, items, TARGET, context(record)), shared.new_result())

    def test_unknown_paths(self):
        record, items = self._seeded()
        no_target = capacity.evaluate(record, {}, items, None, context(record))
        self.assertEqual(len(no_target["unknown"]), 1)
        self.assertIn("no target", no_target["unknown"][0])
        unread = capacity.evaluate(record, {}, None, TARGET, context(record))
        self.assertIn("cluster read failed, so its headroom was not measured", unread["unknown"][0])
        self.assertIn("? node(s)", unread["unknown"][0])
        record["nodePools"][1]["version"] = "latest"
        self.assertIn("version unparsable", capacity.evaluate(record, {}, items, TARGET, context(record))["unknown"][0])
        record, items = self._seeded()
        # The drained node's own allocatable is never read (only its pods' requests are);
        # a peer node's unparsable allocatable is what stops the measurement.
        items[0]["status"]["allocatable"]["cpu"] = "many"
        bad = capacity.evaluate(record, {}, items, TARGET, context(record))
        self.assertIn("the allocatable of node gke-b-default-1 did not parse", bad["unknown"][0])

    def test_empty_pool_is_a_note(self):
        record, items = self._seeded()
        items = [i for i in items if shared.node_pool(i) != "no-surge-pool"] if False else [i for i in items if not (i["kind"] == "Node" and shared.node_pool(i) == "no-surge-pool")]
        out = capacity.evaluate(record, {}, items, TARGET, context(record))
        self.assertIn("has no node", out["notes"][0])

    def test_autoscaler_ceiling_total_beats_per_zone(self):
        record = cluster(locations=["us-central1-a", "us-central1-b"])
        self.assertEqual(capacity.autoscaler_ceiling(pool("p", LAGGING, autoscaling={"enabled": True, "maxNodeCount": 2}), record), (True, 4))
        self.assertEqual(capacity.autoscaler_ceiling(pool("p", LAGGING, autoscaling={"enabled": True, "maxNodeCount": 2, "totalMaxNodeCount": 3}), record), (True, 3))
        self.assertEqual(capacity.autoscaler_ceiling(pool("p", LAGGING, autoscaling={"enabled": True, "maxNodeCount": 2}, locations=["us-central1-a"]), record), (True, 2))
        self.assertEqual(capacity.autoscaler_ceiling(pool("p", LAGGING), record), (False, None))

    def test_node_fits_pod_reads_selector_affinity_and_taints(self):
        plain = node("n", "default-pool")
        tainted = node("t", "no-surge-pool", labels=PIN, taints=[TAINT])
        pinned = pod("ns", "p", "t", node_selector=PIN, tolerations=[TOLERATION])["spec"]
        self.assertFalse(capacity.node_fits_pod(plain, pinned))
        self.assertTrue(capacity.node_fits_pod(tainted, pinned))
        intolerant = pod("ns", "p", "t", node_selector=PIN)["spec"]
        self.assertFalse(capacity.node_fits_pod(tainted, intolerant))
        affinity = {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "seeded-role", "operator": "In", "values": ["no-surge"]}]}]}}}
        affine = pod("ns", "p", "t", tolerations=[TOLERATION], affinity=affinity)["spec"]
        self.assertFalse(capacity.node_fits_pod(plain, affine))
        self.assertTrue(capacity.node_fits_pod(tainted, affine))
        exists_any = {"tolerations": [{"operator": "Exists"}], "containers": []}
        self.assertTrue(capacity.node_fits_pod(tainted, exists_any))
        unknown_op = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "x", "operator": "Gt", "values": ["1"]}]}]}}}}
        self.assertFalse(capacity.node_fits_pod(plain, unknown_op))


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

    def test_multi_zonal_reads_as_zonal_and_the_zone_key_is_a_fallback(self):
        record = cluster(location="us-central1-a", locations=["us-central1-a", "us-central1-b"])
        self.assertEqual(zonal_control_plane.evaluate(record, {}, None, None, {})["risks"][0]["node_locations"], ["us-central1-a", "us-central1-b"])
        record = cluster(location=None)
        del record["location"]
        record["zone"] = "europe-west4-b"
        self.assertEqual(zonal_control_plane.evaluate(record, {}, None, None, {})["risks"][0]["location"], "europe-west4-b")


class ContainerRuntimeRuleTest(unittest.TestCase):
    """Entry 13. The designed shape: node-runtime-probe, a DaemonSet mounting the containerd
    socket on seeded-a's default pool."""

    def _items(self, runtime="containerd://1.7.34", with_client=True, with_nodes=True):
        items = []
        if with_nodes:
            items.append(node("gke-a-default-1", "default-pool", runtime=runtime))
        if with_client:
            items.append(template("DaemonSet", "seeded-shapes", "node-runtime-probe", volumes=[socket_volume()]))
            items.append(pod("seeded-shapes", "node-runtime-probe-x", "gke-a-default-1", owner=("DaemonSet", "node-runtime-probe"), volumes=[socket_volume()]))
        return items

    def test_major_change_under_a_socket_client_is_a_risk(self):
        record = cluster(name="seeded-a", pools=[pool("default-pool", "1.32.4-gke.1000")])
        out = container_runtime.evaluate(record, {}, self._items(), parse_version("1.33.1-gke.1000"), context(record))
        self.assertEqual(out["blocking"], [])
        finding = out["risks"][0]
        self.assertEqual((finding["pool"], finding["image_type"], finding["current_major"], finding["target_major"]), ("default-pool", "COS_CONTAINERD", 1, 2))
        self.assertEqual(finding["clients"], [{"owner": "DaemonSet seeded-shapes/node-runtime-probe", "paths": ["/run/containerd/containerd.sock"]}])
        text = container_runtime.describe(finding)
        self.assertIn("runs containerd 1 on its nodes (containerd://1.7.34) and the target's node image ships containerd 2", text)
        self.assertIn("DaemonSet seeded-shapes/node-runtime-probe (/run/containerd/containerd.sock)", text)

    def test_nodes_already_on_containerd_2_are_no_change(self):
        record = cluster(name="seeded-a", pools=[pool("default-pool", "1.32.4-gke.1000")])
        out = container_runtime.evaluate(record, {}, self._items(runtime="containerd://2.0.10"), parse_version("1.33.1-gke.1000"), context(record))
        self.assertEqual(out["risks"], [])
        self.assertIn("no pool's containerd major changes at the target", out["notes"][0])
        self.assertIn("DaemonSet seeded-shapes/node-runtime-probe", out["notes"][0])

    def test_nodes_unread_fall_back_to_the_table_by_pool_minor(self):
        record = cluster(name="seeded-a", pools=[pool("default-pool", "1.32.4-gke.1000")])
        out = container_runtime.evaluate(record, {}, self._items(with_nodes=False), parse_version("1.33.1-gke.1000"), context(record))
        self.assertIn("by its version 1.32, nodes unread", out["risks"][0]["current"])
        record = cluster(name="seeded-a", pools=[pool("default-pool", "1.33.0-gke.1000")])
        out = container_runtime.evaluate(record, {}, self._items(with_nodes=False), parse_version("1.34.1-gke.1000"), context(record))
        self.assertEqual(out["risks"], [])

    def test_windows_pools_move_at_1_35(self):
        record = cluster(name="win", pools=[pool("win-pool", "1.34.2-gke.1000", image="WINDOWS_LTSC_CONTAINERD")])
        items = [template("DaemonSet", "monitoring", "win-agent", volumes=[socket_volume()])]
        self.assertEqual(container_runtime.evaluate(record, {}, items, parse_version("1.34.5-gke.1000"), context(record))["risks"], [])
        out = container_runtime.evaluate(record, {}, items, parse_version("1.35.0-gke.1000"), context(record))
        self.assertEqual(out["risks"][0]["target_major"], 2)

    def test_change_without_clients_is_a_note_and_a_read_failure_is_unknown(self):
        record = cluster(name="seeded-a", pools=[pool("default-pool", "1.32.4-gke.1000")])
        out = container_runtime.evaluate(record, {}, self._items(with_client=False), parse_version("1.33.1-gke.1000"), context(record))
        self.assertEqual(out["risks"], [])
        self.assertIn("no pod mounts the runtime socket", out["notes"][0])
        unread = container_runtime.evaluate(record, {}, None, parse_version("1.33.1-gke.1000"), context(record))
        self.assertIn("cluster read failed, so whether a pod mounts the runtime socket is unknown", unread["unknown"][0])
        self.assertEqual(container_runtime.evaluate(cluster(pools=[pool("p", "1.33.0-gke.1")]), {}, None, parse_version("1.34.0-gke.1"), context(cluster(pools=[pool("p", "1.33.0-gke.1")]))), shared.new_result())

    def test_no_target_and_unparsable_versions_are_unknown_only_with_clients(self):
        record = cluster(name="seeded-a", pools=[pool("default-pool", "1.32.4-gke.1000")])
        self.assertIn("no target", container_runtime.evaluate(record, {}, self._items(), None, context(record))["unknown"][0])
        self.assertEqual(container_runtime.evaluate(record, {}, self._items(with_client=False), None, context(record)), shared.new_result())
        record["nodePools"][0]["version"] = "latest"
        self.assertIn("version unparsable", container_runtime.evaluate(record, {}, self._items(), TARGET, context(record))["unknown"][0])

    def test_socket_clients_dedupe_and_accept_both_spellings(self):
        items = [
            template("Deployment", "ops", "agent", volumes=[socket_volume("/var/run/docker.sock"), socket_volume("/var/run/containerd/containerd.sock")]),
            deployment_pod("ops", "agent", "n1", volumes=[socket_volume("/var/run/docker.sock")]),
            template("StatefulSet", "ops", "db", volumes=[{"name": "data", "hostPath": {"path": "/mnt/data"}}]),
        ]
        self.assertEqual(container_runtime.socket_clients(items), [{"owner": "Deployment ops/agent", "paths": ["/var/run/containerd/containerd.sock", "/var/run/docker.sock"]}])

    def test_the_shared_table(self):
        self.assertEqual(tables.containerd_major_at((1, 32), tables.NODE_OS_LINUX), 1)
        self.assertEqual(tables.containerd_major_at((1, 33), tables.NODE_OS_LINUX), 2)
        self.assertEqual(tables.containerd_major_at((1, 34), tables.NODE_OS_WINDOWS), 1)
        self.assertEqual(tables.containerd_major_at((1, 35), tables.NODE_OS_WINDOWS), 2)
        self.assertEqual(tables.node_image_os_family("ubuntu_containerd"), tables.NODE_OS_LINUX)
        self.assertEqual(tables.node_image_os_family("WINDOWS_SAC_CONTAINERD"), tables.NODE_OS_WINDOWS)
        self.assertEqual(tables.node_image_os_family(""), tables.NODE_OS_LINUX)


class DataplaneRuleTest(unittest.TestCase):
    """Entry 16. The fleet's shape: legacy dataplane, network policy add-on off, default-deny
    policies in most namespaces, beside a Dataplane V2 host cluster."""

    def _items(self):
        return [netpol("seeded-debug", "default-deny"), netpol("seeded-upgrade", "default-deny"), netpol("seeded-upgrade", "apiserver-egress")]

    def test_unenforced_policies_on_legacy_are_a_risk_naming_the_namespaces(self):
        record = cluster(name="seeded-b")
        host = cluster(name="platform-agent-host", location="us-central1", networkConfig={"datapathProvider": "ADVANCED_DATAPATH"})
        out = dataplane.evaluate(record, {}, self._items(), TARGET, context(record, clusters=[host, record]))
        self.assertEqual(out["blocking"], [])
        finding = out["risks"][0]
        self.assertEqual(finding["namespaces"], {"seeded-debug": 1, "seeded-upgrade": 2})
        self.assertEqual((finding["policies"], finding["enforced"], finding["peers_on_v2"], finding["datapath"]), (3, False, ["platform-agent-host"], "LEGACY_DATAPATH"))
        text = dataplane.describe(finding)
        self.assertIn("policy enforcement off and NetworkPolicies in seeded-debug (1), seeded-upgrade (2)", text)
        self.assertIn("(platform-agent-host already enforce them)", text)
        self.assertIn("start to apply the moment enforcement turns on", text)

    def test_without_a_v2_peer_the_shape_is_still_a_risk(self):
        record = cluster(name="seeded-b")
        out = dataplane.evaluate(record, {}, self._items(), TARGET, context(record))
        self.assertEqual(out["risks"][0]["peers_on_v2"], [])
        self.assertNotIn("already enforce", dataplane.describe(out["risks"][0]))

    def test_enforced_by_the_addon_is_a_note(self):
        record = cluster(name="seeded-b", networkPolicy={"enabled": True, "provider": "CALICO"}, addonsConfig={"networkPolicyConfig": {}})
        out = dataplane.evaluate(record, {}, self._items(), TARGET, context(record))
        self.assertEqual(out["risks"], [])
        self.assertIn("enforced by the network policy add-on", out["notes"][0])
        record["addonsConfig"]["networkPolicyConfig"]["disabled"] = True
        self.assertEqual(len(dataplane.evaluate(record, {}, self._items(), TARGET, context(record))["risks"]), 1)

    def test_dataplane_v2_and_policy_free_clusters_are_clean(self):
        v2 = cluster(name="host", networkConfig={"datapathProvider": "ADVANCED_DATAPATH"})
        self.assertEqual(dataplane.evaluate(v2, {}, self._items(), TARGET, context(v2)), shared.new_result())
        self.assertEqual(dataplane.evaluate(v2, {}, None, TARGET, context(v2)), shared.new_result())
        legacy = cluster(name="seeded-c", networkConfig={"datapathProvider": "LEGACY_DATAPATH"})
        self.assertEqual(dataplane.evaluate(legacy, {}, [], TARGET, context(legacy)), shared.new_result())

    def test_read_failure_on_legacy_is_unknown(self):
        record = cluster(name="seeded-b")
        self.assertIn("NetworkPolicies were not read", dataplane.evaluate(record, {}, None, TARGET, context(record))["unknown"][0])


class InTreeVolumesRuleTest(unittest.TestCase):
    """Entry 19. The designed shape: intree-pd, an in-tree gcePersistentDisk volume bound by
    the intree-pd claim and mounted by intree-pd-reader on seeded-a."""

    def _items(self, with_pod=True, with_template=True):
        items = [pv("intree-pd", pd_name="seeded-a-intree-pd"), pv("csi-pd", csi=True, claim=("other", "csi"))]
        if with_template:
            items.append(template("Deployment", "seeded-shapes", "intree-pd-reader", volumes=[claim_volume("intree-pd")]))
        if with_pod:
            items.append(deployment_pod("seeded-shapes", "intree-pd-reader", "n1", volumes=[claim_volume("intree-pd")]))
        return items

    def test_addon_off_with_a_consumer_blocks(self):
        record = cluster(name="seeded-a", addonsConfig={"gcePersistentDiskCsiDriverConfig": {}})
        out = intree_volumes.evaluate(record, {}, self._items(), TARGET, context(record))
        self.assertEqual(out["risks"], [])
        finding = out["blocking"][0]
        self.assertEqual((finding["pv"], finding["disk"], finding["claim"], finding["phase"], finding["addon_enabled"]), ("intree-pd", "seeded-a-intree-pd", "seeded-shapes/intree-pd", "Bound", False))
        self.assertEqual(finding["workloads"], ["Deployment seeded-shapes/intree-pd-reader"])
        text = intree_volumes.describe(finding)
        self.assertIn("PersistentVolume intree-pd (in-tree gcePersistentDisk seeded-a-intree-pd, claim seeded-shapes/intree-pd, Bound)", text)
        self.assertIn("the attach fails when a drain moves Deployment seeded-shapes/intree-pd-reader; enable the add-on", text)

    def test_addon_on_is_a_risk_naming_the_migration(self):
        record = cluster(name="seeded-a", addonsConfig={"gcePersistentDiskCsiDriverConfig": {"enabled": True}})
        out = intree_volumes.evaluate(record, {}, self._items(), TARGET, context(record))
        self.assertEqual(out["blocking"], [])
        self.assertEqual(len(out["risks"]), 1)
        text = intree_volumes.describe(out["risks"][0])
        self.assertIn("attaches through CSI migration for Deployment seeded-shapes/intree-pd-reader; move it to pd.csi.storage.gke.io", text)

    def test_addon_off_without_a_consumer_is_a_risk(self):
        record = cluster(name="seeded-a")
        out = intree_volumes.evaluate(record, {}, self._items(with_pod=False, with_template=False), TARGET, context(record))
        self.assertEqual(out["blocking"], [])
        self.assertIn("nothing mounts it now", intree_volumes.describe(out["risks"][0]))

    def test_a_template_consumer_counts_without_a_running_pod(self):
        record = cluster(name="seeded-a")
        out = intree_volumes.evaluate(record, {}, self._items(with_pod=False), TARGET, context(record))
        self.assertEqual(out["blocking"][0]["workloads"], ["Deployment seeded-shapes/intree-pd-reader"])

    def test_migrated_to_annotation_and_unbound_volume(self):
        record = cluster(name="seeded-a", addonsConfig={"gcePersistentDiskCsiDriverConfig": {"enabled": True}})
        items = [pv("loose", claim=None, phase="Available", annotations={"pv.kubernetes.io/migrated-to": "pd.csi.storage.gke.io"})]
        out = intree_volumes.evaluate(record, {}, items, TARGET, context(record))
        self.assertEqual((out["risks"][0]["claim"], out["risks"][0]["migrated_to"]), (None, "pd.csi.storage.gke.io"))
        self.assertIn("claim unbound, Available", intree_volumes.describe(out["risks"][0]))

    def test_clean_and_unknown(self):
        record = cluster(name="seeded-a")
        self.assertEqual(intree_volumes.evaluate(record, {}, [pv("csi-pd", csi=True)], TARGET, context(record)), shared.new_result())
        self.assertIn("PersistentVolumes were not read", intree_volumes.evaluate(record, {}, None, TARGET, context(record))["unknown"][0])


class FoldingTest(unittest.TestCase):
    def test_every_rule_module_meets_the_contract(self):
        ids = [m.RULE_ID for m in r.EXTRA_RULES]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(sorted(m.ENTRY for m in r.EXTRA_RULES), [2, 11, 13, 16, 19])
        for module in r.EXTRA_RULES:
            self.assertTrue(callable(module.evaluate) and callable(module.describe), module.RULE_ID)
            record = cluster(location="us-central1", pools=[pool("p", CURRENT)])
            self.assertEqual(set(module.evaluate(record, {}, [], TARGET, context(record))), set(shared.RESULT_KEYS))

    def test_findings_are_stamped_and_reasons_prefixed(self):
        record = cluster(name="seeded-a", pools=[pool("default-pool", LAGGING)])
        items = [pv("intree-pd"), deployment_pod("seeded-shapes", "intree-pd-reader", "n1", volumes=[claim_volume("intree-pd")]), netpol("seeded-debug", "default-deny")]
        folded = r.evaluate_extra_rules(record, {}, items, TARGET, context(record))
        self.assertEqual(sorted(folded["rules"]), sorted(m.RULE_ID for m in r.EXTRA_RULES))
        self.assertEqual([f["rule"] for f in folded["blocking"]], ["in-tree-volumes"])
        self.assertEqual([f["rule"] for f in folded["risks"]], ["zonal-control-plane", "network-dataplane"])
        self.assertEqual(folded["unknown"], [])
        self.assertIn("PersistentVolume intree-pd", r.describe_extra_finding(folded["blocking"][0]))
        self.assertIn("control plane is zonal", r.describe_extra_finding(folded["risks"][0]))
        self.assertEqual(r.describe_extra_finding({"rule": "nobody", "x": 1}), "{'rule': 'nobody', 'x': 1}")
        unread = r.evaluate_extra_rules(record, {}, None, TARGET, context(record))
        self.assertTrue(all(":" in reason for reason in unread["unknown"]))
        self.assertIn("network-dataplane: cluster read failed, so its NetworkPolicies were not read", unread["unknown"])

    def test_a_rule_that_raises_is_unknown_not_a_crash(self):
        class Broken:
            RULE_ID = "broken"
            ENTRY = 0

            @staticmethod
            def evaluate(*args):
                raise ValueError("boom")

            @staticmethod
            def describe(finding):
                return ""

        original = list(r.EXTRA_RULES)
        r.EXTRA_RULES.append(Broken)
        try:
            folded = r.evaluate_extra_rules(cluster(location="us-central1"), {}, [], TARGET, {})
        finally:
            r.EXTRA_RULES[:] = original
        self.assertEqual(folded["unknown"], ["broken: rule failed (boom); not evaluated"])
        self.assertEqual(folded["rules"]["broken"]["blocking"], [])

    def test_verdict_folds_blocking_and_unknown_and_ignores_risks(self):
        clear = {"blocking_exclusions": [], "undecided_exclusions": []}
        no_skew = {"blocking": [], "unknown": []}
        pdbs = {"blocking": []}
        self.assertEqual(r.readiness_status(pdbs, clear, no_skew, True, {"blocking": [], "unknown": [], "risks": [{"rule": "x"}]}), "ready")
        self.assertEqual(r.readiness_status(pdbs, clear, no_skew, True, {"blocking": [], "unknown": ["why"], "risks": []}), "unknown")
        self.assertEqual(r.readiness_status(None, clear, no_skew, False, {"blocking": [{"rule": "x"}], "unknown": ["why"], "risks": []}), "blocked")
        self.assertEqual(r.readiness_status(pdbs, clear, no_skew, True), "ready")


if __name__ == "__main__":
    unittest.main()
