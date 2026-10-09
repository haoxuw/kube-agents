#!/usr/bin/env python3
"""Unit tests for the readiness_rules package: each workload rule on fixture-shaped objects.

The objects mirror the seeded fleet's shapes on bench/tf/fleet (cache-on-emptydir,
arch-pinned-worker, cgroup-blind-jvm, multi-process-worker, cni-shaped-agent,
cuda-pinned-trainer, legacy-registry-pull) plus the variants each rule grades differently:
the blocking, risk, unknown and clean paths a rule has.
"""

import os
import sys
import unittest
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as rules  # noqa: E402
import upgrade_shape_tables as tables  # noqa: E402
from fleet_upgrade_report import parse_version  # noqa: E402
from readiness_rules import (  # noqa: E402
    cgroup_v2_runtime,
    gpu_driver_mismatch,
    group_oom_kill,
    node_image_coupled_agent,
    node_local_state,
    removed_node_label,
    retired_registry,
)

AT = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
TARGET_TEXT = "1.34.1-gke.1000"
TARGET = parse_version(TARGET_TEXT)
NS = "seeded-shapes"


def template(kind, name, spec, namespace=NS, workload_spec=None):
    pod = {"metadata": {"labels": {"app": name}}, "spec": spec}
    if kind == "CronJob":
        return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, "spec": {"suspend": True, "jobTemplate": {"spec": {"template": pod}}}}
    return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, "spec": {"replicas": 1, "template": pod, **(workload_spec or {})}}


def node(name, pool="default-pool", labels=None):
    base = {tables.NODEPOOL_LABEL: pool, "kubernetes.io/arch": "amd64", "beta.kubernetes.io/arch": "amd64", "kubernetes.io/os": "linux", "topology.kubernetes.io/zone": "us-central1-a"}
    return {"kind": "Node", "metadata": {"name": name, "labels": {**base, **(labels or {})}}}


def pool(name="default-pool", version="1.33.4-gke.1000", **config):
    return {"name": name, "version": version, "parsed": parse_version(version), "config": config}


def context(pools, target_text=TARGET_TEXT):
    return {"at": AT, "target_text": target_text, "master": parse_version("1.33.4-gke.1000"), "pools": pools, "autopilot": False}


def run(rule, items, pools=None, target=TARGET):
    pools = [pool()] if pools is None else pools
    return rule.evaluate({}, {}, items, target, context(pools))


def busybox(command=None, name="worker"):
    container = {"name": name, "image": "busybox:1.36"}
    if command:
        container["command"] = command
    return container


