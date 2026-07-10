#!/usr/bin/env python3
"""Fail closed over the composite enterprise assurance evidence set."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


E2E_REQUIRED_IDS = [
    "static_harness_validation",
    "compose_stack_bootstrap",
    "platform_readiness",
    "oidc_tenant_isolation",
    "postgres_migration_cas_and_sequence",
    "typesense_secret_ref_rotation",
    "config_mutation_without_notification",
    "config_convergence_without_redis",
    "restart_convergence_without_redis",
    "kafka_worker_v2_delivery",
    "otlp_w3c_trace_continuity",
    "typesense_dual_write_backfill_cutover_rollback",
    "typesense_tls_transport",
]

KIND_REQUIRED_IDS = [
    "preflight",
    "cluster_created",
    "immutable_application_images",
    "dependencies_ready",
    "helm_install_ready",
    "migration_init_completed",
    "secure_runtime_context",
    "network_tls_controls",
    "api_data_plane_smoke",
    "performance_profile_gate",
    "tls_edge_verified",
    "single_pod_restart",
    "rolling_restart",
    "pdb_eviction_blocked",
    "workload_node_drain",
    "cleanup",
]

DIGEST = re.compile(r"@sha256:[a-f0-9]{64}(?:,|$)")


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("root must be an object")
    return value


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(errors: list[str], condition: bool, message: str) -> None:
    if not condition:
        errors.append(message)


def validate_provenance(
    errors: list[str],
    *,
    source: str,
    commit: Any,
    dirty: Any,
    expected_commit: str,
    require_clean: bool,
) -> None:
    require(errors, commit == expected_commit, f"{source}: commit does not match candidate")
    require(errors, isinstance(dirty, bool), f"{source}: dirty flag is not boolean")
    if require_clean:
        require(errors, dirty is False, f"{source}: release evidence came from a dirty tree")


def validate_e2e(
    value: dict[str, Any], expected_commit: str, require_clean: bool
) -> list[str]:
    errors: list[str] = []
    provenance = value.get("provenance", {})
    scenarios = value.get("scenarios", [])
    required = provenance.get("required_scenarios")
    actual = [item.get("id") for item in scenarios if isinstance(item, dict)]
    require(errors, value.get("schema_version") == "1.1", "e2e: schema must be 1.1")
    require(errors, value.get("status") == "passed", "e2e: top-level status is not passed")
    require(errors, value.get("finished_at") is not None, "e2e: run is not finalized")
    require(errors, required == E2E_REQUIRED_IDS, "e2e: reviewed required scenario set changed")
    require(errors, actual == required, "e2e: actual scenario IDs do not exactly match required IDs")
    require(
        errors,
        len(scenarios) == len(E2E_REQUIRED_IDS)
        and all(item.get("status") == "passed" for item in scenarios),
        "e2e: every required scenario must pass",
    )
    require(
        errors,
        len(scenarios) == len(E2E_REQUIRED_IDS)
        and all(bool(item.get("assertions")) for item in scenarios),
        "e2e: passed scenarios require non-empty machine assertions",
    )
    require(
        errors,
        provenance.get("dependency_mode") == "compose",
        "e2e: live compose dependency mode is required",
    )
    require(
        errors,
        provenance.get("production_certification") is False,
        "e2e: repository evidence must not claim production certification",
    )
    require(
        errors,
        provenance.get("capabilities", {}).get("docker_daemon_available") is True,
        "e2e: Docker runtime capability was not captured",
    )
    scenario_map = {
        item.get("id"): item.get("assertions", {})
        for item in scenarios
        if isinstance(item, dict)
    }
    tenant = scenario_map.get("oidc_tenant_isolation", {})
    require(
        errors,
        tenant.get("oidc_rs256_jwks_tls") is True,
        "e2e: OIDC RS256/JWKS TLS validation was not exercised",
    )
    require(
        errors,
        tenant.get("tenants_exercised") == 2
        and tenant.get("tenant_scoped_search_exact") is True,
        "e2e: two-tenant search isolation was not proven",
    )
    require(
        errors,
        tenant.get("cross_tenant_read_http_statuses") == [404, 404]
        and tenant.get("cross_tenant_write_http_status") == 403
        and tenant.get("missing_tenant_claim_http_status") == 403,
        "e2e: cross-tenant or missing-claim access did not fail closed",
    )
    require(
        errors,
        tenant.get("cross_tenant_delete_was_filtered_noop") is True
        and tenant.get("same_tenant_document_lifecycle") is True,
        "e2e: tenant-safe update/delete lifecycle was not proven",
    )
    require(
        errors,
        tenant.get("missing_collection_policy_http_status") == 403
        and tenant.get("deny_by_default_policy_verified") is True,
        "e2e: missing collection policy did not fail closed",
    )
    postgres = scenario_map.get("postgres_migration_cas_and_sequence", {})
    require(errors, postgres.get("alembic_head_verified") is True, "e2e: migration head was not verified")
    require(errors, postgres.get("cas_commits") == 1 and postgres.get("cas_conflicts") == 7, "e2e: concurrent CAS evidence is invalid")
    require(errors, postgres.get("audit_chain_verified") is True, "e2e: audit chain was not verified")
    secret_ref = scenario_map.get("typesense_secret_ref_rotation", {})
    require(
        errors,
        secret_ref.get("providers_exercised") == ["env", "file"],
        "e2e: env/file Typesense secret providers were not both exercised",
    )
    require(
        errors,
        secret_ref.get("file_rotation_without_state_reload") is True
        and secret_ref.get("control_plane_revision_unchanged") is True
        and secret_ref.get("authenticated_internal_transit_materialized") is True,
        "e2e: Typesense file secret rotation mutated or reloaded control-plane state",
    )
    require(
        errors,
        secret_ref.get("revoked_key_failed_closed") is True
        and secret_ref.get("revoked_key_http_status") in {401, 403}
        and secret_ref.get("replacement_key_passed") is True
        and secret_ref.get("disposable_rotation_keys_removed") is True,
        "e2e: Typesense key revocation/replacement lifecycle was not proven",
    )
    require(
        errors,
        secret_ref.get("postgres_state_contains_refs_only") is True
        and secret_ref.get("state_export_contains_refs_only") is True
        and secret_ref.get("raw_typesense_secret_absent_from_application_logs") is True
        and secret_ref.get("application_logs_scanned")
        == ["indexing-service", "query-a", "query-b"],
        "e2e: raw Typesense secret reached state, export, or application logs",
    )
    kafka = scenario_map.get("kafka_worker_v2_delivery", {})
    require(errors, kafka.get("checkpoint_backend") == "postgres", "e2e: worker checkpoint was not PostgreSQL")
    require(errors, kafka.get("outbox_rows_after_retry") == 1, "e2e: idempotent retry duplicated durable event")
    require(errors, kafka.get("idempotency_conflict_http_status") == 409, "e2e: idempotency conflict was not rejected")
    trace = scenario_map.get("otlp_w3c_trace_continuity", {})
    require(errors, trace.get("collector_tls_verified") is True, "e2e: OTLP collector TLS was not verified")
    require(errors, trace.get("parent_child_lineage_verified") is True, "e2e: trace parent chain was not verified")
    span_chain = trace.get("span_chain")
    require(
        errors,
        isinstance(span_chain, list)
        and len(span_chain) == 4
        and str(span_chain[0]).startswith("POST /ingest/")
        and span_chain[1:]
        == ["kafka.publish", "kafka.process", "typesense.document.upsert"],
        "e2e: HTTP-to-Typesense span chain is incomplete",
    )
    routing = scenario_map.get("typesense_dual_write_backfill_cutover_rollback", {})
    require(errors, routing.get("machine_owned_backfill_and_parity") is True, "e2e: routing migration used an operator shortcut")
    require(errors, routing.get("backfill_checkpoint_persisted") is True, "e2e: backfill checkpoint was not persisted")
    require(errors, routing.get("dual_write_targets") == 2, "e2e: dual-write did not cover two clusters")
    require(errors, routing.get("rollback_read_active") is True, "e2e: rollback read path was not restored")
    tls = scenario_map.get("typesense_tls_transport", {})
    require(errors, tls.get("certificate_chain_verified") is True and tls.get("hostname_verified") is True, "e2e: Typesense TLS identity was not verified")
    validate_provenance(
        errors,
        source="e2e",
        commit=provenance.get("git_commit"),
        dirty=provenance.get("git_dirty"),
        expected_commit=expected_commit,
        require_clean=require_clean,
    )
    return errors


def validate_dr(
    value: dict[str, Any], expected_commit: str, require_clean: bool
) -> list[str]:
    errors: list[str] = []
    scope = value.get("scope", {})
    timing = value.get("timing", {})
    backup = value.get("backup", {})
    restore = value.get("restore", {})
    retention = value.get("retention", {})
    before = retention.get("before", {})
    after = retention.get("after", {})
    marker = restore.get("recovery_marker", {})
    cleanup = value.get("cleanup", {})
    provenance = value.get("provenance", {})
    require(errors, value.get("schema") == "imposbro.dr-drill-evidence.v1", "dr: wrong schema")
    require(errors, value.get("outcome") == "passed", "dr: outcome is not passed")
    require(errors, scope.get("production_certification") is False, "dr: invalid certification claim")
    require(errors, timing.get("rpo", {}).get("passed") is True, "dr: RPO gate failed")
    require(errors, timing.get("rto", {}).get("passed") is True, "dr: RTO gate failed")
    require(errors, backup.get("encryption") == "age", "dr: real age encryption was not recorded")
    require(errors, backup.get("ciphertext_rejected_as_plaintext_archive") is True, "dr: ciphertext/plaintext guard missing")
    require(errors, restore.get("target_was_empty") is True, "dr: restore target was not clean")
    require(errors, restore.get("transaction_guarded") is True, "dr: restore was not transactional")
    require(errors, restore.get("plaintext_archive_checksum_match") is True, "dr: decrypted checksum mismatch")
    require(errors, restore.get("fingerprints_match") is True, "dr: source/restore fingerprints differ")
    require(errors, retention.get("live_postgresql_executed") is True, "dr: retention was not live PostgreSQL")
    require(errors, retention.get("dry_run_executed_first") is True, "dr: destructive retention lacked dry-run")
    require(errors, after.get("all_invariants_pass") is True, "dr: retention invariants failed")
    require(errors, retention.get("unpublished_rows_deleted") == 0, "dr: unpublished row was deleted")
    require(errors, retention.get("legal_hold_identity_rows_deleted") == 0, "dr: held row was deleted")
    require(errors, retention.get("latest_mutations_deleted") == 0, "dr: latest mutation was deleted")
    require(errors, retention.get("audit_chain_touched") is False, "dr: retention touched audit chain")
    require(errors, retention.get("audit_chain_unchanged") is True, "dr: audit chain changed")
    require(errors, int(before.get("audit_rows", 0)) >= 2, "dr: audit-chain proof is vacuous")
    require(errors, before.get("audit_rows") == after.get("audit_rows") == marker.get("audit_rows"), "dr: audit row count did not survive retention/restore")
    require(errors, before.get("audit_head_sequence") == after.get("audit_head_sequence") == marker.get("audit_head_sequence"), "dr: audit head sequence did not survive retention/restore")
    require(errors, before.get("audit_head_hash") == after.get("audit_head_hash") == marker.get("audit_head_hash"), "dr: audit head hash did not survive retention/restore")
    require(errors, int(marker.get("audit_delivery_rows", 0)) >= 1, "dr: audit delivery checkpoint did not survive restore")
    require(errors, bool(marker.get("audit_delivery_digest")), "dr: audit delivery fingerprint is missing")
    require(errors, int(marker.get("deletion_ledger_rows", 0)) >= 1, "dr: deletion suppression ledger did not survive restore")
    require(errors, int(marker.get("deletion_target_rows", 0)) >= 1, "dr: deletion target receipts did not survive restore")
    require(
        errors,
        bool(marker.get("deletion_ledger_digest")) and bool(marker.get("deletion_target_digest")),
        "dr: deletion suppression fingerprints are missing",
    )
    require(errors, cleanup.get("containers_and_volumes_destroyed") is True, "dr: disposable resources remain")
    require(errors, cleanup.get("decrypted_dump_persisted") is False, "dr: decrypted dump persisted")
    require(errors, cleanup.get("age_identity_persisted_in_repository") is False, "dr: age identity persisted")
    require(errors, provenance.get("evidence_contains_credentials") is False, "dr: evidence may contain credentials")
    validate_provenance(
        errors,
        source="dr",
        commit=provenance.get("git_commit"),
        dirty=provenance.get("dirty_worktree"),
        expected_commit=expected_commit,
        require_clean=require_clean,
    )
    return errors


def validate_kind(
    value: dict[str, Any], expected_commit: str, require_clean: bool
) -> list[str]:
    errors: list[str] = []
    required = value.get("required_assertions")
    assertions = value.get("assertions", [])
    actual = [item.get("id") for item in assertions if isinstance(item, dict)]
    cleanup = value.get("cleanup", {})
    run = value.get("run", {})
    require(errors, value.get("evidence_type") == "kind-enterprise-smoke", "kind: wrong evidence type")
    require(errors, value.get("production_certification") is False, "kind: invalid certification claim")
    require(errors, value.get("status") == "passed", "kind: status is not passed")
    require(errors, required == KIND_REQUIRED_IDS, "kind: reviewed required assertion set changed")
    require(errors, actual == required, "kind: actual assertions do not exactly match required assertions")
    require(errors, len(assertions) == len(KIND_REQUIRED_IDS) and all(item.get("status") == "passed" for item in assertions), "kind: every required assertion must pass")
    require(errors, cleanup.get("status") == "completed", "kind: cleanup did not complete")
    for resource in ("cluster_deleted", "registry_deleted", "network_deleted"):
        require(errors, cleanup.get(resource) is True, f"kind: {resource} is not true")
    git = run.get("git", {})
    validate_provenance(
        errors,
        source="kind",
        commit=git.get("commit"),
        dirty=git.get("dirty"),
        expected_commit=expected_commit,
        require_clean=require_clean,
    )
    return errors


def validate_benchmark(
    value: dict[str, Any], expected_commit: str, require_clean: bool
) -> list[str]:
    errors: list[str] = []
    metadata = value.get("metadata", {})
    profile = value.get("profile", {})
    git = value.get("git", {})
    require(errors, value.get("evidence_type") == "kind-enterprise-benchmark", "benchmark: wrong evidence type")
    require(errors, value.get("production_certification") is False, "benchmark: invalid certification claim")
    require(errors, value.get("status") == "passed", "benchmark: status is not passed")
    require(errors, value.get("slo_violations") == [], "benchmark: SLO violations are present")
    require(errors, profile.get("production_certification") is False, "benchmark: profile certification boundary missing")
    require(errors, int(value.get("documents", 0)) >= 1000, "benchmark: workload is not production-shaped enough")
    thresholds = profile.get("thresholds", {})
    require(errors, thresholds.get("min_ingest_docs_per_second", 0) > 0, "benchmark: throughput threshold missing")
    require(errors, thresholds.get("max_indexing_visible_seconds", 0) > 0, "benchmark: convergence threshold missing")
    require(errors, thresholds.get("max_search_p95_ms", 0) > 0, "benchmark: latency threshold missing")
    require(errors, thresholds.get("max_search_error_rate") == 0, "benchmark: error-rate threshold must be zero")
    require(errors, thresholds.get("allow_partial") is False, "benchmark: partial responses must fail")
    image_set = str(metadata.get("image_set", ""))
    require(errors, len(DIGEST.findall(image_set)) >= 3, "benchmark: immutable measured image set is incomplete")
    require(errors, metadata.get("release") == expected_commit, "benchmark: release metadata does not match candidate")
    validate_provenance(
        errors,
        source="benchmark",
        commit=git.get("commit"),
        dirty=git.get("dirty"),
        expected_commit=expected_commit,
        require_clean=require_clean,
    )
    return errors


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def verify_sidecar(path: Path, sidecar: Path) -> list[str]:
    errors: list[str] = []
    expected = sidecar.read_text(encoding="utf-8").split()[0].lower()
    require(errors, re.fullmatch(r"[a-f0-9]{64}", expected) is not None, "dr: checksum sidecar is malformed")
    require(errors, expected == sha256(path), "dr: checksum sidecar does not match evidence")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--e2e", type=Path, required=True)
    parser.add_argument("--dr", type=Path, required=True)
    parser.add_argument("--dr-sha256", type=Path)
    parser.add_argument("--kind", type=Path, required=True)
    parser.add_argument("--benchmark", type=Path, required=True)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--allow-dirty", action="store_true")
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-markdown", type=Path, required=True)
    args = parser.parse_args()

    paths = {"e2e": args.e2e, "dr": args.dr, "kind": args.kind, "benchmark": args.benchmark}
    validators = {"e2e": validate_e2e, "dr": validate_dr, "kind": validate_kind, "benchmark": validate_benchmark}
    checks = []
    errors: list[str] = []
    for name, path in paths.items():
        try:
            value = load_json(path)
            item_errors = validators[name](value, args.expected_commit, not args.allow_dirty)
            checks.append({"id": name, "path": path.name, "sha256": sha256(path), "status": "passed" if not item_errors else "failed"})
            errors.extend(item_errors)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            checks.append({"id": name, "path": path.name, "status": "failed"})
            errors.append(f"{name}: unreadable evidence ({type(exc).__name__})")

    dr_sidecar = args.dr_sha256 or Path(f"{args.dr}.sha256")
    try:
        errors.extend(verify_sidecar(args.dr, dr_sidecar))
    except (OSError, IndexError):
        errors.append("dr: checksum sidecar is missing or unreadable")

    report = {
        "schema": "imposbro.enterprise-assurance.v1",
        "status": "passed" if not errors else "failed",
        "production_certification": False,
        "expected_commit": args.expected_commit,
        "clean_evidence_required": not args.allow_dirty,
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "checks": checks,
        "errors": errors,
    }
    atomic_write(args.output_json, json.dumps(report, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Enterprise assurance gate",
        "",
        f"- Status: **{report['status'].upper()}**",
        f"- Candidate commit: `{args.expected_commit}`",
        "- Production certification: **false**",
        "",
        "| Evidence | Status | SHA-256 |",
        "| --- | --- | --- |",
    ]
    for check in checks:
        lines.append(f"| {check['id']} | {check['status']} | `{check.get('sha256', 'unavailable')}` |")
    lines.extend(["", "## Failures", ""])
    lines.extend([f"- {error}" for error in errors] or ["No composite-gate failures."])
    lines.extend(["", "This report proves the repository-owned disposable assurance profiles only; deployment certification remains environment-owned.", ""])
    atomic_write(args.output_markdown, "\n".join(lines))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
