---
name: fleet-upgrade-verification
description: Reports every GKE cluster's control-plane and node-pool versions against a target version or each cluster's release-channel default, naming the members that lag and by how many minors; run again during a rollout, it shows which members started, completed or stalled since the previous run; with --readiness, it also grades each member on what would stop the upgrade, naming drain-blocking PodDisruptionBudgets, maintenance exclusions and windows, node-pool version skew, and, from the pod templates, data kept on the node, selectors on removed or deprecated node labels, runtimes and multi-process containers that a move to cgroup v2 breaks, node-image-coupled network agents, CUDA pins the target image's driver does not serve, and images on retired registries. Scans the linked GitOps repositories' manifests for apiVersions the target removes, with each hit's replacement. Read-only against GCP, the clusters and Git, from gcloud container, kubectl get and repository reads, keeping only its own record of each run and per-member kubeconfig files; the executed counterpart to gke-upgrades' advice.
---

# Fleet upgrade verification

Answer "against version X, which clusters lag, by how much, and is it the control plane or a node
pool" with a table read from the fleet, not from memory, and "which manifests in our GitOps
repositories declare an API that version X removes" with a scan read from Git. Use it when a user
asks whether an upgrade has reached every cluster, which members are behind a target, how far the
fleet is from a release-channel default, or whether the repositories are ready for a target
version. Run the version report again during a rollout and it also says, per member, what changed
since the previous run and which members have stopped moving (see "Track a rollout across runs").
With `--readiness` it also says, per member, what would stop the upgrade: a PodDisruptionBudget
that blocks every node drain, a maintenance exclusion or window, node pools too far below the
target, or a workload shape the new node image breaks, and what the pod templates put at risk
(see "Check upgrade readiness"). For upgrade plans, runbooks and checklists, use the
`gke-upgrades` skill; it links back here when the question is one these two scripts answer.

"Version skew" here is the gap between a member's versions and the target. It is not
configuration drift: the fleet-consistency audit compares a cluster's configuration against its
live peers, and the drift-detection design (not yet shipped) means live state diverging from Git.
Neither reads versions against a target.

## Run the report

```bash
./skills/fleet-upgrade-verification/scripts/fleet_upgrade_report.py \
  [--project <project>]... [--target-version <version>] [--rollout-in-progress] \
  [--readiness [--at <RFC 3339>] [--kubeconfig-dir <dir>]] \
  --output /opt/data/scratch/fleet_versions.json
```

- `--project` is repeatable and, when given, is the whole scope. Without it the script takes the
  union of `GCP_PROJECT_ID`, `GKE_PROJECT_ID` and `PROJECT_ID` with `MONITORED_PROJECT_IDS`
  (comma- or whitespace-separated) when set, or with every project visible to
  `gcloud projects list` plus gcloud's configured project when `MONITORED_PROJECT_IDS` is unset
  or blank.
- `--target-version` sets one target for every member, in the `MAJOR.MINOR.PATCH-gke.BUILD` form
  the fleet reports (`1.31.4-gke.1183000`); the `-gke.BUILD` suffix is optional and reads as build
  0 without it. Without the flag, each member is measured against its own release channel's
  `defaultVersion` from `gcloud container get-server-config`, fetched once per project and
  location, and the target column says which baseline was used, for example
  `1.31.4-gke.1183000 channel default (REGULAR)`.
- `--output` writes the same data as JSON: `members[]`, `errors[]`, a `summary` count per
  status, and the `rollout` block described below.
- `--rollout-in-progress` and `--state-dir` belong to rollout tracking, below; `--readiness`,
  `--at` and `--kubeconfig-dir` to the readiness check, below that.

