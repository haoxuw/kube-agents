#!/usr/bin/env python3
"""Unit tests for upgrade_readiness.py: the PDB, webhook, maintenance and skew rules on canned objects."""

import json
import os
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))
import readiness_rules as shared  # noqa: E402
import upgrade_readiness as r  # noqa: E402
from readiness_rules import audit_log  # noqa: E402

AT = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc)
TARGET_TEXT = "1.35.1-gke.1000"
TARGET = (1, 35, 1, 1000)
MASTER = (1, 34, 11, 1000)


def workload(kind, name, replicas, labels, namespace="shop"):
    return {
        "kind": kind,
        "metadata": {"namespace": namespace, "name": name},
        "spec": {"replicas": replicas, "template": {"metadata": {"labels": labels}}},
    }


def pdb(name, spec, expected=None, allowed=None, namespace="shop"):
    record = {"kind": "PodDisruptionBudget", "metadata": {"namespace": namespace, "name": name}, "spec": spec}
    status = {}
    if expected is not None:
        status["expectedPods"] = expected
    if allowed is not None:
        status["disruptionsAllowed"] = allowed
    if status:
        record["status"] = status
    return record


def pool(name, version):
    from fleet_upgrade_report import parse_version  # the report's parser, so the tuples agree
    return {"name": name, "version": version, "parsed": parse_version(version)}


def exclusion(name, scope, start=AT - timedelta(days=2), end=AT + timedelta(days=88)):
    record = {"startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ")}
    if scope is not None:
        record["maintenanceExclusionOptions"] = {"scope": scope}
    return {"window": {"maintenanceExclusions": {name: record}}}


class SelectorTest(unittest.TestCase):
    def test_match_labels(self):
        self.assertTrue(r.selector_matches({"matchLabels": {"app": "web"}}, {"app": "web", "tier": "fe"}))
        self.assertFalse(r.selector_matches({"matchLabels": {"app": "web"}}, {"app": "api"}))

    def test_match_expressions(self):
        sel = {"matchExpressions": [{"key": "app", "operator": "In", "values": ["web", "api"]}, {"key": "canary", "operator": "DoesNotExist"}]}
        self.assertTrue(r.selector_matches(sel, {"app": "api"}))
        self.assertFalse(r.selector_matches(sel, {"app": "api", "canary": "true"}))
        self.assertFalse(r.selector_matches({"matchExpressions": [{"key": "app", "operator": "NotIn", "values": ["web"]}]}, {"app": "web"}))
        self.assertTrue(r.selector_matches({"matchExpressions": [{"key": "app", "operator": "Exists"}]}, {"app": "x"}))

    def test_null_matches_nothing_and_empty_matches_everything(self):
        self.assertFalse(r.selector_matches(None, {"app": "web"}))
        self.assertTrue(r.selector_matches({}, {"app": "web"}))


class PdbGradingTest(unittest.TestCase):
    def _grade(self, *items):
        return r.grade_pdbs(*r.split_items(list(items)))

    def test_max_unavailable_zero_on_fully_scheduled_deployment_is_named(self):
        # #1343 criterion 3: the acceptance criterion.
        result = self._grade(
            workload("Deployment", "web", 3, {"app": "web"}),
            pdb("block-drain", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "web"}}}, expected=3, allowed=0),
        )
        self.assertEqual(len(result["blocking"]), 1)
        finding = result["blocking"][0]
        self.assertEqual(finding["pdb"], "shop/block-drain")
        self.assertEqual(finding["field"], "maxUnavailable: 0")
        self.assertEqual(finding["workloads"], [{"kind": "Deployment", "namespace": "shop", "name": "web", "replicas": 3}])
        self.assertEqual(finding["disruptions_allowed"], 0)
        self.assertEqual(r.describe_finding(finding), "shop/block-drain (maxUnavailable: 0; Deployment shop/web (3 replicas))")

    def test_max_unavailable_zero_percent_blocks_and_positive_percent_does_not(self):
        dep = workload("Deployment", "web", 3, {"app": "web"})
        sel = {"matchLabels": {"app": "web"}}
        self.assertEqual(self._grade(dep, pdb("p", {"maxUnavailable": "0%", "selector": sel}))["blocking"][0]["field"], "maxUnavailable: 0%")
        self.assertEqual(self._grade(dep, pdb("p", {"maxUnavailable": "10%", "selector": sel}))["blocking"], [])
        self.assertEqual(self._grade(dep, pdb("p", {"maxUnavailable": 1, "selector": sel}))["blocking"], [])

    def test_min_available_at_below_and_above_replicas(self):
        dep = workload("StatefulSet", "db", 3, {"app": "db"})
        sel = {"matchLabels": {"app": "db"}}
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 3, "selector": sel}))["blocking"][0]["field"], "minAvailable: 3 (>= 3 expected pods)")
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 2, "selector": sel}))["blocking"], [])
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 4, "selector": sel}))["blocking"][0]["field"], "minAvailable: 4 (>= 3 expected pods)")
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": "3", "selector": sel}))["blocking"][0]["field"], "minAvailable: 3 (>= 3 expected pods)")

    def test_min_available_percentages_round_up_like_the_controller(self):
        sel = {"matchLabels": {"app": "web"}}
        three = workload("Deployment", "web", 3, {"app": "web"})
        self.assertEqual(self._grade(three, pdb("p", {"minAvailable": "100%", "selector": sel}))["blocking"][0]["field"], "minAvailable: 100% (rounds up to 3 of 3 expected pods)")
        self.assertEqual(self._grade(three, pdb("p", {"minAvailable": "50%", "selector": sel}))["blocking"], [])
        # 90% of nine rounds up to nine: every pod, so every drain is blocked.
        nine = workload("Deployment", "web", 9, {"app": "web"})
        self.assertEqual(self._grade(nine, pdb("p", {"minAvailable": "90%", "selector": sel}))["blocking"][0]["field"], "minAvailable: 90% (rounds up to 9 of 9 expected pods)")
        ten = workload("Deployment", "web", 10, {"app": "web"})
        self.assertEqual(self._grade(ten, pdb("p", {"minAvailable": "90%", "selector": sel}))["blocking"], [])

    def test_match_expressions_selector_finds_the_workload(self):
        dep = workload("Deployment", "web", 2, {"app": "web", "tier": "fe"})
        sel = {"matchExpressions": [{"key": "tier", "operator": "In", "values": ["fe"]}]}
        result = self._grade(dep, pdb("p", {"maxUnavailable": 0, "selector": sel}))
        self.assertEqual(result["blocking"][0]["workloads"][0]["name"], "web")

    def test_replicas_absent_means_one(self):
        dep = workload("Deployment", "web", 1, {"app": "web"})
        del dep["spec"]["replicas"]
        result = self._grade(dep, pdb("p", {"minAvailable": 1, "selector": {"matchLabels": {"app": "web"}}}))
        self.assertEqual(result["blocking"][0]["workloads"][0]["replicas"], 1)

    def test_replica_total_spans_every_matched_workload(self):
        # minAvailable 3 over two three-replica Deployments leaves three disruptions; not a blocker.
        a = workload("Deployment", "a", 3, {"team": "shop"})
        b = workload("Deployment", "b", 3, {"team": "shop"})
        sel = {"matchLabels": {"team": "shop"}}
        self.assertEqual(self._grade(a, b, pdb("p", {"minAvailable": 3, "selector": sel}))["blocking"], [])
        finding = self._grade(a, b, pdb("p", {"minAvailable": 6, "selector": sel}))["blocking"][0]
        self.assertEqual([w["name"] for w in finding["workloads"]], ["a", "b"])

    def test_namespace_is_respected(self):
        dep = workload("Deployment", "web", 3, {"app": "web"}, namespace="other")
        result = self._grade(dep, pdb("p", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "web"}}}))
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["orphan"], 1)

    def test_scaled_to_zero_and_orphan_pdbs_are_skipped_and_counted(self):
        zero = workload("Deployment", "idle", 0, {"app": "idle"})
        result = self._grade(
            zero,
            pdb("idle", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "idle"}}}, expected=0),
            pdb("no-status", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "idle"}}}),
            pdb("orphan", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "gone"}}}),
        )
        self.assertEqual(result["blocking"], [])
        self.assertEqual(result["scaled_to_zero"], 2)
        self.assertEqual(result["orphan"], 1)
        self.assertEqual(result["evaluated"], 0)

    def test_expected_pods_above_the_matched_total_sets_the_total(self):
        # A bare ReplicaSet shares the Deployment's labels: the controller expects 5 pods,
        # so minAvailable 4 leaves one disruption and blocks nothing; 5 blocks.
        dep = workload("Deployment", "d", 3, {"app": "x"})
        sel = {"matchLabels": {"app": "x"}}
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 4, "selector": sel}, expected=5, allowed=1))["blocking"], [])
        finding = self._grade(dep, pdb("p", {"minAvailable": 5, "selector": sel}, expected=5, allowed=0))["blocking"][0]
        self.assertEqual(finding["field"], "minAvailable: 5 (>= 5 expected pods)")
        self.assertEqual(finding["expected_pods"], 5)
        # A stale status below the spec total does not shrink it.
        self.assertEqual(self._grade(dep, pdb("p", {"minAvailable": 3, "selector": sel}, expected=1, allowed=0))["blocking"][0]["field"], "minAvailable: 3 (>= 3 expected pods)")

    def test_pdb_covering_pods_of_an_unread_kind_is_unmatched_not_orphan(self):
        result = self._grade(pdb("rs", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "bare"}}}, expected=2, allowed=0))
        self.assertEqual(result["unmatched"], 1)
        self.assertEqual(result["orphan"], 0)

    def test_daemonset_and_unknown_kinds_are_not_workloads(self):
        items = [
            {"kind": "DaemonSet", "metadata": {"namespace": "shop", "name": "agent"}, "spec": {"template": {"metadata": {"labels": {"app": "agent"}}}}},
            "not a dict",
            pdb("p", {"maxUnavailable": 0, "selector": {"matchLabels": {"app": "agent"}}}),
        ]
        pdbs, workloads = r.split_items(items)
        self.assertEqual(len(pdbs), 1)
        self.assertEqual(workloads, [])


