# Software supply-chain controls

This document describes implemented controls and their operating assumptions. It is an evidence map, not a compliance certification.

## CI quality gate

`.github/workflows/ci.yml` is both a normal workflow and the reusable release gate. It runs on every branch push, pull request, manual dispatch, and release invocation. All jobs have explicit timeouts, workflow-level concurrency, read-only default permissions, immutable action SHAs, and checkout credentials disabled.

| Control | Enforced evidence |
| --- | --- |
| Query API regression | Hash-locked Python install followed by the complete Query API test suite. |
| PostgreSQL control-plane contract | A digest-pinned PostgreSQL service runs the real Alembic upgrade and concurrent compare-and-swap integration test; this test is invoked separately so a skip is visible and cannot be mistaken for coverage. |
| Indexing worker regression | Hash-locked Python install followed by the complete worker suite. |
| Admin UI | `npm ci --ignore-scripts`, ESLint, Node tests, and a production Next.js build. Lifecycle scripts are disabled during dependency installation; the application build still executes explicitly. |
| Browser boundary | Playwright drives desktop/mobile operator workflows and `@axe-core/playwright` rejects serious/critical accessibility findings; screenshots, traces and reports are retained. |
| Helm | Helm lint plus the repository's positive and negative render contracts with a pinned Helm version. |
| Dependency locks | Production and CI tool locks are regenerated with pinned `uv 0.11.21` in a temporary tree and compared byte-for-byte. |
| Event contracts | Canonical `contracts/*.schema.json` files are parsed and checked against their declared JSON Schema metaschema, with unique IDs and required object fields enforced. Producer/consumer behavior remains covered by the Query API and indexing worker suites. |
| Dependency risk | `pip-audit`, `npm audit`, and GitHub dependency review reject known high/critical risk according to each tool's advisory data. |
| Secrets and configuration | Trivy filesystem scanning gates high/critical vulnerability, secret, and misconfiguration findings. |
| SAST | CodeQL `security-extended` analyzes Python and JavaScript/TypeScript with a commit-pinned action and per-language SARIF category. |
| Container risk | Every production Dockerfile is built and the resulting image is gated by Trivy for fixed high/critical OS and library vulnerabilities; SARIF evidence is retained for 30 days even when the gate fails. |
| Live dependencies | A fail-closed exact-scenario harness exercises PostgreSQL, Kafka, Redis, two Typesense clusters, TLS and W3C/OTLP continuity; partial/interrupted runs cannot finalize green. |
| Operations contracts | Retention/backup guardrails, Prometheus rules/tests/dashboard PromQL and Alertmanager configuration use digest-pinned upstream `promtool`/`amtool` containers when host tools are absent. |
| Workflow integrity | A repository policy script rejects floating action refs, unsafe service image tags, missing top-level permissions/concurrency, and direct secret interpolation into shell commands. Checksum-verified `actionlint` provides syntax and expression validation. |

`.github/workflows/enterprise-assurance.yml` adds a weekly/manual assurance
surface on clean hosted runners. Independent jobs execute the exact-set live
integration harness, five-node Kind disruption/load profile, and encrypted
PostgreSQL clean restore. A final job downloads all artifacts and rejects any
scenario, cleanup, checksum, threshold, image-digest, dirty-tree, or commit
binding mismatch before emitting the composite assurance manifest.

The CI tests intentionally use Python 3.11 and Node.js 22 because those versions match the production Dockerfiles. The PostgreSQL service credential is a disposable test-only value and is not a production secret.

## Locked tooling

Application locks remain owned by each service. CI-only tooling is declared in `scripts/ci/python-tools-requirements.txt` and compiled twice, constrained by each application's production lock. This prevents test tooling from silently upgrading shared packages away from the versions used in the corresponding production image. The verification script seeds the temporary output with the committed lock so `uv` treats it as a preference set: unrelated new upstream releases do not break a branch, while source changes, removed constraints, hash drift, or a non-canonical generated file still produce a byte-for-byte difference.

To refresh locks:

```bash
uv --version  # must report 0.11.21
(cd query_api && uv pip compile requirements.txt --universal --python-version 3.11 --generate-hashes --output-file requirements.lock)
(cd indexing_service && uv pip compile requirements.txt --universal --python-version 3.11 --generate-hashes --output-file requirements.lock)
(cd scripts/ci && uv pip compile python-tools-requirements.txt --constraint ../../query_api/requirements.lock --universal --python-version 3.11 --generate-hashes --output-file python-tools-query.lock)
(cd scripts/ci && uv pip compile python-tools-requirements.txt --constraint ../../indexing_service/requirements.lock --universal --python-version 3.11 --generate-hashes --output-file python-tools-indexing.lock)
scripts/ci/verify-lockfiles.sh
```

