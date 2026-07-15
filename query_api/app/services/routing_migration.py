"""Bounded, restart-safe Typesense routing backfill and parity adapter."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from domain.routing_rollout import RolloutPhase, RoutingRollout
from services.federation import FederationService


class RoutingMigrationError(RuntimeError):
    """Raised when a backfill or parity check cannot prove safe progress."""


@dataclass(frozen=True)
class BackfillStepResult:
    checkpoint: Dict[str, Any]
    copied: int
    complete: bool
    source_documents: int


@dataclass(frozen=True)
class ParityReport:
    passed: bool
    digest: str
    source_documents: int
    candidate_documents: int
    missing_ids: Tuple[str, ...]
    unexpected_ids: Tuple[str, ...]
    different_ids: Tuple[str, ...]


def _canonical(document: Mapping[str, Any]) -> str:
    try:
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise RoutingMigrationError("Document is not canonical JSON") from exc


def _logical_digest(documents: Mapping[str, Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for document_id in sorted(documents):
        digest.update(document_id.encode("utf-8"))
        digest.update(b"\x1f")
        digest.update(_canonical(documents[document_id]).encode("utf-8"))
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def _parse_export(raw: Any, *, cluster_name: str) -> Iterable[Dict[str, Any]]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise RoutingMigrationError(
            f"Cluster '{cluster_name}' returned a non-text document export"
        )
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RoutingMigrationError(
                f"Cluster '{cluster_name}' returned invalid JSONL at line {line_number}"
            ) from exc
        if not isinstance(document, dict):
            raise RoutingMigrationError(
                f"Cluster '{cluster_name}' returned a non-object document"
            )
        document_id = document.get("id")
        if not isinstance(document_id, str) or not document_id:
            raise RoutingMigrationError(
                f"Cluster '{cluster_name}' exported a document without a string id"
            )
        yield document


class RoutingMigrationExecutor:
    """Provider adapter whose checkpoints make repeated chunks idempotent."""

    def __init__(self, federation: FederationService):
        self.federation = federation

    def _policy_clusters(self, policy: Mapping[str, Any]) -> Tuple[str, ...]:
        names: List[str] = []
        for rule in policy.get("rules", []):
            names.extend(FederationService._rule_targets(rule))
        names.append(str(policy.get("default_cluster") or "default"))
        resolved = []
        for name in names:
            cluster_name = self.federation.resolve_cluster_name(name)
            if cluster_name is None:
                raise RoutingMigrationError(
                    f"Routing policy references unavailable cluster '{name}'"
                )
            if cluster_name not in resolved:
                resolved.append(cluster_name)
        return tuple(resolved)

    def candidate_cluster_names(self, rollout: RoutingRollout) -> Tuple[str, ...]:
        """Return every concrete candidate target for conservative repair fan-out."""
        return self._policy_clusters(rollout.candidate_policy)

    def _export_policy_union(
        self,
        collection: str,
        policy: Mapping[str, Any],
    ) -> Dict[str, Dict[str, Any]]:
        documents: Dict[str, Dict[str, Any]] = {}
        origins: Dict[str, str] = {}
        for cluster_name in self._policy_clusters(policy):
            client = self.federation.clients.get(cluster_name)
            if client is None:
                raise RoutingMigrationError(
                    f"No Typesense client is configured for '{cluster_name}'"
                )
            try:
                raw = client.collections[collection].documents.export()
            except Exception as exc:
                raise RoutingMigrationError(
                    f"Failed to export '{collection}' from '{cluster_name}'"
                ) from exc
            for document in _parse_export(raw, cluster_name=cluster_name):
                document_id = document["id"]
                existing = documents.get(document_id)
                if existing is not None and _canonical(existing) != _canonical(document):
                    raise RoutingMigrationError(
                        "Conflicting copies for document "
                        f"'{document_id}' on '{origins[document_id]}' and '{cluster_name}'"
                    )
                documents[document_id] = document
                origins[document_id] = cluster_name
        return documents

    def _candidate_targets(
        self,
        rollout: RoutingRollout,
        document: Mapping[str, Any],
    ) -> Tuple[str, ...]:
        preview = self.federation.preview_routing(
            rollout.collection,
            dict(document),
            rules_config=rollout.candidate_policy,
        )
        names = []
        for configured_name in preview.get("target_clusters") or []:
            resolved = self.federation.resolve_cluster_name(configured_name)
            if resolved is None:
                raise RoutingMigrationError(
                    f"Candidate policy resolved to unavailable cluster '{configured_name}'"
                )
            if resolved not in names:
                names.append(resolved)
        if not names:
            raise RoutingMigrationError(
                f"Candidate policy produced no target for document '{document.get('id')}'"
            )
        return tuple(names)

    @staticmethod
    def _validate_import_result(cluster_name: str, result: Any) -> None:
        if isinstance(result, str):
            try:
                result = [json.loads(line) for line in result.splitlines() if line.strip()]
            except json.JSONDecodeError as exc:
                raise RoutingMigrationError(
                    f"Cluster '{cluster_name}' returned invalid import diagnostics"
                ) from exc
        if result is None:
            return
        if not isinstance(result, list):
            raise RoutingMigrationError(
                f"Cluster '{cluster_name}' returned an unsupported import response"
            )
        failed = [item for item in result if not isinstance(item, dict) or not item.get("success")]
        if failed:
            safe_errors = [str(item.get("error", "unknown error"))[:256] for item in failed if isinstance(item, dict)]
            raise RoutingMigrationError(
                f"Backfill import failed on '{cluster_name}': "
                + "; ".join(safe_errors or ["unknown error"])
            )

    def run_step(
        self,
        rollout: RoutingRollout,
        *,
        checkpoint: Optional[Mapping[str, Any]] = None,
        max_documents: int = 100,
    ) -> BackfillStepResult:
        if rollout.phase != RolloutPhase.BACKFILL:
            raise RoutingMigrationError("Backfill steps require the backfill phase")
        if max_documents < 1 or max_documents > 10_000:
            raise RoutingMigrationError("max_documents must be between 1 and 10000")
        checkpoint = dict(checkpoint or {})
        last_document_id = str(checkpoint.get("last_document_id") or "")
        source = self._export_policy_union(rollout.collection, rollout.active_policy)
        pending_ids = [item for item in sorted(source) if item > last_document_id]
        selected_ids = pending_ids[:max_documents]
        batches: Dict[str, List[Dict[str, Any]]] = {}
        for document_id in selected_ids:
            document = source[document_id]
            for cluster_name in self._candidate_targets(rollout, document):
                batches.setdefault(cluster_name, []).append(document)

        # A failed or partially acknowledged import leaves the persisted
        # checkpoint unchanged; the same upsert batch is therefore safe to retry.
        for cluster_name, documents in batches.items():
            client = self.federation.clients[cluster_name]
            try:
                result = client.collections[rollout.collection].documents.import_(
                    documents,
                    {"action": "upsert", "return_id": True},
                )
            except Exception as exc:
                raise RoutingMigrationError(
                    f"Backfill import failed on '{cluster_name}'"
                ) from exc
            self._validate_import_result(cluster_name, result)

        complete = len(selected_ids) == len(pending_ids)
        processed = int(checkpoint.get("processed_documents") or 0) + len(selected_ids)
        next_checkpoint = {
            **checkpoint,
            "last_document_id": selected_ids[-1] if selected_ids else last_document_id,
            "processed_documents": processed,
            "source_documents_seen": len(source),
            "source_digest_at_step": _logical_digest(source),
            "backfill_complete": complete,
        }
        return BackfillStepResult(
            checkpoint=next_checkpoint,
            copied=len(selected_ids),
            complete=complete,
            source_documents=len(source),
        )

    def verify_parity(self, rollout: RoutingRollout, *, sample_limit: int = 100) -> ParityReport:
        if rollout.phase not in {RolloutPhase.BACKFILL, RolloutPhase.VERIFYING}:
            raise RoutingMigrationError(
                "Parity verification requires backfill or verifying phase"
            )
        if sample_limit < 1 or sample_limit > 1000:
            raise RoutingMigrationError("sample_limit must be between 1 and 1000")
        source = self._export_policy_union(rollout.collection, rollout.active_policy)
        candidate = self._export_policy_union(
            rollout.collection,
            rollout.candidate_policy,
        )
        source_ids = set(source)
        candidate_ids = set(candidate)
        missing = sorted(source_ids - candidate_ids)
        unexpected = sorted(candidate_ids - source_ids)
        different = sorted(
            document_id
            for document_id in source_ids & candidate_ids
            if _canonical(source[document_id]) != _canonical(candidate[document_id])
        )
        source_digest = _logical_digest(source)
        candidate_digest = _logical_digest(candidate)
        return ParityReport(
            passed=(
                not missing
                and not unexpected
                and not different
                and source_digest == candidate_digest
            ),
            digest=source_digest if source_digest == candidate_digest else "",
            source_documents=len(source),
            candidate_documents=len(candidate),
            missing_ids=tuple(missing[:sample_limit]),
            unexpected_ids=tuple(unexpected[:sample_limit]),
            different_ids=tuple(different[:sample_limit]),
        )
