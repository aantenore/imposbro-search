/**
 * Proxy route: forwards /api/* to the Query API backend.
 * Ensures the Admin UI works when backend runs on a different host/port (e.g. Docker).
 */

import {
  authErrorResponse,
  buildLoginUrl,
  enforceBrowserMutationProtection,
  isAdminOidcEnabled,
  isAdminUiProductionLike,
  noStoreHeaders,
  readAdminSession,
} from '../../lib/adminAuth.js';

const PROXY_HEADER_BLOCKLIST = new Set([
  'connection',
  'content-length',
  'cookie',
  'host',
  'keep-alive',
  'next-url',
  'proxy-authenticate',
  'proxy-authorization',
  'te',
  'trailer',
  'transfer-encoding',
  'upgrade',
]);

const RESPONSE_HEADER_ALLOWLIST = new Set([
  'etag',
  'retry-after',
  'x-api-version',
  'x-control-plane-revision',
  'x-request-id',
  'x-pagination-info',
  'x-pagination-warning',
  'x-ratelimit-limit',
  'x-ratelimit-remaining',
  'x-ratelimit-reset',
]);

const FORBIDDEN_TRUSTED_IDENTITY_HEADERS = new Set([
  ...PROXY_HEADER_BLOCKLIST,
  'authorization',
  'origin',
  'sec-fetch-site',
  'x-api-key',
]);

export async function GET(request, { params }) {
  return proxyRequest(request, params);
}

export async function POST(request, { params }) {
  return proxyRequest(request, params);
}

export async function PUT(request, { params }) {
  return proxyRequest(request, params);
}

export async function PATCH(request, { params }) {
  return proxyRequest(request, params);
}

export async function DELETE(request, { params }) {
  return proxyRequest(request, params);
}

async function proxyRequest(request, params = {}) {
  const resolvedParams = await params;
  const pathParam = resolvedParams?.path;
  const path = Array.isArray(pathParam) ? pathParam.join('/') : (pathParam || '');
  if (path.startsWith('auth/')) {
    return proxyJson(
      { detail: 'Admin UI auth route not found.', code: 'admin_auth_route_not_found' },
      404
    );
  }

  try {
    enforceBrowserMutationProtection(request);
  } catch (error) {
    return authErrorResponse(error);
  }

  const search = request.nextUrl.search || '';
  const backendUrl = process.env.INTERNAL_QUERY_API_URL || 'http://localhost:8000';
  let backendApiPrefix;
  try {
    backendApiPrefix = internalApiPrefix();
  } catch (_) {
    return proxyJson(
      { detail: 'Admin UI proxy is unavailable.', code: 'proxy_configuration_invalid' },
      500
    );
  }
  const adminApiKey = process.env.INTERNAL_QUERY_API_ADMIN_API_KEY || process.env.ADMIN_API_KEY || '';
  const dataApiKey = process.env.INTERNAL_QUERY_API_DATA_API_KEY || process.env.DATA_API_KEY || '';
  const url = `${backendUrl.replace(/\/$/, '')}${backendApiPrefix}/${path}${search}`;

  const headers = new Headers();
  const headerBlocklist = proxyHeaderBlocklistFor(request);
  request.headers.forEach((value, key) => {
    if (!headerBlocklist.has(key.toLowerCase())) {
      headers.set(key, value);
    }
  });

  const needsAdminKey = path.startsWith('admin/');
  const needsDataKey = (
    path.startsWith('search/') ||
    path.startsWith('ingest/') ||
    path.startsWith('documents/')
  );
  let callerProvidedCredentials = headers.has('x-api-key') || headers.has('authorization');
  const canInjectCredentials = canInjectServerCredentials(request);

  if (!callerProvidedCredentials) {
    try {
      const session = await readAdminSession(request);
      if (session?.accessToken) {
        headers.set('Authorization', `Bearer ${session.accessToken}`);
        callerProvidedCredentials = true;
      }
    } catch (err) {
      return authErrorResponse(err);
    }
  }

  if (
    !callerProvidedCredentials &&
    isAdminOidcEnabled() &&
    (needsAdminKey || needsDataKey)
  ) {
    try {
      return proxyJson(
        {
          detail: 'Admin UI login required.',
          code: 'admin_login_required',
          login_url: buildLoginUrl(request),
        },
        401
      );
    } catch (error) {
      return authErrorResponse(error);
    }
  }

  if (
    !callerProvidedCredentials &&
    ((adminApiKey && needsAdminKey) || (dataApiKey && needsDataKey)) &&
    !canInjectCredentials
  ) {
    return proxyJson(
      {
        detail: 'Admin UI server credentials are unavailable for this request.',
        code: 'server_credential_injection_disabled',
      },
      401
    );
  }

  if (adminApiKey && needsAdminKey && !callerProvidedCredentials) {
    headers.set('X-API-Key', adminApiKey);
  }
  if (
    dataApiKey &&
    needsDataKey &&
    !callerProvidedCredentials
  ) {
    headers.set('X-API-Key', dataApiKey);
  }

  let body = null;
  if (['POST', 'PUT', 'PATCH'].includes(request.method)) {
    try {
      body = await request.text();
    } catch (_) {}
  }

  try {
    const res = await fetch(url, {
      method: request.method,
      headers,
      body,
      cache: 'no-store',
    });

    const data = await res.text();
    let json;
    try {
      json = JSON.parse(data);
    } catch {
      return new Response(data, {
        status: res.status,
        statusText: res.statusText,
        headers: responseHeadersFromBackend(res, 'text/plain'),
      });
    }

    return Response.json(json, {
      status: res.status,
      statusText: res.statusText,
      headers: responseHeadersFromBackend(res, 'application/json'),
    });
  } catch (err) {
    return proxyJson(
      { detail: 'Query API temporarily unavailable.', code: 'query_api_unavailable' },
      502
    );
  }
}

