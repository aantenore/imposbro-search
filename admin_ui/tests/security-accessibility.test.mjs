import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

import nextConfig, { buildSecurityHeaders } from '../next.config.js';

function headerMap(headers) {
  return new Map(headers.map(({ key, value }) => [key.toLowerCase(), value]));
}

async function source(relativePath) {
  return readFile(new URL(relativePath, import.meta.url), 'utf8');
}

test('production security-header defaults cover CSP, HSTS, sniffing, referrers, and framing', () => {
  const headers = headerMap(buildSecurityHeaders({ NODE_ENV: 'production' }));
  const csp = headers.get('content-security-policy');

  assert.match(csp, /default-src 'self'/);
  assert.match(csp, /base-uri 'none'/);
  assert.match(csp, /object-src 'none'/);
  assert.match(csp, /frame-ancestors 'none'/);
  assert.match(csp, /upgrade-insecure-requests/);
  assert.equal(headers.get('strict-transport-security'), 'max-age=31536000; includeSubDomains');
  assert.equal(headers.get('x-content-type-options'), 'nosniff');
  assert.equal(headers.get('referrer-policy'), 'strict-origin-when-cross-origin');
  assert.equal(headers.get('x-frame-options'), 'DENY');
});

test('security-header policy supports explicit deployment overrides', () => {
  const headers = headerMap(buildSecurityHeaders({
    NODE_ENV: 'production',
    ADMIN_UI_FRAME_ANCESTORS: "'self' https://portal.example.com",
    ADMIN_UI_CSP_CONNECT_SRC: 'https://telemetry.example.com',
    ADMIN_UI_CSP_IMAGE_SRC: 'https://images.example.com',
    ADMIN_UI_REFERRER_POLICY: 'no-referrer',
    ADMIN_UI_HSTS_MAX_AGE_SECONDS: '63072000',
    ADMIN_UI_HSTS_INCLUDE_SUBDOMAINS: 'false',
    ADMIN_UI_HSTS_PRELOAD: 'true',
  }));
  const csp = headers.get('content-security-policy');

  assert.match(csp, /frame-ancestors 'self' https:\/\/portal\.example\.com/);
  assert.match(csp, /connect-src 'self' https:\/\/telemetry\.example\.com/);
  assert.match(csp, /img-src 'self' data: blob: https:\/\/images\.example\.com/);
  assert.equal(headers.get('referrer-policy'), 'no-referrer');
  assert.equal(headers.get('strict-transport-security'), 'max-age=63072000; preload');
  assert.equal(headers.has('x-frame-options'), false);
});

test('custom CSP retains the separately configured frame-ancestors boundary', () => {
  const headers = headerMap(buildSecurityHeaders({
    ADMIN_UI_CONTENT_SECURITY_POLICY: "default-src 'none'; frame-ancestors https://ignored.example.com",
    ADMIN_UI_FRAME_ANCESTORS: "'self'",
  }));

  assert.equal(
    headers.get('content-security-policy'),
    "default-src 'none'; frame-ancestors 'self';"
  );
  assert.equal(headers.get('x-frame-options'), 'SAMEORIGIN');
});

test('development CSP supports Next tooling without weakening production', () => {
  const developmentHeaders = headerMap(buildSecurityHeaders({ NODE_ENV: 'development' }));
  const productionHeaders = headerMap(buildSecurityHeaders({ NODE_ENV: 'production' }));

  assert.match(developmentHeaders.get('content-security-policy'), /script-src[^;]*'unsafe-eval'/);
  assert.match(developmentHeaders.get('content-security-policy'), /connect-src[^;]*ws: wss:/);
  assert.equal(developmentHeaders.has('strict-transport-security'), false);
  assert.doesNotMatch(productionHeaders.get('content-security-policy'), /'unsafe-eval'/);
  assert.doesNotMatch(productionHeaders.get('content-security-policy'), /connect-src[^;]*ws:/);
});

test('security-header parsing fails closed on malformed configuration', () => {
  assert.throws(
    () => buildSecurityHeaders({ ADMIN_UI_REFERRER_POLICY: 'unsafe-url' }),
    /not a supported referrer policy/
  );
  assert.throws(
    () => buildSecurityHeaders({ ADMIN_UI_FRAME_ANCESTORS: "'none' https://example.com" }),
    /cannot combine 'none'/
  );
  assert.throws(
    () => buildSecurityHeaders({ ADMIN_UI_CSP_CONNECT_SRC: 'https://safe.example.com\r\nX-Test: bad' }),
    /must not contain carriage returns or newlines/
  );
  assert.throws(
    () => buildSecurityHeaders({ ADMIN_UI_HSTS_ENABLED: 'true', ADMIN_UI_HSTS_MAX_AGE_SECONDS: '12days' }),
    /must be a positive integer/
  );
});

