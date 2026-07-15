"""Domain tests for safe routing policy migration."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

from domain.routing_rollout import (
    InvalidRolloutTransition,
    RolloutGateError,
    RolloutPhase,
    RolloutVersionConflict,
    RoutingRollout,
    RoutingRolloutGates,
)


def rollout() -> RoutingRollout:
    return RoutingRollout(
        rollout_id="rollout-1",
        collection="products",
        active_policy={"default_cluster": "cluster-a", "rules": []},
        candidate_policy={"default_cluster": "cluster-b", "rules": []},
        created_by="oidc:test",
        rollback_window_seconds=300,
    )


def test_happy_path_requires_release_evidence_and_preserves_rollback_window():
    item = rollout()
    item = item.transition(RolloutPhase.VALIDATING, expected_version=1)
    gates = RoutingRolloutGates(validation_passed=True, capacity_passed=True)
    item = item.transition(
        RolloutPhase.DUAL_WRITE,
        expected_version=2,
        gates=gates,
    )
    assert item.policy_modes() == (("active", "candidate"), ("active", "candidate"))

    item = item.transition(RolloutPhase.BACKFILL, expected_version=3)
    gates = RoutingRolloutGates(
        validation_passed=True,
        capacity_passed=True,
        backfill_complete=True,
        source_barrier="kafka:topic:42",
    )
    item = item.transition(
        RolloutPhase.VERIFYING,
        expected_version=4,
        gates=gates,
        checkpoint={"last_id": "p-100"},
    )
    gates = RoutingRolloutGates(
        validation_passed=True,
        capacity_passed=True,
        backfill_complete=True,
        source_barrier="kafka:topic:42",
        parity_passed=True,
        parity_digest="sha256:abc",
        kafka_lag=0,
        max_kafka_lag=0,
        unresolved_dlq=0,
    )
    cutover_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
    item = item.transition(
        RolloutPhase.CUTOVER,
        expected_version=5,
        gates=gates,
        now=cutover_at,
    )
    assert item.policy_modes() == (("candidate",), ("active", "candidate"))
    item = item.transition(RolloutPhase.DRAIN, expected_version=6)

    with pytest.raises(RolloutGateError, match="rollback window"):
        item.transition(
            RolloutPhase.COMPLETED,
            expected_version=7,
            now=cutover_at + timedelta(seconds=299),
        )

    item = item.transition(
        RolloutPhase.COMPLETED,
        expected_version=7,
        now=cutover_at + timedelta(seconds=300),
    )
    assert item.policy_modes() == (("candidate",), ("candidate",))


def test_version_conflict_and_illegal_transition_fail_closed():
    item = rollout()
    with pytest.raises(RolloutVersionConflict):
        item.transition(RolloutPhase.VALIDATING, expected_version=99)
    with pytest.raises(InvalidRolloutTransition):
        item.transition(RolloutPhase.CUTOVER, expected_version=1)


@pytest.mark.parametrize(
    ("phase", "target"),
    [
        (RolloutPhase.VALIDATING, RolloutPhase.DUAL_WRITE),
        (RolloutPhase.BACKFILL, RolloutPhase.VERIFYING),
        (RolloutPhase.VERIFYING, RolloutPhase.CUTOVER),
    ],
)
def test_safety_gates_block_progress_without_evidence(phase, target):
    item = rollout()
    object.__setattr__(item, "phase", phase)
    with pytest.raises(RolloutGateError):
        item.transition(target, expected_version=item.version)


@pytest.mark.parametrize(
    "phase",
    [
        RolloutPhase.DUAL_WRITE,
        RolloutPhase.BACKFILL,
        RolloutPhase.VERIFYING,
        RolloutPhase.CUTOVER,
        RolloutPhase.DRAIN,
        RolloutPhase.FAILED,
    ],
)
def test_every_side_effecting_phase_can_enter_rollback(phase):
    item = rollout()
    object.__setattr__(item, "phase", phase)
    rolled = item.transition(RolloutPhase.ROLLING_BACK, expected_version=1)
    completed = rolled.transition(RolloutPhase.ROLLED_BACK, expected_version=2)
    assert completed.policy_modes() == (("active",), ("active",))


def test_serialization_round_trip_preserves_phase_gates_and_timestamps():
    item = rollout().transition(RolloutPhase.VALIDATING, expected_version=1)
    restored = RoutingRollout.from_dict(item.to_dict())
    assert restored == item


def test_unique_targets_keeps_stable_first_occurrence_order():
    assert RoutingRollout.unique_targets(["a", "b", "a", "c"]) == ("a", "b", "c")


def test_only_reconciler_progress_methods_create_backfill_and_parity_evidence():
    item = rollout()
    object.__setattr__(item, "phase", RolloutPhase.BACKFILL)
    item = item.record_backfill_progress(
        expected_version=1,
        checkpoint={"last_document_id": "p-100"},
        source_barrier="indexing-outbox:42",
        complete=True,
    )
    assert item.version == 2
    assert item.gates.backfill_complete is True
    assert item.gates.source_barrier == "indexing-outbox:42"

    item = item.transition(RolloutPhase.VERIFYING, expected_version=2)
    item = item.record_parity_evidence(
        expected_version=3,
        passed=True,
        digest="sha256:abc",
        checkpoint={"verified_barrier": 42},
    )
    assert item.version == 4
    assert item.gates.parity_passed is True
    assert item.gates.parity_digest == "sha256:abc"


def test_reconciler_evidence_methods_are_phase_and_version_fenced():
    item = rollout()
    with pytest.raises(InvalidRolloutTransition):
        item.record_backfill_progress(
            expected_version=1,
            checkpoint={},
            source_barrier="indexing-outbox:1",
            complete=False,
        )
    object.__setattr__(item, "phase", RolloutPhase.BACKFILL)
    with pytest.raises(RolloutVersionConflict):
        item.record_backfill_progress(
            expected_version=99,
            checkpoint={},
            source_barrier="indexing-outbox:1",
            complete=False,
        )
