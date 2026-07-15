import crypto from 'node:crypto';
import { createRemoteJWKSet, customFetch, jwtVerify } from 'jose';

const DEFAULT_SCOPES = 'openid profile email imposbro:admin imposbro:data';
const DEFAULT_RETURN_TO = '/dashboard';
const DEFAULT_SESSION_TTL_SECONDS = 3600;
const TX_COOKIE_NAME = 'imposbro_admin_oidc_tx';
const TX_TTL_SECONDS = 600;
const ID_TOKEN_CLOCK_SKEW_SECONDS = 60;
const DEFAULT_OIDC_FETCH_TIMEOUT_MS = 5000;
const MIN_OIDC_FETCH_TIMEOUT_MS = 250;
const MAX_OIDC_FETCH_TIMEOUT_MS = 30000;
const SEAL_AAD = Buffer.from('imposbro-admin-ui-session-v1');
const JWKS_CACHE = new Map();
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const NO_STORE_HEADERS = Object.freeze({
  'Cache-Control': 'no-store, private',
  Pragma: 'no-cache',
});

export class AdminAuthError extends Error {
  constructor(message, status = 400, code = 'admin_auth_rejected', publicDetail = null) {
    super(message);
    this.name = 'AdminAuthError';
    this.status = status;
    this.code = code;
    this.publicDetail = publicDetail;
  }
}

export function isAdminOidcEnabled() {
  return String(process.env.ADMIN_UI_OIDC_ENABLED || '').toLowerCase() === 'true';
}

export function isAdminUiProductionLike() {
  const securityProfile = String(process.env.ADMIN_UI_SECURITY_PROFILE || '').trim().toLowerCase();
  return (
    process.env.NODE_ENV === 'production' ||
    securityProfile === 'enterprise' ||
    String(process.env.ADMIN_UI_ENTERPRISE_MODE || '').trim().toLowerCase() === 'true'
  );
}

export function authErrorResponse(error) {
  const isKnownError = error instanceof AdminAuthError;
  const status = isKnownError && Number.isInteger(error.status) ? error.status : 500;
  const code = isKnownError ? error.code : 'admin_auth_failed';
  const detail = isKnownError && error.publicDetail
    ? error.publicDetail
    : status >= 500
      ? 'Admin UI authentication is temporarily unavailable.'
      : 'Authentication request rejected.';
  return Response.json(
    { detail, code },
    { status, headers: noStoreHeaders() }
  );
}

export function methodNotAllowedResponse(allowedMethods) {
  const allow = Array.from(new Set(allowedMethods.map((method) => method.toUpperCase()))).join(', ');
  return Response.json(
    { detail: 'Method not allowed.', code: 'method_not_allowed' },
    { status: 405, headers: noStoreHeaders({ Allow: allow }) }
  );
}

export function noStoreHeaders(initial = {}) {
  const headers = new Headers(initial);
  for (const [name, value] of Object.entries(NO_STORE_HEADERS)) {
    headers.set(name, value);
  }
  return headers;
}

export function enforceBrowserMutationProtection(request) {
  const method = String(request.method || 'GET').toUpperCase();
  if (!UNSAFE_METHODS.has(method) || !requiresBrowserMutationProtection()) {
    return;
  }

  const expectedOrigin = publicOriginFor(request, { requireConfigured: isAdminUiProductionLike() });
  const suppliedOrigin = request.headers?.get?.('origin') || '';
  if (!suppliedOrigin || suppliedOrigin === 'null') {
    throw new AdminAuthError(
      'Unsafe browser requests require an Origin header.',
      403,
      'csrf_origin_required',
      'Request origin validation failed.'
    );
  }

  let parsedOrigin;
  try {
    parsedOrigin = parseStrictOrigin(suppliedOrigin, 'Origin');
  } catch (_) {
    throw new AdminAuthError(
      'Unsafe browser request Origin is malformed.',
      403,
      'csrf_origin_invalid',
      'Request origin validation failed.'
    );
  }
  if (parsedOrigin !== expectedOrigin) {
    throw new AdminAuthError(
      'Unsafe browser request Origin does not match the Admin UI public origin.',
      403,
      'csrf_origin_mismatch',
      'Request origin validation failed.'
    );
  }

  const fetchSite = (request.headers?.get?.('sec-fetch-site') || '').trim().toLowerCase();
  if (fetchSite !== 'same-origin') {
    throw new AdminAuthError(
      'Unsafe browser requests require Sec-Fetch-Site: same-origin.',
      403,
      fetchSite ? 'csrf_cross_site_request' : 'csrf_fetch_metadata_required',
      'Request origin validation failed.'
    );
  }
}

