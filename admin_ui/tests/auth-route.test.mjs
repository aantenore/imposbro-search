import assert from 'node:assert/strict';
import test from 'node:test';
import { exportJWK, generateKeyPair, SignJWT } from 'jose';

const loginRoute = await import('../app/api/auth/login/route.js');
const callbackRoute = await import('../app/api/auth/callback/route.js');
const logoutRoute = await import('../app/api/auth/logout/route.js');
const sessionRoute = await import('../app/api/auth/session/route.js');
const { sanitizeReturnTo } = await import('../app/lib/adminAuth.js');

const AUTH_ENV = {
  ADMIN_UI_OIDC_ENABLED: 'true',
  ADMIN_UI_SESSION_SECRET: 'admin-ui-session-secret-32-bytes-minimum',
  ADMIN_UI_OIDC_CLIENT_ID: 'imposbro-admin-ui',
  ADMIN_UI_OIDC_CLIENT_SECRET: 'client-secret',
  ADMIN_UI_OIDC_ISSUER: 'https://idp.example.com',
  ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT: 'https://idp.example.com/oauth2/authorize',
  ADMIN_UI_OIDC_TOKEN_ENDPOINT: 'https://idp.example.com/oauth2/token',
  ADMIN_UI_OIDC_JWKS_URL: 'https://idp.example.com/.well-known/jwks.json',
  ADMIN_UI_OIDC_SCOPES: 'openid profile email imposbro:admin imposbro:data',
};

const TEST_KEY_ID = 'admin-ui-test-key';
const { publicKey: TEST_PUBLIC_KEY, privateKey: TEST_PRIVATE_KEY } = await generateKeyPair('RS256');
const TEST_PUBLIC_JWK = {
  ...(await exportJWK(TEST_PUBLIC_KEY)),
  alg: 'RS256',
  kid: TEST_KEY_ID,
  use: 'sig',
};

function request(url, headers = {}) {
  return {
    method: 'GET',
    url,
    nextUrl: new URL(url),
    headers: new Headers(headers),
  };
}

