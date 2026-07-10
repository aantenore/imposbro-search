"""Thread-safe development implementation of the indexing event store."""

from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from typing import Dict, List

from .contracts import (
    IdempotencyConflictError,
    IndexingEventDraft,
    LatestRepairDraft,
    PreparedIndexingEvent,
    RepairSourceNotFoundError,
    build_event_payload,
    build_latest_repair_draft,
    indexing_idempotency_key_hash,
)


class InMemoryIndexingEventStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._heads: Dict[str, int] = {}
        self._by_key: Dict[str, tuple[str, PreparedIndexingEvent]] = {}
        self._global_position = 0

    def check_ready(self) -> None:
        return None

    def prepare(self, draft: IndexingEventDraft) -> PreparedIndexingEvent:
        with self._lock:
            existing = self._by_key.get(draft.idempotency_key_hash)
            if existing is not None:
                digest, event = existing
                if digest != draft.request_digest:
                    raise IdempotencyConflictError(
                        "Idempotency key was already used for another mutation"
                    )
                return copy.deepcopy(event)
            sequence = self._heads.get(draft.identity_hash, 0) + 1
            now = datetime.now(timezone.utc)
            event_id = "evt:" + uuid.uuid4().hex
            self._global_position += 1
            event = PreparedIndexingEvent(
                event_id=event_id,
                sequence=sequence,
                payload=build_event_payload(draft, event_id, sequence, now),
                created_at=now,
                global_position=self._global_position,
            )
            self._heads[draft.identity_hash] = sequence
            self._by_key[draft.idempotency_key_hash] = (
                draft.request_digest,
                event,
            )
            return copy.deepcopy(event)

    def list_pending(self, *, limit: int = 100) -> List[PreparedIndexingEvent]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            events = [event for _digest, event in self._by_key.values()]
            events.sort(key=lambda item: item.global_position)
            return copy.deepcopy(
                [event for event in events if event.published_at is None][:limit]
            )

    def mark_published(self, event_id: str) -> bool:
        with self._lock:
            for key_hash, (digest, event) in self._by_key.items():
                if event.event_id != event_id or event.published_at is not None:
                    continue
                self._by_key[key_hash] = (
                    digest,
                    replace(
                        event,
                        published_at=datetime.now(timezone.utc),
                        publish_attempts=event.publish_attempts + 1,
                        last_error=None,
                    ),
                )
                return True
            return False

    def record_failure(self, event_id: str, error: str) -> bool:
        with self._lock:
            for key_hash, (digest, event) in self._by_key.items():
                if event.event_id != event_id or event.published_at is not None:
                    continue
                self._by_key[key_hash] = (
                    digest,
                    replace(
                        event,
                        publish_attempts=event.publish_attempts + 1,
                        last_error=str(error)[:4096],
                    ),
                )
                return True
            return False

    def pending_count(self) -> int:
        with self._lock:
            return sum(
                event.published_at is None
                for _digest, event in self._by_key.values()
            )

    def high_water_mark(self) -> int:
        with self._lock:
            return self._global_position

    def list_latest_by_identity(
        self,
        *,
        after_position: int,
        through_position: int,
        limit: int = 1000,
    ) -> List[PreparedIndexingEvent]:
        _validate_position_window(after_position, through_position, limit)
        with self._lock:
            latest: Dict[str, PreparedIndexingEvent] = {}
            for _digest, event in self._by_key.values():
                if not after_position < event.global_position <= through_position:
                    continue
                current = latest.get(event.identity_hash)
                if current is None or event.global_position > current.global_position:
                    latest[event.identity_hash] = event
            events = sorted(
                latest.values(),
                key=lambda item: item.global_position,
            )[:limit]
            return copy.deepcopy(events)

    def prepare_latest_repair(
        self,
        repair: LatestRepairDraft,
    ) -> PreparedIndexingEvent:
        with self._lock:
            candidates = [
                event
                for _digest, event in self._by_key.values()
                if event.identity_hash == repair.identity_hash
            ]
            if not candidates:
                raise RepairSourceNotFoundError(
                    "No durable indexing event exists for the repair identity"
                )
            source = max(candidates, key=lambda item: item.sequence)
            identity = source.payload["identity"]
            key_hash = indexing_idempotency_key_hash(
                str(identity["tenant_id"]),
                str(identity["collection"]),
                str(identity["document_id"]),
                repair.idempotency_key,
            )
            existing = self._by_key.get(key_hash)
            if existing is not None:
                return copy.deepcopy(existing[1])
            draft = build_latest_repair_draft(repair, source)
            return self.prepare(draft)


def _validate_position_window(
    after_position: int,
    through_position: int,
    limit: int,
) -> None:
    if after_position < 0 or through_position < after_position:
        raise ValueError("Position window must satisfy 0 <= after <= through")
    if limit < 1:
        raise ValueError("limit must be positive")
