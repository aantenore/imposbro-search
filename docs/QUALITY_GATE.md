# Enterprise quality gate

Status: **NOT PASS — repository-hosted gates passed; deployment acceptance is
still open**.

This is the ENT-16 handover audit for the requirements in
[the Enterprise Delivery Contract](ENTERPRISE_DELIVERY_CONTRACT.md). It
separates repository implementation, disposable local evidence, hosted release
evidence, and deployment-owned proof. A file existing in Git is not runtime
evidence, and a local pass with a dirty tree is not evidence for a release
commit.

The original audit was performed on 2026-07-10 against a dirty working tree
based on `dffb3a077866f97881dd716edb494983d92c61b3`. The addendum below records
the later hosted evidence without relabeling repository-owned disposable
profiles as deployment or production evidence.

## Evidence addendum — 2026-07-17

The first clean, hosted, exact-commit enterprise assurance run succeeded for
commit `7aaea2bc19e298618bd2b9f31480cdabe9549f3d`. See [workflow run
29549689073](https://github.com/aantenore/imposbro-search/actions/runs/29549689073).
Its retained composite manifest reports:

- `status=passed`, `clean_evidence_required=true`, and `errors=[]`;
- `e2e`, `dr`, `kind`, and `benchmark` all passed and were bound to the same
  candidate commit; and
- `production_certification=false`.

The exact-commit [Enterprise CI push
run](https://github.com/aantenore/imposbro-search/actions/runs/29549683524) also
succeeded. These results replace the earlier claims that a clean candidate and
hosted composite were unavailable or failed. They establish repository-hosted
assurance for this commit only. They do not prove production capacity,
deployment-specific controls, an authorized signed release, or customer/change
approval.

## Status model

- **Complete:** the repository implementation and deterministic checks cover
  the stated boundary; this does not imply runtime acceptance.
- **Partial:** an acceptance behavior or control is still missing, or the
  repository's own security/lifecycle documents record a material gap.
- **Local evidence:** an isolated machine exercise passed its own schema with
  `production_certification=false`; it cannot certify a deployment.
- **Hosted evidence:** a clean hosted run is bound to the exact candidate SHA;
  its scope remains the disposable profiles and assertions executed by that
  run.
- **Deployment pending:** the control depends on an actual IdP, secret manager,
  pager, managed data service, region, custody process, or named owner.

## ENT-01 through ENT-16 audit

| Requirement | Repository implementation | Current evidence | Remaining authoritative proof | Acceptance |
| --- | --- | --- | --- | --- |
| ENT-01 API contracts | Complete: URI-major v1, Problem Details, correlation, reviewed current/previous OpenAPI baselines, drift and compatibility checkers. | Exact-SHA Enterprise CI is green; the composite E2E static and platform-readiness assertions passed. The composite does not replace downstream contract-consumer smoke. | Authorized release execution and downstream candidate smoke. | Repository gate met; release/consumer proof pending. |
| ENT-02 transactional state | Complete: PostgreSQL repository, atomic state/audit/outbox CAS, migration runner, revision API/metrics, legacy read-only import. | Hosted E2E proved the exact migration head, one winner/eight-way CAS behavior, sequence integrity and audit chain; hosted DR proved clean restore. | Deployment PostgreSQL concurrency, migration/rollback, replica and managed-failover evidence. | Hosted profile met; deployment pending. |
| ENT-03 safe routing | Complete at code level: persisted lifecycle, machine-owned backfill/parity, high-water repair, lag/DLQ gates, rollback coverage. | The exact-commit two-cluster E2E backfill, parity, dual-write, cutover and rollback scenario passed. | Deployment-cluster failure, recovery, operational deadline and paging evidence. | Hosted profile met; deployment pending. |
| ENT-04 reconciliation | Complete at code level: PostgreSQL authority, durable polling/outbox and Redis wake-up only as acceleration. | Exact-commit mutation, dropped-notification and restarted-replica convergence scenarios passed without Redis. | Deployment replica convergence and outage/recovery evidence. | Hosted profile met; deployment pending. |
| ENT-05 identity/authorization | Partial: OIDC, scopes, tenant policy, API-key isolation and Admin session/CSRF controls exist; ASVS mapping still records machine-identity, security-event, and deployed IdP gaps. | Hosted E2E proved TLS-backed RS256/JWKS validation, two-tenant isolation, deny-by-default policy and cross-tenant/missing-claim denials in the disposable profile. | Deployed IdP rotation/unavailability, session revocation, workload tenant binding, complete security-event emission and deployment ASVS evidence. | Hosted subset met; deployment pending. |
| ENT-06 secrets/TLS | Complete at repository level for configurable TLS, workload-scoped injection, refs-only Typesense state/export, bounded env/file resolution and rotation without image rebuild. | Exact-commit E2E passed env/file secret rotation, revoked-key rejection, refs-only state/export, log scanning and Typesense TLS identity; Kind TLS/network assertions also passed. | External secret-manager access/custody policy and deployment trust-chain/rotation evidence. | Hosted profile met; deployment pending. |
| ENT-07 operator audit | Partial: atomic hash-chained audit, safe export, provider-neutral exact-boundary delivery checkpoints, executable HTTPS worker, redacted retry state, backlog/failure metrics and alerts, retention guardrails, security-event catalog, and restore integrity exist. Full auth/denial emission and an independently controlled sink remain open. | Exact-commit E2E verified the audit chain; hosted encrypted DR retained a non-vacuous chain and delivery checkpoint through retention and restore. | Deployed sink immutability, complete auth/denial emission, synthetic alert delivery, access, hold and retention proof. | Hosted subset met; deployment pending. |
| ENT-08 lifecycle/DR | Partial: encrypted backup, guarded clean restore, owned-outbox retention, ordered tombstones, atomic deletion ledger/backfill, replay suppression and provider-neutral restored-target reconciliation exist. Production scheduling, target-adapter execution and custody remain open. | Hosted exact-commit DR passed RPO/RTO thresholds, age encryption, guarded clean restore, live retention invariants, deletion-ledger/target-receipt preservation and cleanup. | Deployed reconciler/targets, scheduler, immutable off-site copy, key recovery, deletion convergence, backup expiry and managed failover. | Hosted profile met; deployment pending. |
| ENT-09 reliability/SLO | Partial: SLO policy, Prometheus rules, dashboard, bounded health checks and runbooks exist; deployed reporting, page delivery, explicit Kafka/DLQ/rollout coverage, representative outage recovery, and circuit/overload evidence remain open. | Exact-commit Kind restart, eviction, drain and threshold-gated load assertions passed; this is a disposable cluster, not an SLO observation window. | Deployment 30-day measurement, target discovery, alert delivery, representative outage recovery, capacity and error-budget ownership. | Hosted subset met; deployment pending. |
| ENT-10 diagnostics | Complete at code level: structured redacted logs, metrics, release/revision identity, W3C HTTP-to-Kafka-to-worker spans and OTLP controls. | Exact-commit E2E passed collector TLS and the HTTP-to-Kafka-to-worker-to-Typesense parent/child span chain. | Rendered environment dashboards plus deployed collector/sink access and retention. | Hosted profile met; deployment pending. |
| ENT-11 performance | Harness/profile candidate exists with threshold and provenance contracts. | The exact-commit immutable-image Kind benchmark passed its reviewed ingest, convergence, search-latency, zero-error and no-partial thresholds. Its manifest explicitly sets `production_certification=false`. | Deployment-sized representative workload, sustained measurement, resource/cost envelope and capacity-owner approval. | Repository benchmark met; production capacity **not met**. |
| ENT-12 Kubernetes | Helm hardening and fail-closed Kind harness exist: security contexts, resources, PDB/placement, network policy, TLS, external secrets and digest policy. | All reviewed exact-set multi-node Kind assertions passed, including immutable images, migration, secure runtime, network/TLS, restart, eviction, drain, load and cleanup. | Deployment controller, admission, CNI/CSI and managed-provider validation. | Hosted profile met; deployment pending. |
| ENT-13 supply chain | Partial: pinned workflows, locks, SAST, dependency/secret/container scans, signed images, SBOM/provenance and changelog/rollback metadata are designed; release publication now depends on both reusable CI and exact-commit enterprise assurance. | Exact-SHA Enterprise CI and enterprise assurance are green. No signed release or consumer verification was produced by these runs. | Verify protected-branch/tag settings, then execute an authorized signed release and clean consumer verification. | Hosted candidate gates met; release attestation pending. |
| ENT-14 test depth | Partial: unit, adapter, API, Helm, browser, accessibility, live, DR and Kind/load harnesses plus the test matrix exist. | Exact-SHA Enterprise CI is green and the hosted composite accepted all reviewed E2E, DR, Kind and benchmark sets with no composite errors. | Retained authorized-release evidence and deployment-specific failure/recovery acceptance. | Repository-hosted gate met; release/deployment pending. |
| ENT-15 operator UX | Partial: revision/conflict, rollout phase/gates, audit, degraded states, confirmation and accessible primitives are implemented. Browser tests cover dashboard keyboard navigation and rollout draft creation, but not the full rendered conflict/recovery lifecycle. | Exact-SHA Enterprise CI is green, but the enterprise composite does not add full rendered conflict/recovery or manual keyboard evidence. | Hosted desktop/mobile conflict, recovery, failure, rollback and audit flows plus a retained manual keyboard record. | Not met. |
| ENT-16 handover | Repository ADRs 0001–0006, threat model, lifecycle, SLO, observability, ownership, release/rollback and runbooks are coherent; a dedicated routing-failure runbook and dated agent-led tabletop now exist. | Relative Markdown links resolve locally and the exact-commit composite run is linked above. This is not human review or environment-owner sign-off. | Close tabletop findings, bind all owner roles in a service catalog, and record ADR/runbook and environment-owner approval. | Not met. |

## Current release blockers

The following prevent a PASS regardless of unit-test count:

1. The composite explicitly reports `production_certification=false`.
   Deployment-owned IdP/workload identity, secret-manager custody, TLS trust,
   managed-service and tenant evidence remain absent.
2. ENT-11 production capacity is not established by the passing disposable
   Kind benchmark; representative scale, duration, resource/cost envelope and
   capacity-owner approval remain open.
3. Kafka lag/DLQ and routing-deadline alerting, delivered paging, a production
   SLO window, deployed audit sink/WORM controls, scheduled DR, off-site backup
   custody and restored-target adapter execution remain open.
4. Protected branch/tag/ruleset settings still require external verification,
   and no authorized signed release, release attestation or clean consumer
   verification has been recorded.
5. Tabletop findings, named service-catalog ownership, human ADR/runbook review,
   environment approval and customer/change approval remain open.

## Exact-clean-commit procedure

Run from a fresh checkout of the candidate SHA. Do not use
`--allow-dirty`, do not edit evidence JSON, and do not reuse artifacts from a
different commit or image set.

```bash
test -z "$(git status --porcelain)"
CANDIDATE_COMMIT="$(git rev-parse HEAD)"

make workflow-policy lock-verify contracts helm ops
.venv/bin/python -m pytest -q query_api/tests
.venv/bin/python -m pytest -q indexing_service/tests
npm --prefix admin_ui test
npm --prefix admin_ui run lint
npm --prefix admin_ui run build
npm --prefix admin_ui run test:e2e
```

Run the Docker-owning live suites serially on the same daemon:

```bash
E2E_DEPENDENCY_MODE=compose scripts/e2e/run-enterprise-e2e.sh
DR_EVIDENCE_BASENAME=dr-live-postgres-latest.json \
  ops/dr/run-live-postgres-dr.sh
scripts/e2e/run-kind-enterprise-smoke.sh
```

Then execute the same fail-closed composite verifier used by
`.github/workflows/enterprise-assurance.yml`:

```bash
python3 scripts/ci/verify_enterprise_evidence.py \
  --e2e docs/evidence/enterprise-e2e-live-latest.json \
  --dr docs/evidence/dr-live-postgres-latest.json \
  --dr-sha256 docs/evidence/dr-live-postgres-latest.json.sha256 \
  --kind docs/evidence/kind-enterprise-smoke-live-latest.json \
  --benchmark docs/evidence/kind-enterprise-benchmark-live-latest.json \
  --expected-commit "$CANDIDATE_COMMIT" \
  --output-json artifacts/enterprise-assurance.json \
  --output-markdown artifacts/enterprise-assurance.md
```

The verifier must report `status=passed`, every reviewed scenario/assertion
must appear exactly once and pass, every evidence provenance must identify
`$CANDIDATE_COMMIT` with a clean source tree, cleanup/checksum/threshold/image
contracts must pass, and `production_certification` must remain `false`.

## Promotion decision

A repository release candidate can be marked PASS only when:

- all deterministic and composite commands above pass on the exact clean SHA;
- hosted `ci.yml` and `enterprise-assurance.yml` are green for that same SHA,
  with retained artifacts and no critical skip;
- the release path demonstrably requires that exact composite result;
- no known P0/P1 defect or unexpired risk blocks promotion; and
- protected-branch/release settings and rollback metadata are verified.

The 2026-07-17 addendum satisfies the hosted exact-commit CI/composite portion
for the named SHA. The release workflow at that SHA also requires both reusable
gates before publication, but this is repository design rather than evidence of
an executed signed release. The external settings, consumer, deployment, and
approval portions remain open.

An enterprise deployment needs additional environment evidence: named on-call
owners, IdP/tenant tests, secret rotation and revocation, TLS paths, alert
delivery, immutable backup custody and restore, deletion/hold handling,
managed-service HA, production-shaped capacity, SLO reporting, and customer or
change approval. Source-code PASS must never be relabeled as production
certification.

See the [dated tabletop](TABLETOP_2026-07-10.md) and
[requirement-to-test matrix](TEST_MATRIX.md) for scenario decisions and the
authoritative check mapping.
