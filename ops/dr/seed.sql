\set ON_ERROR_STOP on

INSERT INTO control_plane_state (
  id, revision, schema_version, state_json, state_digest, updated_at
) VALUES (
  'current',
  3,
  1,
  '{"schema_version":1,"clusters":{},"routing_rules":{},"aliases":{},"routing_rollouts":{}}'::jsonb,
  '4a6f3be2d2e81966dd58a99892d926024f9bfea34b9772b679d61e72f8dbc283',
  clock_timestamp() - interval '10 minutes'
);

INSERT INTO control_plane_audit (
  id, sequence, revision, timestamp, actor, action, resource_type,
  resource_id, status, request_id, details_json, previous_hash, event_hash
) VALUES
  (
    '10000000-0000-0000-0000-000000000001', 1, 3,
    '2026-07-10T10:00:00+00:00'::timestamptz,
    'dr-fixture', 'fixture.created', 'dr_fixture', 'fixture-1', 'success',
    'dr-audit-1', '{"fixture":"audit-chain","sensitive":false}'::jsonb,
    repeat('0', 64),
    'cc4a13c94d902f3329696e783c34e2220ff1519cb379d904a3e0d20fc7573ebd'
  ),
  (
    '10000000-0000-0000-0000-000000000002', 2, 3,
    '2026-07-10T10:00:01+00:00'::timestamptz,
    'dr-fixture', 'fixture.verified', 'dr_fixture', 'fixture-1', 'success',
    'dr-audit-2', '{"fixture":"audit-chain","sensitive":false}'::jsonb,
    'cc4a13c94d902f3329696e783c34e2220ff1519cb379d904a3e0d20fc7573ebd',
    'edc8b00f1b2b1ff8ad4f5eebc41b73ee1444da9aa186704fc1bb4c82cd27311c'
  );

UPDATE control_plane_audit_head
SET sequence = 2,
    event_hash = 'edc8b00f1b2b1ff8ad4f5eebc41b73ee1444da9aa186704fc1bb4c82cd27311c'
WHERE id = 'head';

INSERT INTO control_plane_outbox (
  id, revision, event_type, payload_json, created_at, published_at,
  last_error, publish_attempts
) VALUES
  ('00000000-0000-0000-0000-000000000001', 1, 'configuration.changed', '{"revision":1}'::jsonb, clock_timestamp() - interval '10 days', clock_timestamp() - interval '10 days', NULL, 1),
  ('00000000-0000-0000-0000-000000000002', 2, 'configuration.changed', '{"revision":2}'::jsonb, clock_timestamp() - interval '9 days', clock_timestamp() - interval '9 days', NULL, 1),
  ('00000000-0000-0000-0000-000000000003', 3, 'configuration.changed', '{"revision":3}'::jsonb, clock_timestamp() - interval '5 minutes', clock_timestamp() - interval '5 minutes', NULL, 1);

INSERT INTO indexing_event_heads (
  identity_hash, tenant_id, collection, document_id, last_sequence, updated_at
) VALUES
  (repeat('a', 64), 'synthetic-tenant', 'synthetic_collection', 'document-a', 3, clock_timestamp() - interval '4 minutes'),
  (repeat('b', 64), 'synthetic-tenant', 'synthetic_collection', 'document-b', 3, clock_timestamp() - interval '4 minutes'),
  (repeat('c', 64), 'synthetic-tenant', 'synthetic_collection', 'document-c', 2, clock_timestamp() - interval '4 minutes');

