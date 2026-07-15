"""API workflow tests for machine-owned backfill and parity evidence."""

import json
from unittest.mock import MagicMock

from control_plane import InMemoryControlPlaneStore
from domain.routing_rollout import RolloutPhase, RoutingRollout, RoutingRolloutGates
from indexing_events import InMemoryIndexingEventStore
from services.federation import FederationService
from services.kafka_producer import KafkaService
from services.state_manager import StateManager


class FakeDocuments:
    def __init__(self, initial=None):
        self.values = {item["id"]: dict(item) for item in (initial or [])}

    def export(self):
        return "\n".join(json.dumps(item) for item in self.values.values())

    def import_(self, documents, _params):
        for document in documents:
            self.values[document["id"]] = dict(document)
        return [{"success": True, "id": item["id"]} for item in documents]


class FakeCollections:
    def __init__(self, documents):
        self.documents = documents

    def __getitem__(self, _collection):
        return type("Collection", (), {"documents": self.documents})()


class FakeClient:
    def __init__(self, initial=None):
        self.documents = FakeDocuments(initial)
        self.collections = FakeCollections(self.documents)


class FakeProducer:
    def __init__(self):
        self.sent = []

    def send(self, topic, key, value):
        self.sent.append((topic, key, value))

    def flush(self):
        return None


def _setup(client):
    source = FakeClient([{"id": "1", "name": "one"}, {"id": "2", "name": "two"}])
    candidate = FakeClient()
    federation = FederationService()
    federation.clients = {"cluster-a": source, "cluster-b": candidate}
    federation.routing_rules = {
        "products": {
            "collection": "products",
            "rules": [],
            "default_cluster": "cluster-a",
        }
    }
    rollout = RoutingRollout(
        rollout_id="rollout-1",
        collection="products",
        active_policy=federation.routing_rules["products"],
        candidate_policy={
            "collection": "products",
            "rules": [],
            "default_cluster": "cluster-b",
        },
        created_by="test",
        rollback_window_seconds=300,
        phase=RolloutPhase.BACKFILL,
        gates=RoutingRolloutGates(validation_passed=True, capacity_passed=True),
    )
    federation.routing_rollouts = {rollout.rollout_id: rollout.to_dict()}
    store = InMemoryControlPlaneStore()
    manager = StateManager(store=store)
    assert manager.save_state(
        federation.clusters_config,
        federation.routing_rules,
        federation.collection_schemas,
        federation.collection_aliases,
        federation.routing_rollouts,
    )
    federation.mark_applied_revision(manager.current_revision)
    kafka = KafkaService(
        "localhost:9092",
        "test",
        event_store=InMemoryIndexingEventStore(),
    )
    kafka._producer = FakeProducer()
    client.app.state.federation_service = federation
    client.app.state.state_manager = manager
    client.app.state.kafka_service = kafka
    client.app.state.config_notifier = MagicMock()
    return federation, manager, kafka, source, candidate


def test_backfill_and_parity_evidence_are_measured_not_operator_claimed(client):
    federation, manager, _kafka, source, candidate = _setup(client)

    backfill = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/backfill/steps",
        headers={"If-Match": '"1"'},
        json={"expected_version": 1, "max_documents": 100},
    )

    assert backfill.status_code == 200
    assert backfill.json()["raw_copy_complete"] is True
    assert backfill.json()["rollout"]["gates"]["backfill_complete"] is True
    assert backfill.json()["rollout"]["gates"]["source_barrier"] == (
        "indexing-outbox:0"
    )
    assert candidate.documents.values == source.documents.values
    assert manager.current_revision == 2

    verifying = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/transitions",
        headers={"If-Match": '"2"'},
        json={"target_phase": "verifying", "expected_version": 2},
    )
    assert verifying.status_code == 200
    assert verifying.json()["rollout"]["phase"] == "verifying"

    parity = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/parity-verifications",
        headers={"If-Match": '"3"'},
        json={"expected_version": 3},
    )

    assert parity.status_code == 200
    assert parity.json()["passed"] is True
    assert parity.json()["digest"].startswith("sha256:")
    assert parity.json()["rollout"]["gates"]["parity_passed"] is True
    persisted = RoutingRollout.from_dict(
        federation.routing_rollouts["rollout-1"]
    )
    assert persisted.version == 4
    assert persisted.gates.parity_passed is True


def test_parity_fails_closed_when_candidate_has_an_unexpected_document(client):
    _federation, _manager, _kafka, _source, candidate = _setup(client)
    first = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/backfill/steps",
        headers={"If-Match": '"1"'},
        json={"expected_version": 1},
    )
    assert first.status_code == 200
    transitioned = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/transitions",
        headers={"If-Match": '"2"'},
        json={"target_phase": "verifying", "expected_version": 2},
    )
    assert transitioned.status_code == 200
    candidate.documents.values["orphan"] = {"id": "orphan", "name": "extra"}

    parity = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/parity-verifications",
        headers={"If-Match": '"3"'},
        json={"expected_version": 3},
    )

    assert parity.status_code == 200
    assert parity.json()["passed"] is False
    assert parity.json()["unexpected_ids"] == ["orphan"]
    assert parity.json()["rollout"]["gates"]["parity_passed"] is False


def test_concurrent_live_mutation_is_repaired_with_a_higher_sequence(client):
    _federation, _manager, kafka, _source, candidate = _setup(client)
    first = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/backfill/steps",
        headers={"If-Match": '"1"'},
        json={"expected_version": 1, "max_documents": 1},
    )
    assert first.status_code == 200
    assert first.json()["raw_copy_complete"] is False

    kafka.publish_document(
        collection_name="products",
        document={"id": "2", "name": "two-new"},
        target_clusters=["cluster-a", "cluster-b"],
        tenant_id="tenant-a",
        document_version=1,
        sequence=1,
        routing_revision=2,
        idempotency_key="concurrent-live-event",
        rollout_id="rollout-1",
    )

    copied = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/backfill/steps",
        headers={"If-Match": '"2"'},
        json={"expected_version": 2, "max_documents": 1},
    )
    assert copied.status_code == 200
    assert copied.json()["raw_copy_complete"] is True
    assert copied.json()["rollout"]["gates"]["backfill_complete"] is False
    assert candidate.documents.values["2"]["name"] == "two"

    repaired = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/backfill/steps",
        headers={"If-Match": '"3"'},
        json={"expected_version": 3, "max_documents": 10},
    )
    assert repaired.status_code == 200
    assert repaired.json()["rollout"]["gates"]["backfill_complete"] is True
    repair_payload = kafka._producer.sent[-1][2]
    assert repair_payload["document"] == {"id": "2", "name": "two-new"}
    assert repair_payload["sequence"] == 2
    assert repair_payload["target_clusters"] == ["cluster-b"]
