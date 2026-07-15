# Disposable multi-node Kubernetes exercise

This directory is a local, isolated acceptance environment for ENT-12. It is
not a production topology and it does not certify the bundled dependencies for
HA. The harness deploys real PostgreSQL, Redis, Kafka, and two independent
Typesense processes on a dedicated infra worker, while the three IMPOSBRO
workloads run with two replicas across three separate workload workers.

All checked-in external image references and the kind node image are pinned by
digest. The three application images are built from the current worktree,
pushed to a disposable loopback registry, resolved to repository digests, and
then supplied to Helm by immutable reference.

Files:

- `cluster.yaml` defines one control-plane, one infra worker, and three
  workload workers.
- `dependencies.yaml` supplies isolated runtime dependencies for the exercise.
- `values.yaml` enables PDBs, required anti-affinity, topology spread,
  restricted pod security, resource limits, NetworkPolicies, migration init
  containers, and TLS ingress contracts.
- `benchmark-profile.json` is the versioned black-box workload and fail-closed
  threshold contract used by the live run.
- `tls-edge.yaml` provides a real TLS service path used for hostname and trust
  validation without pretending that a local ingress controller is a
  production edge.

Run from the repository root:

```bash
./scripts/e2e/run-kind-enterprise-smoke.sh
```

The default cleanup is unconditional. `KIND_KEEP_CLUSTER=1` is intended only
for interactive diagnosis; evidence from a retained run is marked
`incomplete`, never `passed`.