The script runs `gcloud container clusters list`, `gcloud container get-server-config`,
`gcloud projects list` and `gcloud config get-value project` (plus `gcloud projects describe` for a
project whose `clusters list` was refused as API-disabled or whose configured identifier is a
project number), each with a 60-second timeout, and
with `--readiness` one `gcloud container clusters get-credentials` and two `kubectl get` per member.
It changes nothing in GCP or in any cluster; the only things it writes are its own record under
`/opt/data/state/fleet-upgrade-verification/`, the per-member kubeconfig files `--readiness`
needs, and the `--output` file. A failed or timed-out read is listed under the table and sets
exit code 1; the other projects, locations and members are still reported. A project whose own Kubernetes
Engine API is off holds no cluster and is not a failed read; a refusal naming another project,
such as a quota project, is one.

## Read the table

One row per cluster: project, cluster, location, channel, control-plane version, the lowest
node-pool version with its pool name, target, gap in minors, status, note.

The gap is the target's minor minus the minor of the member's lowest component (control plane or
lowest pool): positive when behind, `0` on the same minor, negative when the lowest component is
ahead. It is empty when the major version differs from the target, and the note says so.

- `lagging`: the control plane or at least one node pool is a minor or more below the target, or
  on a different major.
- `patch-behind`: everything is on the target's minor, but the control plane or a pool has a
  lower patch or gke build. A new patch reaches a channel default before any rollout wave has
  applied it, so a fleet is routinely patch-behind the morning after; report it, and keep it
  apart from `lagging`.
- `current`: control plane and every pool equal the target.
- `ahead`: newer than the target somewhere and nothing below it. Reported, never flagged; channel
  rollout waves are staged, so a member ahead of its channel default is routine. The gap is `0`
  when one component is at the target and the other ahead, negative when both are ahead.
- `unknown`: the control-plane version did not parse, no node pool's version parsed (or the
  cluster record has no node pools), the cluster has no release channel and no
  `--target-version` was given, `get-server-config` failed for the location, or the channel is
  not in that location's server config. The note names which.

A pool whose version does not parse is skipped and named in the note; the row is still graded on
the pools that do parse. A note reading `upgrade in flight` means the cluster or a pool is
`RECONCILING` or `PROVISIONING`; report that row as in progress rather than as a stall.

## Track a rollout across runs

Every run records its per-member result and compares itself with the previous run for the same
target, so two runs during a rollout show what moved between them. The record lives at
`/opt/data/state/fleet-upgrade-verification/<target>.json` (`channel-default.json` for a run
without `--target-version`), on the persistent volume the shell sandbox keeps between turns;
`--state-dir` points it elsewhere. Each target has its own record, so a run against a different
target starts a new baseline rather than comparing across targets.

The first run for a target prints one line saying the baseline was recorded and where. Every later
run prints a "Rollout progress" section after the table, naming the previous run's time and, per
member, the control-plane and lowest-pool versions then and now, the current status, and one of:

- `completed`: the member is `current` or `ahead` after a version change, or its status became
  `current` or `ahead` since the previous run. A `current` member that followed its channel
  default to a new version is `completed`, not `started`.
- `started`: the control plane or the lowest pool changed version without reaching the target,
  or the cluster or a pool is `RECONCILING`/`PROVISIONING`.
- `unchanged`: the same versions as the previous run. A member whose versions are the same but
  whose status changed (the channel default advanced under it) is also `unchanged`, but it starts
  a new observation series: it is not `stalled` in that run, and its elapsed time counts from it.
- `stalled (unchanged for <elapsed>)`: an `unchanged` member that is `lagging` or `patch-behind`
  while a rollout is active, seen at these versions and this status by the previous run too.
  Elapsed counts from the first of the consecutive runs that saw the member so, and keeps growing
  across runs until the member moves.
- `new`: no record of this member in the previous run.

An `unknown` grade (a failed `get-server-config` read, an unparsable version) is not evidence of a
move: the comparison falls back to the versions alone, the record keeps the last graded status
and its clock, and a member that is `current` at the same versions as an ungraded baseline is
`unchanged`, not `completed`.

