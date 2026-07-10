# Kubernetes multi-node acceptance runbook

## Purpose

Use this drill to validate the Kubernetes delivery controls required by
ENT-12 against a disposable multi-node cluster. It exercises the actual Helm
chart and current worktree images, not only rendered YAML.

The drill proves:

- digest-pinned kind, dependency, and application images;
- two replicas of Query API, Admin UI, and Indexing Service with required
  hostname anti-affinity and topology spread;
- PostgreSQL migration init completion under two concurrent Query API pods;
- non-root, read-only application containers with dropped capabilities and no
  service-account token mount;
- PDB, NetworkPolicy, TLS ingress contracts, and a real verified HTTPS service
  path;
- API create/ingest/search/delete behavior through Kafka and two Typesense
  services;
- the declared `kind-enterprise-small-v1` black-box performance profile,
  including ingest throughput, indexing visibility, search p95, zero request
  errors, and zero partial responses;
- a single-pod replacement, a rolling restart of all application deployments,
  PDB rejection when no voluntary disruption is allowed, and a worker
  cordon/drain with continuous Query API readiness.

It does **not** certify cloud load balancers, a production CNI's policy
enforcement, managed dependency HA, external secret controllers, storage
classes, or production certificates. Every evidence artifact hard-codes
`production_certification: false`.

## Preconditions

1. Docker Desktop is running and has capacity for five kind nodes.
2. `kind`, `kubectl`, `helm`, `openssl`, `curl`, Python 3, and Docker are on
   `PATH`.
3. No other IMPOSBRO Compose evidence stack is running. The script refuses to
   contend with one.
4. Port `5001`, cluster name `imposbro-enterprise`, and container name
   `imposbro-kind-registry` are unused.

## Run

```bash
./scripts/e2e/run-kind-enterprise-smoke.sh
```

The run writes
`docs/evidence/kind-enterprise-smoke-live-latest.json` atomically. The artifact
contains tool versions, Git commit/dirty state, assertion timings, sanitized
outcomes, and cleanup status. Credential values, pod environment dumps, and
raw configuration are intentionally excluded.

It also invokes the existing `scripts/benchmark-k8s.py` harness against the
port-forwarded Kubernetes Service and atomically publishes:

- `docs/evidence/kind-enterprise-benchmark-live-latest.json`
- `docs/evidence/kind-enterprise-benchmark-live-latest.md`

The benchmark profile is versioned in `ops/kind/benchmark-profile.json`. The
artifacts record its workload, thresholds, cluster shape, immutable application
image set, exact Git commit, and dirty-worktree state. A threshold miss stops
the entire drill; partial responses are not permitted.

The default timeout is ten minutes per Kubernetes rollout. Override it only
when the local image pull/build environment is known to be slow:

```bash
KIND_TIMEOUT=900s ./scripts/e2e/run-kind-enterprise-smoke.sh
```

## Diagnostic retention

For an interactive failure investigation only:

```bash
KIND_KEEP_CLUSTER=1 ./scripts/e2e/run-kind-enterprise-smoke.sh
kubectl --context kind-imposbro-enterprise -n imposbro-kind get pods -o wide
```

Retained runs are intentionally marked `incomplete`. After diagnosis, remove
both resources explicitly:

```bash
kind delete cluster --name imposbro-enterprise
docker rm -f imposbro-kind-registry
```

Do not edit a failed or incomplete evidence file into a passing result. Run the
drill again from a clean state.

## Failure routing

- **Image pull failures:** verify the disposable registry is attached to the
  `kind` Docker network and inspect the node's
  `/etc/containerd/certs.d/localhost:5001/hosts.toml`.
- **Migration init failures:** inspect the `control-plane-migrate` init logs and
  PostgreSQL readiness; the main Query API container must not be bypassed past
  a failed migration.
- **Worker readiness failures:** inspect Kafka readiness first, then Query API
  internal cluster configuration and PostgreSQL checkpoint connectivity.
- **Rolling restart stalls:** verify there are three schedulable workload
  workers. Required anti-affinity plus `maxUnavailable: 0` needs a third node
  for the surge pod.
- **Drain blocked unexpectedly:** inspect all three PDB statuses before
  changing any budget. Do not use `--disable-eviction`, because that bypasses
  the control under test.
- **TLS verification failure:** confirm the port-forward targets
  `service/kind-tls-edge` and use the run-generated certificate as the trust
  anchor. Do not replace hostname verification with `-k`.
