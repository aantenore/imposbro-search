/**
 * Proxy route: forwards /api/* to the Query API backend.
 * Ensures the Admin UI works when backend runs on a different host/port (e.g. Docker).
 */

const BACKEND_URL = process.env.INTERNAL_QUERY_API_URL || 'http://localhost:8000';
const ADMIN_API_KEY = process.env.INTERNAL_QUERY_API_ADMIN_API_KEY || process.env.ADMIN_API_KEY || '';
const DATA_API_KEY = process.env.INTERNAL_QUERY_API_DATA_API_KEY || process.env.DATA_API_KEY || '';
const TRUSTED_HEADER = process.env.ADMIN_UI_PROXY_TRUSTED_HEADER || '';
const TRUSTED_VALUE = process.env.ADMIN_UI_PROXY_TRUSTED_VALUE || '';

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
  const search = request.nextUrl.search || '';
  const url = `${BACKEND_URL.replace(/\/$/, '')}/${path}${search}`;

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
  const callerProvidedCredentials = headers.has('x-api-key') || headers.has('authorization');
  const canInjectCredentials = canInjectServerCredentials(request);

  if (
    !callerProvidedCredentials &&
    ((ADMIN_API_KEY && needsAdminKey) || (DATA_API_KEY && needsDataKey)) &&
    !canInjectCredentials
  ) {
    return Response.json(
      { detail: 'Admin UI proxy credential injection requires a trusted upstream identity.' },
      { status: 401 }
    );
  }

  if (ADMIN_API_KEY && needsAdminKey && !callerProvidedCredentials) {
    headers.set('X-API-Key', ADMIN_API_KEY);
  }
  if (
    DATA_API_KEY &&
    needsDataKey &&
    !callerProvidedCredentials
  ) {
    headers.set('X-API-Key', DATA_API_KEY);
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

function canInjectServerCredentials(request) {
  if (!TRUSTED_HEADER) {
    return process.env.NODE_ENV !== 'production';
  }
  const actual = request.headers.get(TRUSTED_HEADER);
  if (!actual) return false;
  return TRUSTED_VALUE ? actual === TRUSTED_VALUE : true;
}
