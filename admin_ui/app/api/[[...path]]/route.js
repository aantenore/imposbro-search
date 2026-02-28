/**
 * Proxy route: forwards /api/* to the Query API backend.
 * Ensures the Admin UI works when backend runs on a different host/port (e.g. Docker).
 */

const BACKEND_URL = process.env.INTERNAL_QUERY_API_URL || 'http://localhost:8000';

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

async function proxyRequest(request, { params }) {
  const pathParam = params?.path;
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
