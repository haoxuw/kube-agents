"""
readiness_rules — the rules `fleet_upgrade_report.py --readiness` evaluates after the three
in `upgrade_readiness.py` (drain-blocking PDBs, maintenance, node-pool skew).

One module per rule. Each exposes:

- `RULE_ID`: the key the rule's result is filed under in the JSON and named by in the table;
- `evaluate(cluster, member, items, target, context) -> {"blocking": [...], "risks": [...],
  "unknown": [...]}`: `cluster` is the `gcloud container clusters list` record, `member` the
  graded version row, `items` the objects one `kubectl get` read (None when that read
  failed), `target` the parsed target version tuple (None without one), and `context` the
  per-member read context (`project`, `location`, `cluster_name`, `run_cmd`, `cache`), which
  is how a rule that needs a read it shares with another rule performs it once;
- `describe(finding) -> str`: one finding as a table cell.

A finding is a dict carrying `rule` and `tier` (`blocking`, `risk` or `unknown`) plus the
rule's own fields; an `unknown` finding carries `reason`. A rule never raises for a read it
could not perform: that is an `unknown` finding with the reason, and a blocker is only ever
something the rule read.

`upgrade_readiness.EXTRA_RULES` is the registry, in evaluation order.
"""