class NodeLocalStateTest(unittest.TestCase):
    """Entry 4 has risk and clean paths only: nothing it reads can block, and nothing is undecidable."""

    def test_emptydir_queue_is_a_risk_naming_the_volume(self):
        out = run(node_local_state, [template("Deployment", "cache-on-emptydir", {"containers": [busybox(name="queue")], "volumes": [{"name": "queue", "emptyDir": {}}]})])
        self.assertEqual(out["blocking"], [])
        self.assertEqual(out["unknown"], [])
        self.assertEqual(len(out["risks"]), 1)
        finding = out["risks"][0]
        self.assertEqual((finding["kind"], finding["object"], finding["grade"]), ("Deployment", "seeded-shapes/cache-on-emptydir", "risk"))
        self.assertEqual(finding["volumes"], ["queue (emptyDir; the name suggests state)"])
        self.assertEqual(node_local_state.describe(finding), "Deployment seeded-shapes/cache-on-emptydir: keeps data on the node in queue (emptyDir; the name suggests state); a node rebuild loses it")

    def test_local_ssd_hostpath_and_memory_emptydir_are_named(self):
        spec = {"containers": [busybox()], "volumes": [{"name": "scratch", "emptyDir": {"medium": "Memory"}}, {"name": "ssd", "hostPath": {"path": "/mnt/disks/ssd0"}}, {"name": "cfg", "configMap": {"name": "c"}}]}
        out = run(node_local_state, [template("Deployment", "d", spec)])
        self.assertEqual(out["risks"][0]["volumes"], ["scratch (emptyDir, medium Memory)", "ssd (hostPath /mnt/disks/ssd0, a Local SSD mount)"])

    def test_statefulset_on_a_local_storage_class_is_the_strongest_case(self):
        sc = {"kind": "StorageClass", "metadata": {"name": "local-scsi"}, "provisioner": "kubernetes.io/no-provisioner"}
        sts = template("StatefulSet", "db", {"containers": [busybox()]}, workload_spec={"volumeClaimTemplates": [{"metadata": {"name": "data"}, "spec": {"storageClassName": "local-scsi"}}]})
        out = run(node_local_state, [sc, sts])
        self.assertEqual(out["risks"][0]["volumes"], ["data (volumeClaimTemplate on local StorageClass local-scsi)"])
        self.assertIn("a StatefulSet's identity survives the rebuild while its node-local data does not", out["risks"][0]["detail"])

    def test_claim_on_a_local_class_is_a_risk_and_a_disk_claim_is_not(self):
        items = [
            {"kind": "StorageClass", "metadata": {"name": "local-scsi"}, "provisioner": "kubernetes.io/no-provisioner"},
            {"kind": "StorageClass", "metadata": {"name": "standard-rwo"}, "provisioner": "pd.csi.storage.gke.io"},
            {"kind": "PersistentVolumeClaim", "metadata": {"namespace": NS, "name": "fast"}, "spec": {"storageClassName": "local-scsi"}},
            {"kind": "PersistentVolumeClaim", "metadata": {"namespace": NS, "name": "disk"}, "spec": {"storageClassName": "standard-rwo"}},
            template("Deployment", "on-ssd", {"containers": [busybox()], "volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": "fast"}}]}),
            template("Deployment", "on-disk", {"containers": [busybox()], "volumes": [{"name": "v", "persistentVolumeClaim": {"claimName": "disk"}}]}),
        ]
        out = run(node_local_state, items)
        self.assertEqual([f["name"] for f in out["risks"]], ["on-ssd"])
        self.assertEqual(out["risks"][0]["volumes"], ["v (claim fast on local StorageClass local-scsi)"])

    def test_clean_daemonsets_cronjobs_and_system_namespaces_are_not_read(self):
        items = [
            template("Deployment", "stateless", {"containers": [busybox()], "volumes": [{"name": "cfg", "configMap": {"name": "c"}}]}),
            template("DaemonSet", "agent", {"containers": [busybox()], "volumes": [{"name": "logs", "hostPath": {"path": "/var/log"}}]}),
            template("CronJob", "job", {"containers": [busybox()], "volumes": [{"name": "tmp", "emptyDir": {}}]}),
            template("Deployment", "kube-dns", {"containers": [busybox()], "volumes": [{"name": "tmp", "emptyDir": {}}]}, namespace="kube-system"),
            template("Deployment", "collector", {"containers": [busybox()], "volumes": [{"name": "tmp", "emptyDir": {}}]}, namespace="gke-managed-cim"),
        ]
        self.assertEqual(run(node_local_state, items), rules.empty_result())


