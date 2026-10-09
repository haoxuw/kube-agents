"""
Entry 16 of docs/designs/upgrade-failure-catalogue.md: the network dataplane changes.

On the legacy dataplane a NetworkPolicy is enforced only while the network policy add-on
is on, and the add-on takes effect at the node rebuild an upgrade performs; Dataplane V2
enforces every policy, and a cluster moves to it only by being recreated. So the shape
that breaks is a legacy cluster with policy enforcement off and NetworkPolicy objects in
its namespaces: GKE's recommender files it as `NETWORK_POLICIES_UNRECONCILED`, and the
policies start to apply the moment enforcement turns on, at the rebuild that follows
enabling the add-on or on the Dataplane V2 cluster that replaces this one. The rule reads
`networkConfig.datapathProvider` (absent means legacy), the add-on
(`networkPolicy.enabled`, which `addonsConfig.networkPolicyConfig.disabled` turns off)
and the NetworkPolicy objects, and reports that shape as a risk naming the namespaces
and their policy counts, with the clusters in the run already on Dataplane V2 where a
rehearsal would enforce the same policies. A legacy cluster whose add-on enforces its
policies is a note; a Dataplane V2 cluster is nothing.

Sources: https://docs.cloud.google.com/kubernetes-engine/docs/concepts/dataplane-v2 (the
no-migration rule) and the recommender's network-policy insight,
https://docs.cloud.google.com/kubernetes-engine/docs/how-to/optimize-with-recommenders.
"""

from readiness_rules import (
    KIND_NETWORK_POLICY,
    LIST_SEPARATOR,
    items_of_kind,
    new_result,
)

RULE_ID = "network-dataplane"
ENTRY = 16

# `networkConfig.datapathProvider` values. A record without the field is on the legacy
# dataplane: the API sets the field only when Dataplane V2 was chosen.
DATAPATH_PROVIDER_PATH = ("networkConfig", "datapathProvider")
DATAPATH_ADVANCED = "ADVANCED_DATAPATH"
DATAPATH_LEGACY = "LEGACY_DATAPATH"
NETWORK_POLICY_PATH = ("networkPolicy", "enabled")
NETWORK_POLICY_ADDON_DISABLED_PATH = ("addonsConfig", "networkPolicyConfig", "disabled")
NAMESPACE_COUNT_FORMAT = "{namespace} ({count})"

FINDING_TEXT = (
    "legacy dataplane with policy enforcement off and NetworkPolicies in {namespaces}: the policies apply nowhere today "
    "and start to apply the moment enforcement turns on, at the node rebuild that follows enabling the network policy add-on "
    "or on a Dataplane V2 cluster{peers}; test what they deny before either"
)
PEERS_TEXT = " ({peers} already enforce them)"
NOTE_ENFORCED = "legacy dataplane; NetworkPolicies in {namespaces} are enforced by the network policy add-on"
UNKNOWN_READ = "cluster read failed, so its NetworkPolicies were not read"


def _get(record: dict, path: tuple) -> object:
    value = record
    for key in path:
        value = (value or {}).get(key) if isinstance(value, dict) else None
    return value


def datapath_provider(cluster: dict) -> str:
    return str(_get(cluster, DATAPATH_PROVIDER_PATH) or DATAPATH_LEGACY)


def policy_enforced(cluster: dict) -> bool:
    return bool(_get(cluster, NETWORK_POLICY_PATH)) and not bool(_get(cluster, NETWORK_POLICY_ADDON_DISABLED_PATH))


def policies_by_namespace(items) -> dict[str, int]:
    counts: dict[str, int] = {}
    for policy in items_of_kind(items, KIND_NETWORK_POLICY):
        namespace = (policy.get("metadata") or {}).get("namespace", "")
        counts[namespace] = counts.get(namespace, 0) + 1
    return dict(sorted(counts.items()))


def _namespaces_text(namespaces: dict[str, int]) -> str:
    return LIST_SEPARATOR.join(NAMESPACE_COUNT_FORMAT.format(namespace=ns, count=count) for ns, count in namespaces.items())


def evaluate(cluster: dict, member: dict, items, target, context) -> dict:
    out = new_result()
    if datapath_provider(cluster) == DATAPATH_ADVANCED:
        return out
    if items is None:
        out["unknown"].append(UNKNOWN_READ)
        return out
    namespaces = policies_by_namespace(items)
    if not namespaces:
        return out
    if policy_enforced(cluster):
        out["notes"].append(NOTE_ENFORCED.format(namespaces=_namespaces_text(namespaces)))
        return out
    name = cluster.get("name", "")
    peers = sorted(
        str(other.get("name", ""))
        for other in (context or {}).get("clusters") or []
        if isinstance(other, dict) and other is not cluster and other.get("name") != name and datapath_provider(other) == DATAPATH_ADVANCED
    )
    out["risks"].append({"datapath": datapath_provider(cluster), "enforced": False, "namespaces": namespaces, "policies": sum(namespaces.values()), "peers_on_v2": peers})
    return out


def describe(finding: dict) -> str:
    peers = PEERS_TEXT.format(peers=LIST_SEPARATOR.join(finding["peers_on_v2"])) if finding["peers_on_v2"] else ""
    return FINDING_TEXT.format(namespaces=_namespaces_text(finding["namespaces"]), peers=peers)