function formRequest(url, returnTo, headers = {}) {
  const form = new FormData();
  form.set('return_to', returnTo);
  return {
    ...request(url, {
      origin: new URL(url).origin,
      'sec-fetch-site': 'same-origin',
      ...headers,
    }),
    method: 'POST',
    formData: async () => form,
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

function base64UrlJson(value) {
  return Buffer.from(JSON.stringify(value))
    .toString('base64')
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/g, '');
}

async function testIdToken(nonce, claims = {}) {
  const now = Math.floor(Date.now() / 1000);
  const {
    aud = AUTH_ENV.ADMIN_UI_OIDC_CLIENT_ID,
    exp = now + 1200,
    iat = now,
    iss = AUTH_ENV.ADMIN_UI_OIDC_ISSUER,
    sub = 'operator-1',
    ...extraClaims
  } = claims;
  return new SignJWT({
    aud,
    exp,
    iat,
    nonce,
    sub,
    ...extraClaims,
  })
    .setIssuer(iss)
    .setProtectedHeader({ alg: 'RS256', kid: TEST_KEY_ID, typ: 'JWT' })
    .sign(TEST_PRIVATE_KEY);
}

function unsignedIdToken(nonce, claims = {}) {
  const now = Math.floor(Date.now() / 1000);
  return [
    base64UrlJson({ alg: 'none', typ: 'JWT' }),
    base64UrlJson({
      aud: AUTH_ENV.ADMIN_UI_OIDC_CLIENT_ID,
      exp: now + 1200,
      iat: now,
      nonce,
      sub: 'operator-1',
      ...claims,
    }),
    'signature',
  ].join('.');
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

async function createSessionCookie({
  returnTo = '/operations',
  expectedReturnTo = returnTo,
  accessToken = 'access-token',
  idTokenClaims = {},
} = {}) {
  const login = await loginRoute.GET(
    request(`http://admin.local/api/auth/login?return_to=${encodeURIComponent(returnTo)}`)
  );
  const authorizeUrl = new URL(redirectLocation(login));
  const txCookie = cookiePair(login, 'imposbro_admin_oidc_tx');
  assert.ok(txCookie, 'transaction cookie is set');

  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, options) => {
    return tokenOrJwksResponse(url, async () => {
      assert.equal(url, AUTH_ENV.ADMIN_UI_OIDC_TOKEN_ENDPOINT);
      const body = new URLSearchParams(String(options.body));
      assert.equal(body.get('grant_type'), 'authorization_code');
      assert.equal(body.get('code'), 'auth-code');
      assert.equal(body.get('client_id'), AUTH_ENV.ADMIN_UI_OIDC_CLIENT_ID);
      assert.equal(body.get('client_secret'), AUTH_ENV.ADMIN_UI_OIDC_CLIENT_SECRET);
      assert.ok(body.get('code_verifier'));

      return new Response(JSON.stringify({
        access_token: accessToken,
        id_token: await testIdToken(authorizeUrl.searchParams.get('nonce'), idTokenClaims),
        token_type: 'Bearer',
        expires_in: 1200,
        scope: AUTH_ENV.ADMIN_UI_OIDC_SCOPES,
      }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
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
    assert.equal(redirectLocation(callback), expectedReturnTo);
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
    assert.equal(response.headers.get('cache-control'), 'no-store, private');
    assert.equal(response.headers.get('pragma'), 'no-cache');
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
    assert.equal(response.headers.get('cache-control'), 'no-store, private');
  });
});

test('login and logout sanitize unsafe return targets', async () => {
  await withEnv(AUTH_ENV, async () => {
    assert.equal(sanitizeReturnTo('/safe/path?tab=1'), '/safe/path?tab=1');
    assert.equal(sanitizeReturnTo('/\nadmin'), '/dashboard');
    assert.equal(sanitizeReturnTo('/\\evil.com'), '/dashboard');

    const login = await loginRoute.GET(
      request(`http://admin.local/api/auth/login?return_to=${encodeURIComponent('/\\evil.com')}`)
    );
    const loginLocation = new URL(redirectLocation(login));
    assert.ok(loginLocation.searchParams.get('state'));

    const sessionCookie = await createSessionCookie({
      returnTo: '/\\evil.com',
      expectedReturnTo: '/dashboard',
    });
    assert.ok(sessionCookie, 'unsafe return target falls back and still creates a session');

    const logout = await logoutRoute.POST(
      formRequest('http://admin.local/api/auth/logout', '/\\evil.com')
    );
    assert.equal(logout.status, 302);
    assert.equal(redirectLocation(logout), '/dashboard');
  });
});

test('callback rejects missing or invalid state', async () => {
  await withEnv(AUTH_ENV, async () => {
    const response = await callbackRoute.GET(
      request('http://admin.local/api/auth/callback?code=auth-code&state=wrong')
    );
    assert.equal(response.status, 401);
    assert.equal((await response.json()).code, 'oidc_state_invalid');
  });
});

test('callback rejects token responses without an id token', async () => {
  await withEnv(AUTH_ENV, async () => {
    const login = await loginRoute.GET(
      request('http://admin.local/api/auth/login?return_to=/operations')
    );
    const authorizeUrl = new URL(redirectLocation(login));
    const txCookie = cookiePair(login, 'imposbro_admin_oidc_tx');
    const originalFetch = globalThis.fetch;

    globalThis.fetch = async () => new Response(JSON.stringify({
      access_token: 'access-token',
      token_type: 'Bearer',
      expires_in: 1200,
      scope: AUTH_ENV.ADMIN_UI_OIDC_SCOPES,
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    });

    try {
      const response = await callbackRoute.GET(
        request(
          `http://admin.local/api/auth/callback?code=auth-code&state=${authorizeUrl.searchParams.get('state')}`,
          { cookie: txCookie }
        )
      );
      assert.equal(response.status, 502);
      const payload = await response.json();
      assert.equal(payload.code, 'admin_auth_rejected');
      assert.doesNotMatch(payload.detail, /id token/i);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test('callback rejects id tokens that do not match the login nonce', async () => {
  await withEnv(AUTH_ENV, async () => {
    const login = await loginRoute.GET(
      request('http://admin.local/api/auth/login?return_to=/operations')
    );
    const authorizeUrl = new URL(redirectLocation(login));
    const txCookie = cookiePair(login, 'imposbro_admin_oidc_tx');
    const originalFetch = globalThis.fetch;

    globalThis.fetch = async (url) => tokenOrJwksResponse(url, async () => new Response(JSON.stringify({
      access_token: 'access-token',
      id_token: await testIdToken('different-nonce'),
      token_type: 'Bearer',
      expires_in: 1200,
      scope: AUTH_ENV.ADMIN_UI_OIDC_SCOPES,
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));

    try {
      const response = await callbackRoute.GET(
        request(
          `http://admin.local/api/auth/callback?code=auth-code&state=${authorizeUrl.searchParams.get('state')}`,
          { cookie: txCookie }
        )
      );
      assert.equal(response.status, 401);
      assert.equal((await response.json()).code, 'admin_auth_rejected');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test('callback rejects id tokens from a different issuer', async () => {
  await withEnv(AUTH_ENV, async () => {
    const login = await loginRoute.GET(
      request('http://admin.local/api/auth/login?return_to=/operations')
    );
    const authorizeUrl = new URL(redirectLocation(login));
    const txCookie = cookiePair(login, 'imposbro_admin_oidc_tx');
    const originalFetch = globalThis.fetch;

    globalThis.fetch = async (url) => tokenOrJwksResponse(url, async () => new Response(JSON.stringify({
      access_token: 'access-token',
      id_token: await testIdToken(authorizeUrl.searchParams.get('nonce'), {
        iss: 'https://attacker.example',
      }),
      token_type: 'Bearer',
      expires_in: 1200,
      scope: AUTH_ENV.ADMIN_UI_OIDC_SCOPES,
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));

    try {
      const response = await callbackRoute.GET(
        request(
          `http://admin.local/api/auth/callback?code=auth-code&state=${authorizeUrl.searchParams.get('state')}`,
          { cookie: txCookie }
        )
      );
      assert.equal(response.status, 401);
      assert.equal((await response.json()).code, 'admin_auth_rejected');
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});

test('callback rejects unsigned id tokens', async () => {
  await withEnv(AUTH_ENV, async () => {
    const login = await loginRoute.GET(
      request('http://admin.local/api/auth/login?return_to=/operations')
    );
    const authorizeUrl = new URL(redirectLocation(login));
    const txCookie = cookiePair(login, 'imposbro_admin_oidc_tx');
    const originalFetch = globalThis.fetch;

    globalThis.fetch = async (url) => tokenOrJwksResponse(url, async () => new Response(JSON.stringify({
      access_token: 'access-token',
      id_token: unsignedIdToken(authorizeUrl.searchParams.get('nonce')),
      token_type: 'Bearer',
      expires_in: 1200,
      scope: AUTH_ENV.ADMIN_UI_OIDC_SCOPES,
    }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));

    try {
      const response = await callbackRoute.GET(
        request(
          `http://admin.local/api/auth/callback?code=auth-code&state=${authorizeUrl.searchParams.get('state')}`,
          { cookie: txCookie }
        )
      );
      assert.equal(response.status, 401);
      assert.equal((await response.json()).code, 'admin_auth_rejected');
    } finally {
      globalThis.fetch = originalFetch;
    }
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
    const response = await logoutRoute.POST(
      formRequest('http://admin.local/api/auth/logout', '/dashboard')
    );

    assert.equal(response.status, 302);
    assert.equal(redirectLocation(response), '/dashboard');
    assert.match(setCookieHeader(response), /imposbro_admin_session=/);
    assert.match(setCookieHeader(response), /imposbro_admin_oidc_tx=/);
    assert.match(setCookieHeader(response), /Max-Age=0/);
    assert.equal(response.headers.get('cache-control'), 'no-store, private');
  });
});

test('GET logout is non-mutating and advertises POST', async () => {
  await withEnv(AUTH_ENV, async () => {
    const response = await logoutRoute.GET(
      request('http://admin.local/api/auth/logout?return_to=/dashboard', {
        cookie: 'imposbro_admin_session=must-not-be-cleared',
      })
    );

    assert.equal(response.status, 405);
    assert.equal(response.headers.get('allow'), 'POST');
    assert.equal(response.headers.get('set-cookie'), null);
    assert.equal((await response.json()).code, 'method_not_allowed');
  });
});

test('logout rejects missing and cross-site browser provenance without clearing cookies', async () => {
  await withEnv(AUTH_ENV, async () => {
    const missingOrigin = formRequest('http://admin.local/api/auth/logout', '/dashboard');
    missingOrigin.headers.delete('origin');
    const missingResponse = await logoutRoute.POST(missingOrigin);
    assert.equal(missingResponse.status, 403);
    assert.equal((await missingResponse.json()).code, 'csrf_origin_required');
    assert.equal(missingResponse.headers.get('set-cookie'), null);

    const crossSite = formRequest('http://admin.local/api/auth/logout', '/dashboard', {
      origin: 'https://attacker.example',
      'sec-fetch-site': 'cross-site',
    });
    const crossSiteResponse = await logoutRoute.POST(crossSite);
    assert.equal(crossSiteResponse.status, 403);
    assert.equal((await crossSiteResponse.json()).code, 'csrf_origin_mismatch');
    assert.equal(crossSiteResponse.headers.get('set-cookie'), null);

    const missingMetadata = formRequest('http://admin.local/api/auth/logout', '/dashboard');
    missingMetadata.headers.delete('sec-fetch-site');
    const metadataResponse = await logoutRoute.POST(missingMetadata);
    assert.equal(metadataResponse.status, 403);
    assert.equal((await metadataResponse.json()).code, 'csrf_fetch_metadata_required');
  });
});

test('OIDC endpoints are HTTPS and issuer-bound at runtime', async () => {
  await withEnv({
    ...AUTH_ENV,
    ADMIN_UI_OIDC_TOKEN_ENDPOINT: 'http://idp.example.com/oauth2/token',
  }, async () => {
    const insecure = await loginRoute.GET(request('http://admin.local/api/auth/login'));
    assert.equal(insecure.status, 500);
    assert.equal((await insecure.json()).code, 'oidc_configuration_invalid');
  });

  await withEnv({
    ...AUTH_ENV,
    ADMIN_UI_OIDC_JWKS_URL: 'https://keys.attacker.example/jwks.json',
  }, async () => {
    const unbound = await loginRoute.GET(request('http://admin.local/api/auth/login'));
    assert.equal(unbound.status, 500);
    assert.equal((await unbound.json()).code, 'oidc_configuration_invalid');
  });
});

test('unexpected OIDC fetch failures are bounded and externally redacted', async () => {
  await withEnv({
    ...AUTH_ENV,
    ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT: '',
    ADMIN_UI_OIDC_TOKEN_ENDPOINT: '',
    ADMIN_UI_OIDC_JWKS_URL: '',
    ADMIN_UI_OIDC_FETCH_TIMEOUT_MS: '750',
  }, async () => {
    const originalFetch = globalThis.fetch;
    let fetchOptions;
    globalThis.fetch = async (_url, options) => {
      fetchOptions = options;
      throw new Error('dial tcp oidc-secret.internal:8443');
    };
    try {
      const response = await loginRoute.GET(request('http://admin.local/api/auth/login'));
      const payload = await response.json();
      assert.equal(response.status, 502);
      assert.equal(payload.code, 'oidc_provider_unavailable');
      assert.equal(payload.detail, 'Admin UI authentication is temporarily unavailable.');
      assert.doesNotMatch(JSON.stringify(payload), /oidc-secret|dial tcp/i);
      assert.equal(fetchOptions.redirect, 'error');
      assert.ok(fetchOptions.signal instanceof AbortSignal);
    } finally {
      globalThis.fetch = originalFetch;
    }
  });
});
