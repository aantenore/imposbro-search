from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "outbox_retention.py"
SPEC = importlib.util.spec_from_file_location("outbox_retention", MODULE_PATH)
assert SPEC and SPEC.loader
retention = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = retention
SPEC.loader.exec_module(retention)


def policy_value(*, deny_all: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "schema": "imposbro.outbox-retention-policy.v1",
        "policy_id": "unit-review-001",
        "database_id": "unit-db",
        "database_name": "imposbro_unit",
        "schema_revision": "0003_audit_delivery_deletion",
        "not_before": (now - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "deny_all": deny_all,
        "legal_hold_snapshot": {
            "snapshot_id": "unit-holds-001",
            "captured_at": (now - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            "expires_at": (now + timedelta(minutes=50)).isoformat().replace("+00:00", "Z"),
            "complete": True,
        },
        "allowed_tables": ["control_plane_outbox", "indexing_event_outbox"],
        "tables": {
            "control_plane_outbox": {
                "retention_seconds": 86_400,
                "max_unpublished": 10,
                "held_revisions": [7],
            },
            "indexing_event_outbox": {
                "retention_seconds": 86_400,
                "max_unpublished": 20,
                "held_identity_hashes": ["a" * 64],
            },
        },
    }


def write_policy(path: Path, value: dict | None = None) -> Path:
    path.write_text(json.dumps(value or policy_value()), encoding="utf-8")
    path.chmod(0o600)
    return path


def test_policy_is_strict_bounded_and_confirmation_is_digest_bound(tmp_path: Path) -> None:
    policy_path = write_policy(tmp_path / "policy.json")
    policy = retention.RetentionPolicy.load(policy_path)

    assert policy.allowed_tables == (
        "control_plane_outbox",
        "indexing_event_outbox",
    )
    assert policy.tables["indexing_event_outbox"].held_identity_hashes == ("a" * 64,)
    assert policy.digest == hashlib.sha256(policy_path.read_bytes()).hexdigest()
    assert policy.confirmation == (
        f"APPLY:unit-db:unit-review-001:{policy.digest[:12]}"
    )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update({"schema_revision": "future"}), "unsupported_schema_revision"),
        (lambda value: value.update({"allowed_tables": ["control_plane_audit"]}), "invalid_policy_allowlist"),
        (
            lambda value: value["tables"]["indexing_event_outbox"].update(
                {"retention_seconds": 60}
            ),
            "invalid_policy_schema",
        ),
        (
            lambda value: value["tables"]["indexing_event_outbox"].update(
                {"held_identity_hashes": ["NOT-A-HASH"]}
            ),
            "invalid_policy_schema",
        ),
        (
            lambda value: value["legal_hold_snapshot"].update({"complete": False}),
            "incomplete_legal_hold_snapshot",
        ),
    ],
)
def test_invalid_or_unsafe_policy_fails_closed(tmp_path: Path, mutation, code: str) -> None:
    value = policy_value()
    mutation(value)
    path = write_policy(tmp_path / "policy.json", value)

    with pytest.raises(retention.RetentionError) as captured:
        retention.RetentionPolicy.load(path)
    assert captured.value.code == code


