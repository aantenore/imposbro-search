import assert from 'node:assert/strict';
import test from 'node:test';

process.env.INTERNAL_QUERY_API_URL = 'http://backend.internal';
process.env.INTERNAL_QUERY_API_ADMIN_API_KEY = 'admin-secret';
process.env.INTERNAL_QUERY_API_DATA_API_KEY = 'data-secret';

const routeModule = await import('../app/api/[[...path]]/route.js');

test('proxies catch-all route params to the backend path', async () => {
  const originalFetch = globalThis.fetch;
  let proxied;

  globalThis.fetch = async (url, options) => {
    proxied = {
      url,
      method: options.method,
      headers: Object.fromEntries(options.headers.entries()),
    };

    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const response = await routeModule.GET(
      {
        method: 'GET',
        nextUrl: new URL('http://admin.local/api/admin/stats?refresh=1'),
        headers: new Headers({
          accept: 'application/json',
          host: 'admin.local',
        }),
      },
      { params: Promise.resolve({ path: ['admin', 'stats'] }) }
    );

    assert.equal(proxied.url, 'http://backend.internal/admin/stats?refresh=1');
    assert.equal(proxied.method, 'GET');
    assert.equal(proxied.headers.host, undefined);
    assert.equal(proxied.headers['x-api-key'], 'admin-secret');
    assert.deepEqual(await response.json(), { ok: true });
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('injects data-plane key for search and ingest paths only when caller has no credentials', async () => {
  const originalFetch = globalThis.fetch;
  const proxied = [];

  globalThis.fetch = async (url, options) => {
    proxied.push({
      url,
      headers: Object.fromEntries(options.headers.entries()),
    });

    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    await routeModule.GET(
      {
        method: 'GET',
        nextUrl: new URL('http://admin.local/api/search/products?q=codex&query_by=name'),
        headers: new Headers(),
      },
      { params: Promise.resolve({ path: ['search', 'products'] }) }
    );

    await routeModule.POST(
      {
        method: 'POST',
        nextUrl: new URL('http://admin.local/api/ingest/products'),
        headers: new Headers({ authorization: 'Bearer caller-token' }),
        text: async () => '{"id":"doc-1"}',
      },
      { params: Promise.resolve({ path: ['ingest', 'products'] }) }
    );

    assert.equal(proxied[0].url, 'http://backend.internal/search/products?q=codex&query_by=name');
    assert.equal(proxied[0].headers['x-api-key'], 'data-secret');
    assert.equal(proxied[1].url, 'http://backend.internal/ingest/products');
    assert.equal(proxied[1].headers.authorization, 'Bearer caller-token');
    assert.equal(proxied[1].headers['x-api-key'], undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('production proxy requires trusted upstream identity before injecting server keys', async () => {
  const originalFetch = globalThis.fetch;
  const originalEnv = {
    INTERNAL_QUERY_API_URL: process.env.INTERNAL_QUERY_API_URL,
    INTERNAL_QUERY_API_ADMIN_API_KEY: process.env.INTERNAL_QUERY_API_ADMIN_API_KEY,
    ADMIN_UI_PROXY_TRUSTED_HEADER: process.env.ADMIN_UI_PROXY_TRUSTED_HEADER,
    ADMIN_UI_PROXY_TRUSTED_VALUE: process.env.ADMIN_UI_PROXY_TRUSTED_VALUE,
    NODE_ENV: process.env.NODE_ENV,
  };
  const proxied = [];

  process.env.INTERNAL_QUERY_API_URL = 'http://backend.internal';
  process.env.INTERNAL_QUERY_API_ADMIN_API_KEY = 'admin-secret';
  process.env.ADMIN_UI_PROXY_TRUSTED_HEADER = 'x-authenticated-user';
  process.env.ADMIN_UI_PROXY_TRUSTED_VALUE = 'operator';
  process.env.NODE_ENV = 'production';

  const productionRouteModule = await import(
    `../app/api/[[...path]]/route.js?trusted=${Date.now()}`
  );

  globalThis.fetch = async (url, options) => {
    proxied.push({
      url,
      headers: Object.fromEntries(options.headers.entries()),
    });

    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const denied = await productionRouteModule.GET(
      {
        method: 'GET',
        nextUrl: new URL('http://admin.local/api/admin/stats'),
        headers: new Headers(),
      },
      { params: Promise.resolve({ path: ['admin', 'stats'] }) }
    );
    assert.equal(denied.status, 401);
    assert.equal(proxied.length, 0);

    const allowed = await productionRouteModule.GET(
      {
        method: 'GET',
        nextUrl: new URL('http://admin.local/api/admin/stats'),
        headers: new Headers({ 'x-authenticated-user': 'operator' }),
      },
      { params: Promise.resolve({ path: ['admin', 'stats'] }) }
    );

    assert.equal(allowed.status, 200);
    assert.equal(proxied[0].url, 'http://backend.internal/admin/stats');
    assert.equal(proxied[0].headers['x-api-key'], 'admin-secret');
  } finally {
    globalThis.fetch = originalFetch;
    for (const [key, value] of Object.entries(originalEnv)) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  }
});
