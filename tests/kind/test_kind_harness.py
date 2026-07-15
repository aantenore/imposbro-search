from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DIGEST = re.compile(r"@sha256:[a-f0-9]{64}$")


def test_shell_harness_is_syntactically_valid() -> None:
    subprocess.run(
        ["bash", "-n", str(ROOT / "scripts/e2e/run-kind-enterprise-smoke.sh")],
        check=True,
    )


def test_disruption_probes_traverse_stable_tls_edge() -> None:
    script = (ROOT / "scripts/e2e/run-kind-enterprise-smoke.sh").read_text()

    assert script.count('continuous_cluster_tls_probe "$TMP_DIR/') == 4
    assert "exec -i kind-tls-probe" in script
    assert 'ssl.create_default_context(cafile="/tls/tls.crt")' in script
    assert 'http.client.HTTPSConnection(host, 443' in script
    assert "imposbro.dev/test-resource: kind-enterprise-smoke" in script


def test_expected_pdb_rejection_does_not_trigger_global_error_trap() -> None:
    script = (ROOT / "scripts/e2e/run-kind-enterprise-smoke.sh").read_text()

    assert "if EVICTION_OUTPUT=$(trap - ERR; kubectl" in script
    pdb_section = script.split("begin_assertion pdb_eviction_blocked", 1)[1].split(
        "begin_assertion workload_node_drain", 1
    )[0]
    assert "set +e" not in pdb_section


def test_post_drain_check_does_not_reuse_a_pod_bound_port_forward() -> None:
    script = (ROOT / "scripts/e2e/run-kind-enterprise-smoke.sh").read_text()
    drain_section = script.split("begin_assertion workload_node_drain", 1)[1]

    assert 'continuous_cluster_tls_probe "$TMP_DIR/post-drain-failures" 1 0' in drain_section
    assert 'wait_url "http://127.0.0.1:$QUERY_PORT/ready"' not in drain_section


def test_cluster_is_multi_node_and_node_image_is_pinned_in_harness() -> None:
    cluster = (ROOT / "ops/kind/cluster.yaml").read_text()
    assert len(re.findall(r"^  - role: control-plane$", cluster, re.MULTILINE)) == 1
    assert len(re.findall(r"^  - role: worker$", cluster, re.MULTILINE)) >= 4
    script = (ROOT / "scripts/e2e/run-kind-enterprise-smoke.sh").read_text()
    match = re.search(r'^KIND_NODE_IMAGE="([^\"]+)"$', script, re.MULTILINE)
    assert match and DIGEST.search(match.group(1))


def test_dependency_images_are_digest_pinned_and_isolated() -> None:
    content = (ROOT / "ops/kind/dependencies.yaml").read_text()
    assert content.count("kind: Deployment") == 5
    for name in ("postgres", "redis", "kafka", "typesense-a", "typesense-b"):
        assert re.search(rf"^  name: {name}$", content, re.MULTILINE)
    assert content.count("automountServiceAccountToken: false") == 5
    assert content.count("nodeSelector: {imposbro.dev/role: infra}") == 5
    images = re.findall(r"^          image: (.+)$", content, re.MULTILINE)
    # Five long-running dependencies plus the non-root Kafka config initializer.
    assert len(images) == 6
    assert all(DIGEST.search(image) for image in images)


def test_non_root_kafka_has_fs_group_writable_config_volume() -> None:
    content = (ROOT / "ops/kind/dependencies.yaml").read_text()
    kafka = content.split("  name: kafka", 1)[1].split("---", 1)[0]
    assert "runAsUser: 1001" in kafka
    assert "fsGroup: 1001" in kafka
    assert 'KAFKA_CFG_PROCESS_ROLES, value: "controller,broker"' in kafka
    assert 'KAFKA_CFG_CONTROLLER_QUORUM_VOTERS, value: "0@127.0.0.1:9093"' in kafka
    assert "mountPath: /opt/bitnami/kafka/config" in kafka
    assert "name: config, emptyDir: {sizeLimit: 16Mi}" in kafka
    assert "name: initialize-kafka-config" in kafka
    assert "cp -R /opt/bitnami/kafka/config/. /writable-config/" in kafka
    assert "rm -f /writable-config/server.properties" in kafka
    assert "startupProbe:" in kafka
    assert "failureThreshold: 120" in kafka
    assert "tcpSocket: {port: kafka}" in kafka
    assert 'KAFKA_HEAP_OPTS, value: "-Xms256m -Xmx512m"' in kafka
    assert "mountPath: /opt/bitnami/kafka/logs" in kafka
    assert "name: logs, emptyDir: {sizeLimit: 256Mi}" in kafka


def test_kind_dependencies_use_immutable_roots_with_explicit_writable_mounts() -> None:
    content = (ROOT / "ops/kind/dependencies.yaml").read_text()

    assert content.count("readOnlyRootFilesystem: true") == 6
    assert "{name: PGDATA, value: /var/lib/postgresql/data/pgdata}" in content
    assert "mountPath: /var/run/postgresql" in content
    assert content.count("mountPath: /tmp") == 5