INSERT INTO indexing_event_outbox (
  event_id, idempotency_key_hash, request_digest, identity_hash, sequence,
  payload_json, created_at, published_at, last_error, publish_attempts
) VALUES
  ('event-a-1', repeat('1', 64), repeat('1', 64), repeat('a', 64), 1, '{"operation":"upsert","fixture":true}'::jsonb, clock_timestamp() - interval '10 days', clock_timestamp() - interval '10 days', NULL, 1),
  ('event-a-2', repeat('2', 64), repeat('2', 64), repeat('a', 64), 2, '{"operation":"upsert","fixture":true}'::jsonb, clock_timestamp() - interval '9 days', clock_timestamp() - interval '9 days', NULL, 1),
  ('event-a-3', repeat('3', 64), repeat('3', 64), repeat('a', 64), 3, '{"operation":"delete","fixture":true}'::jsonb, clock_timestamp() - interval '4 minutes', clock_timestamp() - interval '4 minutes', NULL, 1),
  ('event-b-1', repeat('4', 64), repeat('4', 64), repeat('b', 64), 1, '{"operation":"upsert","fixture":true}'::jsonb, clock_timestamp() - interval '10 days', clock_timestamp() - interval '10 days', NULL, 1),
  ('event-b-2', repeat('5', 64), repeat('5', 64), repeat('b', 64), 2, '{"operation":"upsert","fixture":true}'::jsonb, clock_timestamp() - interval '9 days', clock_timestamp() - interval '9 days', NULL, 1),
  ('event-b-3', repeat('6', 64), repeat('6', 64), repeat('b', 64), 3, '{"operation":"delete","fixture":true}'::jsonb, clock_timestamp() - interval '8 days', clock_timestamp() - interval '8 days', NULL, 1),
  ('event-c-1', repeat('7', 64), repeat('7', 64), repeat('c', 64), 1, '{"operation":"upsert","fixture":true}'::jsonb, clock_timestamp() - interval '10 days', clock_timestamp() - interval '10 days', NULL, 1),
  ('event-c-2', repeat('8', 64), repeat('8', 64), repeat('c', 64), 2, '{"operation":"delete","fixture":true}'::jsonb, clock_timestamp() - interval '3 minutes', NULL, NULL, 0);

INSERT INTO indexing_checkpoints (
  checkpoint_key, identity_key, tenant_id, collection, document_id,
  target_cluster, sequence, document_version, operation, event_id,
  event_digest, occurred_at, applied_at_ms, tombstone, routing_revision,
  rollout_id
) VALUES (
  repeat('e', 64),
  'synthetic-tenant:synthetic_collection:document-a',
  'synthetic-tenant',
  'synthetic_collection',
  'document-a',
  'synthetic-cluster',
  3,
  3,
  'delete',
  'event-a-3',
  repeat('f', 64),
  clock_timestamp() - interval '4 minutes',
  1000,
  true,
  3,
  'synthetic-rollout'
);

INSERT INTO audit_delivery_checkpoints (
  destination_id, last_sequence, last_event_hash, last_success_at,
  failure_attempts, last_failure_at, last_error_code, next_retry_at,
  created_at, updated_at
) VALUES (
  'dr-security-archive',
  2,
  'edc8b00f1b2b1ff8ad4f5eebc41b73ee1444da9aa186704fc1bb4c82cd27311c',
  clock_timestamp() - interval '2 minutes',
  0,
  NULL,
  NULL,
  NULL,
  clock_timestamp() - interval '5 minutes',
  clock_timestamp() - interval '2 minutes'
);

INSERT INTO deletion_ledger (
  identity_hash, tenant_id, collection, document_id, tombstone_sequence,
  document_version, event_id, event_digest, routing_revision, rollout_id,
  requested_at, retention_until, legal_hold, legal_hold_reference_hash,
  converged_at, last_verified_at, updated_at
) VALUES (
  repeat('a', 64),
  'synthetic-tenant',
  'synthetic_collection',
  'document-a',
  3,
  3,
  'event-a-3',
  repeat('d', 64),
  3,
  'synthetic-rollout',
  clock_timestamp() - interval '4 minutes',
  NULL,
  false,
  NULL,
  clock_timestamp() - interval '3 minutes',
  clock_timestamp() - interval '3 minutes',
  clock_timestamp() - interval '3 minutes'
);

INSERT INTO deletion_ledger_targets (
  identity_hash, target_id, tombstone_sequence, status, checkpoint_sequence,
  receipt_hash, verified_at, updated_at
) VALUES (
  repeat('a', 64),
  'synthetic-cluster',
  3,
  'applied',
  3,
  repeat('c', 64),
  clock_timestamp() - interval '3 minutes',
  clock_timestamp() - interval '3 minutes'
);
