# k8s agent — end-to-end on a test cluster

Manifests for exercising the `k8s` agent kind on a real cluster (the test
EKS). The fixtures always come from here (`fixtures.yaml`, namespace
`ari-k8s-e2e`). The agent itself is best installed with the oncallari Helm
chart (`ariK8sAgent`, see "Agent from the chart" below), which is how it is
deployed for real; `agent.yaml` is a chart-free alternative. The agent's
write permission is cluster-wide, so only ever point scenarios at the
fixtures.

Every step below except the last one writes to the cluster; run them only
with the cluster owner's go-ahead.

```bash
CTX=test                       # kube context of the target cluster
TAG=<image tag built from this branch>
TEMPORAL_HOST=temporal-frontend.insightfinder.svc.cluster.local:7233   # on the test cluster

# 1. Temporal namespace for this kind (once), from a pod with the temporal CLI:
#    temporal operator namespace create --namespace k8s-ari-agent

# 2. Agent namespace, RBAC, secret (values come from your environment), then the agent
kubectl --context $CTX create namespace ari-k8s-agent
kubectl --context $CTX apply -f docs/examples/k8s-rbac.yaml
kubectl --context $CTX -n ari-k8s-agent create secret generic ari-k8s-agent-env \
  --from-literal=AGENT_API_KEYS="$ARI_API_KEY" \
  --from-literal=TEMPORAL_PAYLOAD_ENCRYPTION_KEY="$(openssl rand -base64 32)"
kubectl --context $CTX apply -f <(sed "s|__IMAGE_TAG__|$TAG|g; s|__TEMPORAL_HOST__|$TEMPORAL_HOST|g" deploy/k8s-agent-e2e/agent.yaml)

# 3. Fixtures
kubectl --context $CTX apply -f deploy/k8s-agent-e2e/fixtures.yaml

# 4. Run the scenarios (read-only + cancelled proposals; add --allow-writes to approve changes)
kubectl --context $CTX proxy --port=18001 &
# LLM_PROVIDER / LLM_API_KEY / LLM_MODEL from the environment or scripts/.env (git-ignored)
ARI_BASE_URL=http://127.0.0.1:18001/api/v1/namespaces/ari-k8s-agent/services/ari-k8s-api:80/proxy \
  python scripts/k8s_agent_live_test.py
```

### Agent from the chart

```bash
# From the charts repo (charts/oncallari); keys are generated in place and never written down.
# fullnameOverride "ari": the chart adds "-k8s-*" itself (ari-k8s-api, ari-k8s-worker).
kubectl --context $CTX create namespace ari-k8s-agent
helm --kube-context $CTX install ari-k8s charts/oncallari -n ari-k8s-agent \
  --set fullnameOverride=ari --set ariK8sAgent.enabled=true \
  --set ariK8sAgent.api.image.tag=$TAG --set ariK8sAgent.worker.image.tag=$TAG \
  --set ariK8sAgent.temporalHost=$TEMPORAL_HOST \
  --set ariK8sAgent.agentPublicUrl=http://ari-k8s-api.ari-k8s-agent \
  --set ariK8sAgent.agentApiKeys="$(openssl rand -hex 24)" \
  --set ariK8sAgent.temporalPayloadEncryptionKey="$(openssl rand -base64 32)"
kubectl --context $CTX proxy --port=18001 &
ARI_BASE_URL=http://127.0.0.1:18001/api/v1/namespaces/ari-k8s-agent/services/ari-k8s-api:80/proxy \
ARI_API_KEY="$(kubectl --context $CTX -n ari-k8s-agent get secret ari-k8s-secret -o jsonpath='{.data.AGENT_API_KEYS}' | base64 -d)" \
ARI_API_DEPLOYMENT=ari-k8s-agent/ari-k8s-api ARI_API_SERVICE=ari-k8s-agent/ari-k8s-api ARI_WORKER_DEPLOYMENT=ari-k8s-agent/ari-k8s-worker \
KUBE_CONTEXT=$CTX python scripts/k8s_agent_live_test.py --allow-writes
```

The scenarios reach the API through `kubectl proxy` (the API server's
service proxy) rather than `kubectl port-forward`. A port-forward is one
long-lived stream to one pod: over a full run (about two hours) it dropped
several times with "connection reset" / "connection refused" and failed the
scenario in flight, and it dies with the pod when the API restarts. Through
the proxy each request is its own call to the API server and goes to
whichever pod backs the Service. A port-forward URL still works.