A rollout is active when at least one member is `started` or `completed` in the same comparison,
or when the operator passed `--rollout-in-progress`, which is for a rollout whose first wave has
not produced a mover yet. Pass the flag when the user says a rollout is under way; when there is
no earlier record to compare with and the user asks which members have stalled, run the report
twice in the turn with the flag and read the second run's section. Without a mover or the flag,
an unmoved member is `unchanged`, never `stalled`, so two quiet runs a week apart do not invent a
stall. A member that is `current`, `ahead` or
`unknown` is never `stalled`.

A member in the previous record with no row this run is listed once under the section: dropped
from the record when its project was read cleanly (the cluster is gone), carried forward when
its project failed to read or was not in this run's `--project` scope, so a failed read never
loses a record or manufactures a stall. A record the script cannot read is reported on stderr and
replaced by a new baseline; a record it cannot write sets exit code 1 with the table still
printed. In the JSON, each member carries `progress`, `unchanged_since` and
`unchanged_for_seconds`, and the top-level `rollout` block has the record path, both run
timestamps, whether the rollout counted as active and why, a count per progress value, and the
members missing this run.

## Check upgrade readiness

`--readiness` adds a second table after the version table, one row per member, graded against
the same target as the member's version row, and a `readiness` object per member in the JSON
(`members[].readiness`, with a top-level `readiness` block holding the instant evaluated and a
count per verdict). Without the flag nothing changes. Three rules, each derived from a governance
SOP check and named beside it, then the workload rules of the next list, each derived from an
entry of the upgrade-failure catalogue (`docs/designs/upgrade-failure-catalogue.md`); the
maintenance rule departs from its SOP where the two differ, and says so below:

- **Drain-blocking PDBs** (`obtainability_audit_sop.md` §3.4). For each member the script runs
  `gcloud container clusters get-credentials` into a kubeconfig of its own under
  `${HERMES_HOME:-/opt/data}/.kubeconfigs/` (`kubeconfig_<project>_<cluster>_<location>.yaml`, one file per
  target so concurrent reads never share a current-context; `--kubeconfig-dir` moves the
  directory), adding `--dns-endpoint` when the cluster record says its DNS endpoint accepts
  external traffic, then one `kubectl get pdb,deploy,statefulset -A -o json`. A PDB is matched to
  the Deployments and StatefulSets in its namespace whose pod-template labels satisfy its
  selector (`matchLabels` and `matchExpressions`), and it blocks every drain when
  `maxUnavailable` is `0` or `0%`, or when `minAvailable` demands every expected pod: an integer
  at or above the pods the controller expects, or a percentage that rounds up to them the way
  the disruption controller rounds (`100%` always; `90%` on nine replicas too). The expected
  count is the matched workloads' replica total, or the PDB's `status.expectedPods` when that is
  larger, as it is when the selector also covers pods of a kind the read does not include. The
  cell names the PDB as `namespace/name`, the offending field and the workloads with their
  replica counts; each JSON finding carries `expected_pods` and `disruptions_allowed`, from the
  PDB's `status.expectedPods` and `status.disruptionsAllowed`, as corroboration. Skipped and
  counted in the note rather than graded: a PDB whose matched workloads are scaled to zero, an
  orphan matching no workload and covering no pod, and a PDB that covers pods of a kind the read
  does not include (a bare ReplicaSet, a custom controller), which is noted so it is never
  silently `ready`. DaemonSets are never matched: a drain deletes their pods rather than evicting
  them.
