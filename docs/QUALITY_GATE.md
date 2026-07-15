# Enterprise quality gate

Status: **NOT PASS — remediation in progress**.

This is the ENT-16 handover audit for the requirements in
[the Enterprise Delivery Contract](ENTERPRISE_DELIVERY_CONTRACT.md). It
separates repository implementation, disposable local evidence, hosted release
evidence, and deployment-owned proof. A file existing in Git is not runtime
evidence, and a local pass with a dirty tree is not evidence for a release
commit.

The audit was performed on 2026-07-10 against a working tree based on
`dffb3a077866f97881dd716edb494983d92c61b3`. The tree was dirty and parallel
remediation was still running. The status below therefore remains fail-closed
even if a mutable `docs/evidence/*-latest.*` file is replaced later; only a new
composite report bound to the final clean commit can change the decision.

## Status model

- **Complete:** the repository implementation and deterministic checks cover
  the stated boundary; this does not imply runtime acceptance.
- **Partial:** an acceptance behavior or control is still missing, or the
  repository's own security/lifecycle documents record a material gap.
- **Local evidence:** an isolated machine exercise passed its own schema with
  `production_certification=false`; it cannot certify a deployment.
- **Hosted pending:** a clean hosted run for the exact candidate SHA is absent.
- **Deployment pending:** the control depends on an actual IdP, secret manager,
  pager, managed data service, region, custody process, or named owner.

## ENT-01 through ENT-16 audit

| Requirement | Repository implementation | Evidence at audit | Remaining authoritative proof | Acceptance |
| --- | --- | --- | --- | --- |
| ENT-01 API contracts | Complete: URI-major v1, Problem Details, correlation, reviewed current/previous OpenAPI baselines, drift and compatibility checkers. | Deterministic tests/checkers exist, but this audit is not bound to a final clean candidate. | Green hosted contract/compatibility job and downstream candidate smoke. | Not met for release. |
| ENT-02 transactional state | Complete: PostgreSQL repository, atomic state/audit/outbox CAS, migration runner, revision API/metrics, legacy read-only import. | Unit/integration coverage exists; current composite live E2E did not pass. | Exact-commit PostgreSQL concurrency, migration/rollback, and replica evidence. | Not met. |
| ENT-03 safe routing | Complete at code level: persisted lifecycle, machine-owned backfill/parity, high-water repair, lag/DLQ gates, rollback coverage. | State-machine tests exist; current two-cluster live artifact did not pass. | Exact-commit live dual-write, failure, cutover, rollback, and recovery smoke. | Not met. |
| ENT-04 reconciliation | Complete at code level: PostgreSQL authority, durable polling/outbox and Redis wake-up only as acceleration. | Deterministic convergence tests exist; current multi-replica live artifact did not pass. | Dropped-notification and restarted-replica exact revision convergence on the candidate. | Not met. |
| ENT-05 identity/authorization | Partial: OIDC, scopes, tenant policy, API-key isolation and Admin session/CSRF controls exist; ASVS mapping still records machine-identity, security-event, and deployed IdP gaps. | Negative suites are repository evidence only. | Live cross-tenant denials, IdP rotation/unavailability, session revocation, workload tenant binding, and deployment ASVS evidence. | Not met. |
| ENT-06 secrets/TLS | Complete at repository level for configurable TLS, workload-scoped injection, refs-only Typesense state/export, bounded env/file resolution and rotation without image rebuild. | Helm/adapter tests and the live rotation scenario exist, but exact-clean-commit evidence is still pending. | Exact-commit TLS and env/file rotation smoke, secret scan, revoked-key rejection, external secret-manager policy, and deployment trust-chain evidence. | Not met. |
| ENT-07 operator audit | Partial: atomic hash-chained audit, safe export, provider-neutral exact-boundary delivery checkpoints, executable HTTPS worker, redacted retry state, backlog/failure metrics and alerts, retention guardrails, security-event catalog, and restore integrity exist. Full auth/denial emission and an independently controlled sink remain open. | Deterministic adapter tests are repository evidence; the local DR artifact proves a non-vacuous chain only in a dirty disposable run. | Clean exact-commit retention/restore/delivery evidence plus deployed sink immutability, synthetic alert delivery, access, hold, and retention proof. | Not met. |
| ENT-08 lifecycle/DR | Partial: encrypted backup, guarded clean restore, owned-outbox retention, ordered tombstones, atomic deletion ledger/backfill, replay suppression and provider-neutral restored-target reconciliation exist. Production scheduling, target-adapter execution and custody remain open. | Deterministic tests prove older replay is suppressed and all target receipts gate convergence; the local DR report remains dirty and non-production. | Clean timed DR plus deployed reconciler/targets, scheduler, immutable off-site copy, key recovery, deletion convergence, backup expiry, and managed failover. | Not met. |
| ENT-09 reliability/SLO | Partial: SLO policy, Prometheus rules, dashboard, bounded health checks and runbooks exist; deployed reporting, page delivery, explicit Kafka/DLQ/rollout coverage, representative outage recovery, and circuit/overload evidence remain open. | Static rule validation is not live SLO evidence. | Hosted failure-injection/Kind evidence and deployment 30-day measurement, target discovery, alert delivery, capacity, and error-budget ownership. | Not met. |
| ENT-10 diagnostics | Complete at code level: structured redacted logs, metrics, release/revision identity, W3C HTTP-to-Kafka-to-worker spans and OTLP controls. | Unit and a local exporter probe exist; current composite trace-continuity artifact did not pass. | Exact-commit captured span lineage, rendered dashboard/runbook artifacts, and deployed collector/sink access and retention. | Not met. |
| ENT-11 performance | Harness/profile candidate exists with threshold and provenance contracts. | No accepted production-shaped benchmark artifact was available to the composite audit. | Clean immutable-image Kind benchmark plus deployment-sized workload profile/capacity approval. | Not met. |
| ENT-12 Kubernetes | Helm hardening and fail-closed Kind harness exist: security contexts, resources, PDB/placement, network policy, TLS, external secrets and digest policy. | Current Kind artifact did not pass its exact assertion set. | Clean multi-node restart, eviction, drain, TLS, network, load, and cleanup artifact; deployment controller/provider validation. | Not met. |
| ENT-13 supply chain | Partial: pinned workflows, locks, SAST, dependency/secret/container scans, signed images, SBOM/provenance and changelog/rollback metadata are designed; release-to-composite enforcement is being remediated. | Workflow files are repository design, not a hosted run or release attestation. | Green hosted CI and enterprise assurance for the exact commit, protected-branch settings, then an authorized signed release and consumer verification. | Not met. |
| ENT-14 test depth | Partial: unit, adapter, API, Helm, browser, accessibility, live, DR and Kind/load harnesses plus the test matrix exist. | Current composite is failed; critical live paths are therefore not accepted. | Green hosted release gate with no critical skip and retained failure-recovery evidence. | Not met. |
| ENT-15 operator UX | Partial: revision/conflict, rollout phase/gates, audit, degraded states, confirmation and accessible primitives are implemented. Browser tests cover dashboard keyboard navigation and rollout draft creation, but not the full rendered conflict/recovery lifecycle. | Local Playwright/axe evidence is useful engineering evidence, not a final hosted artifact. | Hosted desktop/mobile conflict, recovery, failure, rollback and audit flows, serious/critical axe gate, and retained manual keyboard record. | Not met. |
| ENT-16 handover | Repository ADRs 0001–0006, threat model, lifecycle, SLO, observability, ownership, release/rollback and runbooks are coherent; a dedicated routing-failure runbook and dated agent-led tabletop now exist. | Relative Markdown links resolve locally. This is not human review or environment owner sign-off. | Close tabletop findings, bind all owner roles in a service catalog, review ADRs/runbooks, and attach the exact-commit composite report. | Not met. |

