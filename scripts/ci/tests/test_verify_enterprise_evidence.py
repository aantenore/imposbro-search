from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location(
    "verify_enterprise_evidence",
    ROOT / "scripts" / "ci" / "verify_enterprise_evidence.py",
)
verify = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(verify)

COMMIT = "a" * 40


def e2e_fixture():
    assertions = {scenario: {"executed": True} for scenario in verify.E2E_REQUIRED_IDS}
    assertions["oidc_tenant_isolation"] = {
        "oidc_rs256_jwks_tls": True,
        "tenants_exercised": 2,
        "tenant_scoped_search_exact": True,
        "cross_tenant_read_http_statuses": [404, 404],
        "cross_tenant_write_http_status": 403,
        "missing_tenant_claim_http_status": 403,
        "cross_tenant_delete_was_filtered_noop": True,
        "same_tenant_document_lifecycle": True,
        "missing_collection_policy_http_status": 403,
        "deny_by_default_policy_verified": True,
    }
    assertions["postgres_migration_cas_and_sequence"] = {
        "alembic_head_verified": True,
        "cas_commits": 1,
        "cas_conflicts": 7,
        "audit_chain_verified": True,
    }
    assertions["typesense_secret_ref_rotation"] = {
        "providers_exercised": ["env", "file"],
        "file_rotation_without_state_reload": True,
        "control_plane_revision_unchanged": True,
        "authenticated_internal_transit_materialized": True,
        "revoked_key_http_status": 401,
        "revoked_key_failed_closed": True,
        "replacement_key_passed": True,
        "disposable_rotation_keys_removed": True,
        "postgres_state_contains_refs_only": True,
        "state_export_contains_refs_only": True,
        "raw_typesense_secret_absent_from_application_logs": True,
        "application_logs_scanned": ["indexing-service", "query-a", "query-b"],
    }
    assertions["kafka_worker_v2_delivery"] = {
        "checkpoint_backend": "postgres",
        "outbox_rows_after_retry": 1,
        "idempotency_conflict_http_status": 409,
    }
    assertions["otlp_w3c_trace_continuity"] = {
        "collector_tls_verified": True,
        "parent_child_lineage_verified": True,
        "span_chain": [
            "POST /ingest/example",
            "kafka.publish",
            "kafka.process",
            "typesense.document.upsert",
        ],
    }
    assertions["typesense_dual_write_backfill_cutover_rollback"] = {
        "machine_owned_backfill_and_parity": True,
        "backfill_checkpoint_persisted": True,
        "dual_write_targets": 2,
        "rollback_read_active": True,
    }
    assertions["typesense_tls_transport"] = {
        "certificate_chain_verified": True,
        "hostname_verified": True,
    }
    return {
        "schema_version": "1.1",
        "status": "passed",
        "finished_at": "2026-07-10T00:01:00Z",
        "provenance": {
            "required_scenarios": list(verify.E2E_REQUIRED_IDS),
            "dependency_mode": "compose",
            "production_certification": False,
            "git_commit": COMMIT,
            "git_dirty": False,
            "capabilities": {"docker_daemon_available": True},
        },
        "scenarios": [
            {"id": scenario, "status": "passed", "assertions": assertions[scenario]}
            for scenario in verify.E2E_REQUIRED_IDS
        ],
    }


def dr_fixture():
    audit = {
        "audit_rows": 2,
        "audit_head_sequence": 2,
        "audit_head_hash": "b" * 64,
    }
    return {
        "schema": "imposbro.dr-drill-evidence.v1",
        "outcome": "passed",
        "scope": {"production_certification": False},
        "timing": {"rpo": {"passed": True}, "rto": {"passed": True}},
        "backup": {
            "encryption": "age",
            "ciphertext_rejected_as_plaintext_archive": True,
        },
        "restore": {
            "target_was_empty": True,
            "transaction_guarded": True,
            "plaintext_archive_checksum_match": True,
            "fingerprints_match": True,
            "recovery_marker": {
                **audit,
                "audit_delivery_rows": 1,
                "audit_delivery_digest": "c" * 32,
                "deletion_ledger_rows": 1,
                "deletion_ledger_digest": "d" * 32,
                "deletion_target_rows": 1,
                "deletion_target_digest": "e" * 32,
            },
        },
        "retention": {
            "live_postgresql_executed": True,
            "dry_run_executed_first": True,
            "unpublished_rows_deleted": 0,
            "legal_hold_identity_rows_deleted": 0,
            "latest_mutations_deleted": 0,
            "audit_chain_touched": False,
            "audit_chain_unchanged": True,
            "before": dict(audit),
            "after": {**audit, "all_invariants_pass": True},
        },
        "cleanup": {
            "containers_and_volumes_destroyed": True,
            "decrypted_dump_persisted": False,
            "age_identity_persisted_in_repository": False,
        },
        "provenance": {
            "git_commit": COMMIT,
            "dirty_worktree": False,
            "evidence_contains_credentials": False,
        },
    }


