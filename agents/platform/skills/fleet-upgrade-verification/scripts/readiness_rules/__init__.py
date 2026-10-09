"""
readiness_rules — the rules `fleet_upgrade_report.py --readiness` evaluates after the three
in `upgrade_readiness.py` (drain-blocking PDBs, maintenance, node-pool skew).

One module per rule. Each exposes:

- `RULE_ID`: the key the rule's result is filed under in the JSON and named by in the table;
- `evaluate(cluster, member, items, target, context) -> {"blocking": [...], "risks": [...],
  "unknown": [...], "note": ""}`: `cluster` is the `gcloud container clusters list` record,
  `member` the graded version row, `items` the objects one `kubectl get` read (None when
  that read failed), `target` the parsed target version tuple (None without one), and
  `context` the per-member read context: `project`, `location` and `cluster_name` for a
  log filter, `run_cmd` (the report's runner, whose default timeout is the per-call cap),
  `at` (the evaluation instant, an aware datetime: the end of the audit-log window and the
  clock a recency rule reads), `cache` (a dict a rule fills so another rule reads the same
  thing once), and optionally `clock` (a monotonic clock for a read's time budget; tests
  inject one);
- `describe(finding) -> str`: one finding as a table cell.

A finding is a dict carrying `rule` and `tier` (`blocking`, `risk` or `unknown`) plus the
rule's own fields; an `unknown` finding carries `reason`. `unknown` is for a read the rule
could not perform (a failed or timed-out command, objects never read); a fact the rule's
own tables lack, or a page the read did not reach, is a note, so a member's verdict never
turns on what this repository has not written down. A blocker is only ever something the
rule read.

`upgrade_readiness.EXTRA_RULES` is the registry, in evaluation order.
"""