- **Maintenance** (`security_patch_orchestrator_sop.md` §3.7 and §3.8), evaluated at `--at`, an
  RFC 3339 instant, by default now. An exclusion in effect blocks when its scope covers the upgrade
  the target needs: `NO_UPGRADES` (the default when the record carries no scope) always;
  `NO_MINOR_UPGRADES` when the target is a minor above the control plane or a pool;
  `NO_MINOR_OR_NODE_UPGRADES` when it is a minor above the control plane or any pool is below the
  target. This grades the scope against the target, which the SOP's `blocking-exclusion` check
  does not: that check never flags `NO_MINOR_UPGRADES` and adds a 30-day threshold, so the two
  disagree on a cluster whose `NO_MINOR_UPGRADES` exclusion holds exactly the minor upgrade its
  target needs, and this table is the one that reads it as a blocker. The cell says
  `blocks auto-upgrade`, because that is what an exclusion holds back: an
  operator running `gcloud container clusters upgrade` by hand is not subject to it. An exclusion
  in effect whose scope does not cover the upgrade (a patch-only target under
  `NO_MINOR_UPGRADES`) is reported and not a blocker. The maintenance window is reported as
  `open` (with when it closes) or `closed` (with the next opening) at the instant, for a
  `dailyMaintenanceWindow` or a `recurringWindow` whose recurrence is `FREQ=DAILY` or
  `FREQ=WEEKLY` with a plain `BYDAY` list; any other recurrence is `not evaluated`, never a
  verdict. The window is informational: a closed window delays automatic maintenance, it does not
  block a manual upgrade. No window is reported as such.
- **Node-pool skew** (§3.2). Per pool, the target control plane's minor minus the pool's minor:
  more than 2, or a different major, blocks the control-plane upgrade until the pool moves
  (GKE keeps nodes within two minors of the control plane); exactly 2 is at the ceiling and
  goes in the note. Autopilot members read `n/a`.

The workload rules read the pod templates, never Pods: the first read's Deployments and
StatefulSets, and a second `kubectl get daemonset,cronjob,node,pvc,storageclass -A -o json` per
member. Bare Pods and Jobs are not read, and nothing in the namespaces GKE manages
(`kube-system`, `gmp-system`, every `gke-*`) is read: GKE upgrades those with the version. Each
rule is one module under `scripts/readiness_rules/`, and the fact tables they share (runtime
floors, driver and CUDA branches, dropped and deprecated labels, retired hosts, the cgroup v2
timeline) sit in `scripts/upgrade_shape_tables.py` with their sources. A rule grades a shape
`blocking`, `risk` or `unknown`; where its table has no row for the target or the image, the
grade is `unknown` with the reason, never a guess. The table prints the blockers in
`workload blockers` and the risks in `risks`, each as `Kind namespace/name: detail`; a shape a
rule saw and cleared (a pool already on cgroup v2) goes in the note, named, so a reader can check
the reasoning. The rules, with the catalogue entry each reads:

- **Data on the node** (entry 4, `data-on-the-node`). A Deployment or StatefulSet whose
  template mounts an `emptyDir`, a `hostPath`, or a claim on a StorageClass whose provisioner is
  `kubernetes.io/no-provisioner` (how Local SSD becomes a PersistentVolume) is a `risk`, named
  with the volume; a volume name that suggests state is marked. A StatefulSet is the strongest
  case: its identity survives the rebuild, its node-local data does not.
- **Removed node label** (entry 12, `removed-node-label`). Every `nodeSelector` and node
  affinity term is read against the Node list. A label or label value the target minor drops
  (`node-role.kubernetes.io/master` from 1.24, `cloud.google.com/gke-container-runtime=docker`
  from 1.24) is `blocking`; a label no node in the template's pools carries today is a `risk`;
  a deprecated beta label the kubelet still sets (`beta.kubernetes.io/arch`,
  `failure-domain.beta.kubernetes.io/zone` and their siblings) is a `risk`, named with its GA
  replacement. Without Node objects in the read, a selector is `unknown`.
- **cgroup v2 under an old runtime** (entry 14, `cgroup-v2-runtime`). A Java image tag below
  JDK 8u372, 11.0.16 or 15, or a .NET image tag below 5.0, on a pool the target moves to
  cgroup v2 (`effectiveCgroupMode` v1 and a target at or past GKE's 1.33 migration, or a pool
  pinned to v1 and a target at or past the 1.35 removal) is a `risk`; a known runtime whose tag
  names no version on such a pool, or a pool whose mode the record does not carry, is
  `unknown`; a pool already on cgroup v2, or staying on v1 at the target, is a note.