def kind_fixture():
    return {
        "evidence_type": "kind-enterprise-smoke",
        "production_certification": False,
        "status": "passed",
        "required_assertions": list(verify.KIND_REQUIRED_IDS),
        "assertions": [
            {"id": assertion, "status": "passed"}
            for assertion in verify.KIND_REQUIRED_IDS
        ],
        "cleanup": {
            "status": "completed",
            "cluster_deleted": True,
            "registry_deleted": True,
            "network_deleted": True,
        },
        "run": {"git": {"commit": COMMIT, "dirty": False}},
    }


def benchmark_fixture():
    image = "registry.invalid/image@sha256:" + "c" * 64
    return {
        "evidence_type": "kind-enterprise-benchmark",
        "production_certification": False,
        "status": "passed",
        "slo_violations": [],
        "documents": 2000,
        "metadata": {
            "release": COMMIT,
            "image_set": f"query={image},indexing={image},admin={image}",
        },
        "profile": {
            "production_certification": False,
            "thresholds": {
                "min_ingest_docs_per_second": 25,
                "max_indexing_visible_seconds": 180,
                "max_search_p95_ms": 750,
                "max_search_error_rate": 0,
                "allow_partial": False,
            },
        },
        "git": {"commit": COMMIT, "dirty": False},
    }


def test_all_reviewed_evidence_contracts_pass():
    assert verify.validate_e2e(e2e_fixture(), COMMIT, True) == []
    assert verify.validate_dr(dr_fixture(), COMMIT, True) == []
    assert verify.validate_kind(kind_fixture(), COMMIT, True) == []
    assert verify.validate_benchmark(benchmark_fixture(), COMMIT, True) == []


def test_e2e_rejects_shrunken_or_partial_required_set():
    evidence = e2e_fixture()
    evidence["provenance"]["required_scenarios"].pop()
    evidence["scenarios"].pop()

    errors = verify.validate_e2e(evidence, COMMIT, True)

    assert any("reviewed required scenario set changed" in error for error in errors)


def test_e2e_rejects_secret_ref_evidence_without_log_absence_proof():
    evidence = e2e_fixture()
    scenario = next(
        item
        for item in evidence["scenarios"]
        if item["id"] == "typesense_secret_ref_rotation"
    )
    scenario["assertions"][
        "raw_typesense_secret_absent_from_application_logs"
    ] = False

    errors = verify.validate_e2e(evidence, COMMIT, True)

    assert "e2e: raw Typesense secret reached state, export, or application logs" in errors


def test_dr_rejects_vacuous_audit_preservation():
    evidence = dr_fixture()
    for location in (
        evidence["retention"]["before"],
        evidence["retention"]["after"],
        evidence["restore"]["recovery_marker"],
    ):
        location["audit_rows"] = 0

    errors = verify.validate_dr(evidence, COMMIT, True)

    assert "dr: audit-chain proof is vacuous" in errors


def test_clean_release_gate_rejects_dirty_or_wrong_commit():
    evidence = kind_fixture()
    evidence["run"]["git"] = {"commit": "d" * 40, "dirty": True}

    errors = verify.validate_kind(evidence, COMMIT, True)

    assert "kind: commit does not match candidate" in errors
    assert "kind: release evidence came from a dirty tree" in errors


def test_sidecar_is_bound_to_exact_evidence_bytes(tmp_path: Path):
    evidence = tmp_path / "dr.json"
    evidence.write_text('{"outcome":"passed"}\n', encoding="utf-8")
    sidecar = tmp_path / "dr.json.sha256"
    sidecar.write_text(f"{verify.sha256(evidence)}  dr.json\n", encoding="utf-8")

    assert verify.verify_sidecar(evidence, sidecar) == []

    evidence.write_text('{"outcome":"failed"}\n', encoding="utf-8")
    assert "dr: checksum sidecar does not match evidence" in verify.verify_sidecar(
        evidence, sidecar
    )
