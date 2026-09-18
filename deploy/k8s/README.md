# Kubernetes deploy notes

Create the shared env secret first (mirrors `.env.example`):

```bash
kubectl create secret generic ari-oncall-agent-env --from-env-file=../../.env
```

Then apply both deployments:

```bash
kubectl apply -f deployment-api.yaml
kubectl apply -f deployment-worker.yaml
```

`api` and `worker` scale independently — `api` is stateless request
handling, `worker` is where opencode sandboxes/LLM calls/git clones
actually run and is the one to scale for throughput.
