# Credential and trust-material rotation

This is the deployment-neutral rotation drill for IMPOSBRO-owned consumers of
external credentials. The secret manager, IdP, database, Kafka and Typesense
remain authoritative for issuance and revocation. Never copy a secret into a
Helm values file, command history, ticket, log, evidence artifact or chat.

## Preconditions and ownership

- Open a time-bounded change record with service owner, security owner,
  rollback owner, affected environments and secret-manager version IDs.
- Confirm at least two ready replicas, PDB health, current error/latency/lag
  budget, and enough capacity for a rolling restart.
- Freeze unrelated routing/schema/deployment changes and capture current image,
  Helm revision, control-plane revision and `secrets.rolloutVersion`.
- Verify the selected pre-created Secret or ExternalSecret contains only the
  keys declared in `secrets.providedKeys`; each process receives only its
  `secrets.workloadKeys` allowlist.
- Prefer an overlap window in which the external provider accepts old and new
  credentials. If the provider cannot overlap, use its approved maintenance or
  blue/green identity procedure; do not weaken readiness or authentication.

## Generic Kubernetes drill

1. Issue the new credential in the authoritative provider and write it as a
   new version in the external secret manager. Keep the old version active.
2. Wait for External Secrets (or the platform secret sync controller) to report
   a successful refresh. Inspect only metadata/version IDs, never values.
3. Change `secrets.rolloutVersion` to a new opaque, non-secret rotation ID in
   the reviewed deployment values and perform an atomic Helm upgrade. The
   annotation change must roll Query API, worker and Admin UI without rebuilding
   images.
4. Watch each rollout and continuous `/ready` probes. Enterprise readiness is
   strict; do not bypass a failed dependency check.
5. Exercise an authenticated admin read, tenant-scoped search, ingest through
   Kafka/worker, audit export, and one OIDC Admin UI session as applicable.
   Confirm no authentication, TLS, outbox, retry, DLQ or trace-export errors.
6. Revoke the old provider credential. Repeat the probes to prove the old value
   is no longer an accidental fallback.
7. Record provider version IDs, Helm revision, pod UIDs before/after, rollout
   duration, probes, audit/request IDs and revocation result. Evidence must
   contain hashes/IDs only.

Do not use `kubectl set env`, edit a live Secret manually, or restart pods
without changing the declared rotation version. Those paths create drift and
make rollback/evidence ambiguous.

## Credential-specific sequencing

| Credential | Safe sequence and important consequence |
| --- | --- |
| `ADMIN_UI_SESSION_SECRET` | Publish new value and roll all UI replicas together. Existing sealed sessions become invalid; announce forced re-authentication. Roll back only while the old value remains approved. |
| Admin/data/internal Query API keys | Prefer separately named entries in `SCOPED_API_KEYS` so old and new keys overlap with the same least-privilege scopes. Move worker/client callers, prove use of the new key, then remove the old entry. Never give the worker the broad `ADMIN_API_KEY` in enterprise mode. |
| `ADMIN_UI_OIDC_CLIENT_SECRET` | Add the new IdP secret with provider overlap, roll UI, complete Authorization Code + PKCE login/callback, then revoke old. Public clients omit this key rather than storing a placeholder. |
| OIDC signing keys/JWKS | The IdP publishes both keys, Query API/Admin UI refresh through bounded HTTPS JWKS calls, old tokens expire, then the IdP removes the old key. Test wrong issuer/audience and unavailable JWKS remain denied. |
| Typesense keys | Create a least-privilege replacement on every intended cluster, update the revisioned cluster configuration through the Admin API, wait for all replicas/workers to converge, exercise read/write/delete/TLS, then revoke old. Cluster keys currently live in control-plane state, so restore-ready exports/backups must follow their protected retention policy. |
| Kafka SASL credential or client certificate | Configure broker overlap, update the external Secret and trust/client-certificate mount, roll producer and consumers, prove publish/consume/checkpoint and zero unexpected DLQ, then revoke old. Watch consumer-group rebalance and lag. |
| PostgreSQL/Redis credential or CA | Use a provider-supported dual role/user or trust overlap, roll clients, verify migration lock/state CAS/rate limiter/config convergence, then revoke old. A CA rotation should publish a bundle containing old+new roots until every endpoint and client has moved. |
| OTLP exporter header | Rotate in the selected Secret, roll Query API/worker, and prove a correlated captured trace before revoking old. Telemetry failure must alert but must not mutate an already accepted data-plane result. |

## Failures and rollback

- Stop the rollout if strict readiness, authentication, TLS hostname/chain,
  Kafka lag, DLQ, audit delivery or trace export crosses its budget.
- Before revocation, roll back the Secret version and
  `secrets.rolloutVersion`, then verify every old pod was replaced and normal
  probes recover.
- After revocation, do not silently re-enable a compromised credential. Issue a
  new version and execute the incident procedure; treat unexpected old-key use
  as a security event.
- A partial provider-side rotation is not complete. Record which principals,
  clusters and replicas accepted each version and assign an owner/deadline.

## Acceptance evidence

The drill passes only when image digests are unchanged, every intended pod UID
changes through a healthy rollout, authenticated workflows succeed before and
after old-version revocation, old-version use is rejected, no secret value
appears in evidence, and rollback metadata is complete. Repository Helm tests
prove the rotation mechanism and least-privilege references; each deployment
must retain its own live issuance/revocation evidence.
