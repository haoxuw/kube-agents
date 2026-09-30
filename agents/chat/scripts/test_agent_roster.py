"""Tests for the shared specialist roster.

This logic used to live in ``router_server.py`` and be reachable only through
the ``list_agents`` MCP tool. It moved here when the roster started being
injected into every turn as well, so both consumers describe the same fleet in
the same words; the tests moved with it. ``test_router_server.py`` now only
checks that the tool still delegates here.
"""

import os
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

# Add the directory containing agent_roster.py to sys.path so it can be imported.
sys.path.insert(0, str(Path(__file__).parent.absolute()))

import agent_roster  # noqa: E402

# The readiness rule the roster applies, loaded the way the roster loads it: from the
# repository's platform scripts. The tests write the markers it names rather than
# literal filenames so a renamed marker cannot leave them green against a roster that
# lists nobody.
RULE = agent_roster._load_scaffold_rule()


def scaffold(base, name):
    """A Cluster Agent home as create_profile leaves it: registered, scaffold finished."""
    home = base / name
    home.mkdir(parents=True, exist_ok=True)
    (home / RULE.PROFILE_MARKER).write_text("")
    for artifact in RULE.SCAFFOLD_ARTIFACTS:
        (home / artifact).write_text("")
    return home


class TestDiscovery(unittest.TestCase):
    """The roster lists `platform` and every Cluster Agent whose scaffold finished."""

    def _with_profiles(self, tmp, names):
        base = Path(tmp) / "profiles"
        for name in names:
            if name in (agent_roster.SELF_PROFILE, agent_roster.PLATFORM_PROFILE):
                (base / name).mkdir(parents=True)
            else:
                scaffold(base, name)
        agent_roster.PROFILES_BASE = base
        return base

    def test_excludes_default_and_lists_specialists(self):
        with TemporaryDirectory() as tmp:
            base = self._with_profiles(tmp, ["default", "platform", "cluster-a"])
            # A CAPABILITIES.md is the preferred description source.
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            # SOUL.md is the fallback when no CAPABILITIES.md exists.
            (base / "cluster-a" / "SOUL.md").write_text("# Title\n\nRead-only cluster diagnostics.\n")

            out = agent_roster.render()
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertIn("- cluster-a: Read-only cluster diagnostics.", out)
            self.assertNotIn("default", out)

    def test_empty_when_no_specialists(self):
        with TemporaryDirectory() as tmp:
            self._with_profiles(tmp, ["default"])
            self.assertIn("No specialist agents", agent_roster.render())

    def test_an_explicit_base_overrides_the_module_default(self):
        # The injecting plugin passes its own resolved path rather than relying
        # on the module picking up the right HERMES_HOME at import time.
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "elsewhere"
            (base / "platform").mkdir(parents=True)
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet work.")
            agent_roster.PROFILES_BASE = Path(tmp) / "does-not-exist"
            self.assertIn("- platform: Fleet work.", agent_roster.render(base))


