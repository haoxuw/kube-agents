"""
Entry 15, the OOM killer kills the whole container: from Kubernetes 1.28 the kubelet sets
`memory.oom.group` on every container on a cgroup v2 node, so a container running
several processes loses all of them where it used to lose one. This rule is a heuristic
on the command and the image, marked as such in its findings, because the process count
is not readable from the API: a supervisor program in the command or args
(`SUPERVISOR_PROGRAMS`), a shell `-c` script that forks a background job, or an image
whose documented entrypoint is a supervisor (`MULTI_PROCESS_IMAGE_REPOS`). The shape is a
`risk` when the target turns the group kill on for the pool: the pool moves to cgroup v2
at a target of 1.28 or later, or a cgroup v2 pool's kubelet crosses 1.28; it is never
`blocking`. A pool already group-killing today, or one whose node config sets
`singleProcessOOMKill`, is a note; a pool whose cgroup mode is unread is `unknown`.
"""

import readiness_rules as rules
import upgrade_shape_tables as tables

RULE_ID = "group-oom-kill"
ENTRY = 15
HEURISTIC_TEXT = "heuristic: the process count is not readable from the API"
SUPERVISOR_SHAPE = "runs {program}"
SHELL_FORK_SHAPE = "runs a shell script that forks a background job"
IMAGE_SHAPE = "runs {repository}, whose entrypoint is a supervisor"
RISK_MOVES_DETAIL = "container {container} {shape} ({heuristic}) on pool {pool}, which {reason} at the target; a kubelet at {target} then sets memory.oom.group and an OOM kill takes every process in the container"
RISK_CROSSES_DETAIL = "container {container} {shape} ({heuristic}) on pool {pool}, on cgroup v2 with a kubelet at {pool_version}; from 1.{minor} the kubelet sets memory.oom.group and an OOM kill takes every process in the container"
UNKNOWN_DETAIL = "container {container} {shape} ({heuristic}) on pool {pool}: {reason}"
NOTE_ALREADY = "{rule}: {kind} {object} container {container} {shape} on pool {pool}, on cgroup v2 with a kubelet at {pool_version}, which already group-kills; not an upgrade risk"
NOTE_SINGLE_PROCESS = "{rule}: {kind} {object} container {container} {shape} on pool {pool}, whose node config sets singleProcessOOMKill; not an upgrade risk"
NOTE_STAYS = "{rule}: {kind} {object} container {container} {shape} on pool {pool}; {reason}; not a risk at this target"
NOTE_BELOW_KUBELET = "{rule}: {kind} {object} container {container} {shape} on pool {pool}; the target {target} is below 1.{minor}, where the kubelet starts the group kill; not a risk at this target"
# A shell `-c` takes its script as the next word.
SHELL_SCRIPT_OFFSET = 2


def _program(word: str) -> str:
    return word.rsplit(rules.PATH_SEPARATOR, 1)[-1]


def _repository(image: str) -> str:
    """`docker.io/phusion/baseimage:focal` -> `docker.io/phusion/baseimage`."""
    name = image.partition(rules.IMAGE_DIGEST_SEPARATOR)[0]
    path, sep, tag = name.rpartition(rules.IMAGE_TAG_SEPARATOR)
    return path if sep and rules.PATH_SEPARATOR not in tag else name


def multi_process_shape(container: dict) -> str | None:
    """Why the container looks multi-process, or None."""
    command = [str(w) for w in (container.get("command") or []) + (container.get("args") or [])]
    programs = [_program(w) for w in command]
    for program in programs:
        if program in tables.SUPERVISOR_PROGRAMS:
            return SUPERVISOR_SHAPE.format(program=program)
    for index, program in enumerate(programs):
        script_index = index + SHELL_SCRIPT_OFFSET
        if program in tables.SHELL_PROGRAMS and script_index < len(command) and command[index + 1] == tables.SHELL_COMMAND_FLAG:
            if tables.SHELL_FORK_RE.search(command[script_index]):
                return SHELL_FORK_SHAPE
    repository = _repository(container.get("image") or "")
    for repo in tables.MULTI_PROCESS_IMAGE_REPOS:
        if repository == repo or repository.endswith(rules.PATH_SEPARATOR + repo):
            return IMAGE_SHAPE.format(repository=repo)
    return None


def _grade(pool: dict, target, transition: str) -> str:
    """Which text a pool gets: a risk form, a note form, or the unknown form."""
    linux = rules.pool_config(pool).get("linuxNodeConfig") or {}
    if linux.get(tables.SINGLE_PROCESS_OOM_KILL_FIELD) is True:
        return NOTE_SINGLE_PROCESS
    if transition == rules.CGROUP_UNKNOWN:
        return UNKNOWN_DETAIL
    if transition == rules.CGROUP_STAYS_V1:
        return NOTE_STAYS
    if target is not None and target[1] < tables.GROUP_OOM_KILL_KUBELET_MINOR:
        return NOTE_BELOW_KUBELET
    if transition == rules.CGROUP_MOVES:
        return RISK_MOVES_DETAIL
    parsed = pool.get("parsed")
    if parsed is not None and target is not None and parsed[1] < tables.GROUP_OOM_KILL_KUBELET_MINOR:
        return RISK_CROSSES_DETAIL
    return NOTE_ALREADY


def evaluate(cluster: dict, member: dict, items: list, target, context: dict) -> dict:
    result = rules.empty_result()
    for obj, spec, _ in rules.templates(items):
        for container in rules.containers(spec):
            shape = multi_process_shape(container)
            if shape is None:
                continue
            container_name = container.get("name", "")
            for pool in rules.pools_for_template(spec, context.get("pools") or []):
                transition, reason = rules.cgroup_transition(pool, target)
                pool_name = pool.get("name", "")
                form = _grade(pool, target, transition)
                text = form.format(
                    rule=RULE_ID, kind=obj["kind"], object=obj["object"], container=container_name, shape=shape, heuristic=HEURISTIC_TEXT,
                    pool=pool_name, reason=reason, pool_version=pool.get("version"), target=rules.minor_text(target), minor=tables.GROUP_OOM_KILL_KUBELET_MINOR,
                )
                if form in (RISK_MOVES_DETAIL, RISK_CROSSES_DETAIL):
                    result["risks"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_RISK, obj, text, heuristic=True, container=container_name, shape=shape, pool=pool_name))
                elif form == UNKNOWN_DETAIL:
                    result["unknown"].append(rules.finding(RULE_ID, ENTRY, rules.GRADE_UNKNOWN, obj, text, heuristic=True, container=container_name, shape=shape, pool=pool_name))
                else:
                    result["notes"].append(text)
    return result


def describe(entry: dict) -> str:
    return rules.describe(entry)