export async function createLoginResponse(request) {
  const config = await getOidcConfig(request);
  if (!config.enabled) {
    throw new AdminAuthError('Admin UI OIDC login is disabled.', 404);
  }

  const returnTo = sanitizeReturnTo(getRequestUrl(request).searchParams.get('return_to'));
  const state = randomBase64Url(32);
  const nonce = randomBase64Url(32);
  const codeVerifier = randomBase64Url(32);
  const codeChallenge = base64Url(crypto.createHash('sha256').update(codeVerifier).digest());
  const expiresAt = nowSeconds() + TX_TTL_SECONDS;

  const authorizeUrl = new URL(config.authorizationEndpoint);
  authorizeUrl.searchParams.set('client_id', config.clientId);
  authorizeUrl.searchParams.set('response_type', 'code');
  authorizeUrl.searchParams.set('redirect_uri', config.redirectUri);
  authorizeUrl.searchParams.set('scope', config.scopes);
  authorizeUrl.searchParams.set('state', state);
  authorizeUrl.searchParams.set('nonce', nonce);
  authorizeUrl.searchParams.set('code_challenge', codeChallenge);
  authorizeUrl.searchParams.set('code_challenge_method', 'S256');
  if (config.prompt) {
    authorizeUrl.searchParams.set('prompt', config.prompt);
  }

  const txCookie = await sealCookie(
    TX_COOKIE_NAME,
    { state, nonce, codeVerifier, returnTo, expiresAt },
    config.sessionSecret,
    { maxAge: TX_TTL_SECONDS }
  );
  const headers = noStoreHeaders({ Location: authorizeUrl.toString() });
  headers.append('Set-Cookie', txCookie);
  return new Response(null, { status: 302, headers });
}

export async function createCallbackResponse(request) {
  const config = await getOidcConfig(request);
  if (!config.enabled) {
    throw new AdminAuthError('Admin UI OIDC login is disabled.', 404);
  }

  const requestUrl = getRequestUrl(request);
  const error = requestUrl.searchParams.get('error');
  if (error) {
    throw new AdminAuthError(
      requestUrl.searchParams.get('error_description') || `OIDC provider returned ${error}.`,
      401,
      'oidc_provider_rejected_login',
      'The identity provider rejected the login request.'
    );
  }

  const code = requestUrl.searchParams.get('code');
  const state = requestUrl.searchParams.get('state');
  if (!code || !state) {
    throw new AdminAuthError(
      'OIDC callback requires code and state.',
      400,
      'oidc_callback_invalid',
      'OIDC callback was rejected.'
    );
  }

  const tx = await readSealedCookie(request, TX_COOKIE_NAME, config.sessionSecret);
  if (!tx || tx.expiresAt <= nowSeconds() || tx.state !== state || !tx.codeVerifier) {
    throw new AdminAuthError(
      'OIDC login state is invalid or expired.',
      401,
      'oidc_state_invalid',
      'OIDC callback was rejected.'
    );
  }

  const tokenSet = await exchangeCodeForToken(config, code, tx.codeVerifier);
  const tokenType = tokenSet.token_type || 'Bearer';
  if (tokenType.toLowerCase() !== 'bearer') {
    throw new AdminAuthError('OIDC token endpoint did not return a bearer token.', 502);
  }
  if (!tokenSet.access_token) {
    throw new AdminAuthError('OIDC token endpoint did not return an access token.', 502);
  }
  if (!tokenSet.id_token) {
    throw new AdminAuthError('OIDC token endpoint did not return an id token.', 502);
  }
  await verifyIdToken(tokenSet.id_token, tx.nonce, config);

  const tokenExpiresIn = Number(tokenSet.expires_in);
  const expiresIn = Number.isFinite(tokenExpiresIn) && tokenExpiresIn > 0
    ? Math.floor(tokenExpiresIn)
    : config.sessionTtlSeconds;
  const sessionMaxAge = Math.max(1, Math.min(config.sessionTtlSeconds, expiresIn));
  const expiresAt = nowSeconds() + sessionMaxAge;
  const sessionCookie = await sealCookie(
    config.sessionCookieName,
    {
      accessToken: tokenSet.access_token,
      scope: tokenSet.scope || config.scopes,
      expiresAt,
    },
    config.sessionSecret,
    { maxAge: sessionMaxAge }
  );

  const headers = noStoreHeaders({ Location: sanitizeReturnTo(tx.returnTo) });
  headers.append('Set-Cookie', sessionCookie);
  headers.append('Set-Cookie', clearCookie(TX_COOKIE_NAME));
  return new Response(null, { status: 302, headers });
}

