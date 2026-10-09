#!/usr/bin/env python3
"""
upgrade_shape_tables.py — the facts behind the upgrade-failure catalogue's static shapes.

One home for the tables the readiness rules under `readiness_rules/` read, and that the
retrospective collector (`fleet-audit/scripts/upgrade_retrospective.py`) reads the same
shapes with after the fact; the constant names here are the collector's where the
collector has one, so it can import this module instead of carrying copies. Every table
names its source. The tables are deliberately small: a rule whose input has no row here
grades `unknown` with the reason rather than guessing
(docs/designs/upgrade-readiness-checks.md, "What a run produces").

Catalogue entries, as docs/designs/upgrade-failure-catalogue.md numbers them: 4 data on
the node, 12 a node label removed, 13 the container runtime, 14 cgroup v2 under an old
runtime, 15 the group OOM kill, 17 a node networking agent, 18 GPU driver mismatch,
20 images on a retired registry.
"""

import re

# ------------------------------------------------------------------ namespaces, kinds

# Namespaces GKE's own components occupy: kube-system, gmp-system and every `gke-*`
# (gke-gmp-system, gke-managed-*). GKE upgrades what runs there with the version, so the
# readiness rules read nothing in them; Config Sync's namespaces are the one more family
# of managed agents.
SYSTEM_NAMESPACES = ("kube-system", "gmp-system")
SYSTEM_NAMESPACE_PREFIXES = ("gke-",)
MANAGED_AGENT_NAMESPACE_PREFIXES = ("config-management-",)
# Kinds whose spec carries a pod template. Bare Pods and Jobs are not read.
TEMPLATE_KINDS = ("Deployment", "StatefulSet", "DaemonSet", "CronJob")
# The pool a node belongs to, as a node label and as the selector a template pins with.
NODEPOOL_LABEL = "cloud.google.com/gke-nodepool"

# ------------------------------------------------------------------ entry 4: data on the node

# A cache is the acceptable use the catalogue names, so it is not here; the words are
# anchored so `nginx-cache` and `wal-e-bin` do not match on a syllable.
STATEFUL_VOLUME_NAME_RE = re.compile(r"(?<![a-z])(?:data|state|db|queue|store|journal|wal|persist)(?![a-z])", re.I)
# Where a node exposes Local SSD: Compute Engine formats and mounts Local SSD under
# /mnt/disks (cloud.google.com/compute/docs/disks/add-local-ssd#formatandmount); GKE's
# ephemeral Local SSD lives under /mnt/stateful_partition on Container-Optimized OS
# (docs.cloud.google.com/kubernetes-engine/docs/concepts/local-ssd). Data there "does not
# persist when the Pod or node is deleted, repaired, upgraded, or experiences an
# unrecoverable error" (the same page).
LOCAL_SSD_HOSTPATH_PREFIXES = ("/mnt/disks", "/mnt/stateful_partition")
# Local PersistentVolumes are statically provisioned; the StorageClass names this
# provisioner (kubernetes.io/docs/concepts/storage/storage-classes/#local, and GKE's
# Local SSD persistent-volume how-to, which uses the local volume static provisioner).
LOCAL_VOLUME_PROVISIONER = "kubernetes.io/no-provisioner"

# ------------------------------------------------------------------ entry 12: node labels