class RemovedNodeLabelTest(unittest.TestCase):
    NODES = [node("n1"), node("n2")]

    def test_deprecated_beta_arch_label_the_nodes_carry_is_a_risk_with_the_replacement(self):
        items = self.NODES + [template("Deployment", "arch-pinned-worker", {"containers": [busybox()], "nodeSelector": {"beta.kubernetes.io/arch": "amd64"}})]
        out = run(removed_node_label, items)
        self.assertEqual(out["blocking"], [])
        self.assertEqual(out["unknown"], [])
        finding = out["risks"][0]
        self.assertEqual((finding["object"], finding["label"], finding["replacement"]), ("seeded-shapes/arch-pinned-worker", "beta.kubernetes.io/arch", "kubernetes.io/arch"))
        self.assertEqual(removed_node_label.describe(finding), "Deployment seeded-shapes/arch-pinned-worker: selects on beta.kubernetes.io/arch=amd64 (nodeSelector), deprecated since 1.14 in favour of kubernetes.io/arch; the kubelet still sets it today, and a node image that stops setting it strands the pods")

    def test_label_value_the_target_minor_dropped_is_blocking(self):
        items = self.NODES + [template("Deployment", "docker-pinned", {"containers": [busybox()], "nodeSelector": {"cloud.google.com/gke-container-runtime": "docker"}})]
        out = run(removed_node_label, items)
        self.assertEqual(out["risks"], [])
        finding = out["blocking"][0]
        self.assertEqual((finding["grade"], finding["dropped_at"]), ("blocking", "1.24"))
        self.assertIn("cloud.google.com/gke-container-runtime=docker (nodeSelector), dropped at 1.24: GKE 1.24 removes Docker node images; the target 1.34 is at or past it", finding["detail"])
        # The same key with the surviving value is carried by no test node, so it is the risk path, not the drop.
        items = self.NODES + [template("Deployment", "containerd-pinned", {"containers": [busybox()], "nodeSelector": {"cloud.google.com/gke-container-runtime": "containerd"}})]
        out = run(removed_node_label, items)
        self.assertEqual(out["blocking"], [])
        self.assertIn("no node in the cluster carries it today", out["risks"][0]["detail"])

    def test_kubeadm_master_label_is_blocking_and_undecided_without_a_target(self):
        affinity = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "node-role.kubernetes.io/master", "operator": "Exists"}]}]}}}}
        items = self.NODES + [template("Deployment", "reflector", {"containers": [busybox()], **affinity})]
        out = run(removed_node_label, items)
        self.assertEqual(out["blocking"][0]["label"], "node-role.kubernetes.io/master")
        self.assertIn("(nodeAffinity), dropped at 1.24: kubeadm 1.24 stops setting it", out["blocking"][0]["detail"])
        out = run(removed_node_label, items, target=None)
        self.assertEqual(out["blocking"], [])
        self.assertIn("whether the target crosses it needs a target", out["unknown"][0]["detail"])

    def test_label_no_node_carries_is_a_risk_scoped_to_the_selected_pool(self):
        nodes = [node("a1", pool="pool-a", labels={"team/gpu": "yes"}), node("b1", pool="pool-b")]
        pinned = template("Deployment", "pinned", {"containers": [busybox()], "nodeSelector": {tables.NODEPOOL_LABEL: "pool-b", "team/gpu": "yes"}})
        out = run(removed_node_label, nodes + [pinned], pools=[pool("pool-a"), pool("pool-b")])
        self.assertEqual([f["label"] for f in out["risks"]], ["team/gpu"])
        self.assertIn("no node in pool(s) pool-b carries it today", out["risks"][0]["detail"])
        # Across the whole cluster a node does carry it: clean.
        free = template("Deployment", "free", {"containers": [busybox()], "nodeSelector": {"team/gpu": "yes"}})
        self.assertEqual(run(removed_node_label, nodes + [free], pools=[pool("pool-a"), pool("pool-b")]), rules.empty_result())

    def test_no_node_objects_makes_the_carried_question_unknown(self):
        items = [template("Deployment", "pinned", {"containers": [busybox()], "nodeSelector": {"team/gpu": "yes"}})]
        out = run(removed_node_label, items)
        self.assertEqual(out["risks"], [])
        self.assertIn("no Node object was read", out["unknown"][0]["detail"])

    def test_ga_labels_the_nodes_carry_and_system_namespaces_are_clean(self):
        items = self.NODES + [
            template("Deployment", "zonal", {"containers": [busybox()], "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": "topology.kubernetes.io/zone", "operator": "In", "values": ["us-central1-a"]}]}]}}}}),
            template("Deployment", "arch", {"containers": [busybox()], "nodeSelector": {"kubernetes.io/arch": "amd64"}}),
            template("DaemonSet", "fluentbit", {"containers": [busybox()], "nodeSelector": {"beta.kubernetes.io/os": "linux"}}, namespace="kube-system"),
        ]
        self.assertEqual(run(removed_node_label, items), rules.empty_result())