export async function createLogoutResponse(request) {
  enforceBrowserMutationProtection(request);
  const config = getBaseAuthConfig(request);
  let requestedReturnTo = null;
  try {
    const form = await request.formData();
    requestedReturnTo = form.get('return_to');
  } catch (_) {
    throw new AdminAuthError(
      'Logout requires a form-encoded request body.',
      400,
      'logout_body_invalid',
      'Logout request was rejected.'
    );
  }
  const returnTo = sanitizeReturnTo(requestedReturnTo);
  const headers = noStoreHeaders({ Location: returnTo });
  headers.append('Set-Cookie', clearCookie(config.sessionCookieName));
  headers.append('Set-Cookie', clearCookie(TX_COOKIE_NAME));
  return new Response(null, { status: 302, headers });
}

export async function createSessionStatusResponse(request) {
  if (!isAdminOidcEnabled()) {
    return Response.json(
      { enabled: false, authenticated: false },
      { headers: noStoreHeaders() }
    );
  }
  const session = await readAdminSession(request);
  return Response.json(
    {
      enabled: true,
      authenticated: Boolean(session),
      expires_at: session ? new Date(session.expiresAt * 1000).toISOString() : null,
    },
    { headers: noStoreHeaders() }
  );
}

export async function readAdminSession(request) {
  if (!isAdminOidcEnabled()) {
    return null;
  }

  const config = getBaseAuthConfig(request);
  const session = await readSealedCookie(request, config.sessionCookieName, config.sessionSecret);
  if (!session || !session.accessToken || session.expiresAt <= nowSeconds()) {
    return null;
  }
  return session;
}

export function buildLoginUrl(request) {
  const url = new URL('/api/auth/login', publicOriginFor(request));
  url.searchParams.set('return_to', getReturnToFromRequest(request));
  return url.toString();
}

export function sanitizeReturnTo(value, fallback = DEFAULT_RETURN_TO) {
  if (!value || typeof value !== 'string') {
    return fallback;
  }
  if (
    !value.startsWith('/') ||
    value.startsWith('//') ||
    value.startsWith('/api/auth') ||
    value.includes('\\') ||
    /[\u0000-\u001f\u007f]/.test(value)
  ) {
    return fallback;
  }
  return value;
}

function getBaseAuthConfig(request) {
  const sessionSecret = (
    process.env.ADMIN_UI_SESSION_SECRET ||
    process.env.ADMIN_UI_SESSION_COOKIE_SECRET ||
    ''
  ).trim();
  const sessionCookieName = (
    process.env.ADMIN_UI_SESSION_COOKIE_NAME ||
    'imposbro_admin_session'
  ).trim();
  const sessionTtlSeconds = parsePositiveInt(
    process.env.ADMIN_UI_SESSION_TTL_SECONDS,
    DEFAULT_SESSION_TTL_SECONDS
  );

  if (isAdminOidcEnabled() && sessionSecret.length < 32) {
    throw new AdminAuthError(
      'ADMIN_UI_SESSION_SECRET must be at least 32 characters when Admin UI OIDC is enabled.',
      500
    );
  }

  const publicOrigin = publicOriginFor(request, {
    requireConfigured: isAdminOidcEnabled() && isAdminUiProductionLike(),
  });

  return {
    enabled: isAdminOidcEnabled(),
    publicOrigin,
    sessionCookieName,
    sessionSecret,
    sessionTtlSeconds,
  };
}

