# Admin UI Browser and BFF Security Contract

The Admin UI is a browser-facing application and a same-origin backend-for-
frontend (BFF). Its `/api/*` proxy is not a general-purpose public API gateway.
Machine clients should call the Query API directly with their own identity.

## Browser mutation contract

`POST`, `PUT`, `PATCH`, and `DELETE` requests are protected whenever Admin UI
OIDC is enabled, `NODE_ENV=production`, or the Admin UI security profile is
enterprise. A protected request must provide both:

- an `Origin` header exactly equal to `ADMIN_UI_PUBLIC_ORIGIN`; and
- `Sec-Fetch-Site: same-origin`.

Missing, malformed, opaque (`Origin: null`), same-site, and cross-site origins
fail closed before a session is read or the Query API is called. In production
and enterprise profiles, `ADMIN_UI_PUBLIC_ORIGIN` is mandatory, must contain an
origin only (no path, query, fragment, credentials, or trailing slash), and
must use HTTPS. For example:

```dotenv
ADMIN_UI_PUBLIC_ORIGIN=https://admin.example.com
ADMIN_UI_SECURITY_PROFILE=enterprise
```

This policy intentionally rejects non-browser callers that omit Fetch Metadata.
Those callers must use the Query API instead of the browser BFF. Safe BFF reads
remain compatible and do not require these headers.

Logout is a `POST /api/auth/logout` form operation. `GET /api/auth/logout`
returns `405 Method Not Allowed`, advertises `Allow: POST`, and never clears a
cookie. The navigation sidebar uses an accessible POST form with a relative,
sanitized `return_to` field.

## Server credential injection

The BFF can use server-held Query API keys only through
`ADMIN_UI_SERVER_CREDENTIAL_MODE`:

| Mode | Behavior |
| --- | --- |
| `disabled` | Never inject a server-held API key. This is the default in production and enterprise profiles. |
| `development` | Inject configured keys only outside production/enterprise. This is the local-development default. |
| `trusted-header-legacy` | Inject only after an exact legacy trusted-header match and an additional explicit opt-in. |

Legacy mode requires all of the following:

```dotenv
ADMIN_UI_SERVER_CREDENTIAL_MODE=trusted-header-legacy
ADMIN_UI_ALLOW_LEGACY_TRUSTED_HEADER=true
ADMIN_UI_PROXY_TRUSTED_HEADER=x-authenticated-user
ADMIN_UI_PROXY_TRUSTED_VALUE=an-ingress-owned-value
```

The BFF removes the configured trusted header before forwarding the request.
An authenticated edge must also remove every client-supplied copy and set its
own value. This shared-header design remains a migration mechanism, not the
enterprise target. Browser OIDC sessions or audience-bound workload identities
are preferred. Enabling OIDC never permits a missing browser session to fall
back to a server API key.

## OIDC trust and network policy

The Helm chart maps secrets with explicit, component-scoped `secretKeyRef`
entries. In enterprise mode the Admin UI receives only its sealed-session and
optional OIDC-client credentials; Query API and worker data-plane credentials
are rejected from the UI pod. The Query API, indexing worker, and migration
init container have separate allowlists under `secrets.workloadKeys`, so a
shared external Secret does not imply shared process access.

`ADMIN_UI_OIDC_ISSUER` is mandatory when Admin UI OIDC is enabled. Discovery
metadata must return that exact issuer. Authorization, token, and JWKS URLs:

- must use HTTPS;
- must have no URL credentials or fragment;
- must share the issuer origin by default; and
- may use another operator-approved origin only when it appears in the
  comma-separated `ADMIN_UI_OIDC_ALLOWED_ENDPOINT_ORIGINS` allowlist.

The explicit allowlist supports providers that intentionally separate issuer,
token, or JWKS hosts without accepting caller-selected endpoints. Discovery,
token, and JWKS requests reject redirects and use a bounded timeout configured
by `ADMIN_UI_OIDC_FETCH_TIMEOUT_MS` (default `5000`, permitted range
`250..30000` milliseconds).

The redirect URI must be exactly
`<ADMIN_UI_PUBLIC_ORIGIN>/api/auth/callback`. Local development may use an HTTP
OIDC provider only on `localhost`, `127.0.0.1`, or `::1`, and only with
`ADMIN_UI_OIDC_ALLOW_INSECURE_LOCALHOST=true`; production and enterprise ignore
that escape hatch.

## Response and error contract

Session, authentication, logout, and BFF proxy responses always include
`Cache-Control: no-store, private` and `Pragma: no-cache`, regardless of the
Query API cache headers. Backend cookies are never forwarded to the browser.

Locally caught authentication and transport errors expose a stable `code` and a
generic `detail`; exception messages, hostnames, and configuration internals are
not returned. Clients must branch on HTTP status and `code`, not parse the human
text in `detail`. Query API responses that were successfully received continue
to retain their status, body, and allowlisted correlation/CAS/rate-limit
headers.

Compatibility changes for existing Admin UI integrations are therefore:

1. replace GET logout links with the POST form endpoint;
2. configure `ADMIN_UI_PUBLIC_ORIGIN` and send browser provenance on unsafe BFF
   calls;
3. explicitly select legacy static-key injection if it is temporarily required;
4. configure an issuer even when explicit OIDC endpoints are supplied; and
5. treat local BFF error `detail` strings as non-contractual.

`admin_ui/tests/auth-route.test.mjs`, `proxy-route.test.mjs`, and
`security-accessibility.test.mjs` cover the negative origin, Fetch Metadata,
trusted-header, logout, cache, OIDC endpoint, timeout, redaction, and form
contracts. Deployment evidence must still verify real browser headers, ingress
header stripping, TLS, IdP rotation, and proxy cache behavior.