class CgroupV2RuntimeTest(unittest.TestCase):
    JVM = template("Deployment", "cgroup-blind-jvm", {"containers": [{"name": "jvm", "image": "docker.io/library/eclipse-temurin:8u302-b08-jre"}]})

    def test_old_jdk_on_a_pool_gke_migrates_at_the_target_is_a_risk(self):
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")])
        self.assertEqual(out["blocking"], [])
        self.assertEqual(out["unknown"], [])
        finding = out["risks"][0]
        self.assertEqual((finding["object"], finding["image"], finding["runtime"]), ("seeded-shapes/cgroup-blind-jvm", "docker.io/library/eclipse-temurin:8u302-b08-jre", "JDK 8u302, below 8u372"))
        self.assertEqual(cgroup_v2_runtime.describe(finding), "Deployment seeded-shapes/cgroup-blind-jvm: container jvm runs docker.io/library/eclipse-temurin:8u302-b08-jre (JDK 8u302, below 8u372) on pool default-pool, which GKE migrates cgroup v1 pools to v2 from 1.33 at the target; the heap is then sized from the node and the container is OOM-killed")

    def test_pool_already_on_v2_or_staying_on_v1_is_a_note_not_a_risk(self):
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V2")])
        self.assertEqual(out["risks"], [])
        self.assertEqual(out["notes"], ["cgroup-v2-runtime: Deployment seeded-shapes/cgroup-blind-jvm container jvm runs docker.io/library/eclipse-temurin:8u302-b08-jre (JDK 8u302, below 8u372) on pool default-pool, already on cgroup v2; not an upgrade risk"])
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool(version="1.31.0-gke.1", effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")], target=parse_version("1.32.0-gke.1"))
        self.assertEqual(out["risks"], [])
        self.assertIn("the pool stays on cgroup v1 below 1.33; not a risk at this target", out["notes"][0])

    def test_a_v1_pin_holds_until_1_35(self):
        pinned = pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1", linuxNodeConfig={"cgroupMode": "CGROUP_MODE_V1"})
        self.assertEqual(run(cgroup_v2_runtime, [self.JVM], pools=[pinned])["risks"], [])
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pinned], target=parse_version("1.35.0-gke.1"))
        self.assertIn("the pool is pinned to cgroup v1 and GKE removes cgroup v1 at 1.35", out["risks"][0]["detail"])

    def test_unread_cgroup_mode_and_unversioned_tags_are_unknown(self):
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool()])
        self.assertEqual(out["risks"], [])
        self.assertIn("the pool record carries no effectiveCgroupMode", out["unknown"][0]["detail"])
        floating = template("Deployment", "floating", {"containers": [{"name": "jvm", "image": "eclipse-temurin:8-jre"}]})
        out = run(cgroup_v2_runtime, [floating], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")])
        self.assertEqual(out["risks"], [])
        self.assertIn("(JDK 8 with no update in the tag) on pool default-pool, which GKE migrates cgroup v1 pools to v2 from 1.33 at the target; the tag does not say whether it reads cgroup v2", out["unknown"][0]["detail"])
        out = run(cgroup_v2_runtime, [self.JVM], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")], target=None)
        self.assertIn("no target to decide whether the pool moves to cgroup v2", out["unknown"][0]["detail"])

    def test_runtime_verdicts(self):
        below, ok, unversioned = cgroup_v2_runtime.RUNTIME_BELOW, cgroup_v2_runtime.RUNTIME_OK, cgroup_v2_runtime.RUNTIME_UNVERSIONED
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("openjdk:11.0.12-jre")[0], below)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("eclipse-temurin:11.0.20_8-jre")[0], ok)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("eclipse-temurin:8u372-b07-jre")[0], ok)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("openjdk:17")[0], ok)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("adoptopenjdk:14-jre-hotspot"), (below, "JDK 14, which predates cgroup v2 support (from JDK 15)"))
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("openjdk:11")[0], unversioned)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("openjdk:latest")[0], unversioned)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("mcr.microsoft.com/dotnet/aspnet:3.1"), (below, ".NET 3.1, below 5.0"))
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("mcr.microsoft.com/dotnet/aspnet:8.0")[0], ok)
        self.assertEqual(cgroup_v2_runtime.runtime_verdict("mcr.microsoft.com/dotnet/runtime:latest")[0], unversioned)
        self.assertIsNone(cgroup_v2_runtime.runtime_verdict("busybox:1.36"))
        self.assertIsNone(cgroup_v2_runtime.runtime_verdict("registry.k8s.io/pause:3.10"))

    def test_fine_runtimes_and_system_namespaces_are_clean(self):
        items = [
            template("Deployment", "modern", {"containers": [{"name": "jvm", "image": "eclipse-temurin:21-jre"}]}),
            template("Deployment", "old-but-system", {"containers": [{"name": "jvm", "image": "openjdk:8u212-jre"}]}, namespace="kube-system"),
        ]
        self.assertEqual(run(cgroup_v2_runtime, items, pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")]), rules.empty_result())


class GroupOomKillTest(unittest.TestCase):
    WORKER = template("Deployment", "multi-process-worker", {"containers": [busybox(["sh", "-c", "sleep infinity & sleep infinity & wait"], name="supervisor")]})

    def test_forking_shell_on_a_pool_moving_to_v2_is_a_risk_marked_heuristic(self):
        out = run(group_oom_kill, [self.WORKER], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")])
        self.assertEqual(out["blocking"], [])
        finding = out["risks"][0]
        self.assertTrue(finding["heuristic"])
        self.assertEqual(finding["shape"], "runs a shell script that forks a background job")
        self.assertEqual(group_oom_kill.describe(finding), "Deployment seeded-shapes/multi-process-worker: container supervisor runs a shell script that forks a background job (heuristic: the process count is not readable from the API) on pool default-pool, which GKE migrates cgroup v1 pools to v2 from 1.33 at the target; a kubelet at 1.34 then sets memory.oom.group and an OOM kill takes every process in the container")

    def test_kubelet_crossing_1_28_on_a_v2_pool_is_a_risk_and_past_it_a_note(self):
        out = run(group_oom_kill, [self.WORKER], pools=[pool(version="1.27.9-gke.1", effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V2")], target=parse_version("1.29.0-gke.1"))
        self.assertIn("on cgroup v2 with a kubelet at 1.27.9-gke.1; from 1.28 the kubelet sets memory.oom.group", out["risks"][0]["detail"])
        out = run(group_oom_kill, [self.WORKER], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V2")])
        self.assertEqual(out["risks"], [])
        self.assertEqual(out["notes"], ["group-oom-kill: Deployment seeded-shapes/multi-process-worker container supervisor runs a shell script that forks a background job on pool default-pool, on cgroup v2 with a kubelet at 1.33.4-gke.1000, which already group-kills; not an upgrade risk"])

    def test_single_process_oom_kill_and_an_unread_mode(self):
        out = run(group_oom_kill, [self.WORKER], pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1", linuxNodeConfig={"singleProcessOomKill": True})])
        self.assertEqual(out["risks"], [])
        self.assertIn("whose node config sets singleProcessOOMKill; not an upgrade risk", out["notes"][0])
        out = run(group_oom_kill, [self.WORKER], pools=[pool()])
        self.assertEqual(out["risks"], [])
        self.assertIn("the pool record carries no effectiveCgroupMode", out["unknown"][0]["detail"])

    def test_shapes_and_non_shapes(self):
        self.assertEqual(group_oom_kill.multi_process_shape({"command": ["/usr/bin/supervisord", "-n"]}), "runs supervisord")
        self.assertEqual(group_oom_kill.multi_process_shape({"command": ["/init"], "args": ["s6-svscan", "/etc/s6"]}), "runs s6-svscan")
        self.assertEqual(group_oom_kill.multi_process_shape({"command": ["tini", "--", "bash", "-c", "nginx & php-fpm & wait"]}), "runs a shell script that forks a background job")
        self.assertEqual(group_oom_kill.multi_process_shape({"image": "docker.io/phusion/baseimage:jammy-1.0.1"}), "runs phusion/baseimage, whose entrypoint is a supervisor")
        self.assertIsNone(group_oom_kill.multi_process_shape({"command": ["sh", "-c", "date -u > /tmp/stamp && exec tail -f /dev/null 2>&1"]}))
        self.assertIsNone(group_oom_kill.multi_process_shape({"command": ["sh", "-c", "java -version && exec sleep infinity"]}))
        self.assertIsNone(group_oom_kill.multi_process_shape({"image": "nginx:1.27", "args": ["nginx", "-g", "daemon off;"]}))

    def test_clean_when_nothing_looks_multi_process(self):
        items = [template("Deployment", "single", {"containers": [busybox(["sh", "-c", "tail -f /dev/null"])]})]
        self.assertEqual(run(group_oom_kill, items, pools=[pool(effectiveCgroupMode="EFFECTIVE_CGROUP_MODE_V1")]), rules.empty_result())


class NodeImageCoupledAgentTest(unittest.TestCase):
    AGENT = template("DaemonSet", "cni-shaped-agent", {"hostNetwork": True, "containers": [{"name": "agent", "image": "registry.k8s.io/pause:3.10"}], "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d", "type": "Directory"}}]})

    def test_host_network_daemonset_mounting_the_cni_directory_is_a_risk(self):
        out = run(node_image_coupled_agent, [self.AGENT])
        self.assertEqual(out["blocking"], [])
        self.assertEqual(out["unknown"], [])
        finding = out["risks"][0]
        self.assertEqual((finding["kind"], finding["object"], finding["couplings"], finding["pools_below_target"]), ("DaemonSet", "seeded-shapes/cni-shaped-agent", ["mounts /etc/cni/net.d from the node"], ["default-pool"]))
        self.assertEqual(node_image_coupled_agent.describe(finding), "DaemonSet seeded-shapes/cni-shaped-agent: on the node's network and coupled to the node image: mounts /etc/cni/net.d from the node; pool(s) default-pool are below the target and get a new node image in this upgrade")

    def test_pools_at_the_target_and_no_target_change_the_detail_not_the_grade(self):
        out = run(node_image_coupled_agent, [self.AGENT], pools=[pool(version=TARGET_TEXT)])
        self.assertIn("no pool is below the target; the next node upgrade changes the image under it", out["risks"][0]["detail"])
        out = run(node_image_coupled_agent, [self.AGENT], target=None)
        self.assertIn("which pools change image needs a target", out["risks"][0]["detail"])

    def test_label_module_and_modprobe_couplings(self):
        spec = {
            "hostNetwork": True,
            "nodeSelector": {"cloud.google.com/gke-os-distribution": "cos"},
            "containers": [{"name": "a", "image": "x", "command": ["/bin/sh", "-c", "modprobe ip_vs && exec agent"]}],
            "volumes": [{"name": "mods", "hostPath": {"path": "/lib/modules"}}, {"name": "bins", "hostPath": {"path": "/home/kubernetes/bin"}}],
        }
        self.assertEqual(node_image_coupled_agent.couplings(spec), ["selects on node-image label cloud.google.com/gke-os-distribution", "mounts /lib/modules from the node", "mounts /home/kubernetes/bin from the node"])
        spec["containers"][0]["command"] = ["modprobe", "ip_vs"]
        self.assertIn("runs modprobe", node_image_coupled_agent.couplings(spec))

    def test_clean_without_host_network_without_coupling_or_in_managed_namespaces(self):
        items = [
            template("DaemonSet", "pod-network", {"containers": [busybox()], "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d"}}]}),
            template("DaemonSet", "host-network-only", {"hostNetwork": True, "containers": [busybox()], "volumes": [{"name": "logs", "hostPath": {"path": "/var/log"}}]}),
            template("Deployment", "not-an-agent", {"hostNetwork": True, "containers": [busybox()], "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d"}}]}),
            template("DaemonSet", "netd", {"hostNetwork": True, "containers": [busybox()], "volumes": [{"name": "cni-conf", "hostPath": {"path": "/etc/cni/net.d"}}]}, namespace="kube-system"),
            template("DaemonSet", "otel", {"hostNetwork": True, "containers": [busybox()], "volumes": [{"name": "mods", "hostPath": {"path": "/lib/modules"}}]}, namespace="config-management-monitoring"),
        ]
        self.assertEqual(run(node_image_coupled_agent, items), rules.empty_result())