async function getOidcConfig(request) {
  const baseConfig = getBaseAuthConfig(request);
  if (!baseConfig.enabled) {
    return baseConfig;
  }

  const clientId = (process.env.ADMIN_UI_OIDC_CLIENT_ID || '').trim();
  const clientSecret = (process.env.ADMIN_UI_OIDC_CLIENT_SECRET || '').trim();
  const issuer = (process.env.ADMIN_UI_OIDC_ISSUER || '').trim();
  const configuredAuthorizationEndpoint = (
    process.env.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT || ''
  ).trim();
  const configuredTokenEndpoint = (process.env.ADMIN_UI_OIDC_TOKEN_ENDPOINT || '').trim();
  const configuredJwksUri = (process.env.ADMIN_UI_OIDC_JWKS_URL || '').trim();
  const scopes = (process.env.ADMIN_UI_OIDC_SCOPES || DEFAULT_SCOPES).trim();
  const redirectUri = (
    process.env.ADMIN_UI_OIDC_REDIRECT_URI ||
    `${baseConfig.publicOrigin}/api/auth/callback`
  ).trim();
  const prompt = (process.env.ADMIN_UI_OIDC_PROMPT || '').trim();
  const fetchTimeoutMs = parseBoundedInt(
    process.env.ADMIN_UI_OIDC_FETCH_TIMEOUT_MS,
    DEFAULT_OIDC_FETCH_TIMEOUT_MS,
    MIN_OIDC_FETCH_TIMEOUT_MS,
    MAX_OIDC_FETCH_TIMEOUT_MS,
    'ADMIN_UI_OIDC_FETCH_TIMEOUT_MS'
  );

  if (!clientId) {
    throw new AdminAuthError('ADMIN_UI_OIDC_CLIENT_ID is required when Admin UI OIDC is enabled.', 500);
  }
  if (!issuer) {
    throw new AdminAuthError(
      'ADMIN_UI_OIDC_ISSUER is required when Admin UI OIDC is enabled.',
      500,
      'oidc_configuration_invalid'
    );
  }
  if (!scopes.split(/\s+/).includes('openid')) {
    throw new AdminAuthError('ADMIN_UI_OIDC_SCOPES must include openid.', 500);
  }

  const issuerUrl = validateOidcUrl(issuer, 'ADMIN_UI_OIDC_ISSUER', {
    allowPath: true,
    allowQuery: false,
  });
  const allowedEndpointOrigins = oidcAllowedEndpointOrigins(issuerUrl);
  validateRedirectUri(redirectUri, baseConfig.publicOrigin);

  let authorizationEndpoint = configuredAuthorizationEndpoint;
  let tokenEndpoint = configuredTokenEndpoint;
  let jwksUri = configuredJwksUri;
  if (!authorizationEndpoint || !tokenEndpoint || !jwksUri) {
    const metadata = await fetchOidcMetadata(issuer, fetchTimeoutMs);
    authorizationEndpoint = authorizationEndpoint || metadata.authorization_endpoint;
    tokenEndpoint = tokenEndpoint || metadata.token_endpoint;
    jwksUri = jwksUri || metadata.jwks_uri;
  }

  if (!authorizationEndpoint || !tokenEndpoint) {
    throw new AdminAuthError('OIDC provider metadata is missing authorization or token endpoints.', 502);
  }
  if (!jwksUri) {
    throw new AdminAuthError('OIDC provider metadata is missing jwks_uri.', 502);
  }

  authorizationEndpoint = validateOidcEndpoint(
    authorizationEndpoint,
    'OIDC authorization endpoint',
    allowedEndpointOrigins
  );
  tokenEndpoint = validateOidcEndpoint(
    tokenEndpoint,
    'OIDC token endpoint',
    allowedEndpointOrigins
  );
  jwksUri = validateOidcEndpoint(jwksUri, 'OIDC JWKS endpoint', allowedEndpointOrigins);

  return {
    ...baseConfig,
    authorizationEndpoint,
    tokenEndpoint,
    jwksUri,
    clientId,
    clientSecret,
    issuer,
    scopes,
    redirectUri,
    prompt,
    fetchTimeoutMs,
  };
}

async function fetchOidcMetadata(issuer, fetchTimeoutMs) {
  const discoveryUrl = buildDiscoveryUrl(issuer);
  let response;
  try {
    response = await fetch(discoveryUrl, {
      cache: 'no-store',
      redirect: 'error',
      signal: AbortSignal.timeout(fetchTimeoutMs),
    });
  } catch (_) {
    throw new AdminAuthError(
      'OIDC discovery request failed.',
      502,
      'oidc_provider_unavailable'
    );
  }
  if (!response.ok) {
    throw new AdminAuthError(
      `OIDC discovery failed with HTTP ${response.status}.`,
      502,
      'oidc_provider_unavailable'
    );
  }
  let metadata;
  try {
    metadata = await response.json();
  } catch (_) {
    throw new AdminAuthError(
      'OIDC discovery returned invalid JSON.',
      502,
      'oidc_provider_invalid_metadata'
    );
  }
  if (!metadata || typeof metadata !== 'object' || metadata.issuer !== issuer) {
    throw new AdminAuthError(
      'OIDC discovery issuer does not match ADMIN_UI_OIDC_ISSUER.',
      502,
      'oidc_provider_invalid_metadata'
    );
  }
  return metadata;
}

