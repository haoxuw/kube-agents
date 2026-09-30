#!/usr/bin/env python3
# agent_roster.py - Which specialist profiles exist, and what each is for.
#
# The Chat Agent needs this to pick an `assignee` before it delegates, and it
# needs it on essentially every request. Two consumers read it:
#
#   * ``router_server.py``'s ``list_agents`` MCP tool — the on-demand refresh
#     path, for when a named agent is missing or the injected roster is stale.
#   * the ``agent_roster`` plugin (``agents/chat/defaults/plugins/agent_roster``)
#     — injects the roster into every turn's context, so the common case costs
#     no tool call at all. Measured on the live deployment, calling the tool cost
#     a full LLM roundtrip (~6s of a ~17s acknowledgement) to re-read what is, in
#     the end, a directory listing.
#
# Both must describe the same fleet in the same words, which is the reason this
# lives in its own module rather than being duplicated into the plugin: a roster
# that disagrees with itself between the injected block and the tool is worse
# than either one alone.
#
# Everything here degrades rather than raises. The profiles live on a shared
# PVC, so an I/O or permission fault must not turn routing into a traceback.

import importlib.util
import os
import sys

from pathlib import Path
from types import ModuleType

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
# Hermes stores each profile at $HERMES_HOME/profiles/<name> (persists on the data PVC).
PROFILES_BASE = HERMES_HOME / "profiles"
# The front door itself; never a valid delegation target.
SELF_PROFILE = "default"
# The Platform Agent: scaffolded at pod start from the image template, and the roster's
# default specialist. Routable whenever its home exists — the scaffold artifacts checked
# below are a Cluster Agent's (RESERVED_PROFILES in cluster_agent_profile.py names both).
PLATFORM_PROFILE = "platform"
# A dot-prefixed entry (a live install carried a `.deleted` directory beside its profiles)
# is never a profile; no sanitized cluster name or reserved name starts with one.
HIDDEN_PREFIX = "."
# The scaffold-readiness rule — PROFILE_MARKER, written by `hermes profile create`, and
# SCAFFOLD_ARTIFACTS, written last by create_profile — lives in profile_scaffold.py. It is
# loaded by path rather than imported. On the pod it sits beside this script, because the
# image copies agents/chat/scripts/ and agents/platform/scripts/ into one directory; that
# directory is on the writable data PVC, and putting it on the gateway's sys.path would let
# a file written there shadow a lazy import in the front door. The second entry is the
# repository layout, for the tests.
SCAFFOLD_MODULE_NAME = "profile_scaffold.py"
SCAFFOLD_MODULE_DIRS = (
    Path(__file__).resolve().parent,
    Path(__file__).resolve().parent.parent.parent / "platform" / "scripts",
)
SCAFFOLD_IMPORT_NAME = "_kube_agents_profile_scaffold"

EMPTY_ROSTER = "No specialist agents are currently available to route to."
# Discovery failed. Deliberately not EMPTY_ROSTER: an unreadable profiles
# directory must not be reported to the Chat Agent as an empty fleet.
UNKNOWN_ROSTER = (
    "The specialist roster could not be read right now. Do not conclude that no "
    "agents exist; retry, and say so plainly if it keeps failing."
)
NO_DESCRIPTION = "(no description provided)"


def log(msg: str) -> None:
    print(f"[AGENT-ROSTER] {msg}", file=sys.stderr)


# Cached for the process, the failure included: the entrypoint copies the scripts
# before the gateway starts, so a file missing on the first turn stays missing, and
# retrying would log the same line ahead of every turn.
_scaffold_rule: ModuleType | None = None
_scaffold_rule_loaded = False


def _load_scaffold_rule() -> ModuleType | None:
    """Load profile_scaffold.py by path; ``None`` when no copy can be loaded."""
    global _scaffold_rule, _scaffold_rule_loaded
    if _scaffold_rule_loaded:
        return _scaffold_rule
    _scaffold_rule_loaded = True
    found = False
    for base in SCAFFOLD_MODULE_DIRS:
        path = base / SCAFFOLD_MODULE_NAME
        try:
            # is_file() inside the try: pathlib re-raises an EACCES/EIO stat on the PVC.
            if not path.is_file():
                continue
            found = True
            spec = importlib.util.spec_from_file_location(SCAFFOLD_IMPORT_NAME, path)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as e:  # noqa: BLE001 - degrade; this runs ahead of every turn
            log(f"could not load the scaffold rule from {path}: {e}")
            continue
        _scaffold_rule = module
        return module
    where = [str(b) for b in SCAFFOLD_MODULE_DIRS]
    if found:
        log(f"{SCAFFOLD_MODULE_NAME} found under {where} but no copy loaded; only {PLATFORM_PROFILE} is routable")
    else:
        log(f"no {SCAFFOLD_MODULE_NAME} under {where}; only {PLATFORM_PROFILE} is routable")
    return None


