\set ON_ERROR_STOP on

SELECT json_build_object(
  'control_rows', (SELECT count(*) FROM control_plane_outbox),
  'control_unpublished', (
    SELECT count(*) FROM control_plane_outbox WHERE published_at IS NULL
  ),
  'indexing_rows', (SELECT count(*) FROM indexing_event_outbox),
  'indexing_unpublished', (
    SELECT count(*) FROM indexing_event_outbox WHERE published_at IS NULL
  ),
  'audit_rows', (SELECT count(*) FROM control_plane_audit),
  'audit_head_sequence', (
    SELECT sequence FROM control_plane_audit_head WHERE id = 'head'
  ),
  'audit_head_hash', (
    SELECT event_hash FROM control_plane_audit_head WHERE id = 'head'
  ),
  'audit_chain_digest', (
    SELECT md5(coalesce(string_agg(
      id || ':' || sequence::text || ':' || coalesce(revision::text, 'NULL') || ':' ||
      timestamp::text || ':' || actor || ':' || action || ':' || resource_type || ':' ||
      resource_id || ':' || status || ':' || request_id || ':' || details_json::text || ':' ||
      previous_hash || ':' || event_hash,
      '|' ORDER BY sequence
    ), ''))
    FROM control_plane_audit
  )
)::text;