function buildDiscoveryUrl(issuer) {
  const url = new URL(issuer);
  const normalizedPath = url.pathname.replace(/\/$/, '');
  url.pathname = `${normalizedPath}/.well-known/openid-configuration`;
  return url.toString();
}

async function exchangeCodeForToken(config, code, codeVerifier) {
  const body = new URLSearchParams({
    grant_type: 'authorization_code',
    code,
    redirect_uri: config.redirectUri,
    client_id: config.clientId,
    code_verifier: codeVerifier,
  });
  if (config.clientSecret) {
    body.set('client_secret', config.clientSecret);
  }

  let response;
  try {
    response = await fetch(config.tokenEndpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body,
      cache: 'no-store',
      redirect: 'error',
      signal: AbortSignal.timeout(config.fetchTimeoutMs),
    });
  } catch (_) {
    throw new AdminAuthError(
      'OIDC token exchange request failed.',
      502,
      'oidc_provider_unavailable'
    );
  }
  if (!response.ok) {
    throw new AdminAuthError(
      `OIDC token exchange failed with HTTP ${response.status}.`,
      502,
      'oidc_provider_unavailable'
    );
  }
  try {
    return await response.json();
  } catch (_) {
    throw new AdminAuthError(
      'OIDC token exchange returned invalid JSON.',
      502,
      'oidc_provider_invalid_response'
    );
  }
}

async function verifyIdToken(idToken, expectedNonce, config) {
  let claims;
  try {
    const verified = await jwtVerify(
      idToken,
      remoteJwksFor(config.jwksUri, config.fetchTimeoutMs),
      {
        audience: config.clientId,
        clockTolerance: ID_TOKEN_CLOCK_SKEW_SECONDS,
        issuer: config.issuer,
      }
    );
    claims = verified.payload;
  } catch (err) {
    throw new AdminAuthError('OIDC id token signature or claims are invalid.', 401);
  }

  if (!expectedNonce || claims.nonce !== expectedNonce) {
    throw new AdminAuthError('OIDC id token nonce is invalid.', 401);
  }

  const audiences = Array.isArray(claims.aud)
    ? claims.aud.map((audience) => String(audience))
    : [String(claims.aud || '')];
  if (!audiences.includes(config.clientId)) {
    throw new AdminAuthError('OIDC id token audience is invalid.', 401);
  }

  if (config.issuer && claims.iss !== config.issuer) {
    throw new AdminAuthError('OIDC id token issuer is invalid.', 401);
  }

  const now = nowSeconds();
  const expiresAt = Number(claims.exp);
  if (!Number.isFinite(expiresAt) || expiresAt + ID_TOKEN_CLOCK_SKEW_SECONDS <= now) {
    throw new AdminAuthError('OIDC id token is expired.', 401);
  }

  const issuedAt = Number(claims.iat);
  if (!Number.isFinite(issuedAt) || issuedAt - ID_TOKEN_CLOCK_SKEW_SECONDS > now) {
    throw new AdminAuthError('OIDC id token issued-at claim is invalid.', 401);
  }
}

function remoteJwksFor(jwksUri, fetchTimeoutMs) {
  const cacheKey = `${jwksUri}|${fetchTimeoutMs}`;
  if (!JWKS_CACHE.has(cacheKey)) {
    JWKS_CACHE.set(
      cacheKey,
      createRemoteJWKSet(new URL(jwksUri), {
        timeoutDuration: fetchTimeoutMs,
        [customFetch]: (url, options) => fetch(url, { ...options, redirect: 'error' }),
      })
    );
  }
  return JWKS_CACHE.get(cacheKey);
}

async function sealCookie(name, payload, secret, options = {}) {
  const value = await sealPayload(payload, secret);
  return serializeCookie(name, value, options);
}

async function readSealedCookie(request, name, secret) {
  const value = getCookie(request, name);
  if (!value) return null;
  return openPayload(value, secret);
}

