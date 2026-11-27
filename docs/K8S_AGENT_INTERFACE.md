# ARI Kubernetes Agent — interface and cross-repo notes

What the `k8s` agent kind exposes to its callers (MCP Service, UIE/InfrastructureCore, the e2e script) and what
other repositories must do when it changes. **Every change to anything listed here is recorded in the change
log at the bottom, with the date and ticket.** The agent's own design lives in docs/MEMORY.md (II-25173 onward).

## Capabilities (what a caller can route to it)

Reads, no approval: Pod health of a Deployment, StatefulSet, DaemonSet, CronJob, node, namespace or the whole
cluster (crash loops with their cause; what killed a container: an OOM kill, with a suggested memory limit and the
request that would get it proposed, or a startup or liveness probe, with how long the probe allows; image pull
failures; unschedulable pods; warning events; usage near limits); listings of any readable kind (`query_result`).

Changes, each proposed first and applied only after a person approves that proposal (`action` names below):
a Deployment's replicas (`scale_replicas`); a KEDA ScaledObject's or HPA's min/max (`set_autoscaler_bounds`);
CPU/memory of a Deployment's container (`set_resources`, rolls its pods) or of one running pod in place
(`resize_pod`); a rolling restart (`restart_rollout`); deleting one pod of a Deployment (`delete_pod`); more time
for a container to start (`set_startup_probe`); growing a StatefulSet claim-template PVC (`expand_pvc`).

Refused, with the reason in the reply and a `refusal` (see "Never" below): changes to StatefulSets, DaemonSets,
CronJobs, nodes, protected namespaces and workloads; scaling to 0; shrinking a disk or growing a Helm/Flux PVC;
liveness/readiness probe changes; Secrets; anything without an exact value. A caller's routing text (an agent card
description, a coordinator prompt) should name these, and send every request about the cluster to the agent,
including ones it may refuse, rather than answer them itself: it is the agent that knows the cluster's state and
rules. (Found 2026-11-27: a description naming three changes made a chat answer that restarts and disks were "out
of scope" and hand out kubectl commands for exactly what the agent refuses.)

## Calling it (any caller)

- **Request text.** The agent acts only on values the request states, read from its words: pass the person's
  request with the namespace, the workload and every value exactly as given (sizes and CPU/memory with units, a
  start time as a duration such as "10 minutes"). Never invent, convert or change a value, and do not retry a
  refusal with other values; report its reason. A health report ends with the exact request that would get a fix
  proposed; use it as written. `last_run_seconds` (how long a container ran before it was killed) is not how long
  it needs to start.
- **Approval.** A proposal pauses the task (`pending_action`). It is applied only when the caller resumes the task
  with `confirm_pending_action.id` = that `pending_action.id`; declining is cancelling the task. Forward a person's
  explicit decision, not a model's reading of it (found 2026-11-27: a chat model answered "reject" itself and the
  proposal stayed paused), and cancel a proposal that will not be answered any more. A proposal older than
  `proposal_max_age_hours` is refused when confirmed.
- **Timing.** A confirmed change is applied and then verified before the task ends (rollout progress, new pods
  stable for 45 s, a resize as reported by the kubelet, a disk's capacity): the apply step allows 900 s, so wait at
  least 960 s after a confirmation. Cancelling after a confirmation stops only the verification, never the change;
  do not tell anyone that nothing changed (found 2026-11-27: a 300 s wait reported "cancelled" for a change in
  place).
- **Results.** Every applied change's `action_result` has `kubernetes_change: true`; recognise changes by it rather
  than by a list of action names. `query_result` rows are data for the caller to render; never have a model retype
  them. The agent's status message (the task's reply) is written for people; the artifacts are for programs.

## Request

A2A message: the prompt as a TextPart and one DataPart `{"llm": {...}, "options": {"namespace"?,
"high_replica_threshold"?, "node_headroom_ratio"?, "proposal_max_age_hours"?}}`. A confirmation resumes the
paused task with `{"confirm_pending_action": {"id": "<pending_action.id>"}}`; `proposal_max_age_hours` (default
24, 0 turns it off) is read from the confirming request's options. No cluster credential is ever sent.

## Artifacts (data parts, in this order)

| Artifact | Step field | When |
|---|---|---|
| `pending_action` | `propose_change.pending_action` | a change is proposed and the task pauses for approval |
| `health_report` | `check_health.report` | a health check ran |
| `action_result` | `apply_approved_change.action_result` | an approved change was applied |
| `refusal` | `propose_change.refusal` | a change was asked for and refused (nothing proposed) |
| `query_result` | `query_resources.table` | a listing ran (**added II-25740**, see below) |

Order matters: MCP Service confirms the **first** artifact carrying an `id`, and only `pending_action` has one.
Numbers come back as doubles after the A2A round trip (3 -> 3.0); every consumer must accept that.

### `pending_action`

Read by MCP Service: `id` (resume), `action` (its `refused_pending_actions` map), `action_name` (approval
label and email). Other fields are informational for the approver.