# Labels the kubelet still sets but Kubernetes has deprecated in favour of GA names
# (kubernetes.io/docs/reference/labels-annotations-taints/: the beta arch and os labels
# are "deprecated", instance-type and the failure-domain pair "starting in v1.17"; the arch
# and os deprecation is the Kubernetes 1.14 changelog's). A node image that stops setting
# one strands every pod selecting on it.
DEPRECATED_NODE_LABEL_PREFIXES = ("beta.kubernetes.io/", "failure-domain.beta.kubernetes.io/")
DEPRECATED_NODE_LABELS = {
    "beta.kubernetes.io/arch": ("kubernetes.io/arch", "1.14"),
    "beta.kubernetes.io/os": ("kubernetes.io/os", "1.14"),
    "beta.kubernetes.io/instance-type": ("node.kubernetes.io/instance-type", "1.17"),
    "failure-domain.beta.kubernetes.io/region": ("topology.kubernetes.io/region", "1.17"),
    "failure-domain.beta.kubernetes.io/zone": ("topology.kubernetes.io/zone", "1.17"),
}
# Labels (or label values) a minor dropped from the nodes that carried them, as
# (label, value or None for any value, (major, minor), source). kubeadm stopped setting
# `node-role.kubernetes.io/master` on new control planes in 1.24
# (kubernetes/kubernetes#107533, KEP-2067; GKE never set it on a worker node); GKE 1.24
# removed Docker node images, so no node at or above it carries
# `cloud.google.com/gke-container-runtime=docker`
# (docs.cloud.google.com/kubernetes-engine/docs/deprecations/docker-containerd). No GKE
# removal of a label with no value is verified (the catalogue's entry 12 says so).
DROPPED_NODE_LABELS = (
    ("node-role.kubernetes.io/master", None, (1, 24), "kubeadm 1.24 stops setting it (kubernetes/kubernetes#107533)"),
    ("cloud.google.com/gke-container-runtime", "docker", (1, 24), "GKE 1.24 removes Docker node images"),
)

# ------------------------------------------------------------------ entry 13: the runtime socket

CONTAINERD_SOCKET_PATH_PREFIXES = ("/run/containerd", "/var/run/containerd")

# ------------------------------------------------------------------ entries 14 and 15: cgroup v2

# The node pool's cgroup mode as `nodePools[].config.effectiveCgroupMode` reports it
# (GKE REST v1 NodeConfig), and the pin an operator sets through
# `nodePools[].config.linuxNodeConfig.cgroupMode`.
CGROUP_V2_MODE = "EFFECTIVE_CGROUP_MODE_V2"
CGROUP_V1_MODE = "EFFECTIVE_CGROUP_MODE_V1"
CGROUP_MODE_V1_PIN = "CGROUP_MODE_V1"
CGROUP_MODE_V2_PIN = "CGROUP_MODE_V2"
# GKE's cgroup v2 timeline (docs.cloud.google.com/kubernetes-engine/docs/how-to/migrate-cgroupv2):
# v2 is the default for new nodes from 1.26, GKE migrates v1 pools to v2 from 1.33, and
# removes v1 support at 1.35, which is where a pin to v1 ends.
CGROUP_V2_DEFAULT_MINOR = 26
CGROUP_V2_MIGRATION_MINOR = 33
CGROUP_V1_REMOVAL_MINOR = 35
# Runtimes that read their memory limit from cgroup v1 paths, by image repository and
# the first version that reads cgroup v2 (kubernetes.io/docs/concepts/architecture/cgroups/:
# OpenJDK/HotSpot "jdk8u372, 11.0.16, 15 and later"). Pinned tags only: `8-jre` names no
# update, so a rule grades it `unknown`.
JAVA_IMAGE_REPOS = ("eclipse-temurin", "openjdk", "adoptopenjdk")
JAVA_TAG_RE = re.compile(r"^(?:jdk-?|jre-?)?(\d+)(?:u(\d+)|\.(\d+)\.(\d+))")
# The bare major a floating tag carries (`openjdk:17`, `eclipse-temurin:8-jre`): enough to
# clear a major at or above the first one with cgroup v2 support, not enough to grade 8 or 11.
JAVA_MAJOR_TAG_RE = re.compile(r"^(?:jdk-?|jre-?)?(\d+)(?!\d)")
JDK8_CGROUP_V2_UPDATE = 372
JDK11_CGROUP_V2_PATCH = (0, 16)
JDK_FIRST_MAJOR_WITH_CGROUP_V2 = 15
# .NET read cgroup v2 from 5.0: dotnet/runtime#30337 ("Add cgroup v2 support to .NET Core",
# milestone 5.0.0) and its runtime change dotnet/runtime#34334; .NET Core 3.1 and 2.1 read
# cgroup v1 only. The collector's copy of this constant says (3, 1); this is the sourced value.
DOTNET_IMAGE_REPO_MARKERS = ("mcr.microsoft.com/dotnet/", "microsoft/dotnet")
DOTNET_TAG_RE = re.compile(r"^(\d+)\.(\d+)")
DOTNET_CGROUP_V2_VERSION = (5, 0)
# From Kubernetes 1.28 the kubelet sets memory.oom.group on every container on a cgroup v2
# node (kubernetes/kubernetes#117070); `singleProcessOOMKill` in the node system config
# turns it off, from GKE 1.32.4-gke.1132000 and 1.33.0-gke.1748000
# (docs.cloud.google.com/kubernetes-engine/docs/how-to/node-system-config). The REST record
# spells the field `linuxNodeConfig.singleProcessOomKill`.
GROUP_OOM_KILL_KUBELET_MINOR = 28
SINGLE_PROCESS_OOM_KILL_FIELD = "singleProcessOomKill"
# Entry 15 is a heuristic on the command and the image: the process count is not readable
# from the API. Programs whose job is to run several services in one container
# (supervisord, s6-overlay's s6-svscan, runit's runsvdir), a shell script that forks a
# background job (`a & b & wait`; `&&`, `2>&1` and `&>` are not forks), and images whose
# documented entrypoint is such a supervisor: phusion/baseimage and phusion/passenger run
# services under runit (github.com/phusion/baseimage-docker), webdevops/php-nginx and
# richarvey/nginx-php-fpm run nginx and PHP-FPM under supervisord (their READMEs).
SUPERVISOR_PROGRAMS = ("supervisord", "s6-svscan", "runsvdir")
SHELL_PROGRAMS = ("sh", "bash", "ash", "dash", "zsh")
SHELL_COMMAND_FLAG = "-c"
SHELL_FORK_RE = re.compile(r"(?<![&>\d])&(?![&>])")
MULTI_PROCESS_IMAGE_REPOS = ("phusion/baseimage", "phusion/passenger", "webdevops/php-nginx", "richarvey/nginx-php-fpm")