test('security headers can be disabled explicitly and otherwise cover every route', async () => {
  assert.deepEqual(
    buildSecurityHeaders({ ADMIN_UI_SECURITY_HEADERS_ENABLED: 'false' }),
    []
  );

  const routes = await nextConfig.headers();
  assert.equal(routes.length, 1);
  assert.equal(routes[0].source, '/:path*');
  assert.ok(routes[0].headers.length >= 4);
});

test('form primitives associate labels and expose inline errors to assistive technology', async () => {
  const inputSource = await source('../app/components/ui/Input.jsx');

  assert.match(inputSource, /htmlFor=\{controlId\}/);
  assert.match(inputSource, /aria-describedby=\{describedByValue/);
  assert.match(inputSource, /aria-invalid=\{error \? true : ariaInvalid\}/);
  assert.match(inputSource, /role="alert"/);
});

test('confirmation dialog owns focus, supports Escape and restores the trigger', async () => {
  const modalSource = await source('../app/components/ui/ConfirmationModal.jsx');

  assert.match(modalSource, /role="alertdialog"/);
  assert.match(modalSource, /aria-modal="true"/);
  assert.match(modalSource, /aria-describedby=\{descriptionId\}/);
  assert.match(modalSource, /event\.key === 'Escape'/);
  assert.match(modalSource, /event\.key !== 'Tab'/);
  assert.match(modalSource, /previouslyFocused\.focus\(\)/);
  assert.match(modalSource, /cancelButtonRef\.current\?\.focus\(\)/);
});

test('notifications, navigation, and page shell expose semantic accessibility hooks', async () => {
  const [notificationSource, layoutSource, sidebarSource] = await Promise.all([
    source('../app/hooks/useNotification.js'),
    source('../app/layout.jsx'),
    source('../app/components/Sidebar.jsx'),
  ]);

  assert.match(notificationSource, /role=\{isError \? 'alert' : 'status'\}/);
  assert.match(notificationSource, /aria-live=\{isError \? 'assertive' : 'polite'\}/);
  assert.match(notificationSource, /aria-label="Dismiss notification"/);
  assert.match(layoutSource, /href="#main-content"/);
  assert.match(layoutSource, /id="main-content"/);
  assert.match(sidebarSource, /aria-label="Primary navigation"/);
  assert.match(sidebarSource, /aria-current=\{isActive \? 'page' : undefined\}/);
  assert.match(sidebarSource, /<form action="\/api\/auth\/logout" method="post">/);
  assert.match(sidebarSource, /<button[\s\S]*type="submit"[\s\S]*aria-label="Sign out of the Admin UI"/);
  assert.doesNotMatch(sidebarSource, /href=\{[^}]*logout/i);
});

test('routing rollout console exposes accessible async, error, evidence, and navigation states', async () => {
  const [consoleSource, detailSource, createSource, routingSource] = await Promise.all([
    source('../app/components/RoutingRolloutsConsole.jsx'),
    source('../app/components/RoutingRolloutDetail.jsx'),
    source('../app/components/RoutingRolloutCreateForm.jsx'),
    source('../app/(pages)/routing/page.jsx'),
  ]);

  assert.match(consoleSource, /aria-busy=\{loadState === 'loading' \|\| isRefreshPending\}/);
  assert.match(consoleSource, /role="status"/);
  assert.match(consoleSource, /role="alert"/);
  assert.match(consoleSource, /aria-label="Routing rollouts"/);
  assert.match(consoleSource, /Retry with latest/);
  assert.match(detailSource, /<fieldset/);
  assert.match(detailSource, /<legend/);
  assert.match(detailSource, /aria-label="Rollout lifecycle status"/);
  assert.match(detailSource, /ConfirmationModal/);
  assert.match(createSource, /label="Candidate rules \(JSON array\)"/);
  assert.match(routingSource, /Direct routing mutations are disabled/);
  assert.doesNotMatch(routingSource, /api\.routing\.(setRules|deleteRules)/);
});