## Current release blockers

The following prevent a PASS regardless of unit-test count:

1. No final clean candidate commit exists yet; mutable local evidence cannot be
   bound authoritatively to the worktree.
2. The current composite verifier result is failed: live E2E and Kind evidence
   are not passing the reviewed exact sets, the benchmark artifact is absent,
   and dirty provenance is rejected. Remediation is in progress.
3. The refs-only Typesense credential implementation still requires passing
   exact-clean-commit rotation/revocation evidence and deployed secret-manager
   access/custody proof; source implementation alone does not close T-03.
4. Release publication must be fail-closed on exact-commit enterprise composite
   assurance, not only the ordinary CI workflow. Remediation is in progress.
5. Kafka lag/DLQ and routing rollout failure/deadline alerting plus delivered
   paging evidence are incomplete. Remediation is in progress.
6. Deployed audit sink/WORM evidence, restored-target adapter execution, hosted
   release attestation, branch/ruleset controls, and deployment ownership remain
   open.

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

An enterprise deployment needs additional environment evidence: named on-call
owners, IdP/tenant tests, secret rotation and revocation, TLS paths, alert
delivery, immutable backup custody and restore, deletion/hold handling,
managed-service HA, production-shaped capacity, SLO reporting, and customer or
change approval. Source-code PASS must never be relabeled as production
certification.

See the [dated tabletop](TABLETOP_2026-07-10.md) and
[requirement-to-test matrix](TEST_MATRIX.md) for scenario decisions and the
authoritative check mapping.
