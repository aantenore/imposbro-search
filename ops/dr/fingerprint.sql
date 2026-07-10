\set ON_ERROR_STOP on

SELECT json_build_object(
  'schema_revision', (SELECT version_num FROM alembic_version LIMIT 1),
  'state_revision', (SELECT revision FROM control_plane_state WHERE id = 'current'),
  'state_digest', (SELECT state_digest FROM control_plane_state WHERE id = 'current'),
  'audit_head_sequence', (SELECT sequence FROM control_plane_audit_head WHERE id = 'head'),
  'audit_head_hash', (SELECT event_hash FROM control_plane_audit_head WHERE id = 'head'),
  'audit_rows', (SELECT count(*) FROM control_plane_audit),
  'audit_chain_digest', (
    SELECT md5(coalesce(string_agg(
      id || ':' || sequence::text || ':' || coalesce(revision::text, 'NULL') || ':' ||
      timestamp::text || ':' || actor || ':' || action || ':' || resource_type || ':' ||
      resource_id || ':' || status || ':' || request_id || ':' || details_json::text || ':' ||
      previous_hash || ':' || event_hash,
      '|' ORDER BY sequence
    ), ''))
    FROM control_plane_audit
  ),
  'audit_delivery_rows', (SELECT count(*) FROM audit_delivery_checkpoints),
  'audit_delivery_digest', (
    SELECT md5(coalesce(string_agg(
      destination_id || ':' || last_sequence::text || ':' || last_event_hash || ':' ||
      coalesce(last_success_at::text, 'NULL') || ':' || failure_attempts::text || ':' ||
      coalesce(last_error_code, 'NULL') || ':' || coalesce(next_retry_at::text, 'NULL'),
      '|' ORDER BY destination_id
    ), ''))
    FROM audit_delivery_checkpoints
  ),
  'control_outbox_rows', (SELECT count(*) FROM control_plane_outbox),
  'control_outbox_unpublished', (SELECT count(*) FROM control_plane_outbox WHERE published_at IS NULL),
  'control_outbox_max_revision', (SELECT max(revision) FROM control_plane_outbox),
  'control_outbox_digest', (
    SELECT md5(coalesce(string_agg(
      id || ':' || revision::text || ':' || event_type || ':' || payload_json::text || ':' ||
      created_at::text || ':' || coalesce(published_at::text, 'NULL') || ':' ||
      publish_attempts::text,
      '|' ORDER BY revision
    ), ''))
    FROM control_plane_outbox
  ),
  'indexing_outbox_rows', (SELECT count(*) FROM indexing_event_outbox),
  'indexing_outbox_unpublished', (SELECT count(*) FROM indexing_event_outbox WHERE published_at IS NULL),
  'indexing_outbox_max_position', (SELECT max(global_position) FROM indexing_event_outbox),
  'indexing_outbox_digest', (
    SELECT md5(coalesce(string_agg(
      event_id || ':' || global_position::text || ':' || idempotency_key_hash || ':' ||
      request_digest || ':' || identity_hash || ':' || sequence::text || ':' ||
      payload_json::text || ':' || created_at::text || ':' ||
      coalesce(published_at::text, 'NULL') || ':' || publish_attempts::text,
      '|' ORDER BY global_position
    ), ''))
    FROM indexing_event_outbox
  ),
  'indexing_head_rows', (SELECT count(*) FROM indexing_event_heads),
  'indexing_head_digest', (
    SELECT md5(coalesce(string_agg(
      identity_hash || ':' || tenant_id || ':' || collection || ':' || document_id || ':' ||
      last_sequence::text || ':' || updated_at::text,
      '|' ORDER BY identity_hash
    ), ''))
    FROM indexing_event_heads
  ),
  'indexing_checkpoint_rows', (SELECT count(*) FROM indexing_checkpoints),
  'indexing_checkpoint_digest', (
    SELECT md5(coalesce(string_agg(
      checkpoint_key || ':' || identity_key || ':' || tenant_id || ':' || collection || ':' ||
      document_id || ':' || target_cluster || ':' || sequence::text || ':' ||
      document_version::text || ':' || operation || ':' || event_id || ':' ||
      event_digest || ':' || occurred_at::text || ':' || applied_at_ms::text || ':' ||
      tombstone::text || ':' || routing_revision::text || ':' || rollout_id,
      '|' ORDER BY checkpoint_key
    ), ''))
    FROM indexing_checkpoints
  ),
  'deletion_ledger_rows', (SELECT count(*) FROM deletion_ledger),
  'deletion_ledger_digest', (
    SELECT md5(coalesce(string_agg(
      identity_hash || ':' || tenant_id || ':' || collection || ':' || document_id || ':' ||
      tombstone_sequence::text || ':' || document_version::text || ':' || event_id || ':' ||
      event_digest || ':' || routing_revision::text || ':' || rollout_id || ':' ||
      requested_at::text || ':' || coalesce(retention_until::text, 'NULL') || ':' ||
      legal_hold::text || ':' || coalesce(converged_at::text, 'NULL'),
      '|' ORDER BY identity_hash
    ), ''))
    FROM deletion_ledger
  ),
  'deletion_target_rows', (SELECT count(*) FROM deletion_ledger_targets),
  'deletion_target_digest', (
    SELECT md5(coalesce(string_agg(
      identity_hash || ':' || target_id || ':' || tombstone_sequence::text || ':' ||
      status || ':' || coalesce(checkpoint_sequence::text, 'NULL') || ':' ||
      coalesce(receipt_hash, 'NULL') || ':' || coalesce(verified_at::text, 'NULL'),
      '|' ORDER BY identity_hash, target_id
    ), ''))
    FROM deletion_ledger_targets
  ),
  'newest_durable_change_at', greatest(
    (SELECT updated_at FROM control_plane_state WHERE id = 'current'),
    (SELECT max(created_at) FROM control_plane_outbox),
    (SELECT max(created_at) FROM indexing_event_outbox),
    (SELECT max(updated_at) FROM audit_delivery_checkpoints),
    (SELECT max(updated_at) FROM deletion_ledger)
  )
)::text;
