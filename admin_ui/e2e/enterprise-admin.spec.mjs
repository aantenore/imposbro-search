import { expect, test } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';

const WCAG_TAGS = ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'];

function json(route, payload, status = 200, headers = {}) {
  return route.fulfill({
    status,
    contentType: 'application/json',
    headers: {
      'Cache-Control': 'no-store',
      'X-Request-ID': 'browser-e2e-request',
      ...headers,
    },
    body: JSON.stringify(payload),
  });
}

async function installApiMock(page, options = {}) {
  const state = {
    revision: 7,
    rollouts: [],
    createRequest: null,
    ...options,
  };

  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;

    if (path === '/api/auth/session') {
      return json(route, { enabled: false, authenticated: false });
    }
    if (path === '/api/health') {
      return json(route, {
        status: 'healthy',
        redis: 'ok',
        kafka: 'ok',
        clusters: 2,
        collections: 1,
        data_clusters: { 'cluster-a': 'ok', 'cluster-b': 'ok' },
        data_cluster_nodes: {
          'cluster-a': [{ host: 'typesense-a', status: 'ok' }],
          'cluster-b': [{ host: 'typesense-b', status: 'ok' }],
        },
      });
    }
    if (path === '/api/admin/stats') {
      return json(route, { clusters: 2, collections: 1 });
    }
    if (path === '/api/admin/routing-map') {
      return json(route, {
        clusters: ['cluster-a', 'cluster-b'],
        collections: {
          orders: { collection: 'orders', default_cluster: 'cluster-a', rules: [] },
        },
      });
    }
    if (path === '/api/admin/audit-log') {
      return json(route, { entries: [], next_cursor: null });
    }
    if (path === '/api/admin/routing-rollouts' && request.method() === 'GET') {
      return json(route, { revision: state.revision, rollouts: state.rollouts }, 200, {
        ETag: `"${state.revision}"`,
      });
    }
    if (path === '/api/admin/routing-rollouts' && request.method() === 'POST') {
      state.createRequest = {
        ifMatch: request.headers()['if-match'],
        payload: request.postDataJSON(),
      };
      const now = '2026-07-10T10:00:00Z';
      const rollout = {
        rollout_id: 'rollout-e2e-1',
        collection: state.createRequest.payload.candidate_policy.collection,
        active_policy: {
          collection: 'orders',
          default_cluster: 'cluster-a',
          rules: [],
        },
        candidate_policy: state.createRequest.payload.candidate_policy,
        created_by: 'browser-e2e',
        rollback_window_seconds: state.createRequest.payload.rollback_window_seconds,
        version: 1,
        phase: 'draft',
        gates: {},
        backfill_checkpoint: {},
        created_at: now,
        updated_at: now,
        cutover_at: null,
        rollback_deadline: null,
        failure_reason: '',
      };
      state.rollouts = [rollout];
      state.revision += 1;
      return json(route, { revision: state.revision, rollout }, 201, {
        ETag: `"${state.revision}"`,
      });
    }

    return json(route, { detail: `Unhandled browser mock route ${request.method()} ${path}` }, 501);
  });

  return state;
}

async function assertWcag(page, testInfo, label) {
  const results = await new AxeBuilder({ page }).withTags(WCAG_TAGS).analyze();
  await testInfo.attach(`${label}-axe.json`, {
    body: Buffer.from(JSON.stringify(results, null, 2)),
    contentType: 'application/json',
  });
  expect(results.violations, JSON.stringify(results.violations, null, 2)).toEqual([]);
}

test('dashboard renders healthy enterprise state and supports keyboard navigation', async ({ page }, testInfo) => {
  await installApiMock(page);
  const runtimeErrors = [];
  page.on('pageerror', (error) => runtimeErrors.push(error.message));
  page.on('console', (message) => {
    if (message.type() === 'error') runtimeErrors.push(message.text());
  });

  await page.goto('/dashboard');
  await expect(page).toHaveTitle('IMPOSBRO Search Admin');
  await expect(page.getByRole('heading', { name: /Welcome to IMPOSBRO Search/i })).toBeVisible();
  await expect(page.getByText('Operational', { exact: true })).toBeVisible();
  await expect(page.getByText('Redis: ok · Kafka: ok')).toBeVisible();
  await expect(page.getByRole('navigation', { name: 'Primary navigation' })).toBeVisible();

  await page.keyboard.press('Tab');
  const skipLink = page.getByRole('link', { name: 'Skip to main content' });
  await expect(skipLink).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.locator('#main-content')).toBeFocused();

  await assertWcag(page, testInfo, 'dashboard');
  await testInfo.attach('dashboard.png', {
    body: await page.screenshot({ fullPage: true }),
    contentType: 'image/png',
  });
  expect(runtimeErrors).toEqual([]);
});

test('operator creates a revision-protected routing rollout draft', async ({ page }, testInfo) => {
  const state = await installApiMock(page);
  await page.goto('/routing-rollouts');

  await expect(page.getByRole('heading', { name: 'Routing Rollouts', exact: true })).toBeVisible();
  await expect(page.getByText('Rollouts · revision 7')).toBeVisible();
  await page.getByRole('button', { name: 'New rollout' }).click();
  await page.getByLabel('Collection').selectOption('orders');
  await page.getByLabel('Candidate default cluster').selectOption('cluster-b');
  await page.getByRole('button', { name: 'Create draft rollout' }).click();

  await expect(page.getByText('Draft rollout created for orders.')).toBeVisible();
  await expect(page).toHaveURL(/\/routing-rollouts\/rollout-e2e-1$/);
  await expect(page).toHaveTitle('IMPOSBRO Search Admin');
  await expect(page.getByText('Revision 8', { exact: true })).toBeVisible();
  expect(state.createRequest).toEqual({
    ifMatch: '"7"',
    payload: {
      candidate_policy: {
        collection: 'orders',
        default_cluster: 'cluster-b',
        rules: [],
      },
      rollback_window_seconds: 900,
    },
  });

  await assertWcag(page, testInfo, 'rollout-detail');
  await testInfo.attach('routing-rollout-created.png', {
    body: await page.screenshot({ fullPage: true }),
    contentType: 'image/png',
  });
});