class ExclusionTest(unittest.TestCase):
    POOLS = [pool("default-pool", "1.34.11-gke.1000")]

    def _one(self, policy, target_text=TARGET_TEXT, target=TARGET, master=MASTER, pools=None, at=AT):
        result = r.evaluate_maintenance(policy, at, target_text, target, master, self.POOLS if pools is None else pools)
        self.assertEqual(len(result["exclusions"]), 1)
        return result

    def test_in_effect_no_minor_upgrades_blocks_a_minor_target(self):
        result = self._one(exclusion("hold-the-minor-lag", "NO_MINOR_UPGRADES"))
        entry = result["exclusions"][0]
        self.assertTrue(entry["in_effect"])
        self.assertTrue(entry["blocks"])
        self.assertEqual(result["blocking_exclusions"], ["hold-the-minor-lag"])
        self.assertIn("blocks auto-upgrade to 1.35.1-gke.1000 until 2026-12-11T15:00Z", entry["detail"])
        self.assertIn("minor upgrade for the control plane and pool default-pool", entry["detail"])

    def test_no_minor_upgrades_does_not_block_a_patch_only_target(self):
        result = self._one(exclusion("x", "NO_MINOR_UPGRADES"), target_text="1.34.12-gke.1", target=(1, 34, 12, 1))
        self.assertFalse(result["exclusions"][0]["blocks"])
        self.assertIn("patch-only upgrade", result["exclusions"][0]["detail"])
        self.assertEqual(result["blocking_exclusions"], [])

    def test_no_minor_upgrades_blocks_when_only_a_pool_needs_the_minor(self):
        result = self._one(exclusion("x", "NO_MINOR_UPGRADES"), master=TARGET, pools=[pool("old", "1.34.0-gke.1")])
        self.assertTrue(result["exclusions"][0]["blocks"])
        self.assertIn("minor upgrade for pool old", result["exclusions"][0]["detail"])

    def test_no_upgrades_blocks_even_a_patch(self):
        result = self._one(exclusion("freeze", "NO_UPGRADES"), target_text="1.34.12-gke.1", target=(1, 34, 12, 1))
        self.assertTrue(result["exclusions"][0]["blocks"])
        self.assertIn("covers every upgrade", result["exclusions"][0]["detail"])

    def test_missing_scope_is_no_upgrades(self):
        result = self._one(exclusion("legacy", None), target_text="1.34.12-gke.1", target=(1, 34, 12, 1))
        self.assertEqual(result["exclusions"][0]["scope"], "NO_UPGRADES")
        self.assertTrue(result["exclusions"][0]["blocks"])

    def test_no_minor_or_node_upgrades_blocks_a_patch_a_pool_needs(self):
        policy = exclusion("x", "NO_MINOR_OR_NODE_UPGRADES")
        patch = self._one(policy, target_text="1.34.12-gke.1", target=(1, 34, 12, 1), master=(1, 34, 12, 1))
        self.assertTrue(patch["exclusions"][0]["blocks"])
        self.assertIn("pool(s) default-pool need a node upgrade", patch["exclusions"][0]["detail"])
        minor = self._one(policy)
        self.assertIn("minor upgrade for the control plane", minor["exclusions"][0]["detail"])
        current = self._one(policy, target_text="1.34.11-gke.1000", target=MASTER, master=MASTER)
        self.assertFalse(current["exclusions"][0]["blocks"])
        self.assertIn("no component is below the target", current["exclusions"][0]["detail"])

    def test_expired_and_future_exclusions_are_not_in_effect(self):
        expired = self._one(exclusion("old", "NO_UPGRADES", start=AT - timedelta(days=30), end=AT - timedelta(days=1)))
        future = self._one(exclusion("soon", "NO_UPGRADES", start=AT + timedelta(days=1), end=AT + timedelta(days=30)))
        for result in (expired, future):
            self.assertFalse(result["exclusions"][0]["in_effect"])
            self.assertFalse(result["exclusions"][0]["blocks"])
            self.assertEqual(result["blocking_exclusions"], [])

    def test_at_moves_the_verdict(self):
        policy = exclusion("soon", "NO_UPGRADES", start=AT + timedelta(days=1), end=AT + timedelta(days=30))
        self.assertTrue(self._one(policy, at=AT + timedelta(days=2))["exclusions"][0]["blocks"])

    def test_unknown_target_leaves_a_minor_scope_undecided_but_not_no_upgrades(self):
        undecided = self._one(exclusion("x", "NO_MINOR_UPGRADES"), target_text=None, target=None)
        self.assertIsNone(undecided["exclusions"][0]["blocks"])
        self.assertEqual(undecided["undecided_exclusions"], ["x"])
        decided = self._one(exclusion("x", "NO_UPGRADES"), target_text=None, target=None)
        self.assertTrue(decided["exclusions"][0]["blocks"])

    def test_unknown_scope_and_bad_times_are_undecided(self):
        odd = self._one(exclusion("x", "NO_SOMETHING"))
        self.assertIsNone(odd["exclusions"][0]["blocks"])
        broken = self._one({"window": {"maintenanceExclusions": {"x": {"startTime": "yesterday", "endTime": "tomorrow"}}}})
        self.assertIsNone(broken["exclusions"][0]["blocks"])
        self.assertEqual(broken["undecided_exclusions"], ["x"])

    def test_no_policy_has_no_exclusions_and_no_window(self):
        result = r.evaluate_maintenance(None, AT, TARGET_TEXT, TARGET, MASTER, self.POOLS)
        self.assertEqual(result["exclusions"], [])
        self.assertEqual(result["window"]["kind"], "none")
        self.assertEqual(result["window"]["state"], "none")


