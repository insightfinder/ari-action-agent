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
  pod's owning Deployment (and adds `pod` and `container`). Actions on other kinds (StatefulSet, DaemonSet,
  CronJob, Node) will set it and may leave `deployment` empty. A proposal without `target` (made before
  II-25740) is still applied.
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

`scale_replicas`, `set_autoscaler_bounds`, `set_resources`, `resize_pod` today
(`k8s_agent/actions/__init__.py`, `ACTIONS`). `resize_pod` (**added II-25740**): CPU/memory of one container
of one running pod, in place through `pods/resize` — no rollout, same pod. Chosen only when the request asks
for an in-place change (decided in code; naming a pod is not enough). Proposed only when the API server serves
`pods/resize` (live discovery) and both the API server and the pod's kubelet are >= 1.35; memory only when
the container's memory `resizePolicy` is `RestartContainer`. The Deployment template is not changed, so the
persistence hint names the template fields to change in Git.

Planned under II-25740 (not built yet), in this order after the phase 1 reads:
- `restart_rollout` (Deployment, StatefulSet, DaemonSet), `rollback`, `set_image`, `pause_rollout`,
  `resume_rollout`, `delete_pod` (controller-owned pods only), `suspend_cronjob`, `resume_cronjob`,
  `trigger_cronjob`, `cordon_node`, `uncordon_node`, `drain_node`, StatefulSet replica scaling, and `batch`
  (one approval for the same change on several targets).
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
  which its controller replaces, is the only delete).
- Changing the content of Services, Ingresses, NetworkPolicies or ConfigMaps: networking and configuration
  change through Git.
- Deleting nodes, or changing node labels or taints (node groups and Cluster Autoscaler own them; cordon,
  uncordon and drain are the node actions).
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
naming it. The chart is this agent's own deployment: any change here
that needs a new RBAC rule, env var or value updates the oncallari chart in the same piece of work (charts
repo, same ticket branch, `Chart.yaml` version bumped).

## Needed from other repositories

Recorded for their owners; not changed from this repository.

| Repo | What | Why | Since |
|---|---|---|---|
| insightfinder-mcp-service | `app/a2a/client.py`: `KUBERNETES_CHANGE_ACTIONS` hard-codes three action names. Recognise `action_result.kubernetes_change == true` instead (or as well). | New actions would otherwise be missing from the incident digest. Now needed: `resize_pod` ships with II-25740. | II-25740 |
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
