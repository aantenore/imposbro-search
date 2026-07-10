"""Backfill adapter tests for restart safety and exact logical parity."""

import json

import pytest

from domain.routing_rollout import RolloutPhase, RoutingRollout
from services.federation import FederationService
from services.routing_migration import RoutingMigrationError, RoutingMigrationExecutor


class FakeDocuments:
    def __init__(self, initial=None, *, fail_import_once=False):
        self.values = {item["id"]: dict(item) for item in (initial or [])}
        self.fail_import_once = fail_import_once
        self.import_calls = []

    def export(self):
        return "\n".join(json.dumps(item) for item in self.values.values())

    def import_(self, documents, params):
        self.import_calls.append(([dict(item) for item in documents], dict(params)))
        if self.fail_import_once:
            self.fail_import_once = False
            # Simulate an ambiguous partial server outcome before the request fails.
            self.values[documents[0]["id"]] = dict(documents[0])
            raise RuntimeError("connection lost")
        for document in documents:
            self.values[document["id"]] = dict(document)
        return [{"success": True, "id": item["id"]} for item in documents]


class FakeCollections:
    def __init__(self, documents):
        self.documents = documents

    def __getitem__(self, _collection):
        return type("Collection", (), {"documents": self.documents})()


class FakeClient:
    def __init__(self, initial=None, *, fail_import_once=False):
        self.documents = FakeDocuments(initial, fail_import_once=fail_import_once)
        self.collections = FakeCollections(self.documents)


def rollout() -> RoutingRollout:
    item = RoutingRollout(
        rollout_id="rollout-1",
        collection="products",
        active_policy={
            "collection": "products",
            "rules": [],
            "default_cluster": "cluster-a",
        },
        candidate_policy={
            "collection": "products",
            "rules": [],
            "default_cluster": "cluster-b",
        },
        created_by="test",
        rollback_window_seconds=300,
    )
    object.__setattr__(item, "phase", RolloutPhase.BACKFILL)
    return item


def federation(source, candidate) -> FederationService:
    service = FederationService()
    service.clients = {"cluster-a": source, "cluster-b": candidate}
    return service


def test_backfill_chunks_resume_and_produce_exact_parity():
    source = FakeClient(
        [
            {"id": "3", "name": "three"},
            {"id": "1", "name": "one"},
            {"id": "2", "name": "two"},
        ]
    )
    candidate = FakeClient()
    executor = RoutingMigrationExecutor(federation(source, candidate))

    first = executor.run_step(rollout(), max_documents=2)
    assert first.copied == 2
    assert first.complete is False
    assert first.checkpoint["last_document_id"] == "2"
    second = executor.run_step(
        rollout(),
        checkpoint=first.checkpoint,
        max_documents=2,
    )

    assert second.copied == 1
    assert second.complete is True
    assert second.checkpoint["processed_documents"] == 3
    report = executor.verify_parity(rollout())
    assert report.passed is True
    assert report.source_documents == report.candidate_documents == 3
    assert report.digest.startswith("sha256:")


def test_failed_ambiguous_import_retries_same_upserts_without_checkpoint_loss():
    source = FakeClient([{"id": "1", "name": "one"}, {"id": "2", "name": "two"}])
    candidate = FakeClient(fail_import_once=True)
    executor = RoutingMigrationExecutor(federation(source, candidate))

    with pytest.raises(RoutingMigrationError, match="Backfill import failed"):
        executor.run_step(rollout(), max_documents=2)

    result = executor.run_step(rollout(), max_documents=2)
    assert result.complete is True
    assert candidate.documents.values == source.documents.values
    assert len(candidate.documents.import_calls) == 2


def test_parity_reports_missing_unexpected_and_different_documents():
    source = FakeClient([{"id": "1", "name": "one"}, {"id": "2", "name": "two"}])
    candidate = FakeClient(
        [
            {"id": "1", "name": "changed"},
            {"id": "extra", "name": "orphan"},
        ]
    )
    report = RoutingMigrationExecutor(federation(source, candidate)).verify_parity(
        rollout()
    )

    assert report.passed is False
    assert report.digest == ""
    assert report.missing_ids == ("2",)
    assert report.unexpected_ids == ("extra",)
    assert report.different_ids == ("1",)


def test_conflicting_source_copies_fail_closed_before_any_import():
    item = rollout()
    object.__setattr__(
        item,
        "active_policy",
        {
            "collection": "products",
            "rules": [{"field": "region", "value": "eu", "cluster": "cluster-b"}],
            "default_cluster": "cluster-a",
        },
    )
    source_a = FakeClient([{"id": "1", "name": "old"}])
    source_b = FakeClient([{"id": "1", "name": "new"}])
    service = FederationService()
    service.clients = {"cluster-a": source_a, "cluster-b": source_b}

    with pytest.raises(RoutingMigrationError, match="Conflicting copies"):
        RoutingMigrationExecutor(service).run_step(item)
