# OWASP ASVS 5.0 Baseline

This is a selected engineering baseline against OWASP Application Security
Verification Standard 5.0.0. It is not a complete assessment, certification, or
claim of ASVS compliance. A control is `Implemented` only when the repository
contains direct implementation and automated evidence for the scoped component;
deployment-dependent or incomplete controls remain `Partial` or `Gap`.

Status legend:

- **Implemented** — direct code and automated repository evidence exists for the
  scoped behavior.
- **Partial** — useful control exists, but coverage, deployment evidence, or a
  required lifecycle behavior is missing.
- **Gap** — no adequate implementation evidence exists.
- **Deployment** — the repository can support the control, but the deployed
  environment must provide and prove it.

Authoritative standard: [OWASP ASVS v5.0.0](https://github.com/OWASP/ASVS/tree/v5.0.0).

## Browser and session controls

| ASVS requirement | Status | Repository evidence | Gap / next proof |
| --- | --- | --- | --- |
| V3.3.2 SameSite cookie appropriate to purpose | Partial | `admin_ui/app/lib/adminAuth.js` emits `SameSite=Lax` for transaction and session cookies; `admin_ui/tests/auth-route.test.mjs`. | Document the cross-site model and add deployed browser tests for login, callback and sensitive mutations. |
| V3.3.4 HttpOnly session cookie | Implemented | `serializeCookie` in `adminAuth.js`; auth route tests inspect the sealed cookie. | Preserve through ingress and application-server upgrades. |
| V3.4.1 HSTS | Partial | `buildSecurityHeaders` in `admin_ui/next.config.js`; `security-accessibility.test.mjs`. | Verify the deployed Admin UI and every browser-facing API response; decide preload separately. |
| V3.4.3 Content Security Policy | Partial | Default CSP, source-list overrides and frame-ancestor normalization in `next.config.js`. | Replace `unsafe-inline` with per-response nonces or hashes and capture deployed DAST evidence. |
| V3.4.4 `X-Content-Type-Options: nosniff` | Implemented | `buildSecurityHeaders`; Node security-header test. | Verify ingress does not remove or duplicate the header. |
| V3.4.5 Referrer policy | Implemented | Allowlisted `ADMIN_UI_REFERRER_POLICY` configuration and default `strict-origin-when-cross-origin`; Node test. | Confirm organization-specific hostname and query sensitivity. |
| V3.5.1 Browser request-forgery defense | Implemented | Unsafe Admin UI auth/BFF routes require an exact configured public `Origin` and `Sec-Fetch-Site: same-origin` under OIDC, production, or enterprise profiles; negative Node tests cover missing/malformed/cross-site provenance. | Capture deployed multi-browser evidence and verify every ingress preserves these request headers. |
| V3.5.3 Safe methods for sensitive functions | Partial | Logout is POST-only, its GET route is non-mutating with `405`, and the sidebar uses an accessible form. | Review other sensitive reads such as restore-ready exports for POST/step-up semantics and capture deployed browser evidence. |
| V7.3.1 Inactivity timeout | Gap | Session has an expiry but no activity-based server-side timeout. | Add a reference session or activity ledger with documented idle timeout. |
| V7.3.2 Absolute session lifetime | Partial | Admin session expiry is capped by access-token and configured session TTL. | Document the risk decision and prove it against the IdP/session deployment. |
| V7.4.1 Session termination | Partial | Logout clears browser cookies. | Revoke or deny the underlying access token and handle IdP back-channel/account-disable events. |

## Authentication, tokens and authorization

| ASVS requirement | Status | Repository evidence | Gap / next proof |
| --- | --- | --- | --- |
| V6.8.2 Authentication assertion signatures | Implemented | Query API `auth.py` validates JWT signatures with asymmetric algorithms; Admin UI verifies ID tokens with `jose`; negative unsigned-token tests exist. | Add live IdP rotation and unavailable-JWKS tests. |
| V9.1.3 Trusted token validation keys | Implemented | Admin UI OIDC requires an issuer, HTTPS endpoint URLs, issuer-origin binding or an explicit endpoint-origin allowlist, redirect rejection, and bounded JWKS fetches; token header key URLs are not caller-selected. | Add live DNS/egress policy, key rotation, and unavailable-JWKS evidence. |
| V9.2.1 Token validity period | Implemented | Query API requires `exp` and `iat`; Admin UI verifies ID-token expiry and session expiry. | Define maximum accepted token lifetime and replay response. |
| V9.2.3 / V10.3.1 Resource-server audience | Implemented | Query API validates configured audience; tests reject a wrong audience. | Include multi-audience and token-exchange deployment tests. |
| V10.5.1 OIDC nonce | Implemented | Login creates nonce and callback verifies it; negative test exists. | Preserve during any move to a hosted identity library. |
| V10.5.3 OIDC issuer metadata | Implemented | Discovery metadata issuer must exactly match the mandatory configured issuer; discovery is HTTPS/issuer-bound, rejects redirects, and has a bounded timeout with negative tests. | Exercise a real provider, approved split-origin endpoints, rotation, and network failure in deployment. |
| V10.5.4 ID-token audience | Implemented | `jwtVerify` and explicit audience check in `adminAuth.js`. | Keep client registration evidence with release artifacts. |
| V8.2.1 Function-level authorization | Partial | Distinct admin read/write/backup/restore/internal scopes and data search/ingest scopes in `query_api/app/deps.py`; extensive auth tests. | Return 403 for authenticated-but-forbidden callers and add resource-scoped admin roles. |
| V8.2.2 Data-specific authorization | Partial | Collection glob scopes and server-side tenant filters cover search, read, write, and delete; enterprise startup and request paths deny collections without an explicit policy. Negative API tests cover policy mismatch. | Add release-environment OIDC tenant-isolation evidence against every live multi-cluster path. |
| V8.2.3 Field-level authorization | Gap | Callers can request fields and document reads return the stored object. | Add readable/writable-field policies with BOPLA tests. |
| V8.4.1 Cross-tenant controls | Partial | Tenant claim values are validated; search/delete filters are injected; writes require authorized tenant subsets. Enterprise rejects missing collection policy and requires `AUTHZ_API_KEY_TENANT_BYPASS=false`; negative tests cover unmatched policies. | Bind non-OIDC machine identities to tenants and retain live cross-tenant denial evidence per release. |

## API, service and data protection

| ASVS requirement | Status | Repository evidence | Gap / next proof |
| --- | --- | --- | --- |
| V4.1.3 Intermediary headers cannot be overridden | Partial | Static credential injection is disabled by default in production/enterprise; legacy trusted-header injection requires two explicit opt-ins and the header is stripped before proxying. | Prove client-header removal/replacement at the trusted edge and replace the shared value with signed audience-bound identity or end-to-end OIDC. |
| V13.2.1 Authenticated backend communications using individual, non-static identities | Partial | Kafka producer/consumer support validated TLS, hostname verification, SASL and bounded clients; Redis/PostgreSQL/Typesense use TLS-capable configuration and a private trust bundle. The worker uses a separately scoped internal Query API key. | Replace long-lived service keys with short-lived workload identity or mTLS and prove rotation/revocation against deployed dependencies. |
| V13.3.1 Secrets-management solution | Partial | Helm supports a pre-created Secret or External Secrets Operator, forbids inline enterprise secrets, maps explicit least-privilege `secretKeyRef` keys per process, and rolls pods on an opaque rotation version. Negative Helm policy tests cover cross-workload key exposure. | Store references rather than cluster credentials in control-plane state, integrate the deployment's KMS/CSI solution, and retain a live rotation/revocation drill. |
| V14.1.1 Sensitive-data inventory and classification | Partial | `docs/DATA_LIFECYCLE.md` inventories PostgreSQL, Kafka, Typesense, Redis, telemetry, logs, DLQ, backups, owners, and initial retention targets. | Attach organization-approved classification levels, residency rules, processors, and data-owner sign-off. |
| V14.2.1 Sensitive values excluded from URLs | Partial | API keys and tokens use headers; POST search exists for sensitive vector payloads. GET search still permits query/filter data in URLs. | Define sensitivity policy and deprecate or constrain URL-based sensitive search. |
| V14.2.2 Sensitive server-cache prevention | Partial | Every Admin UI session/auth/proxy response forces downstream `no-store, private` plus `Pragma: no-cache`, with Node tests proving an upstream public-cache directive is overridden. | Verify deployed browser, CDN, ingress, and direct Query API behavior; classify which direct data responses may be cached. |
| V14.2.7 Sensitive-data retention | Partial | The lifecycle contract models active and retained copies; the guarded outbox-retention CLI preserves unpublished/latest/held records; tombstones and deletion convergence have tests and runbooks; encrypted backup/restore has an isolated drill. | Add an immutable erasure/restore-suppression ledger and deployment evidence for Kafka, DLQ, logs, backups, legal hold, and scheduled expiry. |

## Logging and error handling

| ASVS requirement | Status | Repository evidence | Gap / next proof |
| --- | --- | --- | --- |
| V16.1.1 Logging inventory and retention | Partial | `docs/DATA_LIFECYCLE.md`, `docs/OBSERVABILITY.md`, SLO alerts, and runbooks identify application/audit/metric/trace stores and initial retention ownership. | Add a deployment-approved security-event catalog with final destinations, access policy, legal hold, and sink retention evidence. |
| V16.2.1 Investigation metadata | Partial | PostgreSQL audit entries include UTC time, hashed actor, action, resource, request ID, revision, outcome and safe details; HTTP, outbox, Kafka, worker and trace contexts carry correlation metadata. | Add consistent source network, evaluated scopes, tenant and service identity to every security decision without increasing sensitive or high-cardinality telemetry. |
| V16.3.1 Authentication operations logged | Gap | No durable audit evidence for successful and failed authentication operations. | Emit sanitized events to the external security sink and test both outcomes. |
| V16.3.2 Failed authorization logged | Gap | API authorization rejects requests but does not persist denial events. | Log denial decisions without credentials or sensitive document data; alert on abuse patterns. |
| V16.4.2 Logs protected from modification | Partial | Transactional PostgreSQL audit rows use a verified hash chain; state and audit commit together. Provider-neutral export checkpoints advance only on an exact hash/sequence acknowledgement, retain redacted failure/retry state, and expose backlog/failure alerts. Retention tooling cannot delete the audit chain. | Deploy and verify a destination under independent control, validate its append-only/WORM properties where required, and retain synthetic delivery/page evidence. |
| V16.5.1 Generic consumer errors | Partial | Locally caught Admin UI authentication and Query API transport failures return stable codes and generic details; tests inject sensitive exception messages and prove redaction. | Complete the same stable problem/error contract across every service and capture DAST evidence. |

## Accessibility evidence (not an ASVS compliance claim)

The Admin UI baseline targets WCAG 2.2 AA and the WAI-ARIA modal dialog pattern:

- `Input.jsx` creates stable label associations and connects inline errors with
  `aria-describedby`, `aria-invalid`, and an alert role.
- `ConfirmationModal.jsx` assigns an accessible name/description, focuses the
  safe action, traps Tab, closes on Escape, and restores the trigger.
- notifications expose polite or assertive live regions and an accessible
  dismiss action;
- the layout provides a skip link and focusable main landmark; primary
  navigation exposes `aria-current`.
- `admin_ui/tests/security-accessibility.test.mjs` guards these structural
  contracts.

These source-level tests do not prove WCAG conformance. Required release
evidence remains automated axe scans with zero serious/critical findings,
keyboard-only end-to-end workflows, screen-reader spot checks, contrast/reflow
tests, and issue-level remediation records.

## Completion criteria for a future ASVS claim

Before making any ASVS claim, define the target level and exact application
boundary, assess every applicable v5.0.0 requirement, attach independent runtime
evidence, resolve or formally accept every gap, and have a qualified reviewer
sign and date the assessment. Green unit tests or this mapping alone are not
sufficient.
