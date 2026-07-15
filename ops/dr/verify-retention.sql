\set ON_ERROR_STOP on

SELECT json_build_object(
  'control_rows', (SELECT count(*) FROM control_plane_outbox),
  'control_latest_revision', (SELECT max(revision) FROM control_plane_outbox),
  'indexing_rows', (SELECT count(*) FROM indexing_event_outbox),
  'indexing_unpublished', (SELECT count(*) FROM indexing_event_outbox WHERE published_at IS NULL),
  'held_identity_rows', (
    SELECT count(*) FROM indexing_event_outbox WHERE identity_hash = repeat('b', 64)
  ),
  'heads_with_latest_mutation', (
    SELECT count(*)
    FROM indexing_event_heads AS head
    WHERE EXISTS (
      SELECT 1 FROM indexing_event_outbox AS event
      WHERE event.identity_hash = head.identity_hash
        AND event.sequence = head.last_sequence
    )
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
  ),
  'all_invariants_pass',
    (SELECT count(*) = 1 AND max(revision) = 3 FROM control_plane_outbox)
    AND (SELECT count(*) = 5 FROM indexing_event_outbox)
    AND (SELECT count(*) = 1 FROM indexing_event_outbox WHERE published_at IS NULL)
    AND (SELECT count(*) = 3 FROM indexing_event_outbox WHERE identity_hash = repeat('b', 64))
    AND (
      SELECT count(*) = 3
      FROM indexing_event_heads AS head
      WHERE EXISTS (
        SELECT 1 FROM indexing_event_outbox AS event
        WHERE event.identity_hash = head.identity_hash
          AND event.sequence = head.last_sequence
      )
    )
    AND (SELECT count(*) = 2 FROM control_plane_audit)
    AND (SELECT sequence = 2 FROM control_plane_audit_head WHERE id = 'head')
)::text;