- `id`, `action`, `action_name`, `namespace`, `deployment` — unchanged.
- `target`: `{"kind", "namespace", "name"}` — the object the change is about. **Added II-25740.**
  `kind: "Deployment"`, except `resize_pod` and `delete_pod`, which set `kind: "Pod"` and still fill
  `deployment` with the pod's owning Deployment (and add `pod`), and `expand_pvc`, which sets
  `kind: "PersistentVolumeClaim"` with `deployment` null. Every other change lands on a Deployment or one of
  its pods (decided 2026-11-26; see "Never"). A proposal without `target` (made before II-25740) is still
  applied.
- Per-action fields (spread into the proposal beside the common ones; informational, the agent rebuilds the
  write from them), **added II-25740**: `restart_rollout`: `workload_kind` (`Deployment`), `workload`,
  `restarted_at` (the value the template's `kubectl.kubernetes.io/restartedAt` gets). `delete_pod`:
  `workload_kind`, `workload`, `pod`, `pod_uid`. `resize_pod`: `pod`, `container`. `expand_pvc`: `pvc`,
  `pvc_uid`, `storage` (the size asked, a Kubernetes quantity), `workload_kind` (`StatefulSet`), `workload`
  (the StatefulSet whose claim template made the PVC); `before`/`after` are `{"storage": ...}`.
- `persistence.needed`: `false` when nothing has to change in Git (a restart, a deleted pod, an in-place
  resize back to the template's own values); `reason` says why and `changes` is empty.
  `true` otherwise. **Added II-25740.**
- Refusal fields (the `refusal` artifact): `asked_for` (the action the model picked when the request's own
  words do not ask for it), `protected: true` (a protected namespace or workload, or the cluster's admission
  policy said no). **Added II-25740.**
- `before`, `after`, `requested`, `clamped`, `node_cap`, `risk_tier` (low/medium/high/critical), `risks`,
  `notes`, `capacity_outlook`, `dry_run`, `ownership`, `drift_notice`, `persistence`,
  `estimated_seconds_per_pod_to_ready`, `snapshot`, `created_at` — unchanged.

### `action_result`

Read by MCP Service (`a2a.task_actions`: `action` in its `KUBERNETES_CHANGE_ACTIONS`, `action_name`) and
UIE (`ran`).

- `proposal_id`, `action`, `action_name`, `ran` (always true here), `namespace`, `deployment`, `applied`,
  `requested`, `clamped`, `status`, `verification`, `drift_notice`, `persistence`, `executed_at` — unchanged.
- `target`: as in `pending_action`. **Added II-25740.**
- `kubernetes_change`: `true`. **Added II-25740.** A marker for "this agent changed the cluster" that does
  not depend on the action name.
- `status` for `resize_pod` (verified on the same pod, by uid): `succeeded`, `in_progress`, `resize_infeasible`
  (the kubelet cannot fit it on the node), `resize_deferred` (accepted, waiting for room on the node),
  `pod_replaced` (the pod was replaced while watched), `container_failing` (applied, then the container failed
  or restarted again). **Added II-25740.** The other actions keep their statuses.
- `status` for `restart_rollout` (II-25740): `succeeded`, `in_progress`, `new_pods_failing`, `rollout_failed`,
  `unschedulable`. `delete_pod`: `succeeded` (the old uid is gone and a replacement is ready and stable;
  `verification.replacement` names it), `in_progress`, `new_pods_failing`, `unschedulable`. A pod already
  gone or terminating when the approval arrives was deleted by someone else: refused (see below).
- `status` for `expand_pvc` (2026-11-26): `succeeded` (the PVC's `status.capacity` reached the size;
  `verification.capacity`), `resize_pending` (asked for, the volume has not reached it within the wait; it
  grows on its own, a volume no pod mounts when next mounted; a `ControllerResizeError` / `NodeResizeError`
  condition is named in `verification.detail`, and the resizer retries it), `resize_failed` (the driver or kubelet reported
  it infeasible), `pvc_replaced` (the PVC was deleted or replaced while watched). A PVC already asking for the
  size when the approval arrives was grown by someone else: refused (see below).
- Done by someone else (2026-11-26, every action): when the approval arrives and the target already holds what
  was approved (replicas at the number, the container's resources, the autoscaler bounds, the pod's resources,
  the PVC's size, the pod gone), someone else did it and this agent did nothing, so the approval is refused (the
  step fails; the reason says "it already holds what was approved, done by someone else, not this agent"),
  never reported as this agent's change. Only a retried attempt of the step, after its own first attempt may
  have written (the worker was lost), finds the change in place and verifies it: `applied.already_at_target`
  is true. Since 2026-11-27 that retry asks the cluster who set it: every field the change sets (replicas, the
  container's requests/limits, `minReplicas`/`maxReplicas`/`minReplicaCount`/`maxReplicaCount`, the pod's
  resources, the PVC's `requests.storage`) must be owned by this agent's field manager `ari-k8s-agent` in
  `metadata.managedFields` (shared ownership counts); then `applied.field_manager` is `ari-k8s-agent`. Owned
  only by another manager (`kubectl-patch`, `kubectl-edit`, ...) it is refused as done by someone else; with no
  owner on record it is refused as undecidable ("records no field manager for it"; that refusal does not say
  nothing was changed). A write that errored but left the target at the approved values is judged the same
  way. A pod delete leaves no owner to read, so a retried delete still counts an already-gone pod as its own;
  to keep that retry for a lost worker only, a delete step that fails before its DELETE is sent ends there
  (refused, "failed before its change was sent ... It is not retried"), and the person asks again.
  A restart cannot be done by someone else: its approved `restartedAt` is the proposal's own.
- An approval is not applied (the step fails, nothing is written) when its target has become protected, when
  its kind is no longer changed (a StatefulSet or DaemonSet restart, or a delete of such a pod, proposed before
  2026-11-26), for an action that no longer exists, when the proposal is older than `proposal_max_age_hours`,
  or when what the change depends on moved while the approval waited: the Deployment deleted and made again
  (new uid), its replicas or the container's resources changed by someone else (for a scale-up, any container's
  CPU/memory), an autoscaler added, removed
  or changed, a rollout since a proposed restart (the Deployment's revision moved: its pods are new already), a
  pod no longer ready beside the one to delete, a PVC whose size, StorageClass expansion or `Bound` phase
  changed. When the cluster already holds the approved values, nothing is written and the result is verified;
  `applied.changed_since_proposal` lists what differed from the proposal's snapshot. **Added II-25740.**

### `health_report`: what restarted a container (**added II-25740, 2026-11-27**)

A Pod finding about a container's last run (`CrashLoopBackOff (...)`, `FrequentRestarts (...)`, the new
`Restarted (...)` for 1-2 restarts that a probe caused, or a terminated container) carries, when the cluster
recorded it:
- `killed_by`: `oom` (the runtime's `OOMKilled`), `startup_probe` / `liveness_probe` (the kubelet's Normal
  `Killing` event "Container <c> failed <kind> probe, will be restarted", read by the new tool
  `list_probe_kills`, last 60 minutes), or `not_recorded` (ended by a signal, exit 137 or 143, with neither on
  record: a probe kill whose events expired, a stop that outlasted its grace period, or a kill from outside).
  An ordinary crash (non-zero exit code of its own) has no `killed_by`. The exit code alone never decides it:
  a probe restart is SIGTERM then SIGKILL, so it ends 143, 137, or 0 `Completed` (all three seen on test).
- `diagnosis`: one sentence on what happened and what to change: for a startup probe, raise its
  `failureThreshold` (or `periodSeconds`); for a liveness kill within the liveness probe's allowance and no
  startup probe (`during_startup: true`), add a startup probe with the liveness probe's check rather than
  loosening liveness; for a later liveness kill, a hang or a check slower than its `timeoutSeconds`.
- For a probe kill also `probe_kills` (event count), `last_run_seconds` (the last run, start to finish) and
  `probe`: `{kind, allows_seconds, initial_delay_seconds, period_seconds, failure_threshold, timeout_seconds,
  handler}`, where `allows_seconds` is initialDelaySeconds + failureThreshold x periodSeconds and `handler` is
  the kind of check (`exec`, `httpGet`, `tcpSocket`, `grpc`), never the check itself.
- The reason names the probe: `CrashLoopBackOff (killed by startup probe)`; OOM kills keep
  `CrashLoopBackOff (OOMKilled)`. A probe-killed container's previous log is attached like a crash loop's.
- An unreadable `Killing` list is a gap ("probe kills (Killing events) could not be read").
- An OOM kill also carries `suggestion` (user decision): `{field: "limits.memory", container, from, to, basis,
  note, request}`, `to` = 2 x the current limit (`check_health.OOM_LIMIT_FACTOR`), rounded up to Mi and capped by
  the largest eligible node (as `set_resources` caps; `check_health` now reads nodes and every active pod for
  it, only when there is an OOM finding). `request` (the request that would get it proposed) only for a
  Deployment's pod. No `to` when the container has no memory limit (killed by node memory pressure: the note
  says to reserve memory with a request) or no node can give more than the limit (the note says so). A first
  step, not a measured need: the peak before the kill is not on record. `pattern`: `early` (killed within
  `LONG_RUN_SECONDS`, an hour, of starting: a limit set too low), `long_run` (after an hour or more: more like a
  leak or growth with load; the note says a larger limit only delays the next kill), `unknown` (run time not
  recorded). The value is the same either way. OOM findings now carry `last_run_seconds` too.

### `query_result`

A table, read-only, data only: the agent does no formatting; how to show it is the caller's choice. No `id`.
`schema` (`"k8s.query_result/v1"`; bumped on any incompatible change); `kind`; `query` as applied (`namespace`, `all_namespaces`, `label_selector`,
`filters` [{`column`, `op`, `value`}], `sort` [{`column`, `desc`}], `limit`); `ignored` (parts of the model's
query not applied, with why: a namespace, label selector, filter value or limit the request does not state is
never applied); `columns` (once), `column_types` (same order; see below) and `rows` (lists in that order;
null for missing, never NaN); `matched`,
`returned` (at most 200), `truncated`; `gaps` (a kind the agent may not list, or the cluster does not serve,
or metrics unavailable: never an empty table); `notes`. Kind `Container` is one row per container of every
pod not Succeeded/Failed, with CPU/memory request, limit, usage and usage as % of request and limit. The
agent's text reply only summarises (query, counts, gaps, a few notable rows); the rows are in this artifact
only. A refused query (a Secret, an unknown
kind, a namespace that does not exist) comes back as `query_resources.refusal` in the step results, not as an
artifact.

Column types (`k8s_agent/tools/table.py`, `TYPES` / `column_type`; any value may be null):

| Type | Value |
|---|---|
| `string` | text |
| `integer` | whole number |
| `number` | decimal number |
| `boolean` | true / false |
| `timestamp` | RFC 3339 date-time string |
| `cpu` | Kubernetes CPU quantity string (`500m`, `2`); usage normalised to whole millicores (`741m`) |
| `memory` | Kubernetes memory quantity string (`512Mi`, `2Gi`); usage normalised to whole Mi (`2488Mi`) |
| `storage` | Kubernetes storage quantity string (`10Gi`) |
| `percent` | decimal number, 90.0 = 90%; null when either side is unknown |
| `integer_or_string` | whole number or percentage string (`1`, `50%`; PodDisruptionBudget min/max) |

Each kind's columns and their types are fixed in code (the catalog's row for the kind, `Container` in
`table.py`); a test holds every value to its declared type, and on 2026-11-24 all 35,537 values of every
kind on the test cluster conformed.

