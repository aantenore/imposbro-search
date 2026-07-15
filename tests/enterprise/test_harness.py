"""Static regression tests for the enterprise evidence harness."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch


ROOT = Path(__file__).resolve().parents[2]
RUNNER_PATH = ROOT / "tests" / "enterprise" / "run_live.py"
SPEC = importlib.util.spec_from_file_location("enterprise_run_live", RUNNER_PATH)
assert SPEC and SPEC.loader
RUNNER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUNNER)

TEST_CAPABILITIES = {
    "architecture": "test",
    "clients": {},
    "docker_daemon_available": False,
    "docker_runtime": {
        "server_version": "unavailable",
        "api_version": "unavailable",
        "os": "unavailable",
        "architecture": "unavailable",
        "components": [],
    },
    "kubernetes_context_available": False,
}


def initialize_evidence(store, dependency_mode="static"):
    with patch.object(
        RUNNER,
        "environment_capabilities",
        return_value=TEST_CAPABILITIES,
    ), patch.object(
        RUNNER,
        "_git_metadata",
        return_value={"git_commit": "unit-test", "git_dirty": False},
    ):
        return store.initialize(
            dependency_mode=dependency_mode,
            runtime="unit-test",
        )


class EvidenceStoreTests(unittest.TestCase):
    def test_verified_tls_context_rejects_tls_1_0_and_1_1(self):
        context = RUNNER.verified_tls_client_context()

        self.assertGreaterEqual(
            context.minimum_version,
            RUNNER.ssl.TLSVersion.TLSv1_2,
        )
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, RUNNER.ssl.CERT_REQUIRED)

    def test_generated_run_id_is_exported_for_compose_interpolation(self):
        harness = (ROOT / "scripts/e2e/run-enterprise-e2e.sh").read_text()

        assignment = harness.index('E2E_RUN_ID="$RUN_ID"')
        export = harness.index("export RUN_ID E2E_RUN_ID")
        self.assertLess(assignment, export)

    def test_query_and_worker_share_cutover_consumer_group(self):
        compose = (ROOT / "ops/e2e/docker-compose.enterprise.yml").read_text()

        self.assertEqual(
            compose.count(
                "INDEXING_KAFKA_CONSUMER_GROUP_ID: imposbro-enterprise-e2e"
            ),
            2,
        )

    def test_runtime_secret_initializer_writes_without_stdout(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "runtime" / "typesense-api-key"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "tests/enterprise/write_runtime_secret.py")],
                check=True,
                capture_output=True,
                text=True,
                env={
                    **os.environ,
                    "E2E_RUNTIME_SECRET_FILE": str(destination),
                    "E2E_RUNTIME_SECRET_VALUE": "runtime-secret-value",
                    "E2E_RUNTIME_SECRET_WRITER_UID": str(os.getuid()),
                    "E2E_RUNTIME_SECRET_WRITER_GID": str(os.getgid()),
                },
            )
            self.assertEqual(completed.stdout, "")
            self.assertEqual(completed.stderr, "")
            self.assertEqual(
                destination.read_text(encoding="utf-8"),
                "runtime-secret-value\n",
            )
            self.assertEqual(destination.stat().st_mode & 0o777, 0o444)

    def test_docker_runtime_provenance_is_bounded_and_context_free(self):
        server = {
            "Version": "29.5.3",
            "ApiVersion": "1.52",
            "Os": "linux",
            "Arch": "arm64",
            "Components": [
                {"Name": "Engine", "Version": "29.5.3", "Details": {"leak": "x"}},
                {"Name": "containerd", "Version": "2.2.1"},
            ],
        }
        completed = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=json.dumps(server), stderr=""
        )
        with patch.object(RUNNER.subprocess, "run", return_value=completed):
            provenance = RUNNER._docker_runtime_provenance(True)
        self.assertEqual(provenance["architecture"], "arm64")
        self.assertEqual(
            provenance["components"],
            [
                {"name": "Engine", "version": "29.5.3"},
                {"name": "containerd", "version": "2.2.1"},
            ],
        )
        self.assertNotIn("context", json.dumps(provenance).lower())

    def test_admin_mutation_retries_with_authoritative_revision_and_converges(self):
        with patch.dict(
            os.environ,
            {
                "E2E_QUERY_A_URL": "https://query-a.example",
                "E2E_ADMIN_API_KEY": "admin-key",
            },
            clear=False,
        ), patch.object(
            RUNNER, "_control_revision", return_value=4
        ), patch.object(
            RUNNER,
            "_json_request",
            side_effect=[
                (
                    409,
                    {
                        "detail": {
                            "code": "control_plane_revision_conflict",
                            "expected_revision": 4,
                            "current_revision": 5,
                        }
                    },
                    {},
                ),
                (200, {"revision": 6, "result": "ok"}, {}),
            ],
        ) as request, patch.object(RUNNER, "_assert_revision") as converged:
            response = RUNNER._admin_mutation("/admin/test", {"value": True})

        self.assertEqual(response["revision"], 6)
        self.assertEqual(request.call_args_list[0].kwargs["if_match"], 4)
        self.assertEqual(request.call_args_list[1].kwargs["if_match"], 5)
        self.assertEqual(
            converged.call_args_list,
            [
                call("https://query-a.example", 5),
                call("https://query-a.example", 6),
            ],
        )

    def test_trace_chain_requires_cross_service_parent_child_lineage(self):
        spans = [
            {
                "service_name": "imposbro-query-api",
                "name": "POST /ingest/{collection_name}",
                "span_id": "01",
                "parent_span_id": "00",
            },
            {
                "service_name": "imposbro-query-api",
                "name": "kafka.publish",
                "span_id": "02",
                "parent_span_id": "01",
            },
            {
                "service_name": "imposbro-indexing-service",
                "name": "kafka.process",
                "span_id": "03",
                "parent_span_id": "02",
            },
            {
                "service_name": "imposbro-indexing-service",
                "name": "typesense.document.upsert",
                "span_id": "04",
                "parent_span_id": "03",
            },
        ]
        self.assertIsNotNone(RUNNER._trace_chain(spans))
        spans[-1]["parent_span_id"] = "unrelated"
        self.assertIsNone(RUNNER._trace_chain(spans))

    def test_typesense_requests_use_the_typesense_api_key_header(self):
        endpoint = "https://typesense-a.example:8108"
        with patch.dict(
            os.environ,
            {
                "E2E_TYPESENSE_A_URL": endpoint,
                "E2E_TYPESENSE_B_URL": "https://typesense-b.example:8108",
                "E2E_TYPESENSE_A_API_KEY": "cluster-a-key",
            },
            clear=False,
        ), patch.object(
            RUNNER,
            "_json_request",
            return_value=(200, {"ok": True}, {}),
        ) as request:
            RUNNER._typesense_request(endpoint, "/health")

        request.assert_called_once_with(
            endpoint,
            "/health",
            api_key="cluster-a-key",
            api_key_header="X-TYPESENSE-API-KEY",
            expected=(200,),
        )

    def test_typesense_credentials_are_mapped_per_cluster(self):
        with patch.dict(
            os.environ,
            {
                "E2E_TYPESENSE_A_URL": "https://typesense-a.example:8108",
                "E2E_TYPESENSE_B_URL": "https://typesense-b.example:8108",
                "E2E_TYPESENSE_A_API_KEY": "cluster-a-key",
                "E2E_TYPESENSE_B_API_KEY": "cluster-b-key",
                "E2E_TYPESENSE_API_KEY": "",
            },
            clear=False,
        ):
            self.assertEqual(
                RUNNER._typesense_api_key("https://typesense-a.example:8108"),
                "cluster-a-key",
            )
            self.assertEqual(
                RUNNER._typesense_api_key("https://typesense-b.example:8108"),
                "cluster-b-key",
            )

    def test_secret_log_check_merges_core_assertions_and_fails_on_raw_value(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RUNNER.EvidenceStore(root / "evidence.json")
            initialize_evidence(store)
            store.record(
                {
                    "id": "typesense_secret_ref_rotation",
                    "status": "passed",
                    "started_at": "2026-01-01T00:00:00Z",
                    "finished_at": "2026-01-01T00:00:00.001Z",
                    "duration_ms": 1,
                    "assertions": {"file_rotation_without_state_reload": True},
                }
            )
            logs = root / "application.log"
            logs.write_text("query initialized\n", encoding="utf-8")
            environment = {
                "E2E_APPLICATION_LOGS_FILE": str(logs),
                "E2E_TYPESENSE_API_KEY": "raw-typesense-key",
            }
            with patch.dict(os.environ, environment, clear=False):
                assertions = RUNNER.scenario_typesense_secret_log_check(store)
            self.assertTrue(
                assertions["raw_typesense_secret_absent_from_application_logs"]
            )

            logs.write_text("leak=raw-typesense-key\n", encoding="utf-8")
            with patch.dict(os.environ, environment, clear=False), self.assertRaises(
                RUNNER.ScenarioFailure
            ):
                RUNNER.scenario_typesense_secret_log_check(store)

    def test_writer_redacts_configured_secrets_and_url_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "E2E_API_KEY": "do-not-persist-this-value",
                "DATABASE_PASSWORD": "database-password",
            },
            clear=False,
        ):
            path = Path(directory) / "evidence.json"
            store = RUNNER.EvidenceStore(path)
            store.write(
                {
                    "schema_version": RUNNER.EVIDENCE_SCHEMA_VERSION,
                    "scenarios": [],
                    "api_key": "do-not-persist-this-value",
                    "message": (
                        "postgresql://user:database-password@postgres/db "
                        "do-not-persist-this-value?token=also-private"
                    ),
                }
            )
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("database-password", raw)
            self.assertNotIn("do-not-persist-this-value", raw)
            self.assertNotIn("also-private", raw)
            payload = json.loads(raw)
            self.assertEqual(payload["api_key"], "<redacted>")
            self.assertIn("postgresql://<redacted>@postgres/db", payload["message"])

    def test_diagnostic_tail_is_bounded_and_redacted(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"E2E_ADMIN_API_KEY": "diagnostic-secret-value"},
            clear=False,
        ):
            store = RUNNER.EvidenceStore(Path(directory) / "evidence.json")
            initialize_evidence(store)
            store.attach_diagnostics(
                "x" * 20_000
                + " postgresql://user:password@postgres/db diagnostic-secret-value"
            )
            payload = store.load()
            tail = payload["diagnostics"]["log_tail"]
            self.assertLessEqual(len(tail), 16_000)
            self.assertNotIn("password", tail)
            self.assertNotIn("diagnostic-secret-value", tail)

    def test_finalize_requires_every_recorded_scenario_to_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            store = RUNNER.EvidenceStore(path)
            initialized = initialize_evidence(store)
            required = set(RUNNER.REQUIRED_SCENARIOS_BY_MODE["static"])
            self.assertEqual(
                {item["id"] for item in initialized["scenarios"]}, required
            )
            self.assertEqual(store.finalize()["status"], "incomplete")

            for identifier in required:
                store.record(
                    {
                        "id": identifier,
                        "status": "passed",
                        "started_at": "2026-01-01T00:00:02Z",
                        "finished_at": "2026-01-01T00:00:02.001Z",
                        "duration_ms": 1,
                        "assertions": {"executed": True},
                    }
                )
            self.assertEqual(store.finalize()["status"], "passed")

            store.record(
                {
                    "id": "unexpected_probe",
                    "status": "passed",
                    "started_at": "2026-01-01T00:00:03Z",
                    "finished_at": "2026-01-01T00:00:03.001Z",
                    "duration_ms": 1,
                    "assertions": {"executed": True},
                }
            )
            self.assertEqual(store.finalize()["status"], "incomplete")

    def test_failure_has_priority_over_not_run(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RUNNER.EvidenceStore(Path(directory) / "evidence.json")
            initialize_evidence(store)
            for identifier, status in (("one", "not_run"), ("two", "failed")):
                store.record(
                    {
                        "id": identifier,
                        "status": status,
                        "started_at": "2026-01-01T00:00:00Z",
                        "finished_at": "2026-01-01T00:00:00Z",
                        "duration_ms": 0,
                        "assertions": {},
                        "reason": "unit test terminal status",
                    }
                )
            self.assertEqual(store.finalize()["status"], "failed")

    def test_harness_execution_failure_overrides_complete_required_set(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RUNNER.EvidenceStore(Path(directory) / "evidence.json")
            initialize_evidence(store)
            for identifier in RUNNER.REQUIRED_SCENARIOS_BY_MODE["static"]:
                store.record(
                    {
                        "id": identifier,
                        "status": "passed",
                        "started_at": "2026-01-01T00:00:00Z",
                        "finished_at": "2026-01-01T00:00:00.001Z",
                        "duration_ms": 1,
                        "assertions": {"executed": True},
                    }
                )
            store.record(
                {
                    "id": "harness_execution",
                    "status": "failed",
                    "started_at": "2026-01-01T00:00:01Z",
                    "finished_at": "2026-01-01T00:00:01Z",
                    "duration_ms": 0,
                    "assertions": {"exit_code": 1},
                    "reason": "non-zero composite gate",
                }
            )
            self.assertEqual(store.finalize()["status"], "failed")


class HarnessShapeTests(unittest.TestCase):
    def test_evidence_schema_is_versioned_and_requires_provenance(self):
        schema = json.loads(
            (ROOT / "tests/enterprise/evidence.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(schema["properties"]["schema_version"]["const"], "1.1")
        self.assertIn("provenance", schema["required"])
        self.assertIn("scenarios", schema["required"])
        self.assertIn(
            "required_scenarios",
            schema["properties"]["provenance"]["required"],
        )

    def test_environment_probe_exposes_booleans_not_local_paths(self):
        with patch.object(RUNNER, "_command_succeeds", return_value=False):
            capabilities = RUNNER.environment_capabilities()
        self.assertIsInstance(capabilities["docker_daemon_available"], bool)
        self.assertIsInstance(capabilities["kubernetes_context_available"], bool)
        self.assertIsInstance(capabilities["docker_runtime"], dict)
        self.assertTrue(
            all(
                isinstance(value, bool)
                for value in capabilities["clients"].values()
            )
        )

    def test_compose_stack_contains_every_required_enterprise_probe_component(self):
        compose = (ROOT / "ops/e2e/docker-compose.enterprise.yml").read_text(
            encoding="utf-8"
        )
        for service in (
            "postgres:",
            "redis:",
            "kafka:",
            "typesense-a:",
            "typesense-b:",
            "runtime-secret-init:",
            "query-a:",
            "query-b:",
            "indexing-service:",
            "otel-capture:",
            "typesense-tls:",
            "enterprise-tests:",
        ):
            self.assertIn(service, compose)
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertGreaterEqual(compose.count("@sha256:"), 6)
        worker_section = compose.split("  indexing-service:\n", 1)[1].split(
            "\n  tls-init:", 1
        )[0]
        self.assertIn("DEPLOYMENT_PROFILE: development", worker_section)
        self.assertIn("INDEXING_CHECKPOINT_BACKEND: postgres", worker_section)
        self.assertIn("CONTROL_PLANE_DATABASE_URL:", worker_section)
        self.assertIn("OTEL_TRACES_EXPORTER: otlp", worker_section)
        self.assertIn("https://otel-capture:4318", compose)
        self.assertIn("OIDC_JWKS_URL: https://otel-capture:4318/jwks", compose)
        self.assertIn('AUTHZ_REQUIRE_COLLECTION_POLICY: "true"', compose)
        self.assertIn(
            "oidc_tenant_isolation",
            RUNNER.REQUIRED_SCENARIOS_BY_MODE["compose"],
        )
        self.assertIn(
            "typesense_secret_ref_rotation",
            RUNNER.REQUIRED_SCENARIOS_BY_MODE["compose"],
        )
        self.assertIn("DEFAULT_DATA_CLUSTER_API_KEY_REF: file:typesense-api-key", compose)
        self.assertIn(
            "DEFAULT_DATA2_CLUSTER_API_KEY_REF: env:E2E_TYPESENSE_ENV_KEY",
            compose,
        )
        self.assertIn("runtime-secrets:/runtime-secrets:ro", compose)
        self.assertIn("runtime-secrets:/runtime-secrets\n", compose)

    def test_cleanup_is_non_destructive_by_default(self):
        script = (ROOT / "scripts/e2e/run-enterprise-e2e.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('"${E2E_PRUNE_VOLUMES:-false}" == true', script)
        self.assertIn("compose start redis", script)
        self.assertIn("record_result harness_execution failed", script)
        self.assertIn("trap cleanup EXIT", script)

    def test_kind_file_is_explicitly_a_substrate_not_execution_evidence(self):
        kind = (ROOT / "ops/e2e/kind-enterprise-e2e.yaml").read_text(
            encoding="utf-8"
        )
        self.assertIn("kind: Cluster", kind)
        self.assertIn("apiVersion: kind.x-k8s.io/v1alpha4", kind)
        self.assertIn("not evidence", kind)


if __name__ == "__main__":
    unittest.main()