The chart refuses to render without `agentApiKeys` (or with `change-me`) and
without a payload encryption key, unless `existingSecret` is set.

`ARI_API_DEPLOYMENT`/`ARI_API_SERVICE` enable `resume_after_restart`, which
restarts the API between a proposal and its approval (the task store volume
must carry the proposal over). `ARI_WORKER_DEPLOYMENT` enables
`worker_restart` and, with the API one, the `rbac` and `pods` checks of the
installed chart (ServiceAccount permissions, tini as PID 1, non-root,
read-only root filesystem, no token in the API pod). Every write scenario
puts the fixture fields it changed back with kubectl afterwards, so the run
can be repeated; after an interrupted run, re-apply `fixtures.yaml`.

Restart and pod delete (`restart_deployment`, `delete_pod`) work on
`e2e-plain`. Without `--allow-writes` they still check that the proposal's
dry run changed nothing (the pod a delete names is still there with the same
uid). `resize_pod` resizes one `e2e-plain` pod in place (cpu limit 50m ->
100m): same pod uid, no restart, and the kubelet reports the new limit; the
pod is resized back to its own value with kubectl afterwards. `expand_pvc`
grows `data-e2e-disk-0` (StatefulSet `e2e-disk`'s 1Gi claim template) to
2Gi; a volume cannot shrink, so with `--allow-writes` the scenario first
recreates the PVC at 1Gi (StatefulSet to 0, PVC deleted, back to 1) when a
run left it larger. `refuse_pvc_shrink` and `refuse_pvc_helm` (a PVC the
agent's own chart renders) must get no proposal; `admission_policy` also
dry-runs a write to that Helm-rendered PVC (denied) and, on
`data-e2e-disk-0`, a larger storage request (allowed) and an annotation
(denied: the agent may only raise a PVC's storage request; oncallari 0.3.20). They need oncallari 0.3.19. On the test cluster the agent runs in namespace `insightfinder`
(`ari-k8s-api` / `ari-k8s-worker`, secret `ari-k8s-secret`) rather than
`ari-k8s-agent`; only `ari-k8s-e2e`, the fixtures' StorageClass `ari-k8s-e2e-expand`, the
PersistentVolume behind `data-e2e-disk-0` (`late_pvc_lost`) and, in
`late_protected` and the restart scenarios, the agent's own Deployments are ever changed. Write scenarios belong
here, not on real services: on test, image automation redeploys the
`insightfinder` namespace many times an hour.

`health_cronjob` reads the
CronJob `e2e-cron`, which fires only once a year.

Lowering memory (2026-11-26): `e2e-memuse` holds about 100Mi under a 256Mi
limit. `refuse_memory_decrease` (limit to 110Mi, below 1.2 x its usage) must
end without a proposal; `memory_decrease_high` (limit to 200Mi) must be
proposed as `high` with the usage shown, and is always cancelled. Both need
metrics-server, and a minute after the pod starts for its first sample.

Approvals that arrive late (2026-11-26): each `late_*` scenario proposes,
changes what one approval-time check compares, then approves; nothing may be
written, and the worker's log must hold that check's refusal for the task
(read from the pods of `ARI_WORKER_DEPLOYMENT`, so they need it). There is
one per check an approval runs; the script's section comment lists the three
no cluster can reach. What they change meanwhile:
- Deployments: `e2e-plain` deleted and recreated, an HPA added for it, its
  CPU or memory limit changed, a too-old approval
  (`options.proposal_max_age_hours=0.002`); `e2e-hpa`'s HPA deleted or its
  max changed; `e2e-keda`'s ScaledObject min changed.
- Restarts of `e2e-plain`: another rollout (template annotation), a
  `kubectl rollout restart`, a pause, the strategy set to Recreate.
- Pod deletes: the pod deleted by hand or left terminating by a finalizer
  (refused: this agent deleted nothing), the pod orphaned from its
  ReplicaSet, or the Deployment scaled to that one pod (`pod-deletion-cost`
  picks which pod stays). `late_delete_pod_retry` is the other side: the
  worker is killed right after the agent's own delete, and the retried step
  must report it done (`already_at_target`).
- Pod resizes: the pod deleted, resized by hand, or (`e2e-memuse`) its usage
  raised.
- Memory decreases (`e2e-memuse`): a second process in the pod (`kubectl
  exec`) holds 80Mi more, so the re-read usage refuses the approval.
- PVC growth of `data-e2e-disk-0`: the PVC deleted, then recreated (new
  uid), left terminating, grown by hand, given Helm annotations, given an
  extra owner, its StatefulSet given a controlling owner (a ConfigMap stands
  in for the operator), a second StatefulSet `disk` whose claim template
  makes the same PVC name, the StatefulSet deleted with `--cascade=orphan`,
  or the PVC made `Lost` (`late_pvc_lost`: its PV set to `Retain`, then its
  `claimRef.uid` to another uid; afterwards the PV goes back to `Delete` and
  is removed with its EBS volume).
  `data-e2e-vol-0` uses the fixtures' own StorageClass `ari-k8s-e2e-expand`,
  which is set to disallow expansion, or deleted and recreated.
- Done by someone else (`late_already_*`): replicas, a memory limit, an HPA
  minimum, a pod's CPU limit, a PVC's size set by kubectl to exactly what the
  pending proposal asks for; each approval is refused, never reported as the
  agent's change.
- `retry_done_by_someone_else`: a retried step that finds the approved value
  in place counts it as its own only when the agent's field manager
  (`ari-k8s-agent`) owns the field. A ResourceQuota (`e2e-retry-quota`,
  `requests.storage` 1Mi) makes the first attempt's growth of
  `data-e2e-disk-0` fail at the API; in the 10 s before the retry the quota is
  deleted and kubectl grows the PVC to the approved 2Gi. The retry must refuse
  (done by someone else) and the size stay owned by kubectl.
  `worker_restart` and `late_delete_pod_retry` are the other side: the agent's
  own write, then a retry that reports it done (`worker_restart` also requires
  `applied.field_manager` `ari-k8s-agent`).
- `late_protected` adds `ari-k8s-e2e/e2e-plain` and `ari-k8s-e2e/e2e-disk` to
  the worker's `K8S_AGENT_PROTECTED_WORKLOADS` with `kubectl set env` (the
  worker restarts) and puts the original value back afterwards; the next Helm
  upgrade would restore it too. This is the only scenario that changes the
  agent's own Deployment's configuration.
Each puts back what it changed; PVC scenarios that grew or deleted the disk
recreate it at 1Gi.

What is never changed (2026-11-26): `refuse_statefulset_change` (a restart of
`e2e-sts` is refused), `refuse_protected_namespace` (scaling coredns in
kube-system), `refuse_self` (a restart of the agent's own worker): each must
end without a proposal and with the object unchanged. `admission_policy`
checks the chart's ValidatingAdmissionPolicy directly, with server dry runs
as the worker's ServiceAccount: denied in kube-system and on the agent's own
worker, allowed on `e2e-plain`. `refuse_self` and `admission_policy` need
`ARI_WORKER_DEPLOYMENT` and the oncallari chart from 0.3.17 (it sets the
protected-workload list and the policy); `agent.yaml` here sets neither.

Nodes are only read: `health_node` checks the node an e2e-plain pod runs on
(by its short name), `refuse_node_change` checks that "cordon node ..." is
refused without a proposal or a dry run, and `node_beside_workload` that a
restart quoting the pod's node still targets the Deployment.

`tests/test_k8s_live_script.py` keeps the script honest without a cluster:
the RBAC the `rbac` scenario expects must match `docs/examples/k8s-rbac.yaml`,
and the fixtures the scenarios name must exist here.

Cleanup: the fixtures stay while a piece of work still needs live runs (they
are reused from run to run) and are deleted once it is finished. On the test
cluster, where the agent itself stays installed, that is only:

```bash
kubectl --context test delete namespace ari-k8s-e2e
kubectl --context test delete storageclass ari-k8s-e2e-expand   # cluster-scoped, outlives the namespace
```

Removing an agent installed from these instructions as well:

```bash
# Chart install: helm removes its ClusterRole/Binding and kube-system Role/Binding (ari-k8s, ari-k8s-ca-status)
helm --kube-context $CTX uninstall ari-k8s -n ari-k8s-agent
kubectl --context $CTX delete namespace ari-k8s-e2e ari-k8s-agent
kubectl --context $CTX delete storageclass ari-k8s-e2e-expand

# agent.yaml install instead
kubectl --context $CTX delete namespace ari-k8s-e2e ari-k8s-agent
kubectl --context $CTX delete clusterrolebinding,clusterrole ari-k8s-agent
kubectl --context $CTX -n kube-system delete rolebinding,role ari-k8s-agent-ca-status
```
