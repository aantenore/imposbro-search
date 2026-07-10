# Enterprise requirement-to-test matrix

This matrix is the release contract for `docs/ENTERPRISE_DELIVERY_CONTRACT.md`.
It maps each requirement to the narrowest authoritative check. A referenced
file or a green unit test is not evidence for a runtime property it did not
exercise. Critical tests may not be skipped, converted to warnings, or replaced
by static manifest inspection.

| Requirement | Deterministic repository gate | Live or release evidence |
| --- | --- | --- |
| ENT-01 API contracts | `query_api/tests/test_api_contract.py`; `scripts/ci/openapi-contract.py`; `scripts/ci/openapi-compat.py`; `scripts/ci/tests/test_openapi_compat.py` | Hosted `Canonical event contracts` job against `contracts/openapi-v1.previous.json` |
| ENT-02 transactional state | `test_control_plane_store.py`, `test_control_plane_postgres_integration.py`, Alembic upgrade/downgrade tests | Real PostgreSQL concurrent CAS/migration job plus `postgres_migration_cas_and_sequence` live scenario |
| ENT-03 safe routing | `test_routing_migration*.py`, `test_routing_rollout*.py`, Admin UI rollout tests | `typesense_dual_write_backfill_cutover_rollback` against two independent Typesense clusters, including rollback/recovery |
| ENT-04 reconciliation | `test_config_sync.py`, federation snapshot/store tests | Dropped-Redis notification and restarted-replica scenarios with exact authoritative/applied revision convergence |
| ENT-05 identity/authorization | OIDC, API-key, scope, tenant-policy, Admin BFF/session/CSRF negative suites; `docs/ASVS_BASELINE.md` | Cross-tenant live denials, browser OIDC boundary evidence, and deployment IdP key-rotation/unavailability evidence |
| ENT-06 secrets/TLS | Helm negative policy suite, component-scoped Secret assertions, secret/config scan | Typesense TLS/hostname smoke, Kubernetes TLS path, secret rotation drill, and hosted Trivy secret/misconfiguration gate |
| ENT-07 audit | mutation/audit transaction, hash-chain, outbox/export, retention and DLQ resolver tests | Non-empty audit chain unchanged through retention and clean restore; alert contract for delivery backlog/failure |
| ENT-08 lifecycle/DR | backup/restore guardrails, retention policy tests, tombstone/deletion convergence tests | `ops/dr/run-live-postgres-dr.sh`: real `age` encryption, clean PostgreSQL restore, fingerprints, non-vacuous audit chain, timed RPO/RTO |
| ENT-09 reliability/SLO | health timeout/cache/retry tests; Prometheus rule unit tests; `docs/SLO.md` | Dependency outage/recovery scenarios, rendered dashboard/rules, and Kind restart/drain availability probe |
| ENT-10 diagnostics | structured-log redaction, metric contracts, query/worker telemetry tests | Captured W3C trace continuity from HTTP through outbox/Kafka/worker/Typesense, plus rendered dashboard/runbook artifacts |
| ENT-11 performance | `test_benchmark_harness.py`; versioned `ops/kind/benchmark-profile.json` | Black-box Kind benchmark JSON/Markdown with complete environment/image/commit metadata and enforced throughput, convergence, p95, error and partial-response thresholds |
| ENT-12 Kubernetes | `scripts/test-helm-chart.py`, digest/base-image validators, rendered enterprise manifests | Five-node Kind run with two replicas, PDB/placement/network policy, TLS, rollout, restart, eviction and node drain |
| ENT-13 supply chain | workflow policy, lockfile, release matrix, dependency, secret, SAST and container gates | Green hosted CI for the exact commit; immutable signed GHCR images, SPDX/CycloneDX SBOM and GitHub provenance for an authorized release tag |
| ENT-14 test depth | All component/unit/adapter/API/Helm/browser/ops suites and this matrix | Green hosted release workflow reusing CI plus live dependency, Kind/load, DR and selected failure-injection artifacts; no critical skip |
| ENT-15 operator UX | Admin UI unit/security/accessibility tests, lint and production build | Playwright desktop/mobile rollout/conflict/recovery flows, axe serious/critical gate, keyboard smoke and screenshots/traces |
| ENT-16 handover | ADRs 0001–0006, threat model, lifecycle, SLO, observability, operations ownership, release/rollback and runbook link validators | Quality-gate report and dated tabletop record with findings, owners and closure state |

## Required commands

Run these from a clean checkout of the candidate commit. The hosted workflows
repeat them with locked runtimes and retain their artifacts.

```bash
make workflow-policy lock-verify contracts helm ops
.venv/bin/python -m pytest -q query_api/tests
.venv/bin/python -m pytest -q indexing_service/tests
npm --prefix admin_ui test
npm --prefix admin_ui run lint
npm --prefix admin_ui run build
npm --prefix admin_ui run test:e2e
E2E_DEPENDENCY_MODE=compose scripts/e2e/run-enterprise-e2e.sh
DR_EVIDENCE_BASENAME="dr-candidate-$(date -u +%Y%m%dT%H%M%SZ).json" \
  ops/dr/run-live-postgres-dr.sh
scripts/e2e/run-kind-enterprise-smoke.sh
```

The three live commands own disposable environments and must run serially on a
shared Docker daemon. Their evidence writers are fail-closed: missing,
unexpected, interrupted, retained-cluster, dirty-release, or failed scenarios
cannot be promoted as release evidence.

## Evidence binding

Local artifacts under `docs/evidence/` are engineering evidence and explicitly
set `production_certification=false`. Release evidence is authoritative only
when the hosted run identifies the exact clean commit and immutable image set.
Deployment-owned controls—IdP, external secret manager, paging delivery,
managed-service HA, off-site backup custody, residency and customer
acceptance—must be attached by the deployment owner and are never inferred
from this repository.
