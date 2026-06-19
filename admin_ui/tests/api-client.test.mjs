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
    const result = await api.search.query('products live', {
      q: 'red shoe',
      query_by: 'title,brand',
      offset: 0,
      limit: 10,
    });

    assert.deepEqual(result, { hits: [], found: 0 });
    assert.equal(
      request.url,
      '/api/search/products%20live?q=red+shoe&query_by=title%2Cbrand&offset=0&limit=10'
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
    await api.search.ingest('tenant/products', { id: 'doc-1', title: 'Workbench' });

    assert.equal(request.url, '/api/ingest/tenant%2Fproducts');
    assert.equal(request.options.method, 'POST');
    assert.equal(request.options.body, '{"id":"doc-1","title":"Workbench"}');
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
