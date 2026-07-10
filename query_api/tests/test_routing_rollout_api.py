"""API and data-plane integration tests for safe routing rollouts."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from control_plane import InMemoryControlPlaneStore
from domain.routing_rollout import RolloutPhase, RoutingRollout, RoutingRolloutGates
from indexing_events import InMemoryIndexingEventStore
from services.federation import FederationService
from services.kafka_producer import KafkaService
from services.state_manager import StateManager


def _policy(collection: str, cluster: str) -> dict:
    return {
        "collection": collection,
        "rules": [],
        "default_cluster": cluster,
    }


def _runtime_with_rollout(phase: str) -> FederationService:
    federation = FederationService()
    federation.clients = {"cluster-a": object(), "cluster-b": object()}
    federation.routing_rules = {"products": _policy("products", "cluster-a")}
    rollout = RoutingRollout(
        rollout_id="rollout-1",
        collection="products",
        active_policy=_policy("products", "cluster-a"),
        candidate_policy=_policy("products", "cluster-b"),
        created_by="oidc:test",
        rollback_window_seconds=300,
    )
    payload = rollout.to_dict()
    payload["phase"] = phase
    federation.routing_rollouts = {rollout.rollout_id: payload}
    return federation


def test_data_plane_uses_phase_specific_read_and_write_policies():
    draft = _runtime_with_rollout("draft")
    assert [name for _, name in draft.get_targets_for_document("products", {"id": "1"})] == [
        "cluster-a"
    ]
    assert [name for name, _ in draft.get_named_clients_for_search("products")] == [
        "cluster-a"
    ]

    dual = _runtime_with_rollout("dual_write")
    assert [name for _, name in dual.get_targets_for_document("products", {"id": "1"})] == [
        "cluster-a",
        "cluster-b",
    ]
    assert [name for name, _ in dual.get_named_clients_for_search("products")] == [
        "cluster-a",
        "cluster-b",
    ]

    cutover = _runtime_with_rollout("cutover")
    assert [name for _, name in cutover.get_targets_for_document("products", {"id": "1"})] == [
        "cluster-a",
        "cluster-b",
    ]
    assert [name for name, _ in cutover.get_named_clients_for_search("products")] == [
        "cluster-b"
    ]

    completed = _runtime_with_rollout("completed")
    assert [
        name
        for _, name in completed.get_targets_for_document("products", {"id": "1"})
    ] == ["cluster-b"]
    assert [name for name, _ in completed.get_named_clients_for_search("products")] == [
        "cluster-b"
    ]


def test_routing_rollout_api_commits_cas_audit_and_gated_transitions(client):
    store = InMemoryControlPlaneStore()
    manager = StateManager(store=store)
    federation = FederationService()
    federation.clients = {"cluster-a": object(), "cluster-b": object()}
    federation.routing_rules = {"products": _policy("products", "cluster-a")}
    federation.collection_schemas = {
        "products": {"name": "products", "fields": []}
    }
    client.app.state.state_manager = manager
    client.app.state.federation_service = federation

    created = client.post(
        "/admin/routing-rollouts",
        headers={"If-Match": '"0"'},
        json={
            "candidate_policy": _policy("products", "cluster-b"),
            "rollback_window_seconds": 300,
        },
    )

    assert created.status_code == 201
    assert created.headers["ETag"] == '"1"'
    rollout = created.json()["rollout"]
    rollout_id = rollout["rollout_id"]
    assert rollout["phase"] == "draft"
    assert created.json()["revision"] == 1
    assert store.list_audit()[0]["action"] == "routing_rollout_created"
    assert store.list_outbox()[0].event_type == "routing.rollout.created"

    validating = client.post(
        f"/admin/routing-rollouts/{rollout_id}/transitions",
        headers={"If-Match": '"1"'},
        json={"target_phase": "validating", "expected_version": 1},
    )
    assert validating.status_code == 200
    assert validating.json()["rollout"]["version"] == 2
    assert validating.json()["revision"] == 2

    missing_gates = client.post(
        f"/admin/routing-rollouts/{rollout_id}/transitions",
        headers={"If-Match": '"2"'},
        json={"target_phase": "dual_write", "expected_version": 2},
    )
    assert missing_gates.status_code == 422
    assert missing_gates.json()["detail"]["code"] == "routing_rollout_gate_failed"
    assert manager.current_revision == 2

    dual_write = client.post(
        f"/admin/routing-rollouts/{rollout_id}/transitions",
        headers={"If-Match": '"2"'},
        json={
            "target_phase": "dual_write",
            "expected_version": 2,
            "gates": {"validation_passed": True, "capacity_passed": True},
        },
    )
    assert dual_write.status_code == 200
    assert dual_write.json()["rollout"]["phase"] == "dual_write"
    assert dual_write.json()["revision"] == 3
    assert [
        name for _, name in federation.get_targets_for_document("products", {"id": "1"})
    ] == ["cluster-a", "cluster-b"]

    forged = client.post(
        f"/admin/routing-rollouts/{rollout_id}/transitions",
        headers={"If-Match": '"3"'},
        json={
            "target_phase": "backfill",
            "expected_version": 3,
            "gates": {
                "backfill_complete": True,
                "source_barrier": "operator:claimed",
                "parity_passed": True,
                "parity_digest": "sha256:forged",
            },
        },
    )
    assert forged.status_code == 422
    assert forged.json()["detail"]["code"] == "routing_rollout_evidence_read_only"
    assert manager.current_revision == 3

    stale = client.post(
        f"/admin/routing-rollouts/{rollout_id}/transitions",
        headers={"If-Match": '"2"'},
        json={"target_phase": "backfill", "expected_version": 3},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "control_plane_revision_conflict"
    assert manager.current_revision == 3


def test_cutover_uses_measured_kafka_and_dlq_evidence(client, monkeypatch):
    store = InMemoryControlPlaneStore()
    manager = StateManager(store=store)
    federation = FederationService()
    federation.clients = {"cluster-a": object(), "cluster-b": object()}
    federation.routing_rules = {"products": _policy("products", "cluster-a")}
    rollout = RoutingRollout(
        rollout_id="rollout-cutover",
        collection="products",
        active_policy=_policy("products", "cluster-a"),
        candidate_policy=_policy("products", "cluster-b"),
        created_by="test",
        rollback_window_seconds=300,
        phase=RolloutPhase.VERIFYING,
        gates=RoutingRolloutGates(
            validation_passed=True,
            capacity_passed=True,
            backfill_complete=True,
            source_barrier="indexing-outbox:1",
            parity_passed=True,
            parity_digest="sha256:verified",
        ),
    )
    federation.routing_rollouts = {rollout.rollout_id: rollout.to_dict()}
    assert manager.save_state(
        federation.clusters_config,
        federation.routing_rules,
        federation.collection_schemas,
        federation.collection_aliases,
        federation.routing_rollouts,
    )
    kafka = KafkaService(
        "localhost:9092",
        "test",
        event_store=InMemoryIndexingEventStore(),
    )
    kafka.operational_evidence = MagicMock(
        return_value=SimpleNamespace(consumer_lag=0, unresolved_dlq=0)
    )
    client.app.state.state_manager = manager
    client.app.state.federation_service = federation
    client.app.state.kafka_service = kafka
    client.app.state.config_notifier = MagicMock()
    monkeypatch.setattr("routers.admin.settings.ROUTING_CUTOVER_MAX_KAFKA_LAG", 0)

    response = client.post(
        "/api/v1/admin/routing-rollouts/rollout-cutover/transitions",
        headers={"If-Match": '"1"'},
        json={"target_phase": "cutover", "expected_version": 1},
    )

    assert response.status_code == 200
    assert response.json()["rollout"]["phase"] == "cutover"
    assert response.json()["rollout"]["gates"]["kafka_lag"] == 0
    kafka.operational_evidence.assert_called_once()


def test_cutover_fails_when_measured_dlq_is_not_empty(client, monkeypatch):
    store = InMemoryControlPlaneStore()
    manager = StateManager(store=store)
    federation = _runtime_with_rollout("verifying")
    rollout = RoutingRollout.from_dict(federation.routing_rollouts["rollout-1"])
    object.__setattr__(
        rollout,
        "gates",
        RoutingRolloutGates(
            backfill_complete=True,
            source_barrier="indexing-outbox:1",
            parity_passed=True,
            parity_digest="sha256:verified",
        ),
    )
    federation.routing_rollouts["rollout-1"] = rollout.to_dict()
    assert manager.save_state(
        federation.clusters_config,
        federation.routing_rules,
        federation.collection_schemas,
        federation.collection_aliases,
        federation.routing_rollouts,
    )
    kafka = KafkaService(
        "localhost:9092",
        "test",
        event_store=InMemoryIndexingEventStore(),
    )
    kafka.operational_evidence = MagicMock(
        return_value=SimpleNamespace(consumer_lag=0, unresolved_dlq=2)
    )
    client.app.state.state_manager = manager
    client.app.state.federation_service = federation
    client.app.state.kafka_service = kafka
    client.app.state.config_notifier = MagicMock()
    monkeypatch.setattr("routers.admin.settings.ROUTING_CUTOVER_MAX_KAFKA_LAG", 0)

    response = client.post(
        "/api/v1/admin/routing-rollouts/rollout-1/transitions",
        headers={"If-Match": '"1"'},
        json={"target_phase": "cutover", "expected_version": 1},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "routing_rollout_gate_failed"
    assert "DLQ" in response.json()["detail"]
    assert manager.current_revision == 1
