import assert from 'node:assert/strict';
import test from 'node:test';

import {
  ALLOWED_ROLLOUT_TRANSITIONS,
  RISKY_ROLLOUT_TRANSITIONS,
  isRevisionConflict,
  phaseLabel,
  rolloutOperationalState,
  rolloutTargets,
  sortRollouts,
} from '../app/lib/routingRollouts.js';

test('rollout lifecycle mirrors backend transition boundaries', () => {
  assert.deepEqual(ALLOWED_ROLLOUT_TRANSITIONS.draft, ['validating', 'cancelled']);
  assert.deepEqual(ALLOWED_ROLLOUT_TRANSITIONS.verifying, [
    'backfill',
    'cutover',
    'rolling_back',
    'failed',
  ]);
  assert.deepEqual(ALLOWED_ROLLOUT_TRANSITIONS.completed, []);
  assert.equal(RISKY_ROLLOUT_TRANSITIONS.has('cutover'), true);
  assert.equal(RISKY_ROLLOUT_TRANSITIONS.has('validating'), false);
  assert.equal(phaseLabel('dual_write'), 'Dual Write');
});

test('policy target summary is stable and deduplicated', () => {
  assert.deepEqual(rolloutTargets({
    default_cluster: 'eu',
    rules: [
      { cluster: 'us' },
      { clusters: ['eu', 'apac', 'us'] },
    ],
  }), ['eu', 'us', 'apac']);
});

test('operational state surfaces failures, DLQ, and lag budget breaches', () => {
  assert.deepEqual(
    rolloutOperationalState({ phase: 'failed', gates: {}, failure_reason: 'parity drift' }),
    { label: 'Error', variant: 'error', degraded: true }
  );
  assert.deepEqual(
    rolloutOperationalState({
      phase: 'verifying',
      gates: { unresolved_dlq: 1, kafka_lag: 2, max_kafka_lag: 3 },
    }),
    { label: 'Degraded', variant: 'warning', degraded: true }
  );
  assert.deepEqual(
    rolloutOperationalState({
      phase: 'verifying',
      gates: { unresolved_dlq: 0, kafka_lag: 2, max_kafka_lag: 3 },
    }),
    { label: 'Nominal', variant: 'success', degraded: false }
  );
});

test('rollouts sort newest-first without mutating the input', () => {
  const input = [
    { rollout_id: 'old', updated_at: '2026-01-01T00:00:00Z' },
    { rollout_id: 'new', updated_at: '2026-02-01T00:00:00Z' },
  ];
  const sorted = sortRollouts(input);

  assert.deepEqual(sorted.map((item) => item.rollout_id), ['new', 'old']);
  assert.deepEqual(input.map((item) => item.rollout_id), ['old', 'new']);
  assert.equal(isRevisionConflict({ status: 409 }), true);
  assert.equal(isRevisionConflict({ status: 428 }), true);
  assert.equal(isRevisionConflict({ status: 422 }), false);
});