class GpuDriverMismatchTest(unittest.TestCase):
    def trainer(self, image="docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04", env=None, select=True):
        container = {"name": "trainer", "image": image, "resources": {"limits": {"nvidia.com/gpu": "1"}, "requests": {"nvidia.com/gpu": "1"}}}
        if env:
            container["env"] = env
        spec = {"containers": [container]}
        if select:
            spec["nodeSelector"] = {"cloud.google.com/gke-accelerator": "nvidia-l4"}
        return template("CronJob", "cuda-pinned-trainer", spec)

    def gpu_pool(self, version="1.32.4-gke.1", mode="DEFAULT", image_type="COS_CONTAINERD", accelerator="nvidia-l4"):
        return pool("gpu-pool", version, imageType=image_type, accelerators=[{"acceleratorType": accelerator, "acceleratorCount": "1", "gpuDriverInstallationConfig": {"gpuDriverVersion": mode}}])

    def test_no_pool_carries_the_accelerator_is_a_risk_naming_the_pin(self):
        out = run(gpu_driver_mismatch, [self.trainer()])
        self.assertEqual(out["blocking"], [])
        self.assertEqual(out["unknown"], [])
        finding = out["risks"][0]
        self.assertEqual((finding["kind"], finding["object"], finding["cuda"]), ("CronJob", "seeded-shapes/cuda-pinned-trainer", ["12.2"]))
        self.assertEqual(gpu_driver_mismatch.describe(finding), "CronJob seeded-shapes/cuda-pinned-trainer: requests nvidia.com/gpu on accelerator nvidia-l4 and no pool in the cluster carries that accelerator; the target driver cannot be read, and the pin image docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04 stays unchecked")

    def test_driver_below_the_major_floor_blocks_below_the_toolkit_minimum_is_a_risk_and_served_is_a_note(self):
        target = parse_version("1.33.0-gke.1")
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:13.0.0-runtime-ubuntu24.04")], pools=[self.gpu_pool()], target=target)
        self.assertEqual((out["blocking"][0]["cuda"], out["blocking"][0]["driver"]), ("13.0", 535))
        self.assertIn("the target 1.33 node image ships driver R535 (gpuDriverVersion DEFAULT), below the R580 floor for CUDA 13.x; the device does not open", out["blocking"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer("pytorch/pytorch:2.6.0-cuda12.8-cudnn9-runtime")], pools=[self.gpu_pool()], target=target)
        self.assertEqual(out["blocking"], [])
        self.assertIn("below the R570 CUDA 12.8 names; the build runs under minor version compatibility", out["risks"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()], target=target)
        self.assertEqual((out["blocking"], out["risks"], out["unknown"]), ([], [], []))
        self.assertEqual(out["notes"], ["gpu-driver: CronJob seeded-shapes/cuda-pinned-trainer requests nvidia.com/gpu with image docker.io/nvidia/cuda:12.2.0-base-ubuntu22.04 on pool gpu-pool; the target 1.33 node image ships driver R535 (gpuDriverVersion DEFAULT), which serves CUDA 12.2; forward-compatibility packages inside the image are not readable from the tag"])
        # LATEST on 1.33 is R580, which serves CUDA 13.0.
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:13.0.0-runtime-ubuntu24.04")], pools=[self.gpu_pool(mode="LATEST")], target=target)
        self.assertEqual(out["blocking"], [])
        self.assertIn("driver R580 (gpuDriverVersion LATEST), which serves CUDA 13.0", out["notes"][0])

    def test_unknown_when_the_table_does_not_cover_the_target_or_the_driver_is_not_in_the_record(self):
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()])
        self.assertIn("the COS_CONTAINERD driver table covers 1.26 to 1.33, not the target 1.34", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool(mode="INSTALLATION_DISABLED")], target=parse_version("1.33.0-gke.1"))
        self.assertIn("the operator-installed driver is not in the record", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool(mode=None)], target=parse_version("1.33.0-gke.1"))
        self.assertIn("the pool record carries no gpuDriverVersion", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool(image_type="UBUNTU_CONTAINERD")], target=parse_version("1.30.0-gke.1"))
        self.assertIn("the UBUNTU_CONTAINERD driver table covers 1.31 to 1.33, not the target 1.30", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer("nvidia/cuda:10.2-base")], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1"))
        self.assertIn("CUDA 10.2 is outside the table (majors 11, 12, 13)", out["unknown"][0]["detail"])
        out = run(gpu_driver_mismatch, [self.trainer()], pools=[self.gpu_pool()], target=None)
        self.assertIn("no target to read the driver at", out["unknown"][0]["detail"])

    def test_image_without_a_cuda_version_is_a_risk_and_env_pins_count(self):
        out = run(gpu_driver_mismatch, [self.trainer("tensorflow/tensorflow:2.15.0-gpu")], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1"))
        self.assertEqual(gpu_driver_mismatch.describe(out["risks"][0]), "CronJob seeded-shapes/cuda-pinned-trainer: requests nvidia.com/gpu and its image tensorflow/tensorflow:2.15.0-gpu names no CUDA version; the driver it needs cannot be read from the tag")
        out = run(gpu_driver_mismatch, [self.trainer("my/trainer:1", env=[{"name": "CUDA_VERSION", "value": "12.4.1"}])], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1"))
        self.assertIn("with env CUDA_VERSION=12.4.1 on pool gpu-pool", out["risks"][0]["detail"])

    def test_clean_without_a_gpu_request_and_pool_selection(self):
        plain = template("Deployment", "cpu-only", {"containers": [{"name": "c", "image": "nvidia/cuda:13.0.0-base"}]})
        self.assertEqual(run(gpu_driver_mismatch, [plain], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1")), rules.empty_result())
        # A template selecting another accelerator does not read this pool's driver.
        other = self.trainer("nvidia/cuda:13.0.0-base")
        other["spec"]["jobTemplate"]["spec"]["template"]["spec"]["nodeSelector"] = {"cloud.google.com/gke-accelerator": "nvidia-tesla-t4"}
        out = run(gpu_driver_mismatch, [other], pools=[self.gpu_pool()], target=parse_version("1.33.0-gke.1"))
        self.assertEqual(out["blocking"], [])
        self.assertIn("no pool in the cluster carries that accelerator", out["risks"][0]["detail"])


class RetiredRegistryTest(unittest.TestCase):
    LEGACY = template("Deployment", "legacy-registry-pull", {"containers": [{"name": "pause", "image": "k8s.gcr.io/pause:3.9", "imagePullPolicy": "Always"}]})

    def test_retired_host_is_blocking_naming_the_image_and_the_rebuilt_pools(self):
        out = run(retired_registry, [self.LEGACY])
        self.assertEqual(out["risks"], [])
        self.assertEqual(out["unknown"], [])
        finding = out["blocking"][0]
        self.assertEqual((finding["object"], finding["hosts"], finding["pools_below_target"]), ("seeded-shapes/legacy-registry-pull", ["k8s.gcr.io/"], ["default-pool"]))
        self.assertEqual(retired_registry.describe(finding), "Deployment seeded-shapes/legacy-registry-pull: container pause pulls k8s.gcr.io/pause:3.9 from k8s.gcr.io (frozen 2023-04-03, a redirect to registry.k8s.io since 2023-03-20); a rebuilt node pulls it again; pool(s) default-pool are below the target and are rebuilt in this upgrade")

    def test_pools_at_the_target_and_no_target_keep_the_grade(self):
        out = run(retired_registry, [self.LEGACY], pools=[pool(version=TARGET_TEXT)])
        self.assertIn("no pool is below the target; the next node upgrade pulls it again", out["blocking"][0]["detail"])
        out = run(retired_registry, [self.LEGACY], target=None)
        self.assertIn("which pools are rebuilt needs a target", out["blocking"][0]["detail"])

    def test_every_retired_host_including_init_containers(self):
        items = [
            template("StatefulSet", "tiller", {"initContainers": [{"name": "init", "image": "gcr.io/kubernetes-helm/tiller:v2.17.0"}], "containers": [busybox()]}),
            template("DaemonSet", "old-agent", {"containers": [{"name": "a", "image": "gcr.io/google_containers/pause-amd64:3.1"}]}),
            template("CronJob", "old-job", {"containers": [{"name": "j", "image": "gcr.io/google-containers/busybox:1.27"}]}),
        ]
        out = run(retired_registry, items)
        self.assertEqual([(f["kind"], f["hosts"]) for f in out["blocking"]], [("StatefulSet", ["gcr.io/kubernetes-helm/"]), ("DaemonSet", ["gcr.io/google_containers/"]), ("CronJob", ["gcr.io/google-containers/"])])
        self.assertIn("Helm 2's registry, unsupported since 2020-11-13", out["blocking"][0]["detail"])

    def test_clean_hosts_and_system_namespaces(self):
        items = [
            template("Deployment", "current", {"containers": [{"name": "c", "image": "registry.k8s.io/pause:3.10"}, {"name": "d", "image": "busybox:1.36"}, {"name": "e", "image": "us-docker.pkg.dev/p/r/i:1"}]}),
            template("Deployment", "kube-proxy", {"containers": [{"name": "c", "image": "k8s.gcr.io/kube-proxy:v1.20.0"}]}, namespace="kube-system"),
        ]
        self.assertEqual(run(retired_registry, items), rules.empty_result())


class HelpersTest(unittest.TestCase):
    def test_pools_for_template_follows_the_nodepool_selector(self):
        pools = [pool("a"), pool("b")]
        self.assertEqual([p["name"] for p in rules.pools_for_template({"nodeSelector": {tables.NODEPOOL_LABEL: "b"}}, pools)], ["b"])
        affinity = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{"matchExpressions": [{"key": tables.NODEPOOL_LABEL, "operator": "In", "values": ["a"]}]}]}}}}
        self.assertEqual([p["name"] for p in rules.pools_for_template(affinity, pools)], ["a"])
        self.assertEqual(len(rules.pools_for_template({}, pools)), 2)

    def test_image_repository_and_tag(self):
        self.assertEqual(rules.image_repository_and_tag("docker.io/library/eclipse-temurin:8u302-b08-jre"), ("eclipse-temurin", "8u302-b08-jre"))
        self.assertEqual(rules.image_repository_and_tag("localhost:5000/openjdk"), ("openjdk", ""))
        self.assertEqual(rules.image_repository_and_tag("openjdk:11@sha256:abc"), ("openjdk", "11"))

    def test_system_namespaces(self):
        for namespace in ("kube-system", "gmp-system", "gke-managed-cim", "gke-gmp-system"):
            self.assertTrue(rules.is_system_namespace(namespace))
        self.assertFalse(rules.is_system_namespace("config-management-system"))
        self.assertTrue(rules.is_system_namespace("config-management-system", managed_agents=True))
        self.assertFalse(rules.is_system_namespace("seeded-shapes", managed_agents=True))

    def test_describe_rule_level_entry(self):
        self.assertEqual(rules.describe(rules.rule_unknown("x", None, "why")), "x: why")


if __name__ == "__main__":
    unittest.main()
