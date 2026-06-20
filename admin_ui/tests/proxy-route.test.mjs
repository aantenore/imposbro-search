import assert from 'node:assert/strict';
import test from 'node:test';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';

process.env.INTERNAL_QUERY_API_URL = 'http://backend.internal';
process.env.INTERNAL_QUERY_API_ADMIN_API_KEY = 'admin-secret';
process.env.INTERNAL_QUERY_API_DATA_API_KEY = 'data-secret';

const routeModule = await import('../app/api/[[...path]]/route.js');
const loginRoute = await import('../app/api/auth/login/route.js');
const callbackRoute = await import('../app/api/auth/callback/route.js');

const AUTH_ENV = {
  ADMIN_UI_OIDC_ENABLED: 'true',
  ADMIN_UI_SESSION_SECRET: 'admin-ui-session-secret-32-bytes-minimum',
  ADMIN_UI_OIDC_CLIENT_ID: 'imposbro-admin-ui',
  ADMIN_UI_OIDC_CLIENT_SECRET: 'client-secret',
  ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT: 'https://idp.example.com/oauth2/authorize',
  ADMIN_UI_OIDC_TOKEN_ENDPOINT: 'https://idp.example.com/oauth2/token',
  ADMIN_UI_OIDC_JWKS_URL: 'https://idp.example.com/.well-known/jwks.json',
  ADMIN_UI_OIDC_SCOPES: 'openid profile email imposbro:admin imposbro:data',
  ADMIN_UI_PROXY_TRUSTED_HEADER: '',
  ADMIN_UI_PROXY_TRUSTED_VALUE: '',
};

const TEST_KEY_ID = 'admin-ui-proxy-test-key';
const { publicKey: TEST_PUBLIC_KEY, privateKey: TEST_PRIVATE_KEY } = await generateKeyPair('RS256');
const TEST_PUBLIC_JWK = {
  ...(await exportJWK(TEST_PUBLIC_KEY)),
  alg: 'RS256',
  kid: TEST_KEY_ID,
  use: 'sig',
};

function withEnv(overrides, run) {
  const previous = {};
  for (const key of Object.keys(overrides)) {
    previous[key] = process.env[key];
    process.env[key] = overrides[key];
  }
  return Promise.resolve()
    .then(run)
    .finally(() => {
      for (const [key, value] of Object.entries(previous)) {
        if (value === undefined) {
          delete process.env[key];
        } else {
          process.env[key] = value;
        }
      }
    });
}

function cookiePair(response, name) {
  const match = (response.headers.get('set-cookie') || '').match(new RegExp(`${name}=[^;,\\s]*`));
  return match?.[0] || '';
}

function request(url, headers = {}) {
  return {
    method: 'GET',
    url,
    nextUrl: new URL(url),
    headers: new Headers(headers),
  };
}

async function testIdToken(nonce) {
  const now = Math.floor(Date.now() / 1000);
  return new SignJWT({
      aud: AUTH_ENV.ADMIN_UI_OIDC_CLIENT_ID,
      exp: now + 1200,
      iat: now,
      nonce,
      sub: 'operator-1',
    })
    .setProtectedHeader({ alg: 'RS256', kid: TEST_KEY_ID, typ: 'JWT' })
    .sign(TEST_PRIVATE_KEY);
}

