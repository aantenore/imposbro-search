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
