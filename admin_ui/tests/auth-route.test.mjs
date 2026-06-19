import assert from 'node:assert/strict';
import test from 'node:test';

const loginRoute = await import('../app/api/auth/login/route.js');
const callbackRoute = await import('../app/api/auth/callback/route.js');
const logoutRoute = await import('../app/api/auth/logout/route.js');
const sessionRoute = await import('../app/api/auth/session/route.js');

const AUTH_ENV = {
  ADMIN_UI_OIDC_ENABLED: 'true',
  ADMIN_UI_SESSION_SECRET: 'admin-ui-session-secret-32-bytes-minimum',
  ADMIN_UI_OIDC_CLIENT_ID: 'imposbro-admin-ui',
  ADMIN_UI_OIDC_CLIENT_SECRET: 'client-secret',
  ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT: 'https://idp.example.com/oauth2/authorize',
  ADMIN_UI_OIDC_TOKEN_ENDPOINT: 'https://idp.example.com/oauth2/token',
  ADMIN_UI_OIDC_SCOPES: 'openid profile email imposbro:admin imposbro:data',
};

function request(url, headers = {}) {
  return {
    method: 'GET',
    url,
    nextUrl: new URL(url),
    headers: new Headers(headers),
  };
}

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

function setCookieHeader(response) {
  return response.headers.get('set-cookie') || '';
}

function cookiePair(response, name) {
  const match = setCookieHeader(response).match(new RegExp(`${name}=[^;,\\s]*`));
  return match?.[0] || '';
}

function redirectLocation(response) {
  return response.headers.get('location');
}

async function createSessionCookie({ returnTo = '/operations', accessToken = 'access-token' } = {}) {
  const login = await loginRoute.GET(
    request(`http://admin.local/api/auth/login?return_to=${encodeURIComponent(returnTo)}`)
  );
  const authorizeUrl = new URL(redirectLocation(login));
  const txCookie = cookiePair(login, 'imposbro_admin_oidc_tx');
  assert.ok(txCookie, 'transaction cookie is set');

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    assert.equal(url, AUTH_ENV.ADMIN_UI_OIDC_TOKEN_ENDPOINT);
    const body = new URLSearchParams(String(options.body));
    assert.equal(body.get('grant_type'), 'authorization_code');
    assert.equal(body.get('code'), 'auth-code');
    assert.equal(body.get('client_id'), AUTH_ENV.ADMIN_UI_OIDC_CLIENT_ID);
    assert.equal(body.get('client_secret'), AUTH_ENV.ADMIN_UI_OIDC_CLIENT_SECRET);
    assert.ok(body.get('code_verifier'));

    return new Response(JSON.stringify({
      access_token: accessToken,
      token_type: 'Bearer',
      expires_in: 1200,
      scope: AUTH_ENV.ADMIN_UI_OIDC_SCOPES,
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });
  };

  try {
    const callback = await callbackRoute.GET(
      request(
        `http://admin.local/api/auth/callback?code=auth-code&state=${authorizeUrl.searchParams.get('state')}`,
        { cookie: txCookie }
      )
    );
    assert.equal(callback.status, 302);
    assert.equal(redirectLocation(callback), returnTo);
    const sessionCookie = cookiePair(callback, 'imposbro_admin_session');
    assert.ok(sessionCookie, 'session cookie is set');
    return sessionCookie;
  } finally {
    globalThis.fetch = originalFetch;
  }
}

test('session endpoint reports disabled auth without requiring secrets', async () => {
  await withEnv({ ADMIN_UI_OIDC_ENABLED: 'false' }, async () => {
    const response = await sessionRoute.GET(request('http://admin.local/api/auth/session'));
    assert.deepEqual(await response.json(), { enabled: false, authenticated: false });
  });
});

test('login route redirects to provider with PKCE, state, and a sealed transaction cookie', async () => {
  await withEnv(AUTH_ENV, async () => {
    const response = await loginRoute.GET(
      request('http://admin.local/api/auth/login?return_to=/operations')
    );
    const location = new URL(redirectLocation(response));

    assert.equal(response.status, 302);
    assert.equal(location.origin + location.pathname, AUTH_ENV.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT);
    assert.equal(location.searchParams.get('client_id'), AUTH_ENV.ADMIN_UI_OIDC_CLIENT_ID);
    assert.equal(location.searchParams.get('response_type'), 'code');
    assert.equal(location.searchParams.get('redirect_uri'), 'http://admin.local/api/auth/callback');
    assert.equal(location.searchParams.get('scope'), AUTH_ENV.ADMIN_UI_OIDC_SCOPES);
    assert.equal(location.searchParams.get('code_challenge_method'), 'S256');
    assert.ok(location.searchParams.get('state'));
    assert.ok(location.searchParams.get('nonce'));
    assert.ok(location.searchParams.get('code_challenge'));
    assert.match(setCookieHeader(response), /imposbro_admin_oidc_tx=/);
    assert.match(setCookieHeader(response), /HttpOnly/);
    assert.match(setCookieHeader(response), /SameSite=Lax/);
    assert.match(setCookieHeader(response), /Max-Age=600/);
  });
});

test('callback rejects missing or invalid state', async () => {
  await withEnv(AUTH_ENV, async () => {
    const response = await callbackRoute.GET(
      request('http://admin.local/api/auth/callback?code=auth-code&state=wrong')
    );
    assert.equal(response.status, 401);
    assert.match((await response.json()).detail, /state/i);
  });
});

test('callback exchanges code for a sealed session cookie and session rejects tampering', async () => {
  await withEnv(AUTH_ENV, async () => {
    const sessionCookie = await createSessionCookie({ returnTo: '/operations' });

    const validSession = await sessionRoute.GET(
      request('http://admin.local/api/auth/session', { cookie: sessionCookie })
    );
    const validPayload = await validSession.json();
    assert.equal(validPayload.enabled, true);
    assert.equal(validPayload.authenticated, true);
    assert.ok(validPayload.expires_at);

    const tamperedSession = await sessionRoute.GET(
      request('http://admin.local/api/auth/session', { cookie: `${sessionCookie}x` })
    );
    assert.deepEqual(await tamperedSession.json(), {
      enabled: true,
      authenticated: false,
      expires_at: null,
    });
  });
});

test('logout clears session and transaction cookies', async () => {
  await withEnv(AUTH_ENV, async () => {
    const response = await logoutRoute.GET(
      request('http://admin.local/api/auth/logout?return_to=/dashboard')
    );

    assert.equal(response.status, 302);
    assert.equal(redirectLocation(response), '/dashboard');
    assert.match(setCookieHeader(response), /imposbro_admin_session=/);
    assert.match(setCookieHeader(response), /imposbro_admin_oidc_tx=/);
    assert.match(setCookieHeader(response), /Max-Age=0/);
  });
});