def _is_routable(home: Path) -> bool:
    """Whether a card assigned to this profile would be dispatched and served.

    A directory under profiles/ is not a specialist. The kubelet leaves plugin mount
    points there; ``hermes profile create`` from a terminal leaves a registered but bare
    Hermes profile; a scaffold the bootstrap gate killed leaves a registered one with no
    USER.md, whose worker blocks at preflight. Listing any of them offers it as an
    assignee, described by whatever its SOUL.md says — and a bare Hermes profile's says
    it is a general assistant, which is the target a front door forbidden from doing the
    work itself picks for anything off-topic. Same rule as platform_mcp_server._is_ready,
    so the two rosters agree on who exists. Raises on an unreadable home; discover()
    skips that one profile.
    """
    if home.name.startswith(HIDDEN_PREFIX):
        return False
    if home.name == PLATFORM_PROFILE:
        return True
    rule = _load_scaffold_rule()
    return rule is not None and rule.is_ready(home)


def _summarize_soul(home: Path) -> str:
    """Fallback responsibilities: the first prose line of the profile's SOUL.md."""
    soul = home / "SOUL.md"
    # is_file() is inside the try: pathlib only swallows ENOENT/ENOTDIR/EBADF/ELOOP,
    # so a stat() that fails with EACCES or EIO on the shared PVC raises here too.
    try:
        if not soul.is_file():
            return ""
        lines = soul.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("---"):
            continue
        return s
    return ""


def _responsibilities(home: Path) -> str:
    """A one-shot description of what a profile is responsible for.

    Prefers an explicit CAPABILITIES.md (the routing contract a specialist
    advertises to the Chat Agent); falls back to the SOUL.md summary so a
    profile is still discoverable even without a capabilities file.
    """
    cap = home / "CAPABILITIES.md"
    try:
        text = cap.read_text(encoding="utf-8", errors="replace").strip() if cap.is_file() else ""
    except OSError:
        text = ""
    if text:
        return text
    return _summarize_soul(home)


def discover(base: Path | None = None) -> list[dict[str, str]] | None:
    """Enumerate every routable specialist: `platform`, and each Cluster Agent whose
    scaffold finished (``_is_routable``). `default` is the front door itself.

    Degrades rather than raises. Errors are isolated per profile: one unreadable
    directory costs that one agent, not the whole roster — it is left out, since a
    home that cannot be read cannot be shown to be one a card would reach.

    Returns ``None`` — distinct from ``[]`` — when the profiles directory itself
    could not be listed. "I could not read the fleet" and "there is no fleet"
    are different answers: the second is a fact the Chat Agent should act on,
    the first must not be reported as one.

    ``base`` defaults to the module-level ``PROFILES_BASE`` at call time rather
    than at import time, so a caller (or a test) that rebinds ``PROFILES_BASE``
    is still honoured.
    """
    root = PROFILES_BASE if base is None else base
    agents: list[dict[str, str]] = []
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError as e:
        log(f"cannot list profiles at {root}: {e}")
        return None
    for p in entries:
        try:
            if not p.is_dir() or p.name == SELF_PROFILE or not _is_routable(p):
                continue
            agents.append({"name": p.name, "responsibilities": _responsibilities(p)})
        except OSError as e:
            log(f"skipping unreadable profile {p.name}: {e}")
    return agents


def format_roster(agents: list[dict[str, str]] | None) -> str | None:
    """Render the roster the Chat Agent reads, or ``None`` if it is unknowable.

    Agents sharing an identical role description are grouped so the description
    is stated once instead of repeated verbatim per agent — every Cluster Agent
    is scaffolded from the same template, so a fleet of thirty would otherwise
    be thirty copies of one paragraph. Assignee names are always listed
    individually, because the name is the part the caller has to get exactly
    right.

    ``None`` in (discovery failed) is ``None`` out; callers decide how to say
    "unknown", which is not the same thing as ``EMPTY_ROSTER``.
    """
    if agents is None:
        return None
    if not agents:
        return EMPTY_ROSTER

    groups: dict[str, list[str]] = {}
    # A missing description is an absence, not a shared role: grouping on the
    # placeholder would announce unrelated profiles as one interchangeable
    # fleet, and the "pick the one whose cluster you need" preamble would be a
    # lie about agents nothing is known of. Each gets its own line.
    undescribed: list[str] = []
    for a in agents:
        desc = a["responsibilities"]
        if desc:
            groups.setdefault(desc, []).append(a["name"])
        else:
            undescribed.append(a["name"])

    # Distinct specialists first, then the shared-role fleets — the front door routes to a
    # named specialist far more often than to a specific cluster. The undescribed trail
    # both: they are the least useful entries to route from.
    blocks: list[str] = []
    for desc, names in sorted(groups.items(), key=lambda kv: len(kv[1])):
        if len(names) == 1:
            blocks.append(f"- {names[0]}: {desc}")
            continue
        listed = "\n".join(f"  - {n}" for n in names)
        blocks.append(
            f"The following {len(names)} agents share one role — pick the one whose cluster "
            f"you need:\n{listed}\n  Shared role: {desc}"
        )
    blocks.extend(f"- {name}: {NO_DESCRIPTION}" for name in undescribed)
    return "\n\n".join(blocks)


def render(base: Path | None = None) -> str | None:
    """Discover and format in one call — what both consumers actually want.

    ``None`` means the roster could not be read at all. Both consumers treat
    that as "say nothing about the fleet" rather than "the fleet is empty".
    """
    return format_roster(discover(base))
