import assert from 'node:assert/strict';
import test from 'node:test';
import { getSearchableFields } from '../app/lib/searchFields.js';

test('query_by defaults include text fields and exclude vector fields', () => {
  const fields = [
    { name: 'title', type: 'string' },
    { name: 'tags', type: 'string[]' },
    { name: 'embedding', type: 'float[]' },
    { name: 'price', type: 'float' },
  ];

  assert.deepEqual(getSearchableFields(fields), ['title', 'tags']);
});

test('vector-only schemas do not prefill query_by', () => {
  const fields = [
    { name: 'embedding', type: 'float[]' },
  ];

  assert.deepEqual(getSearchableFields(fields), []);
});