# ------------------------------------------------------------------ entry 17: node networking agents

# Labels that name the node image rather than the pool: the OS distribution (`cos`,
# `ubuntu`), which GKE's Autopilot selector allow-list names and every node carries, and
# the container runtime label of the Docker-to-containerd migration
# (docs.cloud.google.com/kubernetes-engine/docs/how-to/migrate-containerd).
NODE_IMAGE_LABELS = ("cloud.google.com/gke-os-distribution", "cloud.google.com/gke-container-runtime")
# Host paths that couple an agent to the node image: the kernel's module tree, and the
# network plugin's configuration and binary directories the kubelet reads
# (kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/network-plugins/:
# `/etc/cni/net.d` and `/opt/cni/bin`; GKE's node images keep their plugins under
# `/home/kubernetes/bin`).
KERNEL_MODULE_HOST_PATHS = ("/lib/modules", "/usr/lib/modules")
CNI_HOST_PATHS = ("/etc/cni/net.d", "/opt/cni/bin", "/home/kubernetes/bin")
KERNEL_MODULE_COMMANDS = ("modprobe", "insmod")

# ------------------------------------------------------------------ entry 18: GPU drivers

GPU_RESOURCE = "nvidia.com/gpu"
# A CUDA pin in an image tag (`nvidia/cuda:12.2.0-base`) or an env value.
CUDA_IMAGE_PIN_RE = re.compile(r"cuda[:/_-]?(\d+\.\d+)", re.I)
CUDA_ENV_NAME_MARKER = "CUDA"
VERSION_IN_TEXT_RE = re.compile(r"\d+\.\d+")
# The accelerator a template selects, and the pool record's fields for the accelerator and
# its driver install mode (GKE REST v1 NodeConfig.accelerators[]).
ACCELERATOR_LABEL = "cloud.google.com/gke-accelerator"
GPU_DRIVER_DEFAULT = "DEFAULT"
GPU_DRIVER_LATEST = "LATEST"
GPU_DRIVER_INSTALLATION_DISABLED = "INSTALLATION_DISABLED"
# Node image types as `nodePools[].config.imageType` spells them.
IMAGE_TYPE_COS_CONTAINERD = "COS_CONTAINERD"
IMAGE_TYPE_UBUNTU_CONTAINERD = "UBUNTU_CONTAINERD"
# The NVIDIA driver branch GKE installs per minor, (default, latest), from the GPU how-to
# (docs.cloud.google.com/kubernetes-engine/docs/how-to/gpus, "Install drivers
# automatically", read 2026-10-09). The page covers 1.26 to 1.33 for Container-Optimized
# OS and names the Ubuntu default from 1.31; a target outside a table is `unknown`.
GKE_COS_GPU_DRIVERS = {
    (1, 26): (470, 550),
    (1, 27): (470, 550),
    (1, 28): (535, 550),
    (1, 29): (535, 550),
    (1, 30): (535, 550),
    (1, 31): (535, 580),
    (1, 32): (535, 580),
    (1, 33): (535, 580),
}
GKE_UBUNTU_GPU_DRIVERS = {
    (1, 31): (535, 535),
    (1, 32): (535, 535),
    (1, 33): (535, 535),
}
GKE_GPU_DRIVERS_BY_IMAGE_TYPE = {
    IMAGE_TYPE_COS_CONTAINERD: GKE_COS_GPU_DRIVERS,
    IMAGE_TYPE_UBUNTU_CONTAINERD: GKE_UBUNTU_GPU_DRIVERS,
}
# The driver branch a CUDA major needs at all (NVIDIA CUDA Toolkit release notes, "CUDA
# Toolkit and Corresponding Driver Versions", with minor version compatibility: any 11.x
# on R450 and later, any 12.x on R525 and later, any 13.x on R580 and later), and the
# branch each toolkit's GA release names as its own minimum, below which a 12.x
# application runs under minor version compatibility and loses the features newer than
# the driver. Branches are the integer part of the driver version.
CUDA_MAJOR_MIN_DRIVER = {11: 450, 12: 525, 13: 580}
CUDA_TOOLKIT_MIN_DRIVER = {
    (11, 0): 450,
    (11, 1): 455,
    (11, 2): 460,
    (11, 3): 465,
    (11, 4): 470,
    (11, 5): 495,
    (11, 6): 510,
    (11, 7): 515,
    (11, 8): 520,
    (12, 0): 525,
    (12, 1): 530,
    (12, 2): 535,
    (12, 3): 545,
    (12, 4): 550,
    (12, 5): 555,
    (12, 6): 560,
    (12, 8): 570,
    (12, 9): 575,
    (13, 0): 580,
    (13, 1): 590,
    (13, 2): 595,
    (13, 3): 610,
    (13, 4): 615,
}

