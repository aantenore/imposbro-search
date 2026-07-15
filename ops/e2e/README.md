# Enterprise live evidence substrate

`docker-compose.enterprise.yml` is an isolated, disposable dependency stack for
the repository-owned E2E runner. It uses two Query API replicas, PostgreSQL,
Redis, Kafka, one indexing worker, two independent Typesense clusters, and a
short-lived private CA for HTTPS Typesense and OTLP capture probes. The bounded
OTLP sink proves W3C parent/child continuity from HTTP through Kafka and the
worker to the Typesense client span without persisting payloads. Compose-managed
third-party dependency images are pinned to immutable multi-architecture
manifest digests; application-image provenance remains governed by the main
supply-chain pipeline.

The Compose stack intentionally uses the application's development deployment
profile so it can isolate control-plane, event, convergence, and migration
mechanics with local plaintext dependencies. The dedicated reverse proxy proves
one verified HTTPS path. A passing run does not claim that PostgreSQL, Redis, or
Kafka production TLS/SASL policy was exercised; use `external` mode against a
real enterprise deployment for those environment-specific controls.

Run it only through `scripts/e2e/run-enterprise-e2e.sh`. The entrypoint creates
ephemeral test credentials, uses a unique Compose project, writes redacted JSON
evidence, restores Redis after the deliberate notification-loss probe, and
never removes volumes unless `E2E_PRUNE_VOLUMES=true` is explicitly set.

`kind-enterprise-e2e.yaml` is only a substrate for CI environments that already
provide Kind and deploy the Helm chart. Its presence is not evidence that the
Kubernetes path was executed. Use dependency mode `external` against deployed
service endpoints and provide a restart hook to generate that evidence. Copy
`external.env.example` to a local ignored file only as a variable checklist;
never commit populated credentials. External mode creates uniquely named test
collections and control-plane revisions, so target only an explicitly approved
disposable/staging environment—not production. The external Query API must
authorize the generated `e2e_worker_*` and `e2e_route_*` collection names, and
the supplied data credential must carry one tenant identity under the deployed
authorization policy. It must also expose an operator-approved TLS OTLP capture
endpoint for the required trace-continuity scenario.
The tenant-isolation scenario additionally requires three short-lived OIDC
tokens (tenant A, tenant B, and an intentionally claim-less negative token).
ENT-06 additionally proves both `file:` and `env:` Typesense secret references:
the Query API mounts the mutable secret volume read-only, the harness alone
mounts it read-write, and rotation/revocation occurs without a control-plane
revision. External mode therefore requires an operator-approved writable
mounted-secret path plus a log-capture hook; the hook receives a destination
file and must include only bounded Query API and indexing-worker logs.