Dependabot opens weekly GitHub Actions, Python, npm, and Docker update pull requests. A Python dependency update is incomplete until both the service lock and its constrained CI-tool lock are regenerated and the byte-for-byte gate passes.

## Signed release flow

`.github/workflows/release.yml` accepts a SemVer tag such as `v1.2.3` from a tag push or explicit manual dispatch. Publishing cannot start until both the reusable enterprise CI workflow and the reusable exact-commit enterprise assurance workflow succeed for the release SHA.

The component inventory, build contexts, and non-secret build arguments live in `scripts/ci/release-components.json`; `release-matrix.py` validates names, uniqueness, repository boundaries, Dockerfile presence, and output safety before the workflow expands its matrix. Adding a component does not require duplicating release logic.

For each Query API, indexing worker, and Admin UI image, the release workflow:

1. refuses to replace an existing SemVer release tag, then builds a `linux/amd64` OCI image with BuildKit `mode=max` provenance under a run-scoped staging tag;
2. produces JSON SPDX 2.3 and CycloneDX SBOM files from the pushed digest;
3. creates GitHub build-provenance and SBOM attestations using short-lived GitHub OIDC identity and publishes those attestations with the OCI subject;
4. adds a keyless Cosign signature using the same workflow identity;
5. verifies the Cosign signature and GitHub provenance attestation and retains both SBOMs for 90 days;
6. waits for all three components, downloads their immutable descriptors, re-verifies every signature and provenance attestation, creates and signs one complete platform release manifest, and only then promotes the complete component set to the SemVer and commit-SHA tags.

The publish job grants only `contents: read`, `packages: write`, `id-token: write`, and `attestations: write`. No long-lived signing key or registry password is stored. Manual dispatch still requires a valid SemVer tag, and every image is referenced by digest during signing and attestation. A failed signing, attestation, verification, or SBOM-retention step can leave only a run-scoped staging tag; it cannot create the SemVer release tag.

Example consumer verification:

```bash
gh attestation verify \
  oci://ghcr.io/aantenore/imposbro-search/query-api@sha256:IMAGE_DIGEST \
  --repo aantenore/imposbro-search

cosign verify \
  --certificate-identity 'https://github.com/aantenore/imposbro-search/.github/workflows/release.yml@refs/tags/v1.2.3' \
  --certificate-oidc-issuer 'https://token.actions.githubusercontent.com' \
  ghcr.io/aantenore/imposbro-search/query-api@sha256:IMAGE_DIGEST
```

## Repository settings required outside Git

The repository owner must configure these GitHub controls; a workflow file cannot enforce them by itself:

- protect the default branch and require all `Enterprise CI` jobs before merge;
- require at least one code-owner approval and dismiss stale approvals;
- prevent force-push and branch deletion on the protected branch;
- enable GitHub secret scanning, push protection, Dependabot alerts, and private-vulnerability reporting when available for the repository plan;
- restrict allowed Actions to GitHub-owned actions and the explicitly pinned third-party actions in these workflows;
- protect release tags matching `v*` and limit who can run the manual release workflow;
- configure GHCR retention and visibility deliberately, then periodically test `gh attestation verify` from a clean consumer environment.

## Known limitations and non-claims

- These controls do not by themselves establish SOC 2, ISO 27001, SLSA level, ASVS, or any other certification. An auditor must assess process, access, evidence retention, incident handling, and control operation over time.
- GitHub artifact attestations for private/internal repositories require a GitHub plan that supports them. The release fails before promoting the run-scoped staging image if attestation is unavailable; it does not create an unsigned SemVer release tag.
- Vulnerability scanners are point-in-time advisory consumers and can have false positives, false negatives, or delayed disclosures. Findings need triage and explicit, expiring exceptions rather than permanent workflow bypasses.
- Production Dockerfile base images are pinned by digest and workflow policy
  rejects any external `FROM` without `@sha256`. Dependency update automation
  must deliberately refresh tag annotations and reviewed digests together.
- `npm ci --ignore-scripts` reduces install-time execution risk in CI but may not be compatible with a future dependency that legitimately requires a lifecycle build; such a change must be reviewed explicitly.
- The release currently targets `linux/amd64`. Multi-architecture publication requires QEMU/native builders plus per-platform scan and verification evidence.
- GHCR does not provide one transaction spanning three image repositories. Promotion starts only after every component passes and is re-verified, but an external registry failure during the final sequential tag operations can still leave a partially promoted release. Consumers should deploy the recorded digests, not mutable tag lookups, and operators must reconcile or remove a partial tag set before retrying.
- CODEOWNERS has no enforcement effect until branch protection requires code-owner review.