class WindowTest(unittest.TestCase):
    def test_daily_window_closed_with_next_opening(self):
        result = r.evaluate_window({"dailyMaintenanceWindow": {"startTime": "03:00", "duration": "PT4H0M0S"}}, AT)
        self.assertEqual(result["kind"], "daily")
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["next_opening"], "2026-09-15T03:00Z")
        self.assertIsNone(result["closes_at"])
        self.assertEqual(result["detail"], "daily at 03:00Z for 4h")

    def test_daily_window_open(self):
        result = r.evaluate_window({"dailyMaintenanceWindow": {"startTime": "03:00"}}, AT.replace(hour=5))
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["closes_at"], "2026-09-14T07:00Z")
        self.assertEqual(result["next_opening"], "2026-09-15T03:00Z")

    def test_daily_window_spanning_midnight(self):
        result = r.evaluate_window({"dailyMaintenanceWindow": {"startTime": "22:00"}}, AT.replace(hour=1))
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["closes_at"], "2026-09-14T02:00Z")

    def test_weekly_byday(self):
        window = {"recurringWindow": {"recurrence": "FREQ=WEEKLY;BYDAY=SA,SU", "window": {"startTime": "2026-01-03T04:00:00Z", "endTime": "2026-01-03T08:00:00Z"}}}
        weekday = r.evaluate_window(window, AT)  # a Monday
        self.assertEqual(weekday["state"], "closed")
        self.assertEqual(weekday["next_opening"], "2026-09-19T04:00Z")
        sunday = r.evaluate_window(window, datetime(2026, 9, 13, 5, 0, tzinfo=timezone.utc))
        self.assertEqual(sunday["state"], "open")
        self.assertEqual(sunday["closes_at"], "2026-09-13T08:00Z")
        self.assertEqual(sunday["detail"], "SA, SU from 04:00Z for 4h")

    def test_weekly_without_byday_uses_the_start_weekday(self):
        window = {"recurringWindow": {"recurrence": "FREQ=WEEKLY", "window": {"startTime": "2026-01-05T04:00:00Z", "endTime": "2026-01-05T08:00:00Z"}}}  # a Monday
        monday = r.evaluate_window(window, AT.replace(hour=6))
        self.assertEqual(monday["state"], "open")
        self.assertEqual(monday["detail"], "MO from 04:00Z for 4h")
        tuesday = r.evaluate_window(window, AT.replace(day=15, hour=6))
        self.assertEqual(tuesday["state"], "closed")
        self.assertEqual(tuesday["next_opening"], "2026-09-21T04:00Z")

    def test_recurring_daily_with_long_window(self):
        window = {"recurringWindow": {"recurrence": "FREQ=DAILY", "window": {"startTime": "2026-01-01T20:00:00Z", "endTime": "2026-01-02T06:00:00Z"}}}
        result = r.evaluate_window(window, AT.replace(hour=2))
        self.assertEqual(result["state"], "open")
        self.assertEqual(result["closes_at"], "2026-09-14T06:00Z")

    def test_before_the_first_occurrence_is_closed_with_that_opening(self):
        window = {"recurringWindow": {"recurrence": "FREQ=DAILY", "window": {"startTime": "2026-09-16T04:00:00Z", "endTime": "2026-09-16T08:00:00Z"}}}
        result = r.evaluate_window(window, AT)
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["next_opening"], "2026-09-16T04:00Z")
        # A first occurrence beyond the one-week horizon is still the next opening.
        far = {"recurringWindow": {"recurrence": "FREQ=DAILY", "window": {"startTime": "2026-10-01T04:00:00Z", "endTime": "2026-10-01T08:00:00Z"}}}
        result = r.evaluate_window(far, AT)
        self.assertEqual(result["state"], "closed")
        self.assertEqual(result["next_opening"], "2026-10-01T04:00Z")

    def test_unsupported_recurrences_are_not_evaluated(self):
        for rule in ("FREQ=MONTHLY;BYMONTHDAY=1", "FREQ=WEEKLY;INTERVAL=2;BYDAY=SA", "FREQ=WEEKLY;BYDAY=1SA", "FREQ=DAILY;BYDAY=SA", "nonsense", ""):
            window = {"recurringWindow": {"recurrence": rule, "window": {"startTime": "2026-01-03T04:00:00Z", "endTime": "2026-01-03T08:00:00Z"}}}
            result = r.evaluate_window(window, AT)
            self.assertEqual(result["state"], "not evaluated", rule)
            self.assertIsNone(result["next_opening"], rule)

    def test_bad_daily_start_is_not_evaluated(self):
        result = r.evaluate_window({"dailyMaintenanceWindow": {"startTime": "3am"}}, AT)
        self.assertEqual(result["state"], "not evaluated")

    def test_duration_parsing(self):
        self.assertEqual(r.parse_iso_duration("PT4H0M0S"), timedelta(hours=4))
        self.assertEqual(r.parse_iso_duration("P1DT2H"), timedelta(days=1, hours=2))
        self.assertIsNone(r.parse_iso_duration("4h"))
        self.assertIsNone(r.parse_iso_duration(None))

    def test_rfc3339_parsing(self):
        self.assertEqual(r.parse_rfc3339("2026-12-11T14:35:00Z"), datetime(2026, 12, 11, 14, 35, tzinfo=timezone.utc))
        self.assertEqual(r.parse_rfc3339("2026-12-11T15:35:00+01:00"), datetime(2026, 12, 11, 14, 35, tzinfo=timezone.utc))
        self.assertEqual(r.parse_rfc3339("2026-12-11t14:35:00.250z"), datetime(2026, 12, 11, 14, 35, 0, 250000, tzinfo=timezone.utc))
        # RFC 3339 proper: a bare date, the basic form and a naive time are refused, not
        # read as midnight or as UTC.
        for bad in ("2026-13-01T00:00:00Z", "2026-09-14", "20260914T150000Z", "2026-09-14T15:00:00", "2026-09-14 15:00:00Z", "tomorrow", "", None):
            self.assertIsNone(r.parse_rfc3339(bad), bad)


