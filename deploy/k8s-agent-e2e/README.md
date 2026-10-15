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
kubectl --context $CTX -n ari-k8s-agent port-forward svc/ari-k8s-api 18002:80 &
# LLM_PROVIDER / LLM_API_KEY / LLM_MODEL from the environment or scripts/.env (git-ignored)
ARI_BASE_URL=http://127.0.0.1:18002 python scripts/k8s_agent_live_test.py
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
kubectl --context $CTX -n ari-k8s-agent port-forward svc/ari-k8s-api 18002:80 &
ARI_API_KEY="$(kubectl --context $CTX -n ari-k8s-agent get secret ari-k8s-secret -o jsonpath='{.data.AGENT_API_KEYS}' | base64 -d)" \
ARI_API_DEPLOYMENT=ari-k8s-agent/ari-k8s-api ARI_API_SERVICE=ari-k8s-agent/ari-k8s-api ARI_WORKER_DEPLOYMENT=ari-k8s-agent/ari-k8s-worker \
  python scripts/k8s_agent_live_test.py --allow-writes
```

`ARI_API_DEPLOYMENT`/`ARI_API_SERVICE` enable `resume_after_restart`, which
restarts the API between a proposal and its approval (the task store volume
must carry the proposal over). `fix_stuck` raises `e2e-memhungry`'s memory
limit; re-apply `fixtures.yaml` to make it OOM again. Remove with
`helm uninstall ari-k8s -n ari-k8s-agent` and delete the namespace (the PVC
goes with it).

Cleanup:

```bash
kubectl --context $CTX delete namespace ari-k8s-e2e ari-k8s-agent
kubectl --context $CTX delete clusterrolebinding,clusterrole ari-k8s-agent
kubectl --context $CTX -n kube-system delete rolebinding,role ari-k8s-agent-ca-status
```