def test_live_values_enable_enterprise_kubernetes_controls() -> None:
    values = (ROOT / "ops/kind/values.yaml").read_text()
    assert "readOnlyRootFilesystem: true" in values
    assert "capabilities: {drop: [ALL]}" in values
    assert "automountServiceAccountToken: false" in values
    assert re.search(r"networkPolicy:\n  enabled: true", values)
    assert re.search(r"migrations:\n  enabled: true", values)
    assert values.count("replicaCount: 2") == 3
    assert values.count("podDisruptionBudget: {enabled: true, minAvailable: 1}") == 3
    assert values.count("topologySpreadConstraints:") == 3
    assert values.count("secretName: kind-edge-tls") == 2
    assert 'KAFKA_SECURITY_PROTOCOL: PLAINTEXT' in values
    assert 'KAFKA_SASL_MECHANISM: ""' in values


def test_helm_omits_empty_kafka_sasl_mechanism() -> None:
    template = (ROOT / "helm/templates/configmap.yaml").read_text()

    assert "with .Values.config.KAFKA_SASL_MECHANISM" in template
    assert "KAFKA_SASL_MECHANISM: {{ . | quote }}" in template


def test_tls_edge_has_bounded_writable_runtime_mounts() -> None:
    edge = (ROOT / "ops/kind/tls-edge.yaml").read_text()

    assert "readOnlyRootFilesystem: true" in edge
    assert "{name: tmp, mountPath: /tmp}" in edge
    assert "{name: tmp, emptyDir: {sizeLimit: 8Mi}}" in edge


def test_checked_in_evidence_cannot_claim_production_certification() -> None:
    for path in (ROOT / "docs/evidence").glob("kind-*.json"):
        evidence = json.loads(path.read_text())
        assert evidence["production_certification"] is False
        assert evidence["evidence_type"] in {
            "kind-enterprise-smoke",
            "kind-enterprise-benchmark",
        }


def test_benchmark_profile_and_fail_closed_invocation_are_declared() -> None:
    profile = json.loads((ROOT / "ops/kind/benchmark-profile.json").read_text())
    assert profile["production_certification"] is False
    assert profile["workload"]["documents"] >= 1000
    assert profile["workload"]["ingest_concurrency"] >= 2
    assert profile["workload"]["search_concurrency"] >= 2
    assert profile["thresholds"]["min_ingest_docs_per_second"] > 0
    assert profile["thresholds"]["max_indexing_visible_seconds"] > 0
    assert profile["thresholds"]["max_search_p95_ms"] > 0
    assert profile["thresholds"]["max_search_error_rate"] == 0
    assert profile["thresholds"]["allow_partial"] is False
    script = (ROOT / "scripts/e2e/run-kind-enterprise-smoke.sh").read_text()
    assert 'python3 "$ROOT_DIR/scripts/benchmark-k8s.py"' in script
    assert "publish_benchmark_evidence" in script
    for variable in (
        "BENCHMARK_MIN_INGEST_DOCS_PER_SECOND",
        "BENCHMARK_MAX_INDEXING_VISIBLE_SECONDS",
        "BENCHMARK_MAX_SEARCH_P95_MS",
        "BENCHMARK_MAX_SEARCH_ERROR_RATE",
    ):
        assert f'{variable}="${variable}"' in script


def _init_recorder(state: Path, *required: str) -> Path:
    recorder = ROOT / "tests/kind/evidence_recorder.py"
    command = [
        sys.executable,
        str(recorder),
        "init",
        "--state",
        str(state),
        "--run-id",
        "contract-test",
        "--started-at",
        "2026-07-10T10:00:00Z",
        "--commit",
        "deadbeef",
        "--dirty",
        "false",
        "--cluster-name",
        "unit-kind",
        "--namespace",
        "unit",
        "--node-image",
        "kindest/node@sha256:" + "1" * 64,
    ]
    for assertion_id in required:
        command.extend(["--required-assertion", assertion_id])
    subprocess.run(command, check=True)
    return recorder


def _record(recorder: Path, state: Path, assertion_id: str, status: str = "passed"):
    return subprocess.run(
        [
            sys.executable,
            str(recorder),
            "assert",
            "--state",
            str(state),
            "--id",
            assertion_id,
            "--status",
            status,
            "--duration",
            "0",
            "--evidence",
            "contract assertion",
        ],
        text=True,
        capture_output=True,
    )


def _finish_pass(recorder: Path, state: Path, output: Path):
    return subprocess.run(
        [
            sys.executable,
            str(recorder),
            "finish",
            "--state",
            str(state),
            "--output",
            str(output),
            "--status",
            "passed",
            "--finished-at",
            "2026-07-10T10:01:00Z",
            "--cleanup-status",
            "completed",
            "--cluster-deleted",
            "true",
            "--registry-deleted",
            "true",
            "--network-deleted",
            "true",
        ],
        text=True,
        capture_output=True,
    )