def test_policy_rejects_duplicate_json_keys_and_open_permissions(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text('{"schema":"a","schema":"b"}', encoding="utf-8")
    duplicate.chmod(0o600)
    with pytest.raises(retention.RetentionError, match="strict UTF-8 JSON"):
        retention.RetentionPolicy.load(duplicate)

    open_policy = write_policy(tmp_path / "open.json")
    open_policy.chmod(0o644)
    with pytest.raises(retention.RetentionError) as captured:
        retention.RetentionPolicy.load(open_policy)
    assert captured.value.code == "unsafe_policy_permissions"

    symlink = tmp_path / "policy-link.json"
    symlink.symlink_to(write_policy(tmp_path / "target.json"))
    with pytest.raises(retention.RetentionError) as captured:
        retention.RetentionPolicy.load(symlink)
    assert captured.value.code == "unsafe_policy_file"


def test_delete_sql_has_irreducible_guardrails() -> None:
    for sql in (retention.CONTROL_DELETE_SQL, retention.INDEXING_DELETE_SQL):
        normalized = " ".join(sql.lower().split())
        assert "published_at is not null" in normalized
        assert "exists ( select 1" in normalized
        assert "for update of candidate skip locked" in normalized
        assert "limit %s" in normalized

    indexing = " ".join(retention.INDEXING_DELETE_SQL.lower().split())
    assert "newer.identity_hash = victim.identity_hash" in indexing
    assert "newer.sequence > victim.sequence" in indexing
    assert "control_plane_audit" not in retention.CONTROL_DELETE_SQL


def test_evidence_and_checksum_are_atomic_private_and_payload_free(tmp_path: Path) -> None:
    path = tmp_path / "retention.json"
    evidence = {
        "schema": retention.EVIDENCE_SCHEMA,
        "outcome": "success",
        "payloads_recorded": False,
    }
    digest, checksum = retention.write_evidence(path, evidence)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(checksum.stat().st_mode) == 0o600
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert checksum.read_text(encoding="ascii") == f"{digest}  retention.json\n"
    assert "document" not in path.read_text(encoding="utf-8").lower()

    with pytest.raises(retention.RetentionError) as captured:
        retention.write_evidence(path, evidence)
    assert captured.value.code == "evidence_exists"


def test_apply_requires_reviewed_digest_and_writes_sanitized_failure_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    policy_path = write_policy(tmp_path / "policy.json")
    evidence_path = tmp_path / "failure.json"
    monkeypatch.delenv("RETENTION_CONFIRMATION", raising=False)

    exit_code = retention.main(
        [
            "--policy",
            str(policy_path),
            "--evidence",
            str(evidence_path),
            "--apply",
            "--policy-sha256",
            "0" * 64,
        ]
    )

    assert exit_code == 1
    value = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert value["outcome"] == "failed"
    assert value["error_code"] == "policy_digest_mismatch"
    assert value["safety"]["audit_chain_touched"] is False
    assert value["safety"]["payloads_recorded"] is False
    assert "RETENTION_DATABASE_URL" not in evidence_path.read_text(encoding="utf-8")


def test_deny_all_blocks_before_database_access(tmp_path: Path) -> None:
    policy_path = write_policy(tmp_path / "policy.json", policy_value(deny_all=True))
    evidence_path = tmp_path / "denied.json"

    exit_code = retention.main(
        ["--policy", str(policy_path), "--evidence", str(evidence_path)]
    )

    assert exit_code == 1
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["error_code"] == "policy_denies_retention"
    assert evidence["provenance"] == {}


def test_dr_fixture_contains_a_nonempty_coherent_audit_chain() -> None:
    zero = "0" * 64
    first = {
        "id": "10000000-0000-0000-0000-000000000001",
        "sequence": 1,
        "revision": 3,
        "timestamp": "2026-07-10T10:00:00+00:00",
        "actor": "dr-fixture",
        "action": "fixture.created",
        "resource_type": "dr_fixture",
        "resource_id": "fixture-1",
        "status": "success",
        "request_id": "dr-audit-1",
        "details": {"fixture": "audit-chain", "sensitive": False},
        "previous_hash": zero,
    }
    first_hash = hashlib.sha256(
        json.dumps(first, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    second = {
        **first,
        "id": "10000000-0000-0000-0000-000000000002",
        "sequence": 2,
        "timestamp": "2026-07-10T10:00:01+00:00",
        "action": "fixture.verified",
        "request_id": "dr-audit-2",
        "previous_hash": first_hash,
    }
    second_hash = hashlib.sha256(
        json.dumps(second, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    assert first_hash == "cc4a13c94d902f3329696e783c34e2220ff1519cb379d904a3e0d20fc7573ebd"
    assert second_hash == "edc8b00f1b2b1ff8ad4f5eebc41b73ee1444da9aa186704fc1bb4c82cd27311c"
    seed = (MODULE_PATH.parents[2] / "ops" / "dr" / "seed.sql").read_text(
        encoding="utf-8"
    )
    assert seed.count(first_hash) >= 2
    assert seed.count(second_hash) >= 2

    verifier = (
        MODULE_PATH.parents[2] / "ops" / "dr" / "verify-control-plane.py"
    ).read_text(encoding="utf-8")
    assert "store.load()" in verifier
    assert "store.verify_audit_chain()" in verifier
