"""Pure state machine for zero-hidden-document routing migrations."""

from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, Optional, Tuple


class RolloutPhase(str, Enum):
    DRAFT = "draft"
    VALIDATING = "validating"
    DUAL_WRITE = "dual_write"
    BACKFILL = "backfill"
    VERIFYING = "verifying"
    CUTOVER = "cutover"
    DRAIN = "drain"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"


class InvalidRolloutTransition(ValueError):
    """Raised when a requested lifecycle transition is not allowed."""


class RolloutVersionConflict(RuntimeError):
    """Raised when a transition uses a stale rollout version."""

    def __init__(self, expected_version: int, actual_version: int):
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__(
            f"Routing rollout version conflict: expected {expected_version}, "
            f"actual {actual_version}"
        )


class RolloutGateError(ValueError):
    """Raised when required release evidence is missing or outside budget."""


@dataclass(frozen=True)
class RoutingRolloutGates:
    validation_passed: bool = False
    capacity_passed: bool = False
    backfill_complete: bool = False
    source_barrier: str = ""
    parity_passed: bool = False
    parity_digest: str = ""
    unresolved_dlq: int = 0
    kafka_lag: int = 0
    max_kafka_lag: int = 0


_ALLOWED_TRANSITIONS = {
    RolloutPhase.DRAFT: {RolloutPhase.VALIDATING, RolloutPhase.CANCELLED},
    RolloutPhase.VALIDATING: {
        RolloutPhase.DUAL_WRITE,
        RolloutPhase.FAILED,
        RolloutPhase.CANCELLED,
    },
    RolloutPhase.DUAL_WRITE: {
        RolloutPhase.BACKFILL,
        RolloutPhase.ROLLING_BACK,
        RolloutPhase.FAILED,
    },
    RolloutPhase.BACKFILL: {
        RolloutPhase.VERIFYING,
        RolloutPhase.ROLLING_BACK,
        RolloutPhase.FAILED,
    },
    RolloutPhase.VERIFYING: {
        RolloutPhase.BACKFILL,
        RolloutPhase.CUTOVER,
        RolloutPhase.ROLLING_BACK,
        RolloutPhase.FAILED,
    },
    RolloutPhase.CUTOVER: {
        RolloutPhase.DRAIN,
        RolloutPhase.ROLLING_BACK,
        RolloutPhase.FAILED,
    },
    RolloutPhase.DRAIN: {
        RolloutPhase.COMPLETED,
        RolloutPhase.ROLLING_BACK,
        RolloutPhase.FAILED,
    },
    RolloutPhase.FAILED: {RolloutPhase.ROLLING_BACK},
    RolloutPhase.ROLLING_BACK: {RolloutPhase.ROLLED_BACK, RolloutPhase.FAILED},
    RolloutPhase.COMPLETED: set(),
    RolloutPhase.CANCELLED: set(),
    RolloutPhase.ROLLED_BACK: set(),
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RoutingRollout:
    """Revisioned routing policy transition with explicit safety evidence."""

    rollout_id: str
    collection: str
    active_policy: Dict[str, Any]
    candidate_policy: Dict[str, Any]
    created_by: str
    rollback_window_seconds: int
    version: int = 1
    phase: RolloutPhase = RolloutPhase.DRAFT
    gates: RoutingRolloutGates = field(default_factory=RoutingRolloutGates)
    backfill_checkpoint: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=_utc_now)
    updated_at: datetime = field(default_factory=_utc_now)
    cutover_at: Optional[datetime] = None
    rollback_deadline: Optional[datetime] = None
    failure_reason: str = ""

    def transition(
        self,
        target: RolloutPhase,
        *,
        expected_version: int,
        gates: Optional[RoutingRolloutGates] = None,
        checkpoint: Optional[Dict[str, Any]] = None,
        failure_reason: str = "",
        now: Optional[datetime] = None,
    ) -> "RoutingRollout":
        """Return the next immutable version or fail closed."""
        if expected_version != self.version:
            raise RolloutVersionConflict(expected_version, self.version)
        if target not in _ALLOWED_TRANSITIONS[self.phase]:
            raise InvalidRolloutTransition(
                f"Cannot transition routing rollout from {self.phase.value} "
                f"to {target.value}"
            )

        effective_gates = gates or self.gates
        effective_now = now or _utc_now()
        self._validate_transition_gates(target, effective_gates, effective_now)

        cutover_at = self.cutover_at
        rollback_deadline = self.rollback_deadline
        if target == RolloutPhase.CUTOVER:
            cutover_at = effective_now
            rollback_deadline = effective_now + timedelta(
                seconds=self.rollback_window_seconds
            )

        return replace(
            self,
            phase=target,
            version=self.version + 1,
            gates=effective_gates,
            backfill_checkpoint=(
                dict(checkpoint)
                if checkpoint is not None
                else dict(self.backfill_checkpoint)
            ),
            updated_at=effective_now,
            cutover_at=cutover_at,
            rollback_deadline=rollback_deadline,
            failure_reason=failure_reason if target == RolloutPhase.FAILED else "",
        )

    def record_backfill_progress(
        self,
        *,
        expected_version: int,
        checkpoint: Dict[str, Any],
        source_barrier: str,
        complete: bool,
        now: Optional[datetime] = None,
    ) -> "RoutingRollout":
        """Persist reconciler-owned progress without trusting operator gate flags."""
        if expected_version != self.version:
            raise RolloutVersionConflict(expected_version, self.version)
        if self.phase != RolloutPhase.BACKFILL:
            raise InvalidRolloutTransition(
                "Backfill progress can only be recorded in the backfill phase"
            )
        if not source_barrier:
            raise RolloutGateError("A durable source barrier is required for backfill")
        gates = replace(
            self.gates,
            backfill_complete=complete,
            source_barrier=source_barrier,
            parity_passed=False,
            parity_digest="",
        )
        return replace(
            self,
            version=self.version + 1,
            gates=gates,
            backfill_checkpoint=dict(checkpoint),
            updated_at=now or _utc_now(),
        )

    def record_parity_evidence(
        self,
        *,
        expected_version: int,
        passed: bool,
        digest: str,
        checkpoint: Dict[str, Any],
        now: Optional[datetime] = None,
    ) -> "RoutingRollout":
        """Persist machine-measured parity and invalidate incomplete evidence."""
        if expected_version != self.version:
            raise RolloutVersionConflict(expected_version, self.version)
        if self.phase != RolloutPhase.VERIFYING:
            raise InvalidRolloutTransition(
                "Parity evidence can only be recorded in the verifying phase"
            )
        if passed and (not self.gates.backfill_complete or not self.gates.source_barrier):
            raise RolloutGateError(
                "Backfill completion and a source barrier are required for parity"
            )
        if passed and not digest:
            raise RolloutGateError("Passing parity evidence requires a digest")
        gates = replace(
            self.gates,
            parity_passed=passed,
            parity_digest=digest if passed else "",
        )
        return replace(
            self,
            version=self.version + 1,
            gates=gates,
            backfill_checkpoint=dict(checkpoint),
            updated_at=now or _utc_now(),
        )

    def _validate_transition_gates(
        self,
        target: RolloutPhase,
        gates: RoutingRolloutGates,
        now: datetime,
    ) -> None:
        if target == RolloutPhase.DUAL_WRITE:
            if not gates.validation_passed or not gates.capacity_passed:
                raise RolloutGateError(
                    "Validation and capacity gates must pass before dual-write"
                )
        if target == RolloutPhase.VERIFYING:
            if not gates.backfill_complete or not gates.source_barrier:
                raise RolloutGateError(
                    "Backfill completion and a source barrier are required before verification"
                )
        if target == RolloutPhase.CUTOVER:
            if not gates.parity_passed or not gates.parity_digest:
                raise RolloutGateError("Exact parity evidence is required before cutover")
            if gates.unresolved_dlq != 0:
                raise RolloutGateError("Unresolved DLQ messages block cutover")
            if gates.kafka_lag > gates.max_kafka_lag:
                raise RolloutGateError("Kafka lag exceeds the cutover budget")
        if target == RolloutPhase.COMPLETED:
            if self.rollback_deadline is None or now < self.rollback_deadline:
                raise RolloutGateError(
                    "The configured rollback window must elapse before completion"
                )
            if gates.unresolved_dlq != 0 or gates.kafka_lag > gates.max_kafka_lag:
                raise RolloutGateError("Lag and DLQ gates must pass before completion")

    def policy_modes(self) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
        """Return policy names used for reads and writes in the current phase."""
        old_only = ("active",)
        candidate_only = ("candidate",)
        union = ("active", "candidate")

        if self.phase in {
            RolloutPhase.DRAFT,
            RolloutPhase.VALIDATING,
            RolloutPhase.CANCELLED,
            RolloutPhase.ROLLED_BACK,
        }:
            return old_only, old_only
        if self.phase in {
            RolloutPhase.DUAL_WRITE,
            RolloutPhase.BACKFILL,
            RolloutPhase.VERIFYING,
            RolloutPhase.ROLLING_BACK,
        }:
            return union, union
        if self.phase in {RolloutPhase.CUTOVER, RolloutPhase.DRAIN}:
            return candidate_only, union
        if self.phase == RolloutPhase.COMPLETED:
            return candidate_only, candidate_only
        # A failure after side effects began must preserve coverage until an
        # operator explicitly rolls back; union is safer than hiding documents.
        if self.phase == RolloutPhase.FAILED:
            return union, union
        raise AssertionError(f"Unhandled rollout phase {self.phase}")

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["phase"] = self.phase.value
        for key in ("created_at", "updated_at", "cutover_at", "rollback_deadline"):
            value = payload[key]
            payload[key] = value.isoformat() if value else None
        return payload

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RoutingRollout":
        values = dict(payload)
        values["phase"] = RolloutPhase(values["phase"])
        values["gates"] = RoutingRolloutGates(**values.get("gates", {}))
        for key in ("created_at", "updated_at", "cutover_at", "rollback_deadline"):
            value = values.get(key)
            values[key] = datetime.fromisoformat(value) if value else None
        return cls(**values)

    @staticmethod
    def unique_targets(targets: Iterable[str]) -> Tuple[str, ...]:
        """Stable de-duplication helper used when active/candidate routes overlap."""
        return tuple(dict.fromkeys(targets))
