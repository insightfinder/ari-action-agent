# ARI Kubernetes Agent — interface and cross-repo notes

What the `k8s` agent kind exposes to its callers (MCP Service, UIE/InfrastructureCore, the e2e script) and what
other repositories must do when it changes. **Every change to anything listed here is recorded in the change
log at the bottom, with the date and ticket.** The agent's own design lives in docs/MEMORY.md (II-25173 onward).

## Request

A2A message: the prompt as a TextPart and one DataPart `{"llm": {...}, "options": {"namespace"?,
"high_replica_threshold"?, "node_headroom_ratio"?}}`. A confirmation resumes the paused task with
`{"confirm_pending_action": {"id": "<pending_action.id>"}}`. No cluster credential is ever sent.

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
  `kind: "Deployment"`, except `resize_pod`, which sets `kind: "Pod"` and still fills `deployment` with the
  pod's owning Deployment (and adds `pod` and `container`). Actions on other kinds set it and may leave
  `deployment` empty (null): `restart_rollout` of a StatefulSet or DaemonSet (`kind` is that kind), and
  `delete_pod` (`kind: "Pod"`; `deployment` is filled only for a Deployment's pod), `set_image` of a
  StatefulSet or DaemonSet, and the CronJob actions (`kind: "CronJob"`, `deployment` null). A proposal without
  `target` (made before II-25740) is still applied.
- Per-action fields (spread into the proposal beside the common ones; informational, the agent rebuilds the
  write from them): `restart_rollout`: `workload_kind`, `workload`, `restarted_at` (the value the template's
  `kubectl.kubernetes.io/restartedAt` gets). `rollback`: `from_revision`, `to_revision`, `replicaset`,
  `replicaset_uid`, `images` (container -> image of the target revision; the pod template itself is never in
  the proposal). `delete_pod`: `workload_kind`, `workload`, `pod`, `pod_uid`. **Added II-25740.**
  Write batch 2 (**added II-25740**): `set_image`: `workload_kind`, `workload`, `container`, `image`.
  `pause_rollout` / `resume_rollout`: `paused` (the value set), `pending_pods` (pods a resume will replace
  because the template changed while paused; 0 for a pause). `suspend_cronjob` / `resume_cronjob`: `cronjob`,
  `suspend`.
- `persistence.needed`: `false` when nothing has to change in Git (a restart, a deleted pod, an in-place
  resize back to the template's own values, a pause or resume); `reason` says why and
  `changes` is empty. `true` otherwise (a `set_image` names `image`, a CronJob suspend or resume `suspend`).
  **Added II-25740.**
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
- `status` for `restart_rollout` and `rollback` of a Deployment (II-25740): `succeeded`, `in_progress`,
  `new_pods_failing`, `rollout_failed`, `unschedulable`; a rollback also `reverted` (the newer image came back
  while watched: image automation or a Helm upgrade). A StatefulSet or DaemonSet restart: `succeeded`,
  `in_progress`, `new_pods_failing`, `unschedulable`. `delete_pod`: `succeeded` (the old uid is gone and a
  replacement is ready and stable; `verification.replacement` names it), `in_progress`, `new_pods_failing`,
  `unschedulable`. A pod that was already gone or replaced when the approval arrived counts as done:
  `applied.already_at_target` is true and nothing is deleted.
- Write batch 2 statuses (II-25740). `set_image`: as a restart of the same kind, plus `reverted` (another
  image came back while watched). `pause_rollout`: `succeeded` (the controller reports DeploymentPaused and the
  value held), `in_progress`, `reverted`. `resume_rollout`: the same when no pod is replaced; when a template
  change waited (`pending_pods` > 0) as a Deployment rollout, `reverted` meaning paused again.
  `suspend_cronjob` / `resume_cronjob`: `succeeded` (the value held for 45 s; `verification.active_jobs` lists
  runs active then — a resume may start one at once), `reverted`.

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

`scale_replicas`, `set_autoscaler_bounds`, `set_resources`, `resize_pod`, `restart_rollout`, `rollback`,
`delete_pod`, `set_image`, `pause_rollout`, `resume_rollout`, `suspend_cronjob`, `resume_cronjob` today (`k8s_agent/actions/__init__.py`, `ACTIONS`). `resize_pod` (**added II-25740**): CPU/memory of one container
of one running pod, in place through `pods/resize` — no rollout, same pod. Chosen only when the request asks
for an in-place change (decided in code; naming a pod is not enough). Proposed only when the API server serves
`pods/resize` (live discovery) and both the API server and the pod's kubelet are >= 1.35; memory only when
the container's memory `resizePolicy` is `RestartContainer`. The Deployment template is not changed, so the
persistence hint names the template fields to change in Git.

Added II-25740 (write batch 1). Restart, rollback and delete are proposed only when the request's own words ask
for them (decided in code: an on-call prompt quoting "restarted 5 times" does not); otherwise the refusal
carries `asked_for` with the action the model picked.
- `restart_rollout`: a rolling restart of a Deployment, StatefulSet or DaemonSet, as `kubectl rollout restart`
  (sets `kubectl.kubernetes.io/restartedAt` on the pod template). Refused for a paused Deployment, an
  `OnDelete` update strategy, or no pods. Tier `medium`; `high` under `Recreate` or for a one-pod StatefulSet.
- `rollback`: a Deployment back to the previous revision, or to the one the request states, as `kubectl
  rollout undo` (JSON patch replacing `spec.template` with that ReplicaSet's, after a `test` of the current
  revision). Refused for a paused Deployment or when no such revision is kept. Tier `medium`.
- `delete_pod`: one pod, named in the request, that a Deployment, StatefulSet or DaemonSet owns; deleted with a
  uid precondition (a plain delete, not an eviction: a PodDisruptionBudget is reported, not waited for). Tier
  `medium`; `high` when no other pod of the workload is ready.
- `find_workload` now also resolves StatefulSets and DaemonSets (`{"scope": "workload", "kind", ...}` in its
  step result; a Deployment keeps `{"scope": "deployment", ...}`).

Added II-25740 (write batch 2). Each is proposed only when the request's own words ask for it (code, as in
batch 1; a refusal carries `asked_for`).
- `set_image`: one container's image in a Deployment, StatefulSet or DaemonSet, as `kubectl set image`
  (strategic merge patch by container name). The image is the request's own words: a full reference written
  verbatim, or a tag written as a tag ("tag 1.4", "version 1.4", "app:1.4") put on the current repository.
  The container must exist; with one container it need not be named. Refused for a paused Deployment, an
  `OnDelete` strategy or the image already running. Tier `medium`; `high` when the repository changes, under
  `Recreate`, or for a one-pod StatefulSet. Whether the image exists cannot be checked before it rolls out.
- `pause_rollout` / `resume_rollout`: a Deployment's `spec.paused`, as `kubectl rollout pause` / `resume`.
  A pause replaces no pod; while paused, template changes (image automation, Helm upgrades) wait. A resume
  rolls out whatever waited (`pending_pods`, read from the Deployment's status). Tier `medium`.
- `suspend_cronjob` / `resume_cronjob`: a CronJob's `spec.suspend`. A resume without
  `startingDeadlineSeconds` may start one run at once for the latest schedule missed while suspended (a
  `medium` risk). A chart or manifest usually sets `suspend`, so a Helm upgrade or reconcile puts it back
  (drift notice, persistence hint). Tier `medium`.
- Not added: `trigger_cronjob` (run a CronJob now, i.e. create a Job from it). Built and then removed on
  2026-11-25 by user decision: the agent does not re-run CronJobs; whoever needs a run outside the schedule uses
  `kubectl create job --from=cronjob/<name>`. It would have needed `create` on jobs and an exception to the
  "Never" list below.
- `find_workload` also resolves CronJobs (`{"scope": "workload", "kind": "CronJob", ...}`, also from a
  scheduled run's pod name); `check_health` of a CronJob covers its runs' pods and reports a suspended one
  (`Suspended`, info).
- RBAC (oncallari chart 0.3.16): `patch` on `cronjobs` (batch). Before it, both CronJob changes are refused
  with the missing rule named; the other batch 2 actions need no new rule.

Nodes (II-25740): read only. `find_workload` resolves `{"scope": "node", "kind": "Node", "namespace": null,
"name"}` for a node the request writes out (its full name or the part before the first dot), only when no
workload matches (on-call prompts quote node names while asking about a workload); `check_health` of a node
covers the pods on it and reports `NotReady` (warning) and `Cordoned` (info), so `health_report.scope.type` can
now be `node` (with `kind: "Node"`, `name`, `namespace` null). `propose_change` refuses any change to a node.
`cordon_node`, `uncordon_node` and `drain_node` were built and declined by the user on 2026-11-25 (see "Never").

Planned under II-25740 (not built yet), in this order:
- StatefulSet replica scaling, and `batch` (one approval for the same change on several targets).
- Added 2026-11-24 (user decision), in this order: `flux_reconcile` (set `reconcile.fluxcd.io/requestedAt` on a
  HelmRelease or Kustomization so a merged fix applies now; changes no desired state, tier `low`);
  `resize_pod` for StatefulSet pods (same rules as for Deployment pods; persistence hint points at the
  StatefulSet template); `expand_pvc` (grow only, never shrink: needs the StorageClass's
  `allowVolumeExpansion`, a per-request growth cap, tier `high`; irreversible and costs money; a
  StatefulSet's `volumeClaimTemplates` cannot follow, so the proposal says what Git needs).

Later, only after the list above (needs its own design first):
- Suspend/resume Flux reconciliation (HelmRelease/Kustomization): blocks every deploy and is easy to forget,
  so it needs an expiry or reminder; on test a suspended HelmRelease is re-applied by its parent
  Kustomization within minutes (docs/MEMORY.md 2026-10-14), so the parent must be handled too.
- Pause KEDA scaling (`autoscaling.keda.sh/paused`).
- CPU/memory changes for StatefulSet and DaemonSet templates (same mechanism as `set_resources`).

### Never (permanent boundaries)

Not to be added. A change to this list is a decision of its own, recorded here and in docs/MEMORY.md.

- Reading Secrets (any verb), reading ConfigMap values (keys only), copying nested content of custom
  resources (top-level scalars only).
- exec / attach / port-forward into pods.
- Creating or deleting workloads, Services, Ingresses, Namespaces or PVCs (deleting a controller-owned pod,
  which its controller replaces, is the only delete). This includes creating a Job from a CronJob ("run it
  now"): considered as `trigger_cronjob` and declined by the user on 2026-11-25.
- Changing the content of Services, Ingresses, NetworkPolicies or ConfigMaps: networking and configuration
  change through Git.
- Changing nodes at all: deleting them, their labels or taints, and also cordon, uncordon and drain. The three
  were planned, built (commit `80cd84c`) and declined by the user on 2026-11-25: on EKS with Cluster Autoscaler
  and spot nodes a bad node is usually replaced rather than drained, a drain on these clusters stops several
  single-replica services at once, and `patch` on nodes cannot be narrowed by RBAC to `spec.unschedulable`.
  Node groups, Cluster Autoscaler and people with kubectl own nodes.
- Rolling back a HelmRelease (Flux upgrades it again; revert in Git, or roll back the Deployment).
- Writing any custom resource other than a KEDA ScaledObject's min/max (and the Flux reconcile annotation
  above).
- Editing RBAC, writing to Git, or any write without a person's approval of that exact proposal.
- Scaling anything to 0 replicas.

## RBAC

`docs/examples/k8s-rbac.yaml` is the reference; the oncallari chart's
`templates/ari-k8s-agent/ari-k8s-agent-rbac.yaml` (charts repo) mirrors it rule for rule. II-25740 phase 0
reads and writes exactly the same resources as before, so neither changed. Later phases add reads (and the
new writes) and will list the exact rules here. `resize_pod` adds `patch` on `pods/resize` (core group), in
both files from oncallari 0.3.14; until a cluster has that rule, the dry run's 403 comes back as a refusal
naming it. Write batch 1 adds `patch` on `statefulsets` and `daemonsets` (apps group; a restart) and `delete`
on `pods` (core group; `delete_pod`), in both files from oncallari 0.3.15; a Deployment restart or rollback
needs no new rule. The chart is this agent's own deployment: any change here
that needs a new RBAC rule, env var or value updates the oncallari chart in the same piece of work (charts
repo, same ticket branch, `Chart.yaml` version bumped).

## Needed from other repositories

Recorded for their owners; not changed from this repository.

| Repo | What | Why | Since |
|---|---|---|---|
| insightfinder-mcp-service | `app/a2a/client.py`: `KUBERNETES_CHANGE_ACTIONS` hard-codes three action names. Recognise `action_result.kubernetes_change == true` instead (or as well). | New actions would otherwise be missing from the incident digest. Now needed: `resize_pod`, `restart_rollout`, `rollback`, `delete_pod`, `set_image`, `pause_rollout`, `resume_rollout`, `suspend_cronjob` and `resume_cronjob` ship with II-25740. | II-25740 |
| insightfinder-mcp-service | `config/agents/arik8s.yaml` description and `app/oncall/prompt.py` K1–K4 rules describe health checks and three changes only. | The coordinator routes by them; update when the new reads/actions ship. | II-25740 |
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
