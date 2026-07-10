import assert from 'node:assert/strict';
import test from 'node:test';
import { api, ApiError } from '../app/lib/api.js';

function jsonResponse(body, init = {}) {
  return new Response(JSON.stringify(body), {
    status: init.status ?? 200,
    headers: { 'Content-Type': 'application/json', ...(init.headers || {}) },
  });
}

test('search query encodes collection names and query parameters', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ hits: [], found: 0 });
  };

  try {
    const result = await api.search.query('products_live', {
      q: 'red shoe',
      query_by: 'title,brand',
      offset: 0,
      limit: 10,
    });

    assert.deepEqual(result, { hits: [], found: 0 });
    assert.equal(
      request.url,
      '/api/search/products_live?q=red+shoe&query_by=title%2Cbrand&offset=0&limit=10'
    );
    assert.equal(request.options.headers['Content-Type'], 'application/json');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('advanced search posts a JSON body to the encoded collection path', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ hits: [], found: 0 });
  };

  try {
    await api.search.queryAdvanced('products_live', {
      q: '*',
      vector_query: 'embedding:([0.1,0.2], k:10)',
      exclude_fields: 'embedding',
    });

    assert.equal(request.url, '/api/search/products_live');
    assert.equal(request.options.method, 'POST');
    assert.equal(
      request.options.body,
      '{"q":"*","vector_query":"embedding:([0.1,0.2], k:10)","exclude_fields":"embedding"}'
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('state export masks secrets by default', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ version: 'imposbro.state.v1', secrets_included: false });
  };

  try {
    await api.state.exportSnapshot();

    assert.equal(request.url, '/api/admin/state/export');
    assert.equal(request.options.headers['Content-Type'], 'application/json');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('auth session checks the Admin UI session endpoint without auth redirects', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ enabled: true, authenticated: false });
  };

  try {
    const result = await api.auth.session();

    assert.deepEqual(result, { enabled: true, authenticated: false });
    assert.equal(request.url, '/api/auth/session');
    assert.equal(request.options.headers['Content-Type'], 'application/json');
    assert.equal(request.options.redirectOnAuth, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('collection reconciliation posts to the admin endpoint', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ status: 'ok', clusters: {} });
  };

  try {
    await api.collections.reconcile();

    assert.equal(request.url, '/api/admin/collections/reconcile');
    assert.equal(request.options.method, 'POST');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('cluster registration includes the selected protocol in the JSON payload', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const cluster = {
    name: 'cluster-eu',
    protocol: 'https',
    host: 'typesense.example.com',
    port: 443,
    api_key: 'secret',
  };

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ status: 'ok' });
  };

  try {
    await api.clusters.create(cluster);

    assert.equal(request.url, '/api/admin/federation/clusters');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.body, JSON.stringify(cluster));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('alias client lists, upserts, and deletes with encoded names', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];

  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return jsonResponse({ status: 'ok', aliases: [] });
  };

  try {
    await api.aliases.list({ clusterName: 'cluster_eu' });
    await api.aliases.upsert({
      aliasName: 'products_live',
      collectionName: 'products_v2',
      clusterName: 'cluster_eu',
    });
    await api.aliases.delete({
      aliasName: 'products_live',
      clusterName: 'cluster_eu',
    });

    assert.equal(requests[0].url, '/api/admin/aliases?cluster_name=cluster_eu');
    assert.equal(
      requests[1].url,
      '/api/admin/aliases/products_live?collection_name=products_v2&cluster_name=cluster_eu'
    );
    assert.equal(requests[1].options.method, 'PUT');
    assert.equal(
      requests[2].url,
      '/api/admin/aliases/products_live?cluster_name=cluster_eu'
    );
    assert.equal(requests[2].options.method, 'DELETE');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('state export can explicitly include restore secrets', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ version: 'imposbro.state.v1', secrets_included: true });
  };

  try {
    await api.state.exportSnapshot({ includeSecrets: true });

    assert.equal(request.url, '/api/admin/state/export?include_secrets=true');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('state import posts snapshot JSON and apply flag', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const snapshot = {
    version: 'imposbro.state.v1',
    secrets_included: true,
    federation_clusters_config: {},
    collection_routing_rules: {},
    collection_schemas: {},
  };

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ status: 'ok', dry_run: false, counts: {} });
  };

  try {
    await api.state.importSnapshot(snapshot, { apply: true });

    assert.equal(request.url, '/api/admin/state/import?apply=true');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.body, JSON.stringify(snapshot));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('audit list builds sanitized filter query parameters', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ entries: [] });
  };

  try {
    await api.audit.list({
      limit: 10,
      action: 'state_imported',
      resourceType: 'control_plane_state',
    });

    assert.equal(
      request.url,
      '/api/admin/audit-log?limit=10&action=state_imported&resource_type=control_plane_state'
    );
    assert.equal(request.options.headers['Content-Type'], 'application/json');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('routing rollout client sends global revision CAS and rollout version payloads', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  const createPayload = {
    candidate_policy: {
      collection: 'products_live',
      rules: [],
      default_cluster: 'eu',
    },
    rollback_window_seconds: 900,
  };

  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return jsonResponse({
      rollout: { rollout_id: 'rollout-1', collection: 'products_live' },
      revision: requests.length + 7,
    }, { status: requests.length === 1 ? 201 : 200 });
  };

  try {
    await api.routingRollouts.list({ collection: 'products live' });
    await api.routingRollouts.create(createPayload, { revision: 8 });
    await api.routingRollouts.transition('rollout/1', {
      target_phase: 'validating',
      expected_version: 3,
    }, { revision: 9 });
    await api.routingRollouts.runBackfillStep('rollout/1', {
      expected_version: 4,
      max_documents: 250,
    }, { revision: 10 });
    await api.routingRollouts.verifyParity('rollout/1', {
      expected_version: 5,
      sample_limit: 100,
    }, { revision: 11 });

    assert.equal(requests[0].url, '/api/admin/routing-rollouts?collection=products+live');
    assert.equal(requests[1].url, '/api/admin/routing-rollouts');
    assert.equal(requests[1].options.method, 'POST');
    assert.equal(requests[1].options.headers['If-Match'], '"8"');
    assert.equal(requests[1].options.body, JSON.stringify(createPayload));
    assert.equal(
      requests[2].url,
      '/api/admin/routing-rollouts/rollout%2F1/transitions'
    );
    assert.equal(requests[2].options.headers['If-Match'], '"9"');
    assert.equal(
      requests[2].options.body,
      '{"target_phase":"validating","expected_version":3}'
    );
    assert.equal(
      requests[3].url,
      '/api/admin/routing-rollouts/rollout%2F1/backfill/steps'
    );
    assert.equal(requests[3].options.headers['If-Match'], '"10"');
    assert.equal(
      requests[4].url,
      '/api/admin/routing-rollouts/rollout%2F1/parity-verifications'
    );
    assert.equal(requests[4].options.headers['If-Match'], '"11"');
    assert.equal(api.routing.setRules, undefined);
    assert.equal(api.routing.deleteRules, undefined);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('routing rollout conflicts expose a readable code, ETag, and request id', async () => {
  const originalFetch = globalThis.fetch;

  globalThis.fetch = async () => jsonResponse({
    detail: {
      code: 'routing_rollout_version_conflict',
      expected_version: 3,
      current_version: 4,
    },
  }, {
    status: 409,
    headers: { ETag: '"12"', 'X-Request-ID': 'request-12' },
  });

  try {
    await assert.rejects(
      api.routingRollouts.transition('rollout-1', {
        target_phase: 'cutover',
        expected_version: 3,
      }, { revision: 11 }),
      (error) => {
        assert.ok(error instanceof ApiError);
        assert.equal(error.status, 409);
        assert.equal(error.message, 'routing rollout version conflict');
        assert.deepEqual(error.metadata, {
          etag: '"12"',
          requestId: 'request-12',
        });
        return true;
      }
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('routing preview posts draft rules without saving', async () => {
  const originalFetch = globalThis.fetch;
  let request;
  const payload = {
    collection: 'products',
    document: { region: 'IT' },
    rules: [
      {
        field: 'region',
        operator: 'in',
        values: ['IT', 'FR'],
        cluster: 'cluster-eu',
      },
    ],
    default_cluster: 'default',
  };

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ matched: true, routed_to: ['cluster-eu'] });
  };

  try {
    await api.routing.preview(payload);

    assert.equal(request.url, '/api/admin/routing-rules/preview');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.body, JSON.stringify(payload));
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('ingest posts a JSON document to the encoded collection path', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ status: 'ok', document_id: 'doc-1', routed_to: 'cluster-a' });
  };

  try {
    await api.search.ingest('tenant_products', { id: 'doc-1', title: 'Workbench' });

    assert.equal(request.url, '/api/ingest/tenant_products');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.body, '{"id":"doc-1","title":"Workbench"}');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('deleteDocument deletes by encoded collection and document id path', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({ status: 'ok', document_id: 'doc-1', routed_to: 'cluster-a' });
  };

  try {
    await api.search.deleteDocument('tenant_products', 'doc.1');

    assert.equal(request.url, '/api/documents/tenant_products/doc.1');
    assert.equal(request.options.method, 'DELETE');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('getDocument reads by encoded collection and document id path', async () => {
  const originalFetch = globalThis.fetch;
  let request;

  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return jsonResponse({
      status: 'ok',
      document_id: 'doc-1',
      found_in: 'cluster-a',
      document: { id: 'doc-1' },
    });
  };

  try {
    await api.search.getDocument('tenant_products', 'doc.1');

    assert.equal(request.url, '/api/documents/tenant_products/doc.1');
    assert.equal(request.options.method, 'GET');
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test('request raises ApiError with backend detail on non-2xx responses', async () => {
  const originalFetch = globalThis.fetch;

  globalThis.fetch = async () => jsonResponse({ detail: 'Invalid query_by' }, { status: 400 });

  try {
    await assert.rejects(
      () => api.search.query('products', { q: '*', query_by: '' }),
      (err) => {
        assert.ok(err instanceof ApiError);
        assert.equal(err.status, 400);
        assert.equal(err.message, 'Invalid query_by');
        assert.deepEqual(err.data, { detail: 'Invalid query_by' });
        return true;
      }
    );
  } finally {
    globalThis.fetch = originalFetch;
  }
});