function proxyHeaderBlocklistFor(request) {
  const blocklist = new Set(PROXY_HEADER_BLOCKLIST);
  const connection = request.headers.get('connection') || '';
  for (const token of connection.split(',')) {
    const headerName = token.trim().toLowerCase();
    if (headerName) {
      blocklist.add(headerName);
    }
  }
  const trustedHeader = trustedIdentityHeaderName();
  if (trustedHeader) {
    blocklist.add(trustedHeader);
  }
  return blocklist;
}

function responseHeadersFromBackend(response, fallbackContentType) {
  const headers = noStoreHeaders({
    'Content-Type': response.headers.get('Content-Type') || fallbackContentType,
  });
  response.headers.forEach((value, key) => {
    const normalized = key.toLowerCase();
    if (RESPONSE_HEADER_ALLOWLIST.has(normalized)) {
      headers.set(key, value);
    }
  });
  return headers;
}

function hasTrustedUpstreamIdentity(request) {
  const trustedHeader = trustedIdentityHeaderName();
  const trustedValue = process.env.ADMIN_UI_PROXY_TRUSTED_VALUE || '';
  if (!trustedHeader) {
    return false;
  }
  if (!trustedValue) {
    return false;
  }
  let actual;
  try {
    actual = request.headers.get(trustedHeader);
  } catch (_) {
    return false;
  }
  if (!actual) return false;
  return actual === trustedValue;
}

function trustedIdentityHeaderName() {
  const value = String(process.env.ADMIN_UI_PROXY_TRUSTED_HEADER || '').trim().toLowerCase();
  if (!/^[a-z0-9][a-z0-9-]*$/.test(value) || FORBIDDEN_TRUSTED_IDENTITY_HEADERS.has(value)) {
    return '';
  }
  return value;
}

function canInjectServerCredentials(request) {
  const configuredMode = String(process.env.ADMIN_UI_SERVER_CREDENTIAL_MODE || '')
    .trim()
    .toLowerCase();
  const productionLike = isAdminUiProductionLike();
  const mode = configuredMode || (productionLike ? 'disabled' : 'development');

  if (mode === 'disabled') {
    return false;
  }
  if (mode === 'development') {
    return !productionLike;
  }
  if (mode === 'trusted-header-legacy') {
    const legacyEnabled = String(process.env.ADMIN_UI_ALLOW_LEGACY_TRUSTED_HEADER || '')
      .trim()
      .toLowerCase() === 'true';
    return legacyEnabled && hasTrustedUpstreamIdentity(request);
  }
  return false;
}

function proxyJson(payload, status, headers = {}) {
  return Response.json(payload, { status, headers: noStoreHeaders(headers) });
}

function internalApiPrefix() {
  const value = process.env.INTERNAL_QUERY_API_PREFIX || '/api/v1';
  if (!/^\/api\/v[1-9][0-9]*$/.test(value)) {
    throw new Error('INTERNAL_QUERY_API_PREFIX must match /api/v<major>');
  }
  return value;
}