class SkewTest(unittest.TestCase):
    def _verdicts(self, target, pools):
        return {p["name"]: p["verdict"] for p in r.evaluate_skew(target, pools, False)["pools"]}

    def test_three_two_one_and_cross_major(self):
        pools = [pool("three", "1.32.0-gke.1"), pool("two", "1.33.0-gke.1"), pool("one", "1.34.0-gke.1"), pool("major", "0.99.0-gke.1")]
        result = r.evaluate_skew(TARGET, pools, False)
        self.assertEqual(self._verdicts(TARGET, pools), {"three": "blocks", "two": "at ceiling", "one": "ok", "major": "blocks"})
        self.assertEqual(result["blocking"], ["three", "major"])
        self.assertEqual(result["at_ceiling"], ["two"])
        by_name = {p["name"]: p for p in result["pools"]}
        self.assertEqual(by_name["three"]["minors_behind_target"], 3)
        self.assertIn("blocks the control-plane upgrade until the pool moves", by_name["three"]["detail"])
        self.assertEqual(by_name["major"]["detail"], "major version differs from the target")

    def test_pool_ahead_is_ok(self):
        self.assertEqual(self._verdicts(TARGET, [pool("new", "1.36.0-gke.1")]), {"new": "ok"})

    def test_autopilot_is_not_applicable(self):
        result = r.evaluate_skew(TARGET, [pool("x", "1.30.0-gke.1")], True)
        self.assertFalse(result["applicable"])
        self.assertEqual(result["blocking"], [])
        self.assertIn("Autopilot", result["reason"])

    def test_no_target_is_unknown(self):
        result = r.evaluate_skew(None, [pool("x", "1.30.0-gke.1")], False)
        self.assertTrue(result["applicable"])
        self.assertEqual(result["unknown"], ["x"])
        self.assertEqual(result["blocking"], [])

    def test_unparsable_pool_is_unknown(self):
        result = r.evaluate_skew(TARGET, [pool("weird", "latest")], False)
        self.assertEqual(result["unknown"], ["weird"])


def rule_module(rule_id, entry=0, can_block=True, evaluate=None, describe=None):
    """A stand-in for a rule module: a class carrying the contract's attributes."""

    class Rule:
        RULE_ID = rule_id
        ENTRY = entry
        CAN_BLOCK = can_block

    Rule.evaluate = staticmethod(evaluate or (lambda cluster, member, read, target, context: shared.new_result()))
    Rule.describe = staticmethod(describe or (lambda finding: "described " + str(finding.get("what"))))
    return Rule


def bare_module(name, evaluate=None, **attributes):
    """A module object, as EXTRA_RULES carries, with the attributes given and nothing else."""
    module = types.ModuleType(name)
    module.evaluate = evaluate or (lambda cluster, member, read, target, context: shared.new_result())
    module.describe = lambda finding: "described " + str(finding.get("what"))
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def returning(**lists):
    def evaluate(cluster, member, read, target, context):
        out = shared.new_result()
        for key, value in lists.items():
            out[key].extend(value)
        return out

    return evaluate


