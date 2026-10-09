"""
upgrade_shape_tables.py — fact tables about what a GKE version ships, shared by the
readiness rules and anything else that reasons about an upgrade's shape.

Each table names its source beside it. A table is appended to, never rewritten: the
retrospective's collector and the readiness rules import the same names, so a constant
keeps its name and its shape once it exists. Nothing here reads a cluster or runs a
command.
"""

# The containerd major a node image ships at a GKE minor, by node operating system
# family. GKE: "Linux nodes that run GKE 1.32 or earlier, with containerd node images,
# use containerd 1.7 or earlier versions"; "Linux nodes that run GKE 1.33 use containerd
# 2.0"; "Windows Server nodes that run GKE 1.34 or earlier ... use containerd 1.7 or
# earlier versions"; "Windows Server nodes that run GKE 1.35 use containerd 2.0".
# Source: https://docs.cloud.google.com/kubernetes-engine/docs/deprecations/migrate-containerd-2
# The table is the target side of a comparison. For the current side, read the node's
# own `status.nodeInfo.containerRuntimeVersion` where the nodes were read: the
# upgrade-failure reproduction (docs/designs/upgrade-failure-reproductions.md, entry 13)
# saw a patch inside 1.31 move a node from containerd 1.7.34 to 2.0.10, so a running
# node can be ahead of what the table says for its minor.
NODE_OS_LINUX = "linux"
NODE_OS_WINDOWS = "windows"
CONTAINERD_MAJOR_1 = 1
CONTAINERD_MAJOR_2 = 2
# (first minor that ships the major, major), ascending; a minor below the first row ships
# the row's predecessor, here containerd 1.
CONTAINERD_MAJOR_BY_GKE_MINOR = {
    NODE_OS_LINUX: ((1, 33), CONTAINERD_MAJOR_2),
    NODE_OS_WINDOWS: ((1, 35), CONTAINERD_MAJOR_2),
}
CONTAINERD_MAJOR_BEFORE_TABLE = CONTAINERD_MAJOR_1
# The node operating system family of each GKE node image type, as `nodePools[].config.imageType`
# spells them (upper case). Every supported type runs containerd; the Docker-based types
# (`COS`, `UBUNTU`, `WINDOWS_LTSC`) left with 1.24.
# Source: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/node-images
NODE_IMAGE_OS_FAMILY = {
    "COS_CONTAINERD": NODE_OS_LINUX,
    "UBUNTU_CONTAINERD": NODE_OS_LINUX,
    "COS": NODE_OS_LINUX,
    "UBUNTU": NODE_OS_LINUX,
    "WINDOWS_LTSC_CONTAINERD": NODE_OS_WINDOWS,
    "WINDOWS_SAC_CONTAINERD": NODE_OS_WINDOWS,
    "WINDOWS_LTSC": NODE_OS_WINDOWS,
    "WINDOWS_SAC": NODE_OS_WINDOWS,
}
# An image type the table does not name (a new one, or an empty field on an Autopilot
# record) is read as Linux, the family every GKE node pool defaults to.
NODE_IMAGE_DEFAULT_OS_FAMILY = NODE_OS_LINUX


def containerd_major_at(minor: tuple[int, int], os_family: str) -> int:
    """The containerd major the node image ships at `minor` for `os_family`."""
    first_minor, major = CONTAINERD_MAJOR_BY_GKE_MINOR[os_family]
    return major if minor >= first_minor else CONTAINERD_MAJOR_BEFORE_TABLE


def node_image_os_family(image_type: str) -> str:
    return NODE_IMAGE_OS_FAMILY.get(str(image_type or "").upper(), NODE_IMAGE_DEFAULT_OS_FAMILY)