async function sealPayload(payload, secret) {
  const iv = crypto.randomBytes(12);
  const key = deriveKey(secret);
  const cipher = crypto.createCipheriv('aes-256-gcm', key, iv);
  cipher.setAAD(SEAL_AAD);
  const plaintext = Buffer.from(JSON.stringify(payload), 'utf8');
  const encrypted = Buffer.concat([cipher.update(plaintext), cipher.final()]);
  const tag = cipher.getAuthTag();
  return ['v1', base64Url(iv), base64Url(encrypted), base64Url(tag)].join('.');
}

function openPayload(value, secret) {
  try {
    const [version, ivValue, encryptedValue, tagValue] = String(value).split('.');
    if (version !== 'v1' || !ivValue || !encryptedValue || !tagValue) {
      return null;
    }
    const decipher = crypto.createDecipheriv('aes-256-gcm', deriveKey(secret), fromBase64Url(ivValue));
    decipher.setAAD(SEAL_AAD);
    decipher.setAuthTag(fromBase64Url(tagValue));
    const plaintext = Buffer.concat([
      decipher.update(fromBase64Url(encryptedValue)),
      decipher.final(),
    ]);
    return JSON.parse(plaintext.toString('utf8'));
  } catch (_) {
    return null;
  }
}

function deriveKey(secret) {
  return crypto.createHash('sha256').update(secret).digest();
}

function serializeCookie(name, value, { maxAge, path = '/', sameSite = 'Lax' } = {}) {
  const parts = [`${name}=${value}`, `Path=${path}`, `SameSite=${sameSite}`, 'HttpOnly'];
  if (maxAge !== undefined) {
    parts.push(`Max-Age=${Math.max(0, Math.floor(maxAge))}`);
  }
  if (isAdminUiProductionLike()) {
    parts.push('Secure');
  }
  return parts.join('; ');
}

function clearCookie(name) {
  return serializeCookie(name, '', { maxAge: 0 });
}

function getCookie(request, name) {
  const cookieHeader = request.headers?.get?.('cookie') || '';
  return cookieHeader
    .split(';')
    .map((part) => part.trim())
    .filter(Boolean)
    .map((part) => {
      const separator = part.indexOf('=');
      return separator === -1 ? [part, ''] : [part.slice(0, separator), part.slice(separator + 1)];
    })
    .find(([cookieName]) => cookieName === name)?.[1] || '';
}

function getReturnToFromRequest(request) {
  const publicOrigin = publicOriginFor(request);
  const referer = request.headers?.get?.('referer');
  if (referer) {
    try {
      const refererUrl = new URL(referer);
      if (refererUrl.origin === publicOrigin) {
        return sanitizeReturnTo(`${refererUrl.pathname}${refererUrl.search}`);
      }
    } catch (_) {}
  }
  return DEFAULT_RETURN_TO;
}

function getRequestUrl(request) {
  if (request.nextUrl) {
    return request.nextUrl;
  }
  return new URL(request.url || 'http://admin.local/');
}

function requiresBrowserMutationProtection() {
  return isAdminOidcEnabled() || isAdminUiProductionLike();
}

function publicOriginFor(request, { requireConfigured = false } = {}) {
  const configuredOrigin = String(process.env.ADMIN_UI_PUBLIC_ORIGIN || '').trim();
  if (configuredOrigin) {
    const publicOrigin = parseStrictOrigin(configuredOrigin, 'ADMIN_UI_PUBLIC_ORIGIN');
    if (isAdminUiProductionLike() && !publicOrigin.startsWith('https://')) {
      throw new AdminAuthError(
        'ADMIN_UI_PUBLIC_ORIGIN must use HTTPS in production or enterprise mode.',
        500,
        'admin_ui_origin_configuration_invalid'
      );
    }
    return publicOrigin;
  }
  if (requireConfigured) {
    throw new AdminAuthError(
      'ADMIN_UI_PUBLIC_ORIGIN is required in production or enterprise mode.',
      500,
      'admin_ui_origin_configuration_invalid'
    );
  }
  return getRequestUrl(request).origin;
}

