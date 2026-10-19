# Plain Kubernetes manifests

For trying the on-call agent in a local or scratch cluster (kind, minikube, a test namespace) without Helm. Production deployments use the `oncallari` Helm chart, which these manifests follow: one API replica and one worker replica, both rolled out with `strategy: Recreate`, and persistent volumes for the API's task store and the worker's repository and build caches.

You also need a reachable Temporal service. Point `TEMPORAL_HOST` at it in `.env`.

1. Create the shared settings Secret from your `.env` (see `.env.example`):

   ```bash
   kubectl create secret generic ari-oncall-agent-env --from-env-file=../../.env
   ```

2. Apply the volumes and the two deployments:

   ```bash
   kubectl apply -f pvcs.yaml -f deployment-api.yaml -f deployment-worker.yaml
   ```

3. Reach the API:

   ```bash
   kubectl port-forward svc/ari-oncall-agent-api 8000:80
   curl http://localhost:8000/.well-known/agent-card.json
   ```

Why one replica each: the API keeps paused tasks (waiting for approval) on its task-store volume, and each `ReadWriteOnce` volume can be mounted by one pod at a time, so `Recreate` stops the old pod before the new one starts. To run more workers, give each its own caches or use `ReadWriteMany` volumes, and raise `MAX_CONCURRENT_ACTIVITIES` only together with the worker's CPU and memory.

The images are built from `Dockerfile.api` and `Dockerfile.oncall`. The tags `main`, `staging` and `test` follow those branches.
