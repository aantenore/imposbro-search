import assert from 'node:assert/strict';
import test from 'node:test';
import {
  createEmptyClusterForm,
  formatClusterEndpoint,
} from '../app/lib/clusterConfig.js';

test('new and reset cluster forms default to HTTP without retaining prior values', () => {
  const form = createEmptyClusterForm();
  form.protocol = 'https';
  form.api_key = 'secret';
  const resetForm = createEmptyClusterForm();

  assert.notStrictEqual(resetForm, form);
  assert.deepEqual(resetForm, {
    name: '',
    protocol: 'http',
    host: '',
    port: 8108,
    api_key: '',
  });
});

test('cluster endpoints include the configured protocol', () => {
  const endpoint = formatClusterEndpoint({
    protocol: 'https',
    host: 'typesense.example.com',
    port: 443,
    api_key: 'must-not-be-rendered',
  });

  assert.equal(endpoint, 'https://typesense.example.com:443');
  assert.doesNotMatch(endpoint, /must-not-be-rendered/);
});

test('legacy cluster endpoints without protocol remain HTTP', () => {
  assert.equal(
    formatClusterEndpoint({ host: 'typesense.internal', port: 8108 }),
    'http://typesense.internal:8108'
  );
});