class ExtraRulesTest(unittest.TestCase):
    """The fold: every registered rule runs over one member, findings carry their rule and
    text, an `unknown` carries whether its rule can block, and a rule's defect is its own
    unknown rather than a crash."""

    READ = {"kubeconfig": "k", "items": [], "error": None, "read_errors": {kind: None for kind in shared.READ_KINDS}}

    def test_findings_are_stamped_and_counted_per_rule(self):
        blocker = rule_module("blocker", entry=2, evaluate=returning(blocking=[{"what": "no room"}], notes=["pool p has room"]))
        risky = rule_module("risky", entry=11, can_block=False, evaluate=returning(risks=[{"what": "zonal"}], unknown=["odd location"]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[blocker, risky])
        self.assertEqual(folded["blocking"], [{"what": "no room", "text": "described no room", "rule": "blocker", "entry": 2}])
        self.assertEqual(folded["risks"], [{"what": "zonal", "text": "described zonal", "rule": "risky", "entry": 11}])
        self.assertEqual(folded["unknown"], [{"reason": "odd location", "rule": "risky", "entry": 11, "can_block": False, "text": "risky: odd location"}])
        self.assertEqual(folded["notes"], ["blocker: pool p has room"])
        self.assertEqual(folded["rules"], {"blocker": {"entry": 2, "can_block": True, "blocking": 1, "risks": 0, "unknown": 0, "notes": 1}, "risky": {"entry": 11, "can_block": False, "blocking": 0, "risks": 1, "unknown": 1, "notes": 0}})

    def test_unknown_entries_keep_their_fields_and_gain_a_reason(self):
        b_style = {"rule": "x", "entry": 4, "grade": "unknown", "object": None, "detail": "the Node read failed"}
        c_style = {"rule": "x", "tier": "unknown", "reason": "no runner"}
        rule = rule_module("x", entry=4, evaluate=returning(unknown=[b_style, c_style, "plain"]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        self.assertEqual([u["reason"] for u in folded["unknown"]], ["the Node read failed", "no runner", "plain"])
        self.assertEqual(folded["unknown"][0]["detail"], "the Node read failed")
        self.assertEqual(folded["unknown"][1]["tier"], "unknown")
        self.assertTrue(all(u["can_block"] and u["rule"] == "x" and u["entry"] == 4 for u in folded["unknown"]))
        self.assertEqual(folded["unknown"][2]["text"], "x: plain")

    def test_a_rule_that_raises_is_unknown_not_a_crash(self):
        def boom(*args):
            raise ValueError("boom")

        broken = rule_module("broken", entry=0, evaluate=boom)
        fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[broken, fine])
        self.assertEqual([(u["rule"], u["reason"], u["can_block"]) for u in folded["unknown"]], [("broken", "rule raised ValueError('boom'); not evaluated", True)])
        # The guard's own rendering never escapes it: an exception whose argument's repr raises
        # is named by its type, and one carrying a tenant-sized payload is cut.
        class Unprintable:
            def __repr__(self):
                raise RuntimeError("no repr")

        def boom_unprintable(*args):
            raise ValueError(Unprintable())

        def boom_huge(*args):
            raise ValueError("bad spec " + "x" * 5000)

        guarded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule_module("unprintable", evaluate=boom_unprintable), rule_module("huge", evaluate=boom_huge), fine])
        self.assertEqual([(u["rule"], u["reason"]) for u in guarded["unknown"]][0], ("unprintable", "rule raised ValueError; not evaluated"))
        self.assertTrue(guarded["unknown"][1]["reason"].startswith("rule raised ValueError('bad spec xxx"))
        self.assertLessEqual(len(guarded["unknown"][1]["reason"]), len("rule raised ; not evaluated") + shared.ERROR_EXCERPT_CHARS)
        self.assertEqual([f["rule"] for f in guarded["risks"]], ["fine"])
        json.dumps(guarded)
        self.assertEqual(folded["rules"]["broken"], {"entry": 0, "can_block": True, "blocking": 0, "risks": 0, "unknown": 1, "notes": 0})
        self.assertEqual([f["rule"] for f in folded["risks"]], ["fine"])
        # A describe that raises is the same defect, and a rule returning nothing is empty.
        bad_describe = rule_module("bad-describe", evaluate=returning(risks=[{"what": "x"}]), describe=boom)
        empty = rule_module("empty", evaluate=lambda *args: None)
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[bad_describe, empty])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["bad-describe"])
        self.assertEqual(folded["rules"]["empty"], {"entry": 0, "can_block": True, "blocking": 0, "risks": 0, "unknown": 0, "notes": 0})

    def test_a_result_of_the_wrong_shape_is_one_unknown_not_a_crash_or_a_letter_each(self):
        # A string where a list was owed is the likeliest slip; read letter by letter it
        # would print one note per character. A dict, a list for the whole result, a finding
        # that is not a dict, a finding whose text is not a string and a note that is not a
        # string are the same defect: one unknown naming it, the other rules untouched.
        cases = {
            "string-unknown": (lambda *a: {"unknown": "no target to grade against"}, "unknown is str, not a list"),
            "dict-notes": (lambda *a: {**shared.new_result(), "notes": {"a": 1}}, "notes is dict, not a list"),
            "list-result": (lambda *a: [], "evaluate returned list, not a dict"),
            "string-finding": (lambda *a: {**shared.new_result(), "risks": ["context"]}, "a risks finding is str, not a dict"),
            "list-finding": (lambda *a: {**shared.new_result(), "blocking": [["text", 1]]}, "a blocking finding is list, not a dict"),
            "none-text": (lambda *a: {**shared.new_result(), "risks": [{"what": "x", "text": None}]}, "a risks finding's text is NoneType, not a string"),
            "int-note": (lambda *a: {**shared.new_result(), "notes": [3]}, "a note is int, not a string"),
            # A list under a key the contract does not name read as a clean rule; a key left out stays an empty list.
            "foreign-key": (lambda *a: {**shared.new_result(), "risk": [{"what": "pv"}]}, "the result carries ['risk'], not among ['blocking', 'risks', 'unknown', 'notes']"),
            "findings-key": (lambda *a: {"findings": [{"what": "pv"}]}, "the result carries ['findings'], not among ['blocking', 'risks', 'unknown', 'notes']"),
            # An unknown entry with neither a reason nor a detail was rendered as the dict's repr.
            "unknown-without-reason": (lambda *a: {**shared.new_result(), "unknown": [{"rule": "x", "entry": 4, "grade": "unknown", "object": None, "detail": ""}]}, "an unknown entry carries no reason or detail (keys ['detail', 'entry', 'grade', 'object', 'rule'])"),
            "unknown-int": (lambda *a: {**shared.new_result(), "unknown": [3]}, "an unknown entry is int, not a string or a dict with a reason"),
        }
        fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
        for name, (evaluate, reason) in cases.items():
            broken = rule_module(name, evaluate=evaluate)
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[broken, fine])
            self.assertEqual([(u["rule"], u["reason"]) for u in folded["unknown"]], [(name, f"rule raised TypeError({reason!r}); not evaluated")], name)
            self.assertEqual(folded["rules"][name], {"entry": 0, "can_block": True, "blocking": 0, "risks": 0, "unknown": 1, "notes": 0}, name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["fine"], name)
            self.assertEqual(folded["notes"], [], name)

    def test_a_finding_json_cannot_carry_is_the_rules_defect_not_the_writers(self):
        # The findings go to `--output` as they are; a set, a datetime or a tuple-keyed dict
        # would fail json.dump after every member was read, so the fold refuses it here.
        # A NaN or infinite float is refused too: `json.dumps` would write it as a bare
        # token (`NaN`, `Infinity`) that no JSON parser reads.
        for name, value in (("set", {"a", "b"}), ("datetime", AT), ("tuple-keys", {(1, 2): "x"}), ("nan", float("nan")), ("inf", float("inf"))):
            broken = rule_module(name, evaluate=returning(risks=[{"what": value}]))
            fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[broken, fine])
            self.assertEqual([u["rule"] for u in folded["unknown"]], [name], name)
            self.assertIn("the result is not JSON-serialisable", folded["unknown"][0]["reason"], name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["fine"], name)
            json.dumps(folded)

    def test_a_rules_bad_audit_log_cache_entry_is_its_unknown_not_the_reports_crash(self):
        # The report reads the audit-log summaries from the cache the rules fill, after the
        # fold returns; a rule that wrote another shape there raised out of `assess_readiness`
        # after every member was read. The fold checks the cache around each rule, so the
        # defect is that rule's `unknown`, and `summaries` hands the report only what `read`
        # stored.
        def context():
            return {shared.CONTEXT_PROJECT: "p", shared.CONTEXT_LOCATION: "us-central1-a", shared.CONTEXT_CLUSTER_NAME: "c", shared.CONTEXT_RUN_CMD: lambda cmd, timeout=None: (0, "[]", ""), shared.CONTEXT_AT: AT, shared.CONTEXT_CACHE: {}}

        def writing(value):
            def evaluate(cluster, member, read, target, ctx):
                ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = value
                return shared.new_result()

            return evaluate

        def reading(cluster, member, read, target, ctx):
            audit_log.read_removed(ctx)
            return shared.new_result()

        fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
        clean = audit_log.read_removed(context())
        cases = {"list": [{"error": "x"}], "none-entry": {"removed": None}, "stranger": {"other": clean}, "nan-entry": {"removed": {**clean, "entries": float("nan")}}, "short": {"removed": {"read": "removed"}}}
        for name, value in cases.items():
            ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module(name, evaluate=writing(value)), fine])
            self.assertEqual([u["rule"] for u in folded["unknown"]], [name], name)
            self.assertIn("the rule left the audit-log cache unreadable: ", folded["unknown"][0]["reason"], name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["fine"], name)
            self.assertEqual(audit_log.summaries(ctx), {}, name)
            json.dumps(folded)
        # A read made through `audit_log.read` is kept, a later rule's damage to the other
        # entry is that rule's, and the damaged entry alone is left out of the summaries.
        def spoiling(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["deprecated"] = 3
            return shared.new_result()

        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading), rule_module("spoiler", evaluate=spoiling)])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["spoiler"])
        self.assertEqual(list(audit_log.summaries(ctx)), ["removed"])
        self.assertEqual({key: audit_log.summaries(ctx)["removed"][key] for key in ("entries", "pages", "sampled", "error")}, {"entries": 0, "pages": 1, "sampled": False, "error": None})
        self.assertEqual(set(audit_log.summaries(ctx)["removed"]), set(audit_log.SUMMARY_KEYS))
        # A defect already in the cache before a rule runs is not that rule's.
        ctx = context()
        ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = [1]
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[fine])
        self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]]), ([], ["fine"]))
        self.assertEqual((audit_log.summaries(ctx), audit_log.summaries({}), audit_log.summaries({shared.CONTEXT_CACHE: None}), audit_log.cache_defect({})), ({}, {}, {}, None))
        self.assertIsNotNone(audit_log.cache_defect({shared.CONTEXT_CACHE: None}))
        # A rule's damage is put back before the next rule runs: the next rule's read works
        # and is kept, and each later offender is charged on its own, not masked by the first.
        def reading_with_risk(cluster, member, read, target, ctx):
            audit_log.read_removed(ctx)
            out = shared.new_result()
            out["risks"].append({"what": "r"})
            return out

        def spoiling_entry(which, value):
            def evaluate(cluster, member, read, target, ctx):
                ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY][which] = value
                return shared.new_result()

            return evaluate

        def nulling(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE] = None
            return shared.new_result()

        ctx = context()
        rules = [rule_module("list", evaluate=writing([1])), rule_module("reader", evaluate=reading_with_risk), rule_module("nuller", evaluate=spoiling_entry("removed", None)), rule_module("stranger", evaluate=spoiling_entry("deprecated", 3)), fine]
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=rules)
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["list", "nuller", "stranger"])
        self.assertEqual([f["rule"] for f in folded["risks"]], ["reader", "fine"])
        self.assertEqual(list(audit_log.summaries(ctx)), ["removed"])
        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("nulling", evaluate=nulling), rule_module("reader", evaluate=reading_with_risk)])
        self.assertEqual(([u["rule"] for u in folded["unknown"]], [f["rule"] for f in folded["risks"]], list(audit_log.summaries(ctx))), ([], ["reader"], ["removed"]))
        # The cache is put back on the raising path too, so the raising rule is the one
        # charged and the next rule reads what `read` stored; and a well-formed overwrite
        # that drops or changes a stored read is damage, not a clean cache.
        def damaging_raiser(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["removed"] = None
            raise ValueError("boom")

        def nulling_raiser(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE] = None
            raise ValueError("boom")

        def emptying(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = {}
            return shared.new_result()

        def replacing(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE] = {audit_log.CACHE_KEY: dict(ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY])}
            return shared.new_result()

        def reading_deprecated(cluster, member, read, target, ctx):
            audit_log.read_deprecated(ctx)
            out = shared.new_result()
            out["risks"].append({"what": "d"})
            return out

        for name, offender in (("damaging-raiser", damaging_raiser), ("nulling-raiser", nulling_raiser)):
            ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module(name, evaluate=offender), rule_module("second", evaluate=reading_deprecated)])
            self.assertEqual([(u["rule"], u["reason"]) for u in folded["unknown"]], [(name, "rule raised ValueError('boom'); not evaluated")], name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["reader", "second"], name)
            self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"], name)
        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module("emptying", evaluate=emptying), rule_module("second", evaluate=reading_deprecated)])
        self.assertEqual([u["rule"] for u in folded["unknown"]], ["emptying"])
        self.assertIn("the rule left the audit-log cache unreadable: the 'removed' read stored before the rule is gone or changed", folded["unknown"][0]["reason"])
        self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"])
        # A rule that replaces or nulls the cache *in its own context* reaches nobody: each
        # rule is handed its own context, and only the shared cache object is held to.
        for name, offender in (("replacing", replacing), ("nulling", nulling)):
            ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module(name, evaluate=offender), rule_module("second", evaluate=reading_deprecated)])
            self.assertEqual((folded["unknown"], [f["rule"] for f in folded["risks"]], sorted(audit_log.summaries(ctx))), ([], ["reader", "second"], ["deprecated", "removed"]), name)
        # An object the snapshot cannot copy (a generator) is the storing rule's damage, named,
        # and the restore still runs, so the next rule is neither charged nor stopped.
        def storing_generator(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["scratch"] = (e for e in [1, 2])
            return shared.new_result()

        def poisoning_callers(cluster, member, read, target, ctx):
            ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY]["removed"]["callers"].append((e for e in [1]))
            return shared.new_result()

        for name, offender, reason in (("generator", storing_generator, "cache['audit_log'] carries 'scratch', which is not one of the reads"), ("poisoning", poisoning_callers, "the 'removed' entry holds an object that cannot be copied")):
            ctx = context()
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module(name, evaluate=offender), rule_module("second", evaluate=reading_deprecated)])
            self.assertEqual([u["rule"] for u in folded["unknown"]], [name], name)
            self.assertIn(reason, folded["unknown"][0]["reason"], name)
            self.assertEqual([f["rule"] for f in folded["risks"]], ["reader", "second"], name)
            self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"], name)
            self.assertEqual(audit_log.cache_defect(ctx), None, name)
        # A generator in the cache before the fold ran is nobody's: the snapshot stands, the
        # rules run, and the entry is left as it was found.
        ctx = context()
        ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY] = {"scratch": (e for e in [1])}
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), fine])
        self.assertEqual(([u["rule"] for u in folded["unknown"]], [f["rule"] for f in folded["risks"]]), ([], ["reader", "fine"]))
        self.assertIn("scratch", ctx[shared.CONTEXT_CACHE][audit_log.CACHE_KEY])
        # What `read` returns is the rule's own copy: sorting or filtering it in place is
        # not damage, and the rule keeps its findings.
        def sorting_reader(cluster, member, read, target, ctx):
            log = audit_log.read_removed(ctx)
            log["callers"].sort(key=lambda c: -c.get("count", 0))
            log["callers"][:] = []
            out = shared.new_result()
            out["blocking"].append({"what": "sorted"})
            return out

        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("reader", evaluate=reading_with_risk), rule_module("sorter", evaluate=sorting_reader), rule_module("second", evaluate=reading_deprecated)])
        self.assertEqual(folded["unknown"], [])
        self.assertEqual([f["rule"] for f in folded["blocking"]], ["sorter"])
        self.assertEqual(sorted(audit_log.summaries(ctx)), ["deprecated", "removed"])
        # A read name that is not one of the reads is the rule's own error, named as the call.
        def misnamed_reader(cluster, member, read, target, ctx):
            audit_log.read(ctx, "deprecate")
            return shared.new_result()

        ctx = context()
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, ctx, rules=[rule_module("misnamed", evaluate=misnamed_reader), fine])
        self.assertEqual([(u["rule"], u["reason"]) for u in folded["unknown"]], [("misnamed", "rule raised ValueError(\"audit_log.read('deprecate'): not one of the reads ('removed', 'deprecated')\"); not evaluated")])
        self.assertEqual(audit_log.summaries(ctx), {})

    def test_each_rule_is_handed_its_own_copies_so_its_writes_reach_nobody(self):
        # `cluster`, `member`, `read` and the context's fleet records reached every rule by
        # reference, and the report read them after the fold: a rule narrowing `read["items"]`
        # in place emptied the list for every later rule, and one nulling a peer's maintenance
        # policy changed what every later member was graded from.
        def clearing(cluster, member, read, target, ctx):
            read["items"].clear()
            read["read_errors"]["pods"] = "mine"
            member["cluster"] = "renamed"
            cluster["name"] = "renamed"
            return shared.new_result()

        def nulling_peer(cluster, member, read, target, ctx):
            for peer in ctx[shared.CONTEXT_CLUSTERS]:
                peer["maintenancePolicy"] = None
            ctx[shared.CONTEXT_POOLS].clear()
            return shared.new_result()

        def counting(cluster, member, read, target, ctx):
            out = shared.new_result()
            out["notes"].append(f"{len(read['items'])} item(s); {len(ctx[shared.CONTEXT_CLUSTERS])} peer(s) with policy {[p.get('maintenancePolicy') for p in ctx[shared.CONTEXT_CLUSTERS]]}; {len(ctx[shared.CONTEXT_POOLS])} pool(s); {member['cluster']} {cluster['name']}")
            return out

        read = {"kubeconfig": "k", "items": [{"kind": "Pod"}, {"kind": "Node"}], "error": None, "read_errors": {kind: None for kind in shared.READ_KINDS}}
        peers = [{"name": "peer", "maintenancePolicy": {"window": {}}}]
        ctx = {shared.CONTEXT_CLUSTERS: peers, shared.CONTEXT_POOLS: [{"name": "p"}], shared.CONTEXT_CACHE: {}}
        cluster, member = {"name": "c"}, {"cluster": "c"}
        folded = r.evaluate_extra_rules(cluster, member, read, TARGET, ctx, rules=[rule_module("clearing", evaluate=clearing), rule_module("nulling-peer", evaluate=nulling_peer), rule_module("counting", evaluate=counting)])
        self.assertEqual(folded["unknown"], [])
        self.assertEqual(folded["notes"], ["counting: 2 item(s); 1 peer(s) with policy [{'window': {}}]; 1 pool(s); c c"])
        self.assertEqual((read["items"], read["read_errors"]["pods"], member["cluster"], cluster["name"]), ([{"kind": "Pod"}, {"kind": "Node"}], None, "c", "c"))
        self.assertEqual((peers, ctx[shared.CONTEXT_POOLS]), ([{"name": "peer", "maintenancePolicy": {"window": {}}}], [{"name": "p"}]))

    def test_findings_are_copied_before_they_are_stamped(self):
        shared_finding = {"what": "pool p"}
        first = rule_module("first", entry=2, evaluate=lambda *a: {**shared.new_result(), "risks": [shared_finding]}, describe=lambda f: "first's text")
        second = rule_module("second", entry=12, evaluate=lambda *a: {**shared.new_result(), "risks": [shared_finding, shared_finding]}, describe=lambda f: "second's text")
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[first, second])
        # The rule's own object is untouched, and each fold copy carries its own rule and text.
        self.assertEqual(shared_finding, {"what": "pool p"})
        self.assertEqual([(f["rule"], f["entry"], f["text"]) for f in folded["risks"]], [("first", 2, "first's text"), ("second", 12, "second's text"), ("second", 12, "second's text")])
        self.assertEqual(len({id(f) for f in folded["risks"]}), 3)
        self.assertEqual((folded["rules"]["first"]["risks"], folded["rules"]["second"]["risks"]), (1, 2))

    def test_cell_text_is_one_line_and_a_reason_is_cut_while_the_json_keeps_it(self):
        two_lines = "E1013 memcache.go couldn't get current server API group list\nUnable to connect to the server: dial tcp: i/o timeout\n" + "x" * 300
        rule = rule_module("multi", evaluate=returning(unknown=[two_lines], risks=[{"what": "x", "text": "line one\nline two"}], notes=["note\nwith a break"]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        unknown = folded["unknown"][0]
        self.assertEqual(unknown["reason"], two_lines)
        self.assertNotIn("\n", unknown["text"])
        self.assertTrue(unknown["text"].startswith("multi: E1013 memcache.go couldn't get current server API group list Unable to connect"))
        self.assertEqual(len(unknown["text"]), len("multi: ") + shared.NOTE_EXCERPT_CHARS)
        self.assertEqual(folded["risks"][0]["text"], "line one line two")
        self.assertEqual(folded["notes"], ["multi: note with a break"])

    def test_findings_are_stamped_inside_the_guard(self):
        # Stamping and rendering happen before the fold returns, so every blocker and risk
        # the fold emits is a dict with a string text, its rule and its entry.
        rule = rule_module("stamped", entry=19, evaluate=returning(blocking=[{"what": "pv"}], risks=[{"what": "pv2", "text": "own"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        for finding in folded["blocking"] + folded["risks"]:
            self.assertEqual((finding["rule"], finding["entry"]), ("stamped", 19))
            self.assertIsInstance(finding["text"], str)

    def test_module_attributes_are_checked_before_anything_is_derived_from_them(self):
        # RULE_ID keys `rules`, so a list raised `unhashable type` outside the guard and
        # killed the run after every member was read; ENTRY is stamped on the per-rule
        # record and the unknown entries, which a probe of the result alone never saw, so a
        # set reached the writer whenever the rule filed no blocker or risk; bool("False")
        # is True. Each is one unknown under the module's name, unevaluated, the other rules
        # untouched, and the fold is JSON.
        fine = rule_module("fine", evaluate=returning(risks=[{"what": "ok"}]))
        cases = {
            "surge_capacity": (dict(RULE_ID=["surge", "capacity"], ENTRY=2, CAN_BLOCK=True), None, "RULE_ID is list, not a string; not evaluated"),
            "quiet_entry": (dict(RULE_ID="quiet", ENTRY={11}, CAN_BLOCK=True), None, "ENTRY is set, not a JSON value; not evaluated"),
            "loud_entry": (dict(RULE_ID="loud", ENTRY={11}, CAN_BLOCK=True), returning(blocking=[{"what": "b"}]), "ENTRY is set, not a JSON value; not evaluated"),
            "stringly": (dict(RULE_ID="stringly", ENTRY=5, CAN_BLOCK="False"), None, "CAN_BLOCK is str, not a bool; not evaluated"),
            "nan_entry": (dict(RULE_ID="nan", ENTRY=float("nan"), CAN_BLOCK=True), None, "ENTRY is float, not a JSON value; not evaluated"),
        }
        for name, (attributes, evaluate, reason) in cases.items():
            broken = bare_module(name, evaluate=evaluate, **attributes)
            folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[broken, fine])
            self.assertEqual([(u["rule"], u["entry"], u["can_block"], u["reason"], u["text"]) for u in folded["unknown"]], [(name, None, True, reason, f"{name}: {reason}")], name)
            self.assertEqual(folded["rules"][name], {"entry": None, "can_block": True, "blocking": 0, "risks": 0, "unknown": 1, "notes": 0}, name)
            self.assertEqual(folded["rules"]["fine"], {"entry": 0, "can_block": True, "blocking": 0, "risks": 1, "unknown": 0, "notes": 0}, name)
            self.assertEqual((folded["blocking"], [f["rule"] for f in folded["risks"]], folded["notes"]), ([], ["fine"], []), name)
            json.dumps(folded)  # what the writer does with it
        # A module with no RULE_ID at all is keyed by its module name and evaluated as usual.
        nameless = bare_module("nameless", evaluate=returning(risks=[{"what": "n"}]), ENTRY=3, CAN_BLOCK=False)
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[nameless])
        self.assertEqual(folded["rules"], {"nameless": {"entry": 3, "can_block": False, "blocking": 0, "risks": 1, "unknown": 0, "notes": 0}})
        self.assertEqual(folded["risks"][0]["rule"], "nameless")

    def test_a_second_module_with_a_registered_id_is_not_evaluated(self):
        first = rule_module("twice", entry=4, evaluate=returning(risks=[{"what": "a"}]))
        second = rule_module("twice", entry=12, evaluate=returning(blocking=[{"what": "b"}], risks=[{"what": "c"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[first, second])
        self.assertEqual(folded["blocking"], [])
        self.assertEqual([f["what"] for f in folded["risks"]], ["a"])
        self.assertEqual([(u["rule"], u["entry"], u["can_block"], u["reason"]) for u in folded["unknown"]], [("twice", 4, True, "a second module registers RULE_ID twice; the duplicate was not evaluated")])
        self.assertEqual(folded["rules"], {"twice": {"entry": 4, "can_block": True, "blocking": 0, "risks": 1, "unknown": 1, "notes": 0}})
        # The invariant the JSON readers rely on: per-rule counts describe the flat lists.
        for key in ("blocking", "risks", "unknown", "notes"):
            self.assertEqual(folded["rules"]["twice"][key], sum(1 for item in folded[key] if (item["rule"] if isinstance(item, dict) else None) == "twice" or (key == "notes" and item.startswith("twice: "))))
        self.assertEqual(r.readiness_status({"blocking": []}, {"blocking_exclusions": [], "undecided_exclusions": []}, {"blocking": [], "unknown": []}, True, folded), r.READINESS_UNKNOWN)
        # A duplicate of a rule that cannot block carries that rule's can_block: the per-rule
        # record and its entries agree, and the member's verdict is untouched.
        cannot = rule_module("zonal", entry=11, can_block=False, evaluate=returning(risks=[{"what": "z"}]))
        again = rule_module("zonal", entry=11, can_block=True, evaluate=returning(blocking=[{"what": "b"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[cannot, again])
        self.assertEqual([(u["rule"], u["can_block"]) for u in folded["unknown"]], [("zonal", False)])
        self.assertEqual(folded["rules"]["zonal"]["can_block"], False)
        self.assertEqual(folded["blocking"], [])
        self.assertEqual(r.readiness_status({"blocking": []}, {"blocking_exclusions": [], "undecided_exclusions": []}, {"blocking": [], "unknown": []}, True, folded), r.READINESS_READY)

    def test_a_blocker_from_a_rule_that_cannot_block_is_demoted_to_a_risk_with_a_note(self):
        rule = rule_module("cannot", entry=11, can_block=False, evaluate=returning(blocking=[{"what": "oops"}, {"what": "graded", "grade": "blocking"}, {"what": "tiered", "tier": "blocking"}], risks=[{"what": "fine"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        self.assertEqual(folded["blocking"], [])
        self.assertEqual([f["what"] for f in folded["risks"]], ["oops", "graded", "tiered", "fine"])
        # A demoted finding is re-graded under whichever word it carries: no risk says it blocks.
        self.assertEqual([(f.get("grade"), f.get("tier")) for f in folded["risks"]], [(None, None), ("risk", None), (None, "risk"), (None, None)])
        self.assertEqual(folded["notes"], ["cannot: filed a blocker but declares CAN_BLOCK False; reported as a risk"])
        self.assertEqual(folded["rules"]["cannot"]["risks"], 4)

    def test_the_registry_is_the_default_and_rules_selects(self):
        registered = r.evaluate_extra_rules({"name": "c", "location": "us-central1-a"}, {}, self.READ, TARGET, {})
        self.assertEqual(list(registered["rules"]), [m.RULE_ID for m in r.EXTRA_RULES])
        self.assertIn("zonal-control-plane", [f["rule"] for f in registered["risks"]])
        only = r.evaluate_extra_rules({"name": "c", "location": "us-central1-a"}, {}, self.READ, TARGET, {}, rules=[rule_module("other")])
        self.assertEqual(list(only["rules"]), ["other"])
        self.assertEqual(only["risks"], [])

    def test_text_set_by_the_rule_is_kept(self):
        rule = rule_module("pre", evaluate=returning(risks=[{"what": "x", "text": "the rule's own"}]))
        folded = r.evaluate_extra_rules({}, {}, self.READ, TARGET, {}, rules=[rule])
        self.assertEqual(folded["risks"][0]["text"], "the rule's own")


class VerdictTest(unittest.TestCase):
    CLEAR = {"blocking_exclusions": [], "undecided_exclusions": []}
    NO_SKEW = {"blocking": [], "unknown": []}
    NO_WEBHOOKS = {"blocking": []}

    def test_ready_needs_every_rule_evaluated(self):
        pdbs = {"blocking": []}
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, True), "ready")
        self.assertEqual(r.readiness_status(None, None, self.CLEAR, self.NO_SKEW, True), "unknown")
        self.assertEqual(r.readiness_status(pdbs, None, self.CLEAR, self.NO_SKEW, True), "unknown")
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, self.NO_SKEW, False), "unknown")
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, {"blocking_exclusions": [], "undecided_exclusions": ["x"]}, self.NO_SKEW, True), "unknown")
        self.assertEqual(r.readiness_status(pdbs, self.NO_WEBHOOKS, self.CLEAR, {"blocking": [], "unknown": ["p"]}, True), "unknown")

    def test_blocked_beats_unknown(self):
        self.assertEqual(r.readiness_status({"blocking": [{"pdb": "a/b"}]}, self.CLEAR, self.NO_SKEW, False), "blocked")
        self.assertEqual(r.readiness_status(None, {"blocking_exclusions": ["x"], "undecided_exclusions": []}, self.NO_SKEW, True), "blocked")
        self.assertEqual(r.readiness_status(None, self.CLEAR, {"blocking": ["p"], "unknown": []}, True), "blocked")
        rules = {"blocking": [{"rule": "x"}], "risks": [], "unknown": [{"rule": "y", "can_block": True}], "notes": [], "rules": {}}
        self.assertEqual(r.readiness_status(None, self.CLEAR, self.NO_SKEW, False, rules), "blocked")

    def test_extra_rules_fold_into_the_verdict_by_can_block(self):
        pdbs = {"blocking": []}
        clean = {"blocking": [], "risks": [], "unknown": [], "notes": [], "rules": {}}
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True, clean), r.READINESS_READY)
        # Risks and notes never move the verdict.
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True, {**clean, "risks": [{"rule": "zonal-control-plane"}], "notes": ["x: y"]}), r.READINESS_READY)
        # An unknown from a rule that can block is unknown; from one that cannot, it is not.
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": [{"rule": "surge-capacity", "can_block": True, "reason": "no target"}]}), r.READINESS_UNKNOWN)
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": [{"rule": "zonal-control-plane", "can_block": False, "reason": "odd"}]}), r.READINESS_READY)
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": [{"rule": "a", "can_block": False}, {"rule": "b", "can_block": True}]}), r.READINESS_UNKNOWN)
        # An unknown of another shape counts, the conservative reading.
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": ["why"]}), r.READINESS_UNKNOWN)
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True, {**clean, "unknown": [{"rule": "a"}]}), r.READINESS_UNKNOWN)
        # Blocked beats an unknown from either kind of rule; the three rules' own unknown still holds.
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True, {**clean, "blocking": [{"rule": "x"}], "unknown": [{"rule": "y", "can_block": False}]}), r.READINESS_BLOCKED)
        self.assertEqual(r.readiness_status(None, self.CLEAR, self.NO_SKEW, True, clean), r.READINESS_UNKNOWN)
        # None means the per-entry rules did not run; the three rules decide alone.
        self.assertEqual(r.readiness_status(pdbs, self.CLEAR, self.NO_SKEW, True, None), r.READINESS_READY)


if __name__ == "__main__":
    unittest.main()