def test_recorder_rejects_partial_assertion_contract(tmp_path: Path) -> None:
    state = tmp_path / "partial-state.json"
    output = tmp_path / "partial-output.json"
    recorder = _init_recorder(state, "first", "cleanup")
    assert _record(recorder, state, "first").returncode == 0
    result = _finish_pass(recorder, state, output)
    assert result.returncode != 0
    assert json.loads(output.read_text())["status"] == "failed"


def test_recorder_rejects_unexpected_and_duplicate_assertions(tmp_path: Path) -> None:
    state = tmp_path / "strict-state.json"
    recorder = _init_recorder(state, "first")
    unexpected = _record(recorder, state, "other")
    assert unexpected.returncode != 0
    assert "unexpected assertion" in unexpected.stderr
    assert _record(recorder, state, "first").returncode == 0
    duplicate = _record(recorder, state, "first")
    assert duplicate.returncode != 0
    assert "duplicate assertion" in duplicate.stderr


def test_recorder_rejects_non_passing_required_assertion(tmp_path: Path) -> None:
    state = tmp_path / "failed-state.json"
    output = tmp_path / "failed-output.json"
    recorder = _init_recorder(state, "first", "cleanup")
    assert _record(recorder, state, "first", "failed").returncode == 0
    assert _record(recorder, state, "cleanup").returncode == 0
    result = _finish_pass(recorder, state, output)
    assert result.returncode != 0
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "failed"
    assert evidence["assertions"][0]["status"] == "failed"


def test_main_harness_predeclares_exact_cleanup_inclusive_contract() -> None:
    script = (ROOT / "scripts/e2e/run-kind-enterprise-smoke.sh").read_text()
    match = re.search(r'^REQUIRED_ASSERTIONS="([^"]+)"$', script, re.MULTILINE)
    assert match
    required = match.group(1).split()
    assert len(required) == len(set(required))
    assert required[-1] == "cleanup"
    begun = re.findall(r"^begin_assertion ([a-z0-9_]+)$", script, re.MULTILINE)
    assert begun + ["cleanup"] == required
    cleanup_body = script.split("cleanup() {", 1)[1].split("trap on_error ERR", 1)[0]
    assert 'kind get clusters 2>/dev/null | grep -Fxq "$CLUSTER_NAME"' in cleanup_body
    assert 'docker inspect "$REGISTRY_NAME"' in cleanup_body
    assert "docker network inspect kind" in cleanup_body
    assert '--network-deleted "$network_deleted"' in cleanup_body


def test_evidence_recorder_is_atomic_redacted_and_never_certifies_production(
    tmp_path: Path,
) -> None:
    recorder = ROOT / "tests/kind/evidence_recorder.py"
    state = tmp_path / "state.json"
    output = tmp_path / "evidence.json"
    subprocess.run(
        [
            sys.executable,
            str(recorder),
            "init",
            "--state",
            str(state),
            "--run-id",
            "unit-run",
            "--started-at",
            "2026-07-10T10:00:00Z",
            "--commit",
            "deadbeef",
            "--dirty",
            "false",
            "--cluster-name",
            "unit-kind",
            "--namespace",
            "unit",
            "--node-image",
            "kindest/node@sha256:" + "1" * 64,
            "--tool",
            "kind=v0.32.0",
            "--required-assertion",
            "redaction_guard",
            "--required-assertion",
            "unit_assertion",
            "--required-assertion",
            "cleanup",
        ],
        check=True,
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(recorder),
            "assert",
            "--state",
            str(state),
            "--id",
            "redaction_guard",
            "--status",
            "failed",
            "--duration",
            "0",
            "--evidence",
            "api_key=must-not-appear",
        ],
        text=True,
        capture_output=True,
    )
    assert rejected.returncode != 0
    subprocess.run(
        [
            sys.executable,
            str(recorder),
            "assert",
            "--state",
            str(state),
            "--id",
            "redaction_guard",
            "--status",
            "passed",
            "--duration",
            "0.1",
            "--evidence",
            "sensitive marker rejected",
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(recorder),
            "assert",
            "--state",
            str(state),
            "--id",
            "unit_assertion",
            "--status",
            "passed",
            "--duration",
            "0.1",
            "--evidence",
            "sanitized assertion",
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(recorder),
            "assert",
            "--state",
            str(state),
            "--id",
            "cleanup",
            "--status",
            "passed",
            "--duration",
            "0.1",
            "--evidence",
            "resources removed",
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(recorder),
            "finish",
            "--state",
            str(state),
            "--output",
            str(output),
            "--status",
            "passed",
            "--finished-at",
            "2026-07-10T10:01:00Z",
            "--cleanup-status",
            "completed",
            "--cluster-deleted",
            "true",
            "--registry-deleted",
            "true",
            "--network-deleted",
            "true",
        ],
        check=True,
    )
    evidence = json.loads(output.read_text())
    assert evidence["status"] == "passed"
    assert evidence["production_certification"] is False
    assert evidence["cleanup"]["cluster_deleted"] is True
    assert evidence["cleanup"]["network_deleted"] is True
    assert not list(tmp_path.glob("*.tmp"))
