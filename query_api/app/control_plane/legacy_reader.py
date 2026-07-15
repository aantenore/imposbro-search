"""Read-only migration adapter for the legacy Typesense config document."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import typesense

from .integrity import canonical_state_digest, normalize_json_object


LEGACY_STATE_COLLECTION = "_imposbro_state"
LEGACY_STATE_DOCUMENT_ID = "config_v1"
LEGACY_STATE_KEYS = (
    "federation_clusters_config",
    "collection_routing_rules",
    "collection_schemas",
    "collection_aliases",
)


class LegacyStateError(RuntimeError):
    """Raised when a legacy state document cannot be migrated without data loss."""


@dataclass(frozen=True)
class LegacyStateSnapshot:
    """Canonical state read from Typesense before the first PostgreSQL commit."""

    state: Dict[str, Any]
    state_digest: str
    loaded_at: datetime


class TypesenseLegacyStateReader:
    """Reads legacy state without creating collections or writing documents."""

    def __init__(
        self,
        client: typesense.Client,
        *,
        collection_name: str = LEGACY_STATE_COLLECTION,
        document_id: str = LEGACY_STATE_DOCUMENT_ID,
    ):
        self._client = client
        self._collection_name = collection_name
        self._document_id = document_id

    def load(self) -> Optional[LegacyStateSnapshot]:
        try:
            document = (
                self._client.collections[self._collection_name]
                .documents[self._document_id]
                .retrieve()
            )
        except typesense.exceptions.ObjectNotFound:
            return None
        except Exception as exc:
            raise LegacyStateError("Could not read legacy control-plane state") from exc

        raw_state = document.get("state_data") if isinstance(document, dict) else None
        if not isinstance(raw_state, str):
            raise LegacyStateError("Legacy state_data must be a JSON string")
        try:
            decoded = json.loads(raw_state)
        except json.JSONDecodeError as exc:
            raise LegacyStateError("Legacy state_data is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise LegacyStateError("Legacy state_data must contain a JSON object")

        unknown_keys = sorted(set(decoded) - set(LEGACY_STATE_KEYS))
        if unknown_keys:
            raise LegacyStateError(
                "Legacy state contains unsupported keys: " + ", ".join(unknown_keys)
            )

        state: Dict[str, Any] = {}
        for key in LEGACY_STATE_KEYS:
            value = decoded.get(key) or {}
            if not isinstance(value, dict):
                raise LegacyStateError(f"Legacy state field '{key}' must be an object")
            state[key] = value

        try:
            normalized_state = normalize_json_object(state, field_name="legacy state")
        except (TypeError, ValueError) as exc:
            raise LegacyStateError("Legacy state contains unsupported JSON values") from exc
        return LegacyStateSnapshot(
            state=normalized_state,
            state_digest=canonical_state_digest(normalized_state),
            loaded_at=datetime.now(timezone.utc),
        )