async function tokenOrJwksResponse(url, tokenResponse) {
  if (String(url) === AUTH_ENV.ADMIN_UI_OIDC_JWKS_URL) {
    return new Response(JSON.stringify({ keys: [TEST_PUBLIC_JWK] }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  }
  return tokenResponse();
}

async function createSessionCookie(accessToken = 'session-access-token') {
  const login = await loginRoute.GET(request('http://admin.local/api/auth/login?return_to=/dashboard'));
  const authorizeUrl = new URL(login.headers.get('location'));
  const txCookie = cookiePair(login, 'imposbro_admin_oidc_tx');
  const originalFetch = globalThis.fetch;

  globalThis.fetch = async (url) => tokenOrJwksResponse(url, async () => new Response(JSON.stringify({
    access_token: accessToken,
    id_token: await testIdToken(authorizeUrl.searchParams.get('nonce')),
    token_type: 'Bearer',
    expires_in: 1200,
    scope: AUTH_ENV.ADMIN_UI_OIDC_SCOPES,
  }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  }));

  try {
    const callback = await callbackRoute.GET(
      request(
        `http://admin.local/api/auth/callback?code=auth-code&state=${authorizeUrl.searchParams.get('state')}`,
        { cookie: txCookie }
      )
    );
    return cookiePair(callback, 'imposbro_admin_session');
  } finally {
    globalThis.fetch = originalFetch;
  }
}

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

test('does not forward headers nominated by the Connection header', async () => {
  const originalFetch = globalThis.fetch;
  let proxied;

  globalThis.fetch = async (url, options) => {
    proxied = {
      url,
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
        nextUrl: new URL('http://admin.local/api/admin/stats'),
        headers: new Headers({
          connection: 'x-hop, keep-alive',
          'x-hop': 'leak',
          'keep-alive': 'timeout=5',
        }),
      },
      { params: Promise.resolve({ path: ['admin', 'stats'] }) }
    );

    assert.equal(response.status, 200);
    assert.equal(proxied.url, 'http://backend.internal/admin/stats');
    assert.equal(proxied.headers.connection, undefined);
    assert.equal(proxied.headers['x-hop'], undefined);
    assert.equal(proxied.headers['keep-alive'], undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('preserves selected backend headers on JSON responses', async () => {
  const originalFetch = globalThis.fetch;

  globalThis.fetch = async () => new Response(JSON.stringify({ ok: true }), {
    status: 429,
    headers: {
      'Content-Type': 'application/json',
      'Retry-After': '30',
      'X-RateLimit-Limit': '1',
      'X-RateLimit-Remaining': '0',
      'X-RateLimit-Reset': '12345',
      'X-Pagination-Info': '{"page":2}',
      'X-Request-ID': 'trace-123',
      'Set-Cookie': 'backend=leak',
    },
  });

  try {
    const response = await routeModule.GET(
      {
        method: 'GET',
        nextUrl: new URL('http://admin.local/api/search/products?q=codex&query_by=name'),
        headers: new Headers(),
      },
      { params: Promise.resolve({ path: ['search', 'products'] }) }
    );

    assert.equal(response.status, 429);
    assert.deepEqual(await response.json(), { ok: true });
    assert.equal(response.headers.get('retry-after'), '30');
    assert.equal(response.headers.get('x-ratelimit-limit'), '1');
    assert.equal(response.headers.get('x-ratelimit-remaining'), '0');
    assert.equal(response.headers.get('x-ratelimit-reset'), '12345');
    assert.equal(response.headers.get('x-pagination-info'), '{"page":2}');
    assert.equal(response.headers.get('x-request-id'), 'trace-123');
    assert.equal(response.headers.get('set-cookie'), null);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('injects data-plane key for data paths when caller has no credentials', async () => {
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

    await routeModule.DELETE(
      {
        method: 'DELETE',
        nextUrl: new URL('http://admin.local/api/documents/products/doc-1'),
        headers: new Headers(),
      },
      { params: Promise.resolve({ path: ['documents', 'products', 'doc-1'] }) }
    );

    assert.equal(proxied[0].url, 'http://backend.internal/search/products?q=codex&query_by=name');
    assert.equal(proxied[0].headers['x-api-key'], 'data-secret');
    assert.equal(proxied[1].url, 'http://backend.internal/ingest/products');
    assert.equal(proxied[1].headers.authorization, 'Bearer caller-token');
    assert.equal(proxied[1].headers['x-api-key'], undefined);
    assert.equal(proxied[2].url, 'http://backend.internal/documents/products/doc-1');
    assert.equal(proxied[2].headers['x-api-key'], 'data-secret');
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

test('production proxy rejects spoofed trusted header without configured trusted value', async () => {
  await withEnv({
    ADMIN_UI_OIDC_ENABLED: 'false',
    ADMIN_UI_PROXY_TRUSTED_HEADER: 'x-authenticated-user',
    ADMIN_UI_PROXY_TRUSTED_VALUE: '',
    INTERNAL_QUERY_API_ADMIN_API_KEY: 'admin-secret',
    NODE_ENV: 'production',
  }, async () => {
    const originalFetch = globalThis.fetch;
    let called = false;
    globalThis.fetch = async () => {
      called = true;
      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    };

    try {
      const response = await routeModule.GET(
        {
          method: 'GET',
          nextUrl: new URL('http://admin.local/api/admin/stats'),
          headers: new Headers({ 'x-authenticated-user': 'attacker-controlled' }),
        },
        { params: Promise.resolve({ path: ['admin', 'stats'] }) }
      );

      assert.equal(response.status, 401);
      assert.equal(called, false);
      assert.match((await response.json()).detail, /trusted upstream identity/i);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test('injects OIDC session bearer before falling back to server API keys', async () => {
  await withEnv(AUTH_ENV, async () => {
    const originalFetch = globalThis.fetch;
    const sessionCookie = await createSessionCookie('operator-token');
    let proxied;

    globalThis.fetch = async (url, options) => {
      proxied = {
        url,
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
        nextUrl: new URL('http://admin.local/api/admin/stats'),
        headers: new Headers({
          cookie: sessionCookie,
          'content-length': '999',
          'proxy-authorization': 'Basic leaked',
        }),
      },
      { params: Promise.resolve({ path: ['admin', 'stats'] }) }
    );

      assert.equal(response.status, 200);
      assert.equal(proxied.url, 'http://backend.internal/admin/stats');
      assert.equal(proxied.headers.authorization, 'Bearer operator-token');
      assert.equal(proxied.headers.cookie, undefined);
      assert.equal(proxied.headers['content-length'], undefined);
      assert.equal(proxied.headers['proxy-authorization'], undefined);
      assert.equal(proxied.headers['x-api-key'], undefined);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test('OIDC-enabled proxy returns login URL instead of API-key bypass without a session', async () => {
  await withEnv(AUTH_ENV, async () => {
    const originalFetch = globalThis.fetch;
    let called = false;
    globalThis.fetch = async () => {
      called = true;
      return new Response('{}');
    };

    try {
      const response = await routeModule.GET(
        {
          method: 'GET',
          nextUrl: new URL('http://admin.local/api/admin/stats'),
          headers: new Headers({ referer: 'http://admin.local/operations' }),
        },
        { params: Promise.resolve({ path: ['admin', 'stats'] }) }
      );
      const payload = await response.json();

      assert.equal(response.status, 401);
      assert.equal(called, false);
      assert.equal(payload.detail, 'Admin UI login required.');
      assert.equal(
        payload.login_url,
        'http://admin.local/api/auth/login?return_to=%2Foperations'
      );
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test('catch-all proxy does not forward reserved auth paths', async () => {
  const originalFetch = globalThis.fetch;
  let called = false;
  globalThis.fetch = async () => {
    called = true;
    return new Response('{}');
  };

  try {
    const response = await routeModule.GET(
      {
        method: 'GET',
        nextUrl: new URL('http://admin.local/api/auth/missing'),
        headers: new Headers(),
      },
      { params: Promise.resolve({ path: ['auth', 'missing'] }) }
    );

    assert.equal(response.status, 404);
    assert.equal(called, false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