### Action names (`action`)

`scale_replicas`, `set_autoscaler_bounds`, `set_resources`, `resize_pod`, `restart_rollout`, `delete_pod`,
`expand_pvc`, `set_startup_probe`, today (`k8s_agent/actions/__init__.py`, `ACTIONS`). Every one but `expand_pvc` changes a
Deployment, its autoscaler or one of its pods: StatefulSets, DaemonSets, CronJobs and nodes are read only, and
`propose_change` refuses a change to one before asking the model (safety review with the user, 2026-11-26:
nothing that could break a product environment).

`expand_pvc` (**added II-25740, 2026-11-26**): grows ONE PVC to a size the request names; never smaller.
- Resolved by `find_workload` when the request asks for a disk's size to change (`policy.asks_disk_size_change`)
  and names the PVC verbatim: `{"scope": "pvc", "kind": "PersistentVolumeClaim", "namespace", "name"}`, ahead
  of a Deployment of the same name (charts often name both alike). Planned by code alone: the size is read
  from the request's words (`policy.requested_storage`: a unit is required; a size to reach, or an amount to
  add ("by 10Gi", "加 10Gi"), which is added to the current capacity at proposal time, shown in
  `requested.computed` ("10Gi + 10Gi = 20Gi") and written as the size it comes to, so an approval never adds
  twice; both kinds together, or several sizes without one clear target, are refused); the model is not asked.
