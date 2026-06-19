/**
 * Proxy route: forwards /api/* to the Query API backend.
 * Ensures the Admin UI works when backend runs on a different host/port (e.g. Docker).
 */

import {
  buildLoginUrl,
  isAdminOidcEnabled,
  readAdminSession,
} from '../../lib/adminAuth.js';

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
    return Response.json({ detail: 'Admin UI auth route not found.' }, { status: 404 });
  }

  const search = request.nextUrl.search || '';
  const backendUrl = process.env.INTERNAL_QUERY_API_URL || 'http://localhost:8000';
  const adminApiKey = process.env.INTERNAL_QUERY_API_ADMIN_API_KEY || process.env.ADMIN_API_KEY || '';
  const dataApiKey = process.env.INTERNAL_QUERY_API_DATA_API_KEY || process.env.DATA_API_KEY || '';
  const url = `${backendUrl.replace(/\/$/, '')}/${path}${search}`;

  const headers = new Headers();
  request.headers.forEach((value, key) => {
    if (
      key.toLowerCase() !== 'host' &&
      key.toLowerCase() !== 'connection' &&
      key.toLowerCase() !== 'next-url'
    ) {
      headers.set(key, value);
    }
  });

  const needsAdminKey = path.startsWith('admin/');
  const needsDataKey = path.startsWith('search/') || path.startsWith('ingest/');
  let callerProvidedCredentials = headers.has('x-api-key') || headers.has('authorization');
  const trustedIdentity = hasTrustedUpstreamIdentity(request);
  const canInjectCredentials = canInjectServerCredentials(request, trustedIdentity);

  if (!callerProvidedCredentials) {
    try {
      const session = await readAdminSession(request);
      if (session?.accessToken) {
        headers.set('Authorization', `Bearer ${session.accessToken}`);
        callerProvidedCredentials = true;
      }
    } catch (err) {
      return Response.json(
        { detail: err.message || 'Admin UI OIDC session is misconfigured.' },
        { status: err.status || 500 }
      );
    }
  }

  if (
    !callerProvidedCredentials &&
    isAdminOidcEnabled() &&
    (needsAdminKey || needsDataKey) &&
    !trustedIdentity
  ) {
    return Response.json(
      {
        detail: 'Admin UI login required.',
        login_url: buildLoginUrl(request),
      },
      { status: 401 }
    );
  }

  if (
    !callerProvidedCredentials &&
    ((adminApiKey && needsAdminKey) || (dataApiKey && needsDataKey)) &&
    !canInjectCredentials
  ) {
    return Response.json(
      { detail: 'Admin UI proxy credential injection requires a trusted upstream identity.' },
      { status: 401 }
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
        headers: { 'Content-Type': res.headers.get('Content-Type') || 'text/plain' },
      });
    }

    return Response.json(json, {
      status: res.status,
      statusText: res.statusText,
    });
  } catch (err) {
    return Response.json(
      { detail: err.message || 'Backend unreachable' },
      { status: 502 }
    );
  }
}

function hasTrustedUpstreamIdentity(request) {
  const trustedHeader = process.env.ADMIN_UI_PROXY_TRUSTED_HEADER || '';
  const trustedValue = process.env.ADMIN_UI_PROXY_TRUSTED_VALUE || '';
  if (!trustedHeader) {
    return false;
  }
  const actual = request.headers.get(trustedHeader);
  if (!actual) return false;
  return trustedValue ? actual === trustedValue : true;
}

function canInjectServerCredentials(request, trustedIdentity = hasTrustedUpstreamIdentity(request)) {
  const trustedHeader = process.env.ADMIN_UI_PROXY_TRUSTED_HEADER || '';
  if (!trustedHeader) {
    return process.env.NODE_ENV !== 'production';
  }
  return trustedIdentity;
}