function parseStrictOrigin(value, label) {
  let url;
  try {
    url = new URL(value);
  } catch (_) {
    throw new AdminAuthError(
      `${label} must be an absolute HTTP(S) origin.`,
      500,
      'admin_ui_origin_configuration_invalid'
    );
  }
  if (
    !['http:', 'https:'].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.pathname !== '/' ||
    url.search ||
    url.hash ||
    value !== url.origin
  ) {
    throw new AdminAuthError(
      `${label} must contain only an exact HTTP(S) origin without credentials, path, query, or fragment.`,
      500,
      'admin_ui_origin_configuration_invalid'
    );
  }
  return url.origin;
}

function validateOidcUrl(value, label, { allowPath = true, allowQuery = true } = {}) {
  let url;
  try {
    url = new URL(value);
  } catch (_) {
    throw new AdminAuthError(
      `${label} must be an absolute URL.`,
      500,
      'oidc_configuration_invalid'
    );
  }
  const insecureLocalhostAllowed = (
    !isAdminUiProductionLike() &&
    String(process.env.ADMIN_UI_OIDC_ALLOW_INSECURE_LOCALHOST || '').trim().toLowerCase() === 'true' &&
    ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)
  );
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && insecureLocalhostAllowed)) {
    throw new AdminAuthError(
      `${label} must use HTTPS.`,
      500,
      'oidc_configuration_invalid'
    );
  }
  if (
    url.username ||
    url.password ||
    url.hash ||
    (!allowPath && url.pathname !== '/') ||
    (!allowQuery && url.search)
  ) {
    throw new AdminAuthError(
      `${label} contains URL components that are not allowed.`,
      500,
      'oidc_configuration_invalid'
    );
  }
  return url;
}

function oidcAllowedEndpointOrigins(issuerUrl) {
  const configured = String(process.env.ADMIN_UI_OIDC_ALLOWED_ENDPOINT_ORIGINS || '').trim();
  const origins = new Set([issuerUrl.origin]);
  if (!configured) {
    return origins;
  }
  for (const value of configured.split(',')) {
    const candidate = value.trim();
    if (candidate) {
      origins.add(parseStrictOrigin(candidate, 'ADMIN_UI_OIDC_ALLOWED_ENDPOINT_ORIGINS'));
    }
  }
  return origins;
}

function validateOidcEndpoint(value, label, allowedOrigins) {
  const url = validateOidcUrl(value, label);
  if (!allowedOrigins.has(url.origin)) {
    throw new AdminAuthError(
      `${label} origin is not bound to the configured issuer or explicit endpoint-origin allowlist.`,
      500,
      'oidc_configuration_invalid'
    );
  }
  return url.toString();
}

function validateRedirectUri(value, publicOrigin) {
  let redirectUrl;
  try {
    redirectUrl = new URL(value);
  } catch (_) {
    throw new AdminAuthError(
      'ADMIN_UI_OIDC_REDIRECT_URI must be an absolute URL.',
      500,
      'oidc_configuration_invalid'
    );
  }
  if (
    redirectUrl.origin !== publicOrigin ||
    redirectUrl.pathname !== '/api/auth/callback' ||
    redirectUrl.search ||
    redirectUrl.hash ||
    redirectUrl.username ||
    redirectUrl.password
  ) {
    throw new AdminAuthError(
      'ADMIN_UI_OIDC_REDIRECT_URI must be the Admin UI public origin plus /api/auth/callback.',
      500,
      'oidc_configuration_invalid'
    );
  }
}

function parsePositiveInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function parseBoundedInt(value, fallback, minimum, maximum, label) {
  if (value === undefined || value === null || String(value).trim() === '') {
    return fallback;
  }
  if (!/^\d+$/.test(String(value).trim())) {
    throw new AdminAuthError(
      `${label} must be an integer between ${minimum} and ${maximum}.`,
      500,
      'oidc_configuration_invalid'
    );
  }
  const parsed = Number.parseInt(value, 10);
  if (parsed < minimum || parsed > maximum) {
    throw new AdminAuthError(
      `${label} must be between ${minimum} and ${maximum}.`,
      500,
      'oidc_configuration_invalid'
    );
  }
  return parsed;
}

function nowSeconds() {
  return Math.floor(Date.now() / 1000);
}

function randomBase64Url(bytes) {
  return base64Url(crypto.randomBytes(bytes));
}

function base64Url(value) {
  return Buffer.from(value)
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/g, '');
}

function fromBase64Url(value) {
  const padded = value.padEnd(value.length + ((4 - (value.length % 4)) % 4), '=');
  return Buffer.from(padded.replace(/-/g, '+').replace(/_/g, '/'), 'base64');
}