- Only a PVC that a StatefulSet's `volumeClaimTemplates` created (name `<template>-<statefulset>-<ordinal>` of
  a StatefulSet in its namespace, no Helm/Flux/Argo CD label, annotation or field manager, no owner but that
  StatefulSet or its pod). Anything else is refused with "change the size in Git": checked on test, a
  Helm-rendered PVC grown in the cluster makes the release's next upgrade fail (`field can not be less than
  status.capacity`) and stall until Git catches up; a StatefulSet's claim-template PVC grown the same way did
  not affect its release's upgrade.
- The StatefulSet and the namespace must not be protected; the StorageClass must allow volume expansion; the
  PVC must be Bound and not resizing; the size must be larger than the capacity and at most
  max(2 x capacity, capacity + 50Gi). Tier `high` (permanent, costs more); on EBS, a note that the volume
  can change again only after this change finishes optimizing, at most four times in 24 hours (AWS). A disk request about a StatefulSet itself is refused with its PVC
  names.

`resize_pod` (**added II-25740**): CPU/memory of one container of one running pod of a Deployment, in place
through `pods/resize` — no rollout, same pod. Chosen only when the request asks for an in-place change (decided
in code; naming a pod is not enough). Proposed only when the API server serves `pods/resize` (live discovery)
and both the API server and the pod's kubelet are >= 1.35; memory only when the container's memory
`resizePolicy` is `RestartContainer`. The Deployment template is not changed, so the persistence hint names the
template fields to change in Git.

`set_startup_probe` (**added II-25740, 2026-11-27, user decision**): more time for one container of a Deployment
to start, for a start that outlasts its probes (an index rebuild, a Java service's warm-up) and is restarted forever.
- Proposed only when the request asks about the startup probe or how long the container may take to start
  (`policy.asks_startup_change`), with the time in its own words: a duration with a unit ("10 minutes", "600s",
  "10分钟", read by code from the quote, `policy.stated_duration`), or failureThreshold and/or periodSeconds.
- No startup probe: one is added whose check is a copy of the container's liveness probe's (readiness's when it
  has no liveness probe; neither: refused, the agent never makes up a check), initialDelaySeconds 0, the source's
  timeoutSeconds, periodSeconds from the source unless stated. The check is copied from the live Deployment when
  the operation is built (`ActionSpec.build_operations`, dry run and write alike), never carried in the proposal (an
  exec command can hold a password): the proposal has `copy_check_from` (`livenessProbe` / `readinessProbe`) and
  `check_digest` (a keyed fingerprint of the check: HMAC-SHA256 with a key derived from the worker's
  `TEMPORAL_PAYLOAD_ENCRYPTION_KEY`, so it cannot be used to test guesses of a secret in the check; without that
  key no startup probe is added), and a check that changed since is not copied.
- A startup probe: its failureThreshold (and periodSeconds when stated) grows; its check stays.
- Only ever more time than the container gets now (its startup probe's allowance, or its liveness probe's), at
  most 3600 s, one hour (`MAX_STARTUP_SECONDS`, user decision). Fields: `container`, `failure_threshold`, `period_seconds`,
  `copy_check_from`, `check_digest`, `timeout_seconds`; `before`/`after` `{"startupProbe": "<text>"}`. Tier
  `medium` (rolls the pods; `high` under Recreate); a start time beyond the Deployment's progressDeadlineSeconds
  is a `medium` risk (the rollout can be marked failed while pods still start).
- Approval-time: any change to the container's probes since the proposal is drift; "already at target" means the
  startup probe holds the approved failureThreshold, periodSeconds and copied check; a retry counts it as its own
  by `f:startupProbe.f:failureThreshold` / `f:periodSeconds` ownership. Verified as a rollout (`wait_rollout`,
  target kind `startup_probe`; a probe put back while watched is `reverted`).
- Liveness and readiness probes are never changed (Never list).

Lowering CPU/memory (`set_resources` and `resize_pod`, **2026-11-26, user decision**). A decrease is a value
below the current one, a limit where there was none, or a request below the limit it defaulted to.
- A memory request or limit goes down only when metrics.k8s.io reports the container's current memory usage
  (the highest across the Deployment's pods for `set_resources`; the pod's own for `resize_pod`) and the new value
  is at least 1.2 x it (`policy.DECREASE_USAGE_MARGIN`, not configurable); otherwise refused, naming the lowest
  value it would propose. No usage sample (metrics-server missing, the pods crash-looping) refuses the decrease.
  Raises need no metrics.
- Any decrease, CPU too, is a `high` risk that shows the current usage (a sample now, not the peak over time) and
  whether a new CPU limit is below the current CPU usage.
- The approval re-reads usage: a memory decrease that the usage now refuses is not applied ("usage changed").
- A failed rollout or resize is reported (`rollout_failed`, `new_pods_failing`, ...), never undone by the agent;
  this rule is what keeps a lowered value from being the cause.
- `set_resources` on a Recreate Deployment is `high` (every pod stops before the new ones start), as
  `restart_rollout` is.

Added II-25740. Restart and delete are proposed only when the request's own words ask for them
(decided in code: an on-call prompt quoting "restarted 5 times" does not); otherwise the refusal carries
`asked_for` with the action the model picked.
- `restart_rollout`: a rolling restart of a Deployment, as `kubectl rollout restart` (sets
  `kubectl.kubernetes.io/restartedAt` on the pod template). Refused for a paused Deployment or one with no
  pods. Tier `medium`; `high` under `Recreate`.
- `delete_pod`: one pod of a Deployment, named in the request; deleted with a uid precondition (a plain
  delete, not an eviction: a PodDisruptionBudget is reported, not waited for). Refused, at proposal and again
  at approval, when no other pod of the Deployment is ready. Tier `medium`.
- `find_workload` resolves StatefulSets, DaemonSets, CronJobs (also from a scheduled run's pod name) and nodes
  too, for health checks: `{"scope": "workload", "kind", ...}`, `{"scope": "node", "kind": "Node",
  "namespace": null, "name"}` (a node only when no workload matches); a Deployment keeps `{"scope":
  "deployment", ...}`. `check_health` covers a StatefulSet's, DaemonSet's or CronJob's own pods (a suspended
  CronJob is `Suspended`, info) and a node's pods (`NotReady` warning, `Cordoned` info), so
  `health_report.scope.type` can be `node`.

Protected namespaces and workloads (II-25740, 2026-11-26): never changed, whatever a request or an approval
says. The oncallari chart's `ariK8sAgent.protection` values hold both lists and feed two checks:
- the worker's environment (`K8S_AGENT_PROTECTED_NAMESPACES`, `K8S_AGENT_PROTECTED_WORKLOADS`): `propose_change`
  refuses before asking the model (refusal `protected: true`), and `apply_approved_change` refuses an approval
  whose target has become protected (`tools/protection.py`);
- a ValidatingAdmissionPolicy (Kubernetes >= 1.30; left out on an older cluster unless required, see RBAC) that
  has the API server deny the agent's ServiceAccount any write in a protected namespace, and to a protected
  Deployment, its pods (through their ReplicaSet) and its autoscalers (their target before and after the
  request), and any change to what an autoscaler scales (`spec.scaleTargetRef`; the agent only changes bounds);
  from oncallari 0.3.19 also any write to a PVC that Helm, Flux or Argo CD marks, and to a PVC named like a
  protected StatefulSet's claim (`<template>-<name>-<ordinal>`); from 0.3.20 also any PVC update other than a
  larger storage request (labels, annotations, owners, finalizers, volumeAttributesClassName and limits must stay
  as they are). A dry run it denies comes back as a refusal
  (`protected: true`).
- The code holds no workload names (they are installation-specific; the oncallari defaults below are
  InsightFinder's). A chart shipped for other installations should default `workloads` to empty: the agent's
  own API and worker are added anyway.
- Namespaces by default: kube-system, kube-public, kube-node-lease, flux-system, cert-manager, vault, keda,
  traefik, ingress-nginx, istio-system (the last two from oncallari 0.3.21: stg and prod route traffic through
  them; not monitoring, knative-*, reloader or openvpn, user decisions 2026-11-26). Workloads by default (in the release namespace): insightfinder-mcp-service,
  insightfinder-mcp-service-worker, temporal-*, ari-coordinator; the chart always adds the agent's own API
  and worker. The agent runs beside the services it changes (`insightfinder` on test), so it and its approval
  path are protected by name, not by namespace.

Not to be built (decided 2026-11-26): `batch`, `flux_reconcile` (`patch` on HelmReleases/Kustomizations cannot
be narrowed by RBAC to the reconcile annotation), `resize_pod` for StatefulSet pods, and the earlier "later"
list (suspending Flux reconciliation, pausing KEDA, StatefulSet/DaemonSet CPU/memory).

### Never (permanent boundaries)

Not to be added. A change to this list is a decision of its own, recorded here and in docs/MEMORY.md.

- Reading Secrets (any verb), reading ConfigMap values (keys only), copying nested content of custom
  resources (top-level scalars only).
- exec / attach / port-forward into pods.
- Shrinking a PVC, or growing one that Helm, Flux or Argo CD applies (its size changes in Git).
- Creating or deleting workloads, Services, Ingresses, Namespaces or PVCs (deleting one pod of a Deployment,
  which the Deployment replaces, is the only delete). This includes creating a Job from a CronJob ("run it
  now"): considered as `trigger_cronjob` and declined by the user on 2026-11-25. It also includes deleting a
  PVC a StatefulSet scale-down left behind: asked about and declined by the user on 2026-11-26 (with gp3's
  `reclaimPolicy: Delete` the EBS volume and its data go with it, and `delete persistentvolumeclaims` cannot be
  narrowed by RBAC); people delete them with kubectl.
- Changing StatefulSets, DaemonSets or CronJobs, or their pods (decided 2026-11-26: nothing that could break a
  product environment). Built on test and removed: StatefulSet scaling, StatefulSet/DaemonSet restart and pod
  delete, `suspend_cronjob` / `resume_cronjob`. Stateful systems (databases, quorum services such as vault)
  and node-level agents (CNI, CSI, kube-proxy) can break in ways the inverse change does not undo, and a
  suspended CronJob (a backup, a cleanup) fails silently later.
- Pausing or resuming rollouts (`pause_rollout` / `resume_rollout`, built on test and removed on 2026-11-26).
  Checked first on test (helm-controller v1.6.3): a HelmRelease upgrade of a paused Deployment succeeds and the
  resume rolls it out, so it does not break a release; removed for its narrow value (no next step for the
  agent once paused, it holds back fixes too, and `kubectl rollout pause` is one command).
- Changing what code runs: `rollback` and `set_image` were built on test and removed on 2026-11-26. Old code on
  data a newer version migrated, or a new image's own migrations, cannot be undone by another change, and a
  different repository would run arbitrary code with the service's credentials. Images change through Git and
  the image automation.
- Changing anything in a protected namespace or a protected workload (above).
- Several targets under one approval (`batch`, not built).
- Changing the content of Services, Ingresses, NetworkPolicies or ConfigMaps: networking and configuration
  change through Git.
- Changing nodes at all: deleting them, their labels or taints, and also cordon, uncordon and drain. The three
  were planned, built (commit `80cd84c`) and declined by the user on 2026-11-25: on EKS with Cluster Autoscaler
  and spot nodes a bad node is usually replaced rather than drained, a drain on these clusters stops several
  single-replica services at once, and `patch` on nodes cannot be narrowed by RBAC to `spec.unschedulable`.
  Node groups, Cluster Autoscaler and people with kubectl own nodes.
- Rolling back a HelmRelease (Flux upgrades it again; revert in Git, or roll back the Deployment).
- Writing any custom resource other than a KEDA ScaledObject's min/max (no Flux reconcile annotation either).
- Editing RBAC, writing to Git, or any write without a person's approval of that exact proposal.
- Scaling anything to 0 replicas.
- Changing liveness or readiness probes, or a probe's check (decided 2026-11-27): loosening them affects the running
  service (a hung pod restarted later, a broken pod serving longer); only a startup probe gets more time.

## RBAC

`docs/examples/k8s-rbac.yaml` is the reference; the oncallari chart's
`templates/ari-k8s-agent/ari-k8s-agent-rbac.yaml` (charts repo) mirrors it rule for rule. Writes, from
oncallari 0.3.17: `patch` on `deployments` and `deployments/scale` (apps), `horizontalpodautoscalers`
(autoscaling) and `scaledobjects` (keda.sh), `patch` on `pods/resize` and `delete` on `pods` (core); from
0.3.19 also `patch` on `persistentvolumeclaims` (core) for `expand_pvc`. 0.3.15
and 0.3.16 also granted `patch` on statefulsets, daemonsets and cronjobs; 0.3.17 drops them with the actions
that used them. RBAC can only allow, so the protected namespaces and workloads are denied by the chart's
ValidatingAdmissionPolicy (`templates/ari-k8s-agent/ari-k8s-agent-admission-policy.yaml`,
`ariK8sAgent.protection.admissionPolicy`: `auto` by default renders it when the cluster serves
admissionregistration.k8s.io/v1 ValidatingAdmissionPolicy, Kubernetes >= 1.30, and otherwise leaves it out with
a warning in the release notes; `true` requires it and fails the render without the API; `false` never renders
it). Without the policy the worker's own check still applies. InsightFinder's GitOps values should set `true`,
so a cluster without the API fails loudly instead of running unenforced.
Until a cluster has a rule an action needs, the dry run's 403 comes back as a refusal naming it. The chart is
this agent's own deployment: any change here that needs a new RBAC rule, env var or value updates the
oncallari chart in the same piece of work (charts repo, same ticket branch, `Chart.yaml` version bumped).

## Needed from other repositories

Recorded for their owners; not changed from this repository.

| Repo | What | Why | Since |
|---|---|---|---|
| insightfinder-mcp-service | `app/a2a/client.py`: `KUBERNETES_CHANGE_ACTIONS` hard-codes three action names. Recognise `action_result.kubernetes_change == true` instead (or as well). | New actions would otherwise be missing from the incident digest. Now needed: `resize_pod`, `restart_rollout`, `delete_pod`, `expand_pvc` and `set_startup_probe` ship with II-25740 (`rollback`, `set_image`, pause/resume and the CronJob actions were removed before shipping). | II-25740 |
| insightfinder-mcp-service | `config/agents/arik8s.yaml` description and `app/oncall/prompt.py` K1–K4 rules describe health checks and three changes only; they should follow "Capabilities" and "Calling it" above (route every request about the cluster to the agent; values verbatim; no retry of a refusal). In chat: forward approve/reject as the person's decision and cancel unanswered proposals; `a2a.timeout_s` at least 960. | ARI chat on test, 2026-11-27: PVC, restart, StatefulSet and Secret requests never reached the agent (kubectl commands instead); a start time was rewritten and retried down to 25 s; a "reject" was answered without the agent; an approved change was reported "cancelled" after 300 s. A change set that fixed all of these was run on test (docs/MEMORY.md, 2026-11-27). | II-25740 |
| test-fluxcd, eks-fluxcd | Set `ariK8sAgent.protection.admissionPolicy: true` in the ari-k8s HelmRelease values (the chart default `auto` silently leaves the policy out on a cluster without the API). | A missing policy should fail the release, not go unnoticed. | II-25740 |
| eks-fluxcd | When the k8s agent is deployed to stg/prod: check `ariK8sAgent.protection.workloads` against that cluster's names (the defaults are bare names in the release namespace: insightfinder-mcp-service, insightfinder-mcp-service-worker, temporal-*, ari-coordinator) and add whatever runs the approval path there. | The agent and its approval path are protected by name; a name that differs there is not protected. | II-25740 |
| insightfinder-mcp-service | Render the `query_result` artifact (`columns`, `column_types`, `rows`) for the user; the agent's text only summarises it. Do not have a model re-type the rows: 100+ rows exceed its output limit and re-typed rows can be altered. | The stg question returns 127 rows on test's `insightfinder` namespace. | II-25740 |
| insightfinder-mcp-service | Confirm live that a follow-up chat turn ("in table format") can reach the previous turn's agent result, or that the coordinator calls the agent again. | Screenshot of 2026-11-19 (stg): the coordinator answered from memory. | II-25740 |

## Change log

- **2026-11-20, II-25740 phase 0** — `pending_action.target` and `action_result.target` /
  `kubernetes_change` added (additive; old fields unchanged; proposals without `target` still apply). No
  artifact, action name or RBAC rule removed or renamed.
- **2026-11-24, II-25740** — new action `resize_pod` (in-place pod resize): `target.kind: "Pod"`, extra
  `pod` / `container` fields, new `action_result.status` values for it (listed above). RBAC adds `patch` on
  `pods/resize`; oncallari chart 0.3.14. Additive; nothing removed or renamed.
- **2026-11-24, II-25740** — default `options.high_replica_threshold` 50 -> 30 (behaviour change for callers that do not pass it, mcp-service included).
- **2026-11-24, II-25740** — planned actions extended (`flux_reconcile`, `resize_pod` for StatefulSet pods, `expand_pvc`), a "later" list, and a permanent "Never" list (user decision). Documentation only; nothing built.
- **2026-11-24, II-25740 phase 1** — new capability `query_resources` and artifact `query_result` (appended after the existing artifacts; nothing reordered). The agent card's description, skill and examples now mention listings.
- **2026-11-24, II-25740 phase 1** — `query_result` is data only: `schema` and `column_types` added; the agent no longer appends a Markdown table to its reply (never shipped).
- **2026-11-24, II-25740 write batch 1** — new actions `restart_rollout`, `rollback`, `delete_pod` and their
  per-action proposal fields; `pending_action.target.kind` can be `StatefulSet`/`DaemonSet` (restart) or `Pod`
  (delete), with `deployment` null for a non-Deployment workload; `persistence.needed`; refusal `asked_for`;
  new `action_result.status` meanings listed above. RBAC adds `patch` on statefulsets/daemonsets and `delete`
  on pods (oncallari 0.3.15). Additive; nothing removed or renamed.
- **2026-11-25, II-25740** — `scale_replicas` also scales a StatefulSet: `target.kind: "StatefulSet"` with
  `deployment` null; new proposal fields `workload_kind` / `workload` on every `scale_replicas` (a Deployment's
  `deployment` stays); refusal field `controller`. No new RBAC (patch on statefulsets, oncallari 0.3.15).
  Additive; nothing removed or renamed.
- **2026-11-26, II-25740** — safety review (user): removed before shipping `rollback`, `set_image`,
  `pause_rollout`, `resume_rollout`, `suspend_cronjob`, `resume_cronjob`, StatefulSet scaling and every StatefulSet/DaemonSet change (their
  per-action fields and statuses with them); `delete_pod` refuses the only ready pod; new refusal field
  `protected`; protected namespaces and workloads (chart values, worker env, ValidatingAdmissionPolicy).
  oncallari 0.3.17 drops `patch` on statefulsets, daemonsets and cronjobs. `batch` and `flux_reconcile` will not
  be built.
- **2026-11-26, II-25740** — new action `expand_pvc`: `pending_action.target.kind` can be
  `PersistentVolumeClaim` (`deployment` null), per-action fields `pvc`, `pvc_uid`, `storage`, `workload_kind`,
  `workload`; new `action_result.status` values `resize_pending`, `resize_failed`, `pvc_replaced`;
  `find_workload` can answer `{"scope": "pvc", ...}`. oncallari 0.3.19 adds `patch` on persistentvolumeclaims
  and PVC rules to the admission policy. Additive; nothing removed or renamed.
- **2026-11-26, II-25740** — late approvals: new option `proposal_max_age_hours` (default 24); an approval is
  also refused when the Deployment was made again (new uid; `snapshot.deployment_uid`), a scale-up whose
  containers' CPU/memory changed, after a rollout since a
  proposed restart (`snapshot.revision`), or when a PVC's StorageClass no longer allows expansion or it is no
  longer `Bound`; `applied.changed_since_proposal` on an already-applied result. The `expand_pvc` write carries
  the PVC's uid (`metadata.uid` in the patch). Additive; nothing removed or renamed.
- **2026-11-26, II-25740** — an approval whose target already holds the approved result, done by someone else
  (a pod already deleted, replicas/resources/bounds/size already set), is refused instead of reported as
  `succeeded` with `applied.already_at_target` (user decision, every action); only a retried attempt after the
  step's own write still verifies and sets `already_at_target`. Refusals at approval are no longer retried by
  the worker (one attempt, not two). The too-old refusal states ages as "40 seconds", "12 minutes", "25.5
  hours". No field added or removed; such an approval now ends as a failed task with no `action_result`.
- **2026-11-27, II-25740** — a retried apply step that finds the approved values in place reports them as its
  own (`already_at_target`) only when the agent's field manager owns the fields it changes; otherwise refused
  (done by someone else, or undecidable with no owner on record). New `applied.field_manager` on such a result.
  `apply_approved_change` reads `managedFields` (get on the objects it already reads; no new RBAC). A
  `delete_pod` step that fails before the DELETE is sent is no longer retried (refused). Additive.
- **2026-11-27, II-25740** — new sections "Capabilities" and "Calling it" (what callers route to the agent, how to
  pass values, approve, wait and read results), from the ARI chat e2e. Documentation only.
- **2026-11-27, II-25740** — new action `set_startup_probe` (fields above; `target.kind: "Deployment"`). Callers
  that list change actions by name (mcp-service `KUBERNETES_CHANGE_ACTIONS`) need it; `kubernetes_change: true`
  is set on its `action_result` as on every change.
- **2026-11-27, II-25740** — `health_report` Pod findings name what restarted a container: `killed_by`
  (`oom`, `startup_probe`, `liveness_probe`, `not_recorded`), `diagnosis`, and for a probe kill `probe`,
  `probe_kills`, `last_run_seconds`, `during_startup`; reasons `... (killed by startup|liveness probe)` and a new
  `Restarted (...)` finding. New read tool `list_probe_kills` (events, already granted). An OOM finding carries
  `suggestion` (2 x the memory limit, node-capped, with the request). Fields added only.
- **2026-11-26, II-25740** — lowering CPU/memory (`set_resources`, `resize_pod`): a memory decrease below 1.2 x the
  container's current usage, or with no usage to read, is refused, at proposal and at approval; any decrease is
  `high`; `set_resources` under Recreate is `high`. `propose_change` and `apply_approved_change` read
  metrics.k8s.io (RBAC already granted). Protected namespaces add ingress-nginx and istio-system (oncallari
  0.3.21). No field added or removed.
