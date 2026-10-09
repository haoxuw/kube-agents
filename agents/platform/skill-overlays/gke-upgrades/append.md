<!-- kube-agents: local addition (auto-injected by sync-upstream-skills.py) -->

## Executed version checks: the fleet-upgrade-verification skill

This skill plans one upgrade at a time; its references read one cluster at a time. When the
question is which clusters in a fleet lag a target version, by how many minors, and whether the
control plane or a node pool is the laggard, run the
[fleet-upgrade-verification](../fleet-upgrade-verification/SKILL.md) skill's script and paste its
table rather than reasoning from memory:

```bash
./skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py --target-version <version> \
  --output /opt/data/scratch/fleet_versions.json
```

Without `--target-version` it measures each cluster against its own release channel's default and
prints that baseline per member. Run again during a rollout, it says which members started,
completed or stalled since the previous run. Without `--readiness` (below) it reads with
`gcloud container` only and changes nothing in GCP; the only thing it then writes is its own
record of each run under `/opt/data/state/fleet-upgrade-verification/`. The plan, runbook and
checklist for the members it flags are this skill's job.

The same script's `--readiness` flag executes three items of this skill's pre-upgrade checklist
per member, against the same target: PodDisruptionBudgets that would block a node drain
(`maxUnavailable: 0`, or `minAvailable` demanding every expected pod), maintenance exclusions and
the maintenance window at a given instant (`--at`, default now), and node-pool version skew
against the target control plane. It then runs the per-entry rules the fleet-upgrade-verification
skill lists for the upgrade-failure catalogue, each a blocker or a risk the report lists under
`risks` (a zonal control plane is one). Run it before writing the plan; carry its `blocked` rows and
`other blockers` into the checklist rather than asking the operator to check those items by hand,
and its risks as what to watch. The cluster reads cost one `get-credentials` and four `kubectl get`
per member and leave a per-member kubeconfig under `${HERMES_HOME:-/opt/data}/.kubeconfigs/`; an
exclusion is reported as holding back automatic upgrades only.

When the checklist's deprecated-API item comes up, the same skill's `api_deprecation_scan.py` scans
the linked GitOps repositories' manifests for apiVersions the target removes and reports each with
its replacement and the commit it read; run it with `--target-version` and the version report's
`--output` from a full run, since it refuses a `--cluster` run's file as one cluster's floor. It
reads Git only: point at GKE Deprecation Insights for live client usage.
