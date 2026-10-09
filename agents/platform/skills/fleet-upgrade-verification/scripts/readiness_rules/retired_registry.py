"""
Entry 20, images on a retired registry: a rebuilt node has no image cache and pulls every
image again, and a host that stopped publishing (`RETIRED_IMAGE_HOSTS`) answers by
redirect today and not at all one day. Every template outside the system namespaces
whose container or init container names such a host is `blocking`, named with the image:
a node upgrade recreates its pods on rebuilt nodes. The detail names the pools below the
target, whose nodes this upgrade rebuilds, or says no pool is below it yet, in which case
the next node upgrade pulls the image again.
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "retired-registry"
ENTRY = 20
IMAGE_TEXT = "container {container} pulls {image} from {host} ({note})"
DETAIL = "{images}; a rebuilt node pulls it again; {pools}"
POOLS_BELOW_TEXT = "pool(s) {pools} are below the target and are rebuilt in this upgrade"
NO_POOL_BELOW_TEXT = "no pool is below the target; the next node upgrade pulls it again"
NO_TARGET_TEXT = "which pools are rebuilt needs a target"


def retired_host(image: str) -> str | None:
    for host in tables.RETIRED_IMAGE_HOSTS:
        if image.startswith(host):
            return host
    return None


def evaluate(cluster: dict, member: dict, items: list, target, context: dict) -> dict:
    result = rules.empty_result()
    for obj, spec, _ in rules.templates(items):
        images = []
        hosts = []
        for container in rules.containers(spec):
            image = container.get("image") or ""
            host = retired_host(image)
            if host is None:
                continue
            hosts.append(host)
            images.append(IMAGE_TEXT.format(container=container.get("name", ""), image=image, host=host.rstrip(rules.PATH_SEPARATOR), note=tables.RETIRED_IMAGE_HOST_NOTES.get(host, "")))
        if not images:
            continue
        pools = rules.pools_for_template(spec, context.get("pools") or [])
        below = rules.pools_below_target(pools, target)
        if target is None:
            pools_text = NO_TARGET_TEXT
        elif below:
            pools_text = POOLS_BELOW_TEXT.format(pools=rules.LIST_SEPARATOR.join(below))
        else:
            pools_text = NO_POOL_BELOW_TEXT
        detail = DETAIL.format(images=rules.LIST_SEPARATOR.join(images), pools=pools_text)
        result["blocking"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_BLOCKING, obj, detail, hosts=hosts, pools_below_target=below))
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)
