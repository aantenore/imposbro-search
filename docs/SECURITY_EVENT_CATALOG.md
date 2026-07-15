# Minimum security event catalog

Status: **repository event contract; deployment sink coverage is not proven**.

Every security-relevant event must use the tamper-evident audit fields: UTC
timestamp, hashed or workload actor, stable action, resource type/ID, outcome,
request ID, optional committed revision, and allowlisted safe details. Tokens,
API keys, document bodies, database URLs, raw authorization headers, and raw
provider errors are forbidden. Tenant/document identifiers are kept out of
metrics; audit access and retention remain restricted.

| Event family / stable action | Required trigger | Minimum safe details | Repository status |
| --- | --- | --- | --- |
| `authentication_failed` | Invalid/expired token, API key, issuer, audience, signature, nonce or session | auth scheme, stable reason code, route template | Required catalog event; complete emission coverage remains open. |
| `authorization_denied` | Missing scope, collection policy, tenant mismatch, restore/backup permission denial | evaluated scope, resource type, stable denial code | Required catalog event; complete emission coverage remains open. |
| `cluster_registered` / `cluster_deleted` | Provider registry mutation | cluster logical ID, outcome; never host credential | Implemented mutation audit. |
| `routing_rollout_created` / `routing_rollout_transitioned` | Routing lifecycle mutation | rollout ID, collection, from/to phase, rollout version | Implemented mutation audit. |
| `state_exported` / `state_imported` | Backup/export or restore/import request | counts, secrets-included boolean, dry-run/apply, outcome | Implemented; production plaintext-secret export is denied. |
| `collection_created` / `collection_deletion_started` | Collection desired-state mutation | collection logical ID, revision, outcome | Implemented mutation audit. |
| `audit_delivery_failed` / `audit_delivery_recovered` | Destination retry begins or returns to head | destination ID, redacted error code, failure count, sequence gap | Durable checkpoint/metrics implemented; app-level audit emission remains wiring work. |
| `deletion_suppression_registered` / `deletion_converged` | Tombstone is committed or every target receipt is verified | identity hash, sequence, target count, routing revision; no payload | Durable ledger implemented; app-level audit emission remains wiring work. |
| `legal_hold_changed` | Hold applied or released | identity hash, enabled flag, hashed case reference, approver actor | Repository adapter implemented; caller audit wiring and approval workflow remain open. |
| `credential_rotation_started` / `credential_revoked` | Rotation drill or emergency revocation | provider class, version ID/hash, workload, outcome | Deployment-owned emission and evidence. |

The deployment owner must map these actions to alert/severity, final sink,
retention, legal hold, access role, and incident procedure. A source-code catalog
does not certify completeness; negative tests and a deployed synthetic-event
drill must prove every exposed authentication and authorization path.