- **Group OOM kill** (entry 15, `group-oom-kill`). A heuristic, marked as one in every finding,
  because the process count is not readable from the API: a container whose command or args
  run `supervisord`, `s6-svscan` or `runsvdir`, whose shell `-c` script forks a background job,
  or whose image is a documented multi-process base, on a pool where the target turns the group
  kill on (a move to cgroup v2 at 1.28 or later, or a cgroup v2 pool's kubelet crossing 1.28)
  is a `risk`, never `blocking`. A pool already group-killing, or one whose node config sets
  `singleProcessOOMKill`, is a note.
- **Node-image-coupled agent** (entry 17, `node-image-coupled-agent`). A DaemonSet with
  `hostNetwork: true`, outside the system and Config Sync namespaces, that selects on a
  node-image label (`cloud.google.com/gke-os-distribution`,
  `cloud.google.com/gke-container-runtime`), mounts the kernel module tree or the network
  plugin's directories (`/etc/cni/net.d`, `/opt/cni/bin`, `/home/kubernetes/bin`), or runs
  `modprobe`, is a `risk`; the detail names the pools below the target, whose image this
  upgrade changes.
- **GPU driver mismatch** (entry 18, `gpu-driver`). For a template requesting
  `nvidia.com/gpu`, the CUDA version is read from the image tag or a CUDA-named env value, and
  the driver from the pools carrying the accelerator the template selects: the pool's
  `imageType` and `gpuDriverVersion` (`DEFAULT` or `LATEST`) pick a branch from GKE's per-minor
  table at the target's minor. A driver below the CUDA major's floor is `blocking`; one below
  the toolkit's own minimum is a `risk` (minor version compatibility); an image naming no CUDA
  version, or an accelerator no pool carries, is a `risk`; a target the driver table does not
  cover, an operator-installed driver, or a record with no install mode is `unknown`. A pin the
  driver serves is a note, with the reminder that forward-compatibility packages inside the
  image are not readable from the tag.
- **Retired registry** (entry 20, `retired-registry`). A container or init container pulling
  from `k8s.gcr.io`, `gcr.io/google-containers`, `gcr.io/google_containers` or
  `gcr.io/kubernetes-helm` is `blocking`, named with the image: a rebuilt node has no image
  cache and pulls from a host that stopped publishing. The detail names the pools below the
  target, whose nodes this upgrade rebuilds, or says no pool is below it yet.

A member is `blocked` when any rule blocks, whatever else could not be evaluated; `unknown` when
nothing blocked but a rule could not be evaluated (a cluster read failed, there is no target,
an exclusion's scope or a pool's version was unreadable, a workload rule's table has no row for
the target or the image); `ready` only when every rule was evaluated and none blocks. A failed
`get-credentials` or `kubectl get` is listed under the table as a read failure for that member
and sets exit code 1, like a failed gcloud read: a failed credentials fetch or PDB read leaves
the PDB and workload rules ungraded (the workload read is skipped, since the API server did not
answer), a failed workload read alone leaves the workload rules ungraded; the member's
maintenance and skew rules are still graded, and the other members are unaffected. `--at` with a
value that is not RFC 3339, or `--at` or `--kubeconfig-dir` without `--readiness`, is a usage
error (exit 2).

## Scan the GitOps manifests

```bash
./skills/fleet-upgrade-verification/scripts/api_deprecation_scan.py \
  --versions /opt/data/scratch/fleet_versions.json --target-version <version> \
  [--repo <owner/name | host/path>]... [--manifests-dir <path>] --output /opt/data/scratch/api_deprecations.json
```

- `--versions` is the version report's `--output`. The scan's floor is its lowest control-plane
  minor: the API server is what stops serving a removed version, so node-pool versions do not
  enter into it. `--current-version <version>` replaces the file when there is none.
  `--target-version` defaults to the file's `target_version` when the report was run with one.
- Without `--repo` the script scans every repository under `managed_repos`, on every forge, the
  same list the GitOps skills write to; `--repo` is repeatable and, when given, is the whole scope.
  A repository on another forge than GitHub is named `host/path`, as the list names it, and is read
  through the content workspace only: on a broker not armed for content-passing it is listed under
  errors, because the leased checkout reaches GitHub alone.
  `--manifests-dir` scans a local tree instead of, or as well as, repositories.
- It reads each repository the way `inspect-repository` does: through the credential broker's
  content workspaces as a shallow read-only clone, or, on an install whose broker is not armed
  for content-passing, through a leased checkout on the shared volume. That checkout is under
  a lease of the scan's own (`--lease` overrides it), so positioning it on the base branch
  never resets a checkout another skill is working in -- `fleet-audit`'s remediation clone is
  leased on its audit id on the same volume. It runs no `gcloud` and writes to no repository. A repository the broker or git cannot
  serve is listed under errors and sets exit code 1; the other repositories are still reported.
- Removal data is `removed_apis.json` beside the script: Kubernetes 1.16 through 1.32, from the
  upstream Deprecated API Migration Guide, whose URL and `as_of` version the report prints. The
  script cannot call MCP tools, so when the target is newer than the table (the report says so)
  confirm removals after it through `mcp-developer_knowledge` and cite what you read; confirm the
  replacement column the same way before you recommend a migration.
- It reads `*.yaml`, `*.yml` and `*.json`, every document in a file and the items of a
  `kind: List`. A file that does not parse (a Helm template, a Kustomize patch the loader
  refuses) is listed as skipped with the reason and is not scanned; rendering charts and
  overlays is not this script's job.

## Read the deprecation report

One section per repository, headed `repo manifests as of <sha>` (or `local directory <path>`),
so a reader knows which commit was read. A section with hits has one row per manifest: repo
path, kind, name, the apiVersion used, the minor that removes it, the replacement, and the
members affected, which are the members whose control plane is still below that minor. A hit
is an apiVersion removed after the floor and no later than the target; a removal at or below
the floor has already happened on every member and is not reported. A clean section says so
with the file and document counts it read. Under either, `skipped` lines name the files it did
not read and why, and a `partial` line means a size cap stopped the scan; a clean section with
skipped files is clean for what it read, so say that.

The scan reads what Git declares. Whether a client is still calling a deprecated API on a live
cluster is a different question, answered by GKE Deprecation Insights from the cluster's audit
logs: the console page the report's footer links, or
`gcloud recommender insights list --insight-type=google.container.DiagnosisInsight`, which the
report quotes for a human to run. That command is not in the agent's gcloud read allowlist, so
give it to the user rather than running it, and do not present the Git scan as proof that no
client uses the API.

## Report

Paste the table into the reply, then name the lagging members with both versions and the gap,
and the patch-behind members separately. When the run printed a Rollout progress section, paste
it too and name each `stalled` member with its elapsed time and the previous run's time, as an
observation rather than a fault: a member whose wave has not been scheduled yet reads the same
as one that is stuck, and the elapsed time is what lets the user tell them apart. A first-run
baseline line means there is nothing to compare yet; say when to run again. When the run printed
a readiness table, paste it too and name each `blocked` member with what blocks it as the table
states it: the PDB by `namespace/name` with its field and workload, the exclusion by name with
its scope and end time and that it holds back automatic upgrades only, the pool with its skew,
the workload blocker by kind and `namespace/name` with the image or label the cell names. Then
name each member's risks the same way, as what to watch rather than what stops the upgrade, and
say which rules read as `unknown` and why, from the note. Say what the operator has to change
before the upgrade can proceed; do not change it, and do not propose deleting an exclusion. When the question is a target version's readiness, paste each
repository's deprecation section too, with its source line, and state the floor and target the
scan used. Recommend the upgrade path and the manifest migrations; do not run either. Cite no CVE
identifiers: there is no vulnerability feed here, and every finding is version currency,
readiness or a declared API version.
