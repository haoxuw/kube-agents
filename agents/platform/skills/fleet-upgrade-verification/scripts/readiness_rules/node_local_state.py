"""
Entry 4, data on the node: a rebuilt node is a new machine, and what a workload kept on
the old one is gone. Deployments and StatefulSets whose template mounts an `emptyDir`, a
`hostPath`, or a claim on a local StorageClass (`kubernetes.io/no-provisioner`, which is
how Local SSD becomes a PersistentVolume) are a `risk`, named with the volume; a
StatefulSet is the strongest case, because its identity survives the rebuild and its
node-local data does not. DaemonSets (per-node agents by design) and CronJobs (transient
pods) are not read; the volumes GKE's own components use are in system namespaces, which
no rule reads.
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "data-on-the-node"
ENTRY = 4
KINDS = ("Deployment", "StatefulSet")
KIND_PVC = "PersistentVolumeClaim"
KIND_STORAGE_CLASS = "StorageClass"
VOLUME_EMPTY_DIR = "emptyDir"
VOLUME_HOST_PATH = "hostPath"
VOLUME_CLAIM = "persistentVolumeClaim"
MEDIUM_MEMORY = "Memory"
EMPTY_DIR_TEXT = "{name} (emptyDir{medium}{stateful})"
EMPTY_DIR_MEMORY_TEXT = ", medium Memory"
HOST_PATH_TEXT = "{name} (hostPath {path}{ssd}{stateful})"
HOST_PATH_SSD_TEXT = ", a Local SSD mount"
CLAIM_TEXT = "{name} (claim {claim} on local StorageClass {storage_class})"
CLAIM_TEMPLATE_TEXT = "{name} (volumeClaimTemplate on local StorageClass {storage_class})"
STATEFUL_NAME_TEXT = "; the name suggests state"
DETAIL = "keeps data on the node in {volumes}; a node rebuild loses it{statefulset}"
STATEFULSET_TEXT = ", and a StatefulSet's identity survives the rebuild while its node-local data does not"


def _local_storage_classes(items: list) -> set[str]:
    return {
        (sc.get("metadata") or {}).get("name", "")
        for sc in rules.objects_of_kind(items, KIND_STORAGE_CLASS)
        if sc.get("provisioner") == tables.LOCAL_VOLUME_PROVISIONER
    }


def _claims(items: list) -> dict[tuple[str, str], dict]:
    out = {}
    for pvc in rules.objects_of_kind(items, KIND_PVC):
        meta = pvc.get("metadata") or {}
        out[(meta.get("namespace", ""), meta.get("name", ""))] = pvc
    return out


def _stateful(name: str) -> str:
    return STATEFUL_NAME_TEXT if tables.STATEFUL_VOLUME_NAME_RE.search(name or "") else ""


def node_local_volumes(obj: dict, spec: dict, workload: dict, local_classes: set[str], claims: dict) -> list[str]:
    """Every volume of one template that lives on the node, described."""
    out = []
    for volume in spec.get("volumes") or []:
        if not isinstance(volume, dict):
            continue
        name = volume.get("name") or ""
        if VOLUME_EMPTY_DIR in volume:
            medium = EMPTY_DIR_MEMORY_TEXT if (volume.get(VOLUME_EMPTY_DIR) or {}).get("medium") == MEDIUM_MEMORY else ""
            out.append(EMPTY_DIR_TEXT.format(name=name, medium=medium, stateful=_stateful(name)))
        elif VOLUME_HOST_PATH in volume:
            path = (volume.get(VOLUME_HOST_PATH) or {}).get("path") or ""
            ssd = HOST_PATH_SSD_TEXT if path.startswith(tables.LOCAL_SSD_HOSTPATH_PREFIXES) else ""
            out.append(HOST_PATH_TEXT.format(name=name, path=path, ssd=ssd, stateful=_stateful(name)))
        elif VOLUME_CLAIM in volume:
            claim_name = (volume.get(VOLUME_CLAIM) or {}).get("claimName") or ""
            claim = claims.get((obj["namespace"], claim_name)) or {}
            storage_class = (claim.get("spec") or {}).get("storageClassName")
            if storage_class in local_classes:
                out.append(CLAIM_TEXT.format(name=name, claim=claim_name, storage_class=storage_class))
    for template in (workload.get("spec") or {}).get("volumeClaimTemplates") or []:
        if not isinstance(template, dict):
            continue
        storage_class = (template.get("spec") or {}).get("storageClassName")
        if storage_class in local_classes:
            out.append(CLAIM_TEMPLATE_TEXT.format(name=(template.get("metadata") or {}).get("name", ""), storage_class=storage_class))
    return out


def evaluate(cluster: dict, member: dict, items: list, target, context: dict) -> dict:
    result = rules.empty_result()
    local_classes = _local_storage_classes(items)
    claims = _claims(items)
    for obj, spec, workload in rules.templates(items, KINDS):
        volumes = node_local_volumes(obj, spec, workload, local_classes, claims)
        if not volumes:
            continue
        statefulset = STATEFULSET_TEXT if obj["kind"] == rules.KIND_STATEFULSET else ""
        detail = DETAIL.format(volumes=rules.LIST_SEPARATOR.join(volumes), statefulset=statefulset)
        result["risks"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, detail, volumes=volumes))
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)