# ------------------------------------------------------------------ entry 20: retired registries

# Registry hosts that stopped publishing. `k8s.gcr.io` was frozen on 2023-04-03 and has
# redirected to registry.k8s.io since 2023-03-20 (kubernetes.io/blog/2023/02/06/
# k8s-gcr-io-freeze-announcement/, kubernetes.io/blog/2023/03/10/image-registry-redirect/);
# `gcr.io/google-containers` and its underscore spelling are the project the alias stood
# for until 2020-04-01 (kubernetes/kubernetes#55129, kubernetes/kubeadm#2051);
# `gcr.io/kubernetes-helm` served Helm 2's Tiller, unsupported since 2020-11-13
# (helm.sh/blog/helm-2-becomes-unsupported/). A rebuilt node has no image cache and pulls
# from the host again.
RETIRED_IMAGE_HOSTS = ("k8s.gcr.io/", "gcr.io/google-containers/", "gcr.io/google_containers/", "gcr.io/kubernetes-helm/")
RETIRED_IMAGE_HOST_NOTES = {
    "k8s.gcr.io/": "frozen 2023-04-03, a redirect to registry.k8s.io since 2023-03-20",
    "gcr.io/google-containers/": "the project behind k8s.gcr.io, read-only since 2020-04-01",
    "gcr.io/google_containers/": "the project behind k8s.gcr.io, read-only since 2020-04-01",
    "gcr.io/kubernetes-helm/": "Helm 2's registry, unsupported since 2020-11-13",
}
