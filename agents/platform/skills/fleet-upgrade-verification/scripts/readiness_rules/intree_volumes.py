"""
Entry 19 of docs/designs/upgrade-failure-catalogue.md: in-tree volumes lose their CSI
path.

A PersistentVolume written against the in-tree `gcePersistentDisk` plugin attaches only
through CSI migration to the PD CSI driver, so on a cluster whose
`gcePersistentDiskCsiDriverConfig` add-on is off it cannot attach: the pod a node
upgrade reschedules stays Pending. The rule reads every PersistentVolume with an in-tree
source, the claim bound to it, and the pods and templates that mount that claim. With
the add-on off, a volume a workload mounts is blocking (the attach fails when the
drain moves the pod) and an unmounted one a risk; with the add-on on, every in-tree
volume is a risk, since it still depends on the migration shim rather than on the
driver.

Sources: https://kubernetes.io/blog/2022/09/26/storage-in-tree-to-csi-migration-status-update-1.25/
and https://cloud.google.com/kubernetes-engine/docs/how-to/persistent-volumes/gce-pd-csi-driver
"""

from readiness_rules import (
    KIND_PERSISTENT_VOLUME,
    LIST_SEPARATOR,
    active_pods,
    claim_names,
    get_path,
    items_of_kind,
    new_result,
    owner_label,
    pod_owner,
    template_specs,
)

RULE_ID = "in-tree-volumes"
ENTRY = 19

IN_TREE_SOURCE_KEY = "gcePersistentDisk"
DISK_NAME_KEY = "pdName"
CSI_ADDON_PATH = ("addonsConfig", "gcePersistentDiskCsiDriverConfig", "enabled")
CSI_DRIVER = "pd.csi.storage.gke.io"
CLAIM_FORMAT = "{namespace}/{name}"
NO_CLAIM = "unbound"

VOLUME_TEXT = "PersistentVolume {pv} (in-tree gcePersistentDisk {disk}, claim {claim}, {phase})"
BLOCKING_TEXT = "{volume} with the PD CSI driver add-on off: the attach fails when a drain moves {workloads}; enable the add-on"
RISK_ADDON_OFF_TEXT = "{volume} with the PD CSI driver add-on off: nothing mounts it now, and it cannot attach until the add-on is on"
RISK_ADDON_ON_TEXT = "{volume} attaches through CSI migration{workloads}; move it to {driver}"
RISK_WORKLOADS_TEXT = " for {workloads}"
PHASE_UNKNOWN = "phase unknown"
UNKNOWN_READ = "cluster read failed, so its PersistentVolumes were not read"


def csi_addon_enabled(cluster: dict) -> bool:
    return bool(get_path(cluster, CSI_ADDON_PATH))


def in_tree_volumes(items) -> list[dict]:
    return [pv for pv in items_of_kind(items, KIND_PERSISTENT_VOLUME) if isinstance((pv.get("spec") or {}).get(IN_TREE_SOURCE_KEY), dict)]


def claim_consumers(items, namespace: str, claim: str) -> list[str]:
    """Owners of the pods and templates that mount the claim, each once."""
    owners = set()
    for pod in active_pods(items):
        if (pod.get("metadata") or {}).get("namespace", "") == namespace and claim in claim_names(pod.get("spec") or {}):
            owners.add(owner_label(pod_owner(pod)))
    for owner, spec in template_specs(items):
        if owner["namespace"] == namespace and claim in claim_names(spec):
            owners.add(owner_label(owner))
    return sorted(owners)


def evaluate(cluster: dict, member: dict, items, target, context) -> dict:
    out = new_result()
    if items is None:
        out["unknown"].append(UNKNOWN_READ)
        return out
    addon_on = csi_addon_enabled(cluster)
    for pv in in_tree_volumes(items):
        meta = pv.get("metadata") or {}
        spec = pv.get("spec") or {}
        claim_ref = spec.get("claimRef") or {}
        claim = None
        workloads: list[str] = []
        if isinstance(claim_ref, dict) and claim_ref.get("name"):
            claim = CLAIM_FORMAT.format(namespace=claim_ref.get("namespace", ""), name=claim_ref.get("name", ""))
            workloads = claim_consumers(items, claim_ref.get("namespace", ""), claim_ref.get("name", ""))
        finding = {
            "pv": meta.get("name", ""),
            "disk": (spec.get(IN_TREE_SOURCE_KEY) or {}).get(DISK_NAME_KEY, ""),
            "claim": claim,
            "phase": (pv.get("status") or {}).get("phase"),
            "workloads": workloads,
            "addon_enabled": addon_on,
        }
        if not addon_on and workloads:
            out["blocking"].append(finding)
        else:
            out["risks"].append(finding)
    return out


def describe(finding: dict) -> str:
    volume = VOLUME_TEXT.format(pv=finding["pv"], disk=finding["disk"], claim=finding["claim"] or NO_CLAIM, phase=finding["phase"] or PHASE_UNKNOWN)
    workloads = LIST_SEPARATOR.join(finding["workloads"])
    if not finding["addon_enabled"]:
        if workloads:
            return BLOCKING_TEXT.format(volume=volume, workloads=workloads)
        return RISK_ADDON_OFF_TEXT.format(volume=volume)
    return RISK_ADDON_ON_TEXT.format(volume=volume, workloads=RISK_WORKLOADS_TEXT.format(workloads=workloads) if workloads else "", driver=CSI_DRIVER)
