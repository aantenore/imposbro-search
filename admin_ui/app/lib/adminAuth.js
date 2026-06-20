import crypto from 'node:crypto';
import { createRemoteJWKSet, jwtVerify } from 'jose';

const DEFAULT_SCOPES = 'openid profile email imposbro:admin imposbro:data';
const DEFAULT_RETURN_TO = '/dashboard';
const DEFAULT_SESSION_TTL_SECONDS = 3600;
const TX_COOKIE_NAME = 'imposbro_admin_oidc_tx';
const TX_TTL_SECONDS = 600;
const ID_TOKEN_CLOCK_SKEW_SECONDS = 60;
const SEAL_AAD = Buffer.from('imposbro-admin-ui-session-v1');
const JWKS_CACHE = new Map();

export class AdminAuthError extends Error {
  constructor(message, status = 400) {
    super(message);
    this.name = 'AdminAuthError';
    this.status = status;
  }
}

export function isAdminOidcEnabled() {
  return String(process.env.ADMIN_UI_OIDC_ENABLED || '').toLowerCase() === 'true';
}

export function authErrorResponse(error) {
  const status = error instanceof AdminAuthError ? error.status : 500;
  return Response.json(
    { detail: error.message || 'Admin UI authentication failed.' },
    { status }
  );
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
  const headers = new Headers({ Location: authorizeUrl.toString() });
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
      401
    );
  }

  const code = requestUrl.searchParams.get('code');
  const state = requestUrl.searchParams.get('state');
  if (!code || !state) {
    throw new AdminAuthError('OIDC callback requires code and state.', 400);
  }

  const tx = await readSealedCookie(request, TX_COOKIE_NAME, config.sessionSecret);
  if (!tx || tx.expiresAt <= nowSeconds() || tx.state !== state || !tx.codeVerifier) {
    throw new AdminAuthError('OIDC login state is invalid or expired.', 401);
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

  const expiresIn = Number(tokenSet.expires_in || config.sessionTtlSeconds);
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

  const headers = new Headers({ Location: sanitizeReturnTo(tx.returnTo) });
  headers.append('Set-Cookie', sessionCookie);
  headers.append('Set-Cookie', clearCookie(TX_COOKIE_NAME));
  return new Response(null, { status: 302, headers });
}

export async function createLogoutResponse(request) {
  const config = getBaseAuthConfig(request);
  const returnTo = sanitizeReturnTo(getRequestUrl(request).searchParams.get('return_to'));
  const headers = new Headers({ Location: returnTo });
  headers.append('Set-Cookie', clearCookie(config.sessionCookieName));
  headers.append('Set-Cookie', clearCookie(TX_COOKIE_NAME));
  return new Response(null, { status: 302, headers });
}

export async function createSessionStatusResponse(request) {
  if (!isAdminOidcEnabled()) {
    return Response.json({ enabled: false, authenticated: false });
  }
  const session = await readAdminSession(request);
  return Response.json({
    enabled: true,
    authenticated: Boolean(session),
    expires_at: session ? new Date(session.expiresAt * 1000).toISOString() : null,
  });
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
  const url = new URL('/api/auth/login', getRequestUrl(request).origin);
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

  return {
    enabled: isAdminOidcEnabled(),
    requestOrigin: getRequestUrl(request).origin,
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
    `${baseConfig.requestOrigin}/api/auth/callback`
  ).trim();
  const prompt = (process.env.ADMIN_UI_OIDC_PROMPT || '').trim();

  if (!clientId) {
    throw new AdminAuthError('ADMIN_UI_OIDC_CLIENT_ID is required when Admin UI OIDC is enabled.', 500);
  }
  if (!scopes.split(/\s+/).includes('openid')) {
    throw new AdminAuthError('ADMIN_UI_OIDC_SCOPES must include openid.', 500);
  }

  let authorizationEndpoint = configuredAuthorizationEndpoint;
  let tokenEndpoint = configuredTokenEndpoint;
  let jwksUri = configuredJwksUri;
  if (!authorizationEndpoint || !tokenEndpoint || !jwksUri) {
    if (!issuer) {
      throw new AdminAuthError(
        'ADMIN_UI_OIDC_ISSUER or explicit Admin UI OIDC endpoints plus ADMIN_UI_OIDC_JWKS_URL are required.',
        500
      );
    }
    const metadata = await fetchOidcMetadata(issuer);
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
  };
}

async function fetchOidcMetadata(issuer) {
  const discoveryUrl = buildDiscoveryUrl(issuer);
  const response = await fetch(discoveryUrl, { cache: 'no-store' });
  if (!response.ok) {
    throw new AdminAuthError(`OIDC discovery failed with HTTP ${response.status}.`, 502);
  }
  const metadata = await response.json();
  if (metadata.issuer && metadata.issuer !== issuer) {
    throw new AdminAuthError('OIDC discovery issuer does not match ADMIN_UI_OIDC_ISSUER.', 502);
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

  const response = await fetch(config.tokenEndpoint, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body,
    cache: 'no-store',
  });
  if (!response.ok) {
    throw new AdminAuthError(`OIDC token exchange failed with HTTP ${response.status}.`, 502);
  }
  return response.json();
}

async function verifyIdToken(idToken, expectedNonce, config) {
  let claims;
  try {
    const verified = await jwtVerify(idToken, remoteJwksFor(config.jwksUri), {
      audience: config.clientId,
      clockTolerance: ID_TOKEN_CLOCK_SKEW_SECONDS,
      issuer: config.issuer || undefined,
    });
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

function remoteJwksFor(jwksUri) {
  if (!JWKS_CACHE.has(jwksUri)) {
    JWKS_CACHE.set(jwksUri, createRemoteJWKSet(new URL(jwksUri)));
  }
  return JWKS_CACHE.get(jwksUri);
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
  if (process.env.NODE_ENV === 'production') {
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
  const requestUrl = getRequestUrl(request);
  const referer = request.headers?.get?.('referer');
  if (referer) {
    try {
      const refererUrl = new URL(referer);
      if (refererUrl.origin === requestUrl.origin) {
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

function parsePositiveInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
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