class TestReadiness(unittest.TestCase):
    """A directory under profiles/ is not a specialist until a card assigned to it would run.

    The rule is the Platform Agent's own (platform_mcp_server._is_ready): registered by
    `hermes profile create` and carrying every artifact create_profile writes last.
    """

    def _base(self, tmp):
        base = Path(tmp) / "profiles"
        (base / "default").mkdir(parents=True)
        (base / "platform").mkdir()
        (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
        agent_roster.PROFILES_BASE = base
        return base

    def test_a_directory_that_is_not_a_finished_scaffold_is_left_out(self):
        with TemporaryDirectory() as tmp:
            base = self._base(tmp)
            # A plugin mount point the kubelet left behind.
            (base / "stray").mkdir()
            # `hermes profile create testing-profile` from a terminal: a bare Hermes
            # profile whose SOUL.md describes a general assistant — the one target a
            # front door forbidden from doing the work itself picks for anything
            # off-topic, which is how a gardening request got delegated to it.
            (base / "testing-profile").mkdir()
            (base / "testing-profile" / "SOUL.md").write_text(
                "You are Hermes Agent, built by Nous Research. Be direct.\n")
            # Hermes' own bin for removed profiles, a finished scaffold inside it.
            scaffold(base / ".deleted", "cluster-old")
            (base / ".deleted" / "cluster-old" / "CAPABILITIES.md").write_text("Old cluster.")

            out = agent_roster.render()

            self.assertIn("- platform: Fleet + GitOps write path.", out)
            for name in ("stray", "testing-profile", ".deleted", "cluster-old", "Hermes Agent"):
                self.assertNotIn(name, out)

    def test_a_scaffold_that_stopped_early_appears_once_it_finishes(self):
        # create_profile registers and stamps the profile before it fetches the
        # credential and writes USER.md; a worker handed a card in that window blocks
        # at preflight. The roster is re-read every turn, so the moment the scaffold
        # finishes the agent is routable.
        with TemporaryDirectory() as tmp:
            base = self._base(tmp)
            home = base / "cluster-a"
            home.mkdir()
            (home / RULE.PROFILE_MARKER).write_text("")
            (home / "CAPABILITIES.md").write_text("Read-only diagnostics for cluster a.")
            self.assertNotIn("cluster-a", agent_roster.render())

            for artifact in RULE.SCAFFOLD_ARTIFACTS:
                (home / artifact).write_text("")
            self.assertIn("- cluster-a: Read-only diagnostics for cluster a.", agent_roster.render())

    def test_platform_needs_no_scaffold_artifacts(self):
        # The Platform Agent is scaffolded from the image template at pod start and
        # never carries a Cluster Agent's USER.md; its home existing is the whole test.
        with TemporaryDirectory() as tmp:
            base = self._base(tmp)
            self.assertFalse((base / "platform" / RULE.PROFILE_MARKER).exists())
            self.assertIn("- platform: Fleet + GitOps write path.", agent_roster.render())

    def test_without_the_rule_only_platform_is_routable(self):
        # profile_scaffold.py missing beside the roster is a broken image, not a
        # reason to offer every directory as an assignee again: the default
        # specialist still routes, and nothing unproven does.
        with TemporaryDirectory() as tmp:
            base = self._base(tmp)
            scaffold(base, "cluster-a")
            (base / "cluster-a" / "CAPABILITIES.md").write_text("Diagnostics for cluster a.")
            saved = agent_roster._scaffold_rule, agent_roster._scaffold_rule_loaded
            agent_roster._scaffold_rule, agent_roster._scaffold_rule_loaded = None, True
            try:
                out = agent_roster.render()
            finally:
                agent_roster._scaffold_rule, agent_roster._scaffold_rule_loaded = saved

            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertNotIn("cluster-a", out)

    def _with_rule_dirs(self, dirs):
        """Point the loader at `dirs` with a cold cache; restore both on exit."""
        saved = (agent_roster.SCAFFOLD_MODULE_DIRS, agent_roster._scaffold_rule,
                 agent_roster._scaffold_rule_loaded)
        agent_roster.SCAFFOLD_MODULE_DIRS = tuple(dirs)
        agent_roster._scaffold_rule, agent_roster._scaffold_rule_loaded = None, False

        def restore():
            (agent_roster.SCAFFOLD_MODULE_DIRS, agent_roster._scaffold_rule,
             agent_roster._scaffold_rule_loaded) = saved
        self.addCleanup(restore)

    def test_a_missing_rule_file_degrades_to_platform_only(self):
        # Neither directory holds profile_scaffold.py: the loader answers None
        # through its own code path, and the roster still routes to platform.
        with TemporaryDirectory() as tmp:
            base = self._base(tmp)
            scaffold(base, "cluster-a")
            self._with_rule_dirs([Path(tmp) / "nowhere", Path(tmp) / "nor-here"])
            self.assertIsNone(agent_roster._load_scaffold_rule())
            out = agent_roster.render()
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertNotIn("cluster-a", out)

    def test_a_rule_file_that_fails_to_load_degrades_to_platform_only(self):
        # The file is there but raises at import: the failure is logged with the
        # path, the loader answers None, and the roster still routes to platform.
        with TemporaryDirectory() as tmp:
            base = self._base(tmp)
            scaffold(base, "cluster-a")
            broken = Path(tmp) / "scripts"
            broken.mkdir()
            (broken / agent_roster.SCAFFOLD_MODULE_NAME).write_text("raise RuntimeError('boom')\n")
            self._with_rule_dirs([broken])
            self.assertIsNone(agent_roster._load_scaffold_rule())
            out = agent_roster.render()
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertNotIn("cluster-a", out)

    def test_the_rule_is_loaded_from_beside_the_roster_first(self):
        # On the pod both scripts share one directory; the repository path is the
        # fallback for these tests, and the roster must not reach past a sibling to it.
        self.assertEqual(
            Path(agent_roster.__file__).resolve().parent,
            agent_roster.SCAFFOLD_MODULE_DIRS[0],
        )
        self.assertTrue((agent_roster.SCAFFOLD_MODULE_DIRS[1] / agent_roster.SCAFFOLD_MODULE_NAME).is_file())
        self.assertIsNotNone(RULE)


class TestSharedRoleGrouping(unittest.TestCase):
    """Agents with an identical description are stated once, not repeated per agent.

    Every Cluster Agent is scaffolded from the same template, so a fleet of N
    clusters otherwise repeats one CAPABILITIES.md verbatim N times — the bulk of
    what the front door reads on every single delegation.
    """

    # Sized like the real Cluster Agent CAPABILITIES.md (~885 bytes), because the
    # win depends on it: the grouped form costs a fixed ~76-char preamble, so for a
    # short description and a small fleet it is actually *longer* than one line per
    # agent. Break-even is roughly (N-1) x len(desc) > 76.
    FLEET = ("Read-only diagnostics for one GKE cluster. "
             + "Scoped to a single cluster; no write paths. " * 19).strip()

    def _fleet(self, tmp):
        base = Path(tmp) / "profiles"
        for name in ("default", "platform"):
            (base / name).mkdir(parents=True)
        (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
        for name in ("cluster-a", "cluster-b", "cluster-c"):
            (scaffold(base, name) / "CAPABILITIES.md").write_text(self.FLEET)
        agent_roster.PROFILES_BASE = base

    def test_shared_description_stated_once(self):
        with TemporaryDirectory() as tmp:
            self._fleet(tmp)
            out = agent_roster.render()

            # The expensive part — the repeated blob — appears exactly once...
            self.assertEqual(out.count(self.FLEET), 1)
            # ...while every cluster is still individually addressable as an assignee.
            for name in ("cluster-a", "cluster-b", "cluster-c"):
                self.assertIn(f"  - {name}", out)

    def test_unique_specialist_kept_inline_and_ordered_first(self):
        with TemporaryDirectory() as tmp:
            self._fleet(tmp)
            out = agent_roster.render()

            # A one-off specialist stays on a single `- name: desc` line.
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            # Distinct specialists sort ahead of shared-role fleets: the front door
            # routes to a named specialist far more often than to a given cluster.
            self.assertLess(out.index("- platform:"), out.index("share one role"))

    def test_grouping_shrinks_the_roster(self):
        with TemporaryDirectory() as tmp:
            self._fleet(tmp)
            grouped = agent_roster.render()

        # Compare against what the un-grouped one-line-per-agent form would cost.
        ungrouped = "\n".join(
            ["- platform: Fleet + GitOps write path."]
            + [f"- {n}: {self.FLEET}" for n in ("cluster-a", "cluster-b", "cluster-c")]
        )
        self.assertLess(len(grouped), len(ungrouped))

    def test_undescribed_agents_are_not_a_shared_role(self):
        # Two profiles nothing is known about have no role in common. Grouping on
        # the placeholder would file them under "pick the one whose cluster you
        # need" — a claim about interchangeability the roster has no basis for.
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            (base / "default").mkdir(parents=True)
            for name in ("mystery-a", "mystery-b"):
                scaffold(base, name)
            agent_roster.PROFILES_BASE = base

            out = agent_roster.render()

            self.assertIn("- mystery-a: (no description provided)", out)
            self.assertIn("- mystery-b: (no description provided)", out)
            self.assertNotIn("share one role", out)


@unittest.skipIf(os.geteuid() == 0, "root bypasses the mode bits these tests rely on")
class TestDiscoveryDegradesOnIOError(unittest.TestCase):
    """Discovery must degrade, never raise.

    It backs both the Chat Agent's routing tool and the block injected into
    every turn, so a raise here is a front door that cannot route at all.
    pathlib swallows only ENOENT/ENOTDIR/EBADF/ELOOP, so `is_dir()`/`is_file()`/
    `iterdir()` on the shared PVC raise PermissionError (EACCES) for real.
    """

    @contextmanager
    def _locked(self, path):
        """Make `path` unreadable, restoring the mode so TemporaryDirectory can clean up."""
        original = path.stat().st_mode
        path.chmod(0o000)
        try:
            yield
        finally:
            path.chmod(original)

    def test_unreadable_profile_costs_only_that_agent(self):
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            for name in ("default", "platform"):
                (base / name).mkdir(parents=True)
            (base / "platform" / "CAPABILITIES.md").write_text("Fleet + GitOps write path.")
            scaffold(base, "broken")
            agent_roster.PROFILES_BASE = base

            with self._locked(base / "broken"):
                out = agent_roster.render()

            # The healthy specialist still routes; the unreadable one is left out
            # rather than taking down the whole roster — a home that cannot be read
            # cannot be shown to be one a card would reach.
            self.assertIn("- platform: Fleet + GitOps write path.", out)
            self.assertNotIn("broken", out)

    def test_unreadable_profiles_base_is_unknown_not_empty(self):
        # "I could not read the fleet" must never be rendered as "there is no
        # fleet": the front door would stop routing and tell the user there is
        # nobody to route to. render() returns None so the injecting plugin says
        # nothing and the model falls back to `list_agents`.
        with TemporaryDirectory() as tmp:
            base = Path(tmp) / "profiles"
            (base / "platform").mkdir(parents=True)
            agent_roster.PROFILES_BASE = base

            with self._locked(base):
                self.assertIsNone(agent_roster.render())

    def test_missing_profiles_base_returns_empty_roster(self):
        # A directory that is absent, unlike one that cannot be read, is a fact:
        # pathlib's is_dir() swallows ENOENT, so this is a genuinely empty fleet.
        with TemporaryDirectory() as tmp:
            agent_roster.PROFILES_BASE = Path(tmp) / "does-not-exist"
            self.assertIn("No specialist agents", agent_roster.render())


if __name__ == "__main__":
    unittest.main()
