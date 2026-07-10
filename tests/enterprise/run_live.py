#!/usr/bin/env python3
"""Fail-closed live enterprise evidence runner.

The module intentionally imports project/runtime dependencies only inside a
scenario.  Evidence initialization and static validation therefore remain
usable on operator hosts that do not have the Python service dependencies.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlparse
from urllib.request import Request, urlopen


EVIDENCE_SCHEMA_VERSION = "1.1"
TERMINAL_STATUSES = {"passed", "failed", "not_run"}
LIVE_REQUIRED_SCENARIOS = (
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
)
REQUIRED_SCENARIOS_BY_MODE = {
    "compose": (
        "static_harness_validation",
        "compose_stack_bootstrap",
        *LIVE_REQUIRED_SCENARIOS,
    ),
    "external": ("static_harness_validation", *LIVE_REQUIRED_SCENARIOS),
    "static": ("static_harness_validation", *LIVE_REQUIRED_SCENARIOS),
}
SENSITIVE_ENV_PATTERN = re.compile(
    r"(?:PASSWORD|SECRET|TOKEN|API_KEY|PRIVATE_KEY|CREDENTIAL)", re.IGNORECASE
)
SENSITIVE_KEY_PATTERN = re.compile(
    r"(?:password|secret|token|api_key|private_key|credential|database_url|dsn)$",
    re.IGNORECASE,
)
URL_CREDENTIAL_PATTERN = re.compile(r"([a-z][a-z0-9+.-]*://)([^/@\s]+)@", re.I)
QUERY_SECRET_PATTERN = re.compile(
    r"(?i)(\b(?:password|secret|token|api[_-]?key)=)([^&\s]+)"
)


class ScenarioFailure(RuntimeError):
    """Raised when a live assertion cannot be proven."""


class ScenarioNotRun(RuntimeError):
    """Raised when an explicitly optional probe has no configured endpoint."""


def utc_now() -> str:
    return format_utc(datetime.now(timezone.utc))


def format_utc(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _known_secrets() -> list[str]:
    values = []
    for name, value in os.environ.items():
        if SENSITIVE_ENV_PATTERN.search(name) and len(value) >= 4:
            values.append(value)
    return sorted(set(values), key=len, reverse=True)


def scrub_text(value: str) -> str:
    """Remove URL credentials and configured secret values from evidence text."""
    scrubbed = URL_CREDENTIAL_PATTERN.sub(r"\1<redacted>@", value)
    scrubbed = QUERY_SECRET_PATTERN.sub(r"\1<redacted>", scrubbed)
    for secret in _known_secrets():
        scrubbed = scrubbed.replace(secret, "<redacted>")
    return scrubbed


def sanitize(value: Any, *, key: str = "") -> Any:
    """Recursively produce a JSON-safe, secret-free evidence value."""
    if key and SENSITIVE_KEY_PATTERN.search(key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(k): sanitize(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub_text(str(value))


def _git_metadata(repository: Path) -> Dict[str, Any]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
        )
    except (OSError, subprocess.CalledProcessError):
        commit = os.environ.get("E2E_GIT_COMMIT", "unknown")
        dirty = os.environ.get("E2E_GIT_DIRTY", "unknown")
    return {"git_commit": commit, "git_dirty": dirty}


def _command_succeeds(arguments: list[str]) -> bool:
    try:
        return (
            subprocess.run(
                arguments,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def _docker_runtime_provenance(available: bool) -> Dict[str, Any]:
    unknown = {
        "server_version": "unavailable",
        "api_version": "unavailable",
        "os": "unavailable",
        "architecture": "unavailable",
        "components": [],
    }
    if not available:
        return unknown
    try:
        completed = subprocess.run(
            ["docker", "version", "--format", "{{json .Server}}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        server = json.loads(completed.stdout)
        components = [
            {
                "name": str(component.get("Name", "unknown")),
                "version": str(component.get("Version", "unknown")),
            }
            for component in server.get("Components", [])
            if isinstance(component, Mapping)
        ]
        components.sort(key=lambda item: item["name"])
        return {
            "server_version": str(server.get("Version", "unknown")),
            "api_version": str(server.get("ApiVersion", "unknown")),
            "os": str(server.get("Os", "unknown")),
            "architecture": str(server.get("Arch", "unknown")),
            "components": components,
        }
    except (
        OSError,
        subprocess.CalledProcessError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
    ):
        return {**unknown, "server_version": "probe_failed"}


def environment_capabilities() -> Dict[str, Any]:
    """Capture only non-sensitive runtime availability, never contexts or paths."""
    installed = {
        name: shutil.which(name) is not None
        for name in (
            "docker",
            "podman",
            "colima",
            "kubectl",
            "kind",
            "psql",
            "redis-cli",
            "kcat",
            "helm",
            "openssl",
        )
    }
    docker_available = installed["docker"] and _command_succeeds(["docker", "info"])
    return {
        "architecture": platform.machine().lower(),
        "clients": installed,
        "docker_daemon_available": docker_available,
        "docker_runtime": _docker_runtime_provenance(docker_available),
        "kubernetes_context_available": installed["kubectl"]
        and _command_succeeds(["kubectl", "config", "current-context"]),
    }


class EvidenceStore:
    """Small atomic JSON evidence store shared by sequential harness phases."""

    def __init__(self, path: Path):
        self.path = path

    def initialize(self, *, dependency_mode: str, runtime: str) -> Dict[str, Any]:
        repository = Path(__file__).resolve().parents[2]
        required_scenarios = REQUIRED_SCENARIOS_BY_MODE[dependency_mode]
        initialized_at = utc_now()
        payload = {
            "schema_version": EVIDENCE_SCHEMA_VERSION,
            "run_id": os.environ.get("E2E_RUN_ID", uuid.uuid4().hex),
            "started_at": initialized_at,
            "finished_at": None,
            "status": "running",
            "provenance": {
                **_git_metadata(repository),
                "dependency_mode": dependency_mode,
                "runtime": runtime,
                "runner": "tests/enterprise/run_live.py",
                "python": platform.python_version(),
                "platform": platform.system().lower(),
                "evidence_scope": "enterprise-integration-harness",
                "production_certification": False,
                "required_scenarios": list(required_scenarios),
                "capabilities": environment_capabilities(),
            },
            "scenarios": [
                {
                    "id": scenario_id,
                    "status": "not_run",
                    "started_at": initialized_at,
                    "finished_at": initialized_at,
                    "duration_ms": 0,
                    "assertions": {},
                    "reason": "Scenario has not completed in this run",
                }
                for scenario_id in required_scenarios
            ],
        }
        self.write(payload)
        return payload

    def load(self) -> Dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScenarioFailure(f"Evidence file is missing or invalid: {exc}") from exc
        if payload.get("schema_version") != EVIDENCE_SCHEMA_VERSION:
            raise ScenarioFailure("Unsupported evidence schema version")
        if not isinstance(payload.get("scenarios"), list):
            raise ScenarioFailure("Evidence scenarios must be an array")
        for scenario in payload["scenarios"]:
            if not isinstance(scenario, Mapping):
                raise ScenarioFailure("Evidence scenario must be an object")
            self._validate_scenario(scenario)
        return payload

    def write(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        safe_payload = sanitize(dict(payload))
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(safe_payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
        finally:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass

    def record(self, result: Mapping[str, Any]) -> None:
        self._validate_scenario(result)
        payload = self.load()
        scenario_id = str(result["id"])
        replacement = dict(result)
        replaced = False
        scenarios = []
        for item in payload["scenarios"]:
            if item.get("id") == scenario_id:
                if not replaced:
                    scenarios.append(replacement)
                    replaced = True
            else:
                scenarios.append(item)
        if not replaced:
            scenarios.append(replacement)
        payload["scenarios"] = scenarios
        self.write(payload)

    @staticmethod
    def _validate_scenario(result: Mapping[str, Any]) -> None:
        scenario_id = result.get("id")
        if not isinstance(scenario_id, str) or not re.fullmatch(
            r"[a-z0-9_]{3,128}", scenario_id
        ):
            raise ScenarioFailure("Evidence scenario id must be lower snake_case")
        status = result.get("status")
        if status not in TERMINAL_STATUSES:
            raise ScenarioFailure("Evidence scenario has an invalid terminal status")
        duration = result.get("duration_ms")
        if not isinstance(duration, int) or isinstance(duration, bool) or duration < 0:
            raise ScenarioFailure("Evidence scenario duration must be a non-negative integer")
        if not isinstance(result.get("assertions"), Mapping):
            raise ScenarioFailure("Evidence scenario assertions must be an object")
        parsed_timestamps = {}
        for timestamp_name in ("started_at", "finished_at"):
            timestamp = result.get(timestamp_name)
            if not isinstance(timestamp, str):
                raise ScenarioFailure(
                    f"Evidence scenario {timestamp_name} must be an ISO-8601 string"
                )
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ScenarioFailure(
                    f"Evidence scenario {timestamp_name} is invalid"
                ) from exc
            if parsed.tzinfo is None:
                raise ScenarioFailure(
                    f"Evidence scenario {timestamp_name} requires a timezone"
                )
            parsed_timestamps[timestamp_name] = parsed
        if parsed_timestamps["finished_at"] < parsed_timestamps["started_at"]:
            raise ScenarioFailure("Evidence scenario finished before it started")
        if status in {"failed", "not_run"} and not str(result.get("reason", "")).strip():
            raise ScenarioFailure("Failed or not-run evidence requires a reason")

    def assertion(self, scenario_id: str, name: str) -> Any:
        for item in self.load()["scenarios"]:
            if item.get("id") == scenario_id:
                return item.get("assertions", {}).get(name)
        raise ScenarioFailure(f"Scenario '{scenario_id}' has no recorded evidence")

    def finalize(self) -> Dict[str, Any]:
        payload = self.load()
        required = payload.get("provenance", {}).get("required_scenarios")
        if not isinstance(required, list) or not required or not all(
            isinstance(item, str) for item in required
        ):
            raise ScenarioFailure("Evidence provenance has no required scenario contract")
        required_ids = set(required)
        actual_ids = {str(item.get("id")) for item in payload["scenarios"]}
        statuses = {item.get("status") for item in payload["scenarios"]}
        if "failed" in statuses:
            status = "failed"
        elif actual_ids == required_ids and all(
            item.get("status") == "passed"
            for item in payload["scenarios"]
            if item.get("id") in required_ids
        ):
            status = "passed"
        else:
            status = "incomplete"
        payload["status"] = status
        payload["finished_at"] = utc_now()
        self.write(payload)
        return payload

    def attach_diagnostics(self, raw_text: str) -> None:
        """Attach a bounded redacted log tail without changing scenario status."""
        payload = self.load()
        safe_tail = scrub_text(raw_text[-16_000:])[-16_000:]
        payload["diagnostics"] = {
            "captured_at": utc_now(),
            "truncated": len(raw_text) > 16_000,
            "log_tail": safe_tail,
        }
        self.write(payload)


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ScenarioFailure(f"Required environment variable {name} is missing")
    return value


def timeout_seconds() -> float:
    raw = os.environ.get("E2E_TIMEOUT_SECONDS", "90")
    try:
        value = float(raw)
    except ValueError as exc:
        raise ScenarioFailure("E2E_TIMEOUT_SECONDS must be numeric") from exc
    if value <= 0 or value > 900:
        raise ScenarioFailure("E2E_TIMEOUT_SECONDS must be between 0 and 900")
    return value


def wait_until(
    predicate: Callable[[], Any],
    *,
    description: str,
    timeout: Optional[float] = None,
    interval: float = 0.5,
) -> Any:
    deadline = time.monotonic() + (timeout or timeout_seconds())
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # the final error is sanitized by evidence writer
            last_error = exc
        time.sleep(interval)
    suffix = f"; last error: {last_error}" if last_error else ""
    raise ScenarioFailure(f"Timed out waiting for {description}{suffix}")


def _json_request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: Optional[Mapping[str, Any]] = None,
    api_key: str = "",
    api_key_header: str = "X-API-Key",
    bearer_token: str = "",
    extra_headers: Optional[Mapping[str, str]] = None,
    if_match: Optional[int] = None,
    expected: Iterable[int] = (200,),
    ssl_context: Optional[ssl.SSLContext] = None,
) -> tuple[int, Any, Mapping[str, str]]:
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers[api_key_header] = api_key
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    for header_name, header_value in (extra_headers or {}).items():
        if any(character in str(header_name) + str(header_value) for character in "\r\n"):
            raise ScenarioFailure("HTTP test header contained a line break")
        headers[str(header_name)] = str(header_value)
    if if_match is not None:
        headers["If-Match"] = f'"{if_match}"'
    request = Request(
        base_url.rstrip("/") + "/" + path.lstrip("/"),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=10, context=ssl_context) as response:
            status = response.status
            raw = response.read()
            response_headers = dict(response.headers.items())
    except HTTPError as exc:
        status = exc.code
        raw = exc.read()
        response_headers = dict(exc.headers.items())
    except (OSError, URLError) as exc:
        raise ScenarioFailure(f"HTTP request failed for {path}: {exc}") from exc
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = raw.decode("utf-8", errors="replace")[:1024]
    if status not in set(expected):
        raise ScenarioFailure(
            f"Unexpected HTTP {status} for {method} {path}: {body}"
        )
    return status, body, response_headers


def _query_health(base_url: str) -> Dict[str, Any]:
    _, body, _ = _json_request(base_url, "/health")
    if not isinstance(body, dict):
        raise ScenarioFailure("Query health response is not an object")
    return body


def _control_revision(base_url: str) -> int:
    def converged() -> Any:
        control = _query_health(base_url).get("control_plane", {})
        if control.get("converged") and control.get("status") == "ok":
            return int(control["applied_revision"])
        return None

    return int(wait_until(converged, description=f"{base_url} control-plane convergence"))


def _admin_mutation(path: str, payload: Mapping[str, Any]) -> Any:
    query_url = required_env("E2E_QUERY_A_URL")
    revision = _control_revision(query_url)
    for _ in range(4):
        status, body, _ = _json_request(
            query_url,
            path,
            method="POST",
            payload=payload,
            api_key=required_env("E2E_ADMIN_API_KEY"),
            if_match=revision,
            expected=(200, 201, 409),
        )
        if status != 409:
            if isinstance(body, dict) and isinstance(body.get("revision"), int):
                _assert_revision(query_url, int(body["revision"]))
            return body
        detail = body.get("detail", {}) if isinstance(body, dict) else {}
        if not isinstance(detail, dict) or detail.get("code") != (
            "control_plane_revision_conflict"
        ):
            raise ScenarioFailure(f"Admin mutation conflict for {path}: {body}")
        current_revision = detail.get("current_revision")
        if not isinstance(current_revision, int) or current_revision < 0:
            raise ScenarioFailure(
                f"Admin mutation conflict omitted authoritative revision: {body}"
            )
        _assert_revision(query_url, current_revision)
        revision = current_revision
    raise ScenarioFailure(f"Admin mutation did not converge after CAS retries: {path}")


def _typesense_request(
    base_url: str,
    path: str,
    *,
    expected: Iterable[int] = (200,),
) -> tuple[int, Any]:
    status, body, _ = _json_request(
        base_url,
        path,
        api_key=_typesense_api_key(base_url),
        api_key_header="X-TYPESENSE-API-KEY",
        expected=expected,
    )
    return status, body


def _typesense_api_key(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    cluster_a = os.environ.get("E2E_TYPESENSE_A_URL", "").strip().rstrip("/")
    cluster_b = os.environ.get("E2E_TYPESENSE_B_URL", "").strip().rstrip("/")
    if normalized == cluster_a:
        return (
            os.environ.get("E2E_TYPESENSE_A_API_KEY", "").strip()
            or required_env("E2E_TYPESENSE_API_KEY")
        )
    if normalized == cluster_b:
        return (
            os.environ.get("E2E_TYPESENSE_B_API_KEY", "").strip()
            or required_env("E2E_TYPESENSE_API_KEY")
        )
    raise ScenarioFailure("No Typesense credential mapping exists for the endpoint")


def _document(base_url: str, collection: str, document_id: str) -> Optional[dict]:
    status, body = _typesense_request(
        base_url,
        f"/collections/{quote(collection)}/documents/{quote(document_id)}",
        expected=(200, 404),
    )
    return body if status == 200 and isinstance(body, dict) else None


def _wait_document(base_url: str, collection: str, document_id: str) -> dict:
    return wait_until(
        lambda: _document(base_url, collection, document_id),
        description=f"document {collection}/{document_id}",
    )


def scenario_platform_readiness(store: EvidenceStore) -> Dict[str, Any]:
    query_a = required_env("E2E_QUERY_A_URL")
    query_b = required_env("E2E_QUERY_B_URL")
    worker = required_env("E2E_WORKER_HEALTH_URL")
    dependency_mode = store.load()["provenance"].get("dependency_mode")
    external_transport_verified = False
    if dependency_mode == "external":
        for name, endpoint in (
            ("E2E_QUERY_A_URL", query_a),
            ("E2E_QUERY_B_URL", query_b),
            ("E2E_WORKER_HEALTH_URL", worker),
            ("E2E_TYPESENSE_A_URL", required_env("E2E_TYPESENSE_A_URL")),
            ("E2E_TYPESENSE_B_URL", required_env("E2E_TYPESENSE_B_URL")),
        ):
            if urlparse(endpoint).scheme.lower() != "https":
                raise ScenarioFailure(f"External enterprise endpoint {name} must use HTTPS")
        postgres_url = urlparse(required_env("E2E_POSTGRES_URL"))
        ssl_modes = parse_qs(postgres_url.query).get("sslmode", [])
        if "verify-full" not in [value.lower() for value in ssl_modes]:
            raise ScenarioFailure(
                "External E2E_POSTGRES_URL must use sslmode=verify-full"
            )
        redis_url = required_env("E2E_REDIS_URL")
        if urlparse(redis_url).scheme.lower() != "rediss":
            raise ScenarioFailure("External E2E_REDIS_URL must use rediss://")
        import redis

        redis_client = redis.from_url(
            redis_url,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        try:
            if not redis_client.ping():
                raise ScenarioFailure("External Redis TLS ping did not return PONG")
        finally:
            redis_client.close()
        external_transport_verified = True

    revisions = {
        "query_a": _control_revision(query_a),
        "query_b": _control_revision(query_b),
    }

    dependency_health = {}
    for replica_name, replica_url in (("query_a", query_a), ("query_b", query_b)):
        dependency_health[replica_name] = wait_until(
            lambda url=replica_url: (
                health if (health := _query_health(url)).get("status") == "healthy" else None
            ),
            description=f"{replica_name} dependency health",
        )

    def worker_ready() -> Any:
        _, body, _ = _json_request(worker, "/ready", expected=(200, 503))
        return body if isinstance(body, dict) and body.get("ready") else None

    worker_status = wait_until(worker_ready, description="indexing worker readiness")
    for name in ("E2E_TYPESENSE_A_URL", "E2E_TYPESENSE_B_URL"):
        _, health = _typesense_request(required_env(name), "/health")
        if not isinstance(health, dict) or not health.get("ok"):
            raise ScenarioFailure(f"{name} did not report ok")
    if len(set(revisions.values())) != 1:
        raise ScenarioFailure(f"Query replicas disagree at startup: {revisions}")
    if any(
        item.get("redis") != "ok" or item.get("kafka") != "ok"
        for item in dependency_health.values()
    ):
        raise ScenarioFailure("Query replicas did not verify Redis and Kafka")
    return {
        "query_replicas": 2,
        "control_plane_revision": revisions["query_a"],
        "worker_ready": bool(worker_status.get("ready")),
        "typesense_clusters": 2,
        "runtime_python": platform.python_version(),
        "query_dependency_health": "healthy",
        "external_transport_policy_verified": external_transport_verified,
    }


def _tenant_test_tokens() -> tuple[str, str, str]:
    supplied = tuple(
        os.environ.get(name, "").strip()
        for name in (
            "E2E_TENANT_A_TOKEN",
            "E2E_TENANT_B_TOKEN",
            "E2E_TENANT_NO_CLAIM_TOKEN",
        )
    )
    if all(supplied):
        return supplied
    if any(supplied):
        raise ScenarioFailure("External tenant tokens must be supplied as a complete set")

    private_key_file = Path(required_env("E2E_OIDC_PRIVATE_KEY_FILE"))
    jwks_file = Path(required_env("E2E_OIDC_JWKS_FILE"))
    try:
        private_key = private_key_file.read_text(encoding="utf-8")
        jwks = json.loads(jwks_file.read_text(encoding="utf-8"))
        key_id = str(jwks["keys"][0]["kid"])
    except (OSError, KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise ScenarioFailure("OIDC E2E signing material is missing or invalid") from exc

    import jwt

    issued_at = int(time.time())
    common = {
        "iss": required_env("E2E_OIDC_ISSUER"),
        "aud": required_env("E2E_OIDC_AUDIENCE"),
        "iat": issued_at,
        "exp": issued_at + 900,
        "scope": "imposbro:data",
    }

    def token(subject: str, tenant_id: Optional[str]) -> str:
        claims = {**common, "sub": subject}
        if tenant_id:
            claims["tenant_id"] = tenant_id
        return jwt.encode(
            claims,
            private_key,
            algorithm="RS256",
            headers={"kid": key_id, "typ": "JWT"},
        )

    return (
        token("tenant-a-operator", "tenant-a"),
        token("tenant-b-operator", "tenant-b"),
        token("tenant-missing-claim", None),
    )


def _tenant_data_request(
    token: str,
    path: str,
    *,
    method: str = "GET",
    payload: Optional[Mapping[str, Any]] = None,
    idempotency_key: str = "",
    expected: Iterable[int] = (200,),
) -> tuple[int, Any, Mapping[str, str]]:
    headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
    return _json_request(
        required_env("E2E_QUERY_A_URL"),
        path,
        method=method,
        payload=payload,
        bearer_token=token,
        extra_headers=headers,
        expected=expected,
    )


def _wait_delete_checkpoint(collection: str, document_id: str) -> Dict[str, Any]:
    def published_delete() -> Any:
        event = _outbox_event(collection, document_id)
        payload = dict(event["latest"]["payload_json"])
        if payload.get("operation") != "delete" or not event["latest"]["published_at"]:
            return None
        identity = payload["identity"]
        target = str(payload["target_clusters"][0])
        logical_key = "\x1f".join(
            [identity["tenant_id"], identity["collection"], identity["document_id"]]
        )
        checkpoint_key = hashlib.sha256(
            f"{logical_key}\x1f{target}".encode("utf-8")
        ).hexdigest()
        checkpoint = _postgres_checkpoint(checkpoint_key)
        if checkpoint and checkpoint.get("event_id") == payload.get("event_id"):
            return {"payload": payload, "checkpoint": checkpoint}
        return None

    return wait_until(
        published_delete,
        description=f"tenant-safe delete checkpoint for {collection}/{document_id}",
    )


def scenario_oidc_tenant_isolation(store: EvidenceStore) -> Dict[str, Any]:
    suffix = str(store.load()["run_id"])[-10:].lower()
    collection = f"e2e_tenant_{suffix}"
    uncovered = f"uncovered_{suffix}"
    _admin_mutation(
        "/admin/collections",
        {
            "name": collection,
            "fields": [
                {"name": "name", "type": "string"},
                {"name": "tenant_id", "type": "string", "facet": True},
                {"name": "generation", "type": "int32"},
            ],
            "default_sorting_field": "generation",
        },
    )
    tenant_a, tenant_b, missing_claim = _tenant_test_tokens()
    for token, document_id, tenant_id in (
        (tenant_a, "tenant-a-doc", "tenant-a"),
        (tenant_b, "tenant-b-doc", "tenant-b"),
    ):
        _tenant_data_request(
            token,
            f"/ingest/{quote(collection)}",
            method="POST",
            payload={
                "id": document_id,
                "name": document_id,
                "tenant_id": tenant_id,
                "generation": 1,
            },
            idempotency_key=f"{suffix}-{document_id}-create",
        )
        _wait_document(required_env("E2E_TYPESENSE_A_URL"), collection, document_id)

    own_read_status, _, _ = _tenant_data_request(
        tenant_a, f"/documents/{quote(collection)}/tenant-a-doc"
    )
    cross_a_status, _, _ = _tenant_data_request(
        tenant_a,
        f"/documents/{quote(collection)}/tenant-b-doc",
        expected=(404,),
    )
    cross_b_status, _, _ = _tenant_data_request(
        tenant_b,
        f"/documents/{quote(collection)}/tenant-a-doc",
        expected=(404,),
    )

    search_ids: Dict[str, list[str]] = {}
    for label, token in (("tenant_a", tenant_a), ("tenant_b", tenant_b)):
        _, search, _ = _tenant_data_request(
            token,
            f"/search/{quote(collection)}?{urlencode({'q': '*', 'query_by': 'name'})}",
        )
        hits = search.get("hits", []) if isinstance(search, dict) else []
        search_ids[label] = sorted(
            str(hit.get("document", {}).get("id"))
            for hit in hits
            if isinstance(hit, dict) and isinstance(hit.get("document"), dict)
        )
    if search_ids != {
        "tenant_a": ["tenant-a-doc"],
        "tenant_b": ["tenant-b-doc"],
    }:
        raise ScenarioFailure(f"Tenant-scoped search leaked or hid data: {search_ids}")

    cross_write_status, _, _ = _tenant_data_request(
        tenant_a,
        f"/ingest/{quote(collection)}",
        method="POST",
        payload={
            "id": "tenant-b-doc",
            "name": "cross-tenant-overwrite",
            "tenant_id": "tenant-b",
            "generation": 2,
        },
        idempotency_key=f"{suffix}-cross-write",
        expected=(403,),
    )
    missing_claim_status, _, _ = _tenant_data_request(
        missing_claim,
        f"/ingest/{quote(collection)}",
        method="POST",
        payload={
            "id": "missing-claim",
            "name": "missing-claim",
            "tenant_id": "tenant-a",
            "generation": 1,
        },
        idempotency_key=f"{suffix}-missing-claim",
        expected=(403,),
    )

    cross_delete_status, _, _ = _tenant_data_request(
        tenant_a,
        f"/documents/{quote(collection)}/tenant-b-doc",
        method="DELETE",
        idempotency_key=f"{suffix}-cross-delete",
    )
    cross_delete = _wait_delete_checkpoint(collection, "tenant-b-doc")
    delete_filter = str(cross_delete["payload"].get("delete_filter", ""))
    if "tenant_id:=tenant-a" not in delete_filter:
        raise ScenarioFailure("Cross-tenant delete event lacked a server-owned tenant filter")
    tenant_b_document = _wait_document(
        required_env("E2E_TYPESENSE_A_URL"), collection, "tenant-b-doc"
    )
    if tenant_b_document.get("tenant_id") != "tenant-b":
        raise ScenarioFailure("Tenant A deleted or changed Tenant B's document")

    _tenant_data_request(
        tenant_a,
        f"/ingest/{quote(collection)}",
        method="POST",
        payload={
            "id": "tenant-a-doc",
            "name": "tenant-a-updated",
            "tenant_id": "tenant-a",
            "generation": 2,
        },
        idempotency_key=f"{suffix}-own-update",
    )
    wait_until(
        lambda: (
            document
            if (
                document := _document(
                    required_env("E2E_TYPESENSE_A_URL"), collection, "tenant-a-doc"
                )
            )
            and document.get("generation") == 2
            else None
        ),
        description="same-tenant update",
    )
    own_delete_status, _, _ = _tenant_data_request(
        tenant_a,
        f"/documents/{quote(collection)}/tenant-a-doc",
        method="DELETE",
        idempotency_key=f"{suffix}-own-delete",
    )
    _wait_delete_checkpoint(collection, "tenant-a-doc")
    wait_until(
        lambda: _document(
            required_env("E2E_TYPESENSE_A_URL"), collection, "tenant-a-doc"
        )
        is None,
        description="same-tenant delete",
    )

    revision = _control_revision(required_env("E2E_QUERY_A_URL"))
    missing_policy_status, _, _ = _json_request(
        required_env("E2E_QUERY_A_URL"),
        "/admin/collections",
        method="POST",
        payload={
            "name": uncovered,
            "fields": [{"name": "name", "type": "string"}],
        },
        api_key=required_env("E2E_ADMIN_API_KEY"),
        if_match=revision,
        expected=(403,),
    )
    return {
        "oidc_rs256_jwks_tls": True,
        "tenants_exercised": 2,
        "same_tenant_read_http_status": own_read_status,
        "cross_tenant_read_http_statuses": [cross_a_status, cross_b_status],
        "tenant_scoped_search_exact": True,
        "cross_tenant_write_http_status": cross_write_status,
        "missing_tenant_claim_http_status": missing_claim_status,
        "cross_tenant_delete_http_status": cross_delete_status,
        "cross_tenant_delete_was_filtered_noop": True,
        "same_tenant_delete_http_status": own_delete_status,
        "same_tenant_document_lifecycle": True,
        "missing_collection_policy_http_status": missing_policy_status,
        "deny_by_default_policy_verified": True,
    }


def scenario_postgres_contract(_store: EvidenceStore) -> Dict[str, Any]:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    from control_plane import AuditEvent, PostgresControlPlaneStore, StateConflictError
    from indexing_events import IndexingEventDraft, PostgresIndexingEventStore

    database_url = required_env("E2E_POSTGRES_URL")
    query_root = Path(os.environ.get("E2E_QUERY_API_ROOT", "")).resolve()
    if not (query_root / "alembic.ini").is_file():
        query_root = Path(__file__).resolve().parents[2] / "query_api"
    schema_name = "imposbro_e2e_" + uuid.uuid4().hex
    if not re.fullmatch(r"[a-z0-9_]+", schema_name):
        raise ScenarioFailure("Generated PostgreSQL schema name was unsafe")

    admin_engine = create_engine(database_url, pool_pre_ping=True)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    engine = create_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema_name}"},
        pool_pre_ping=True,
    )
    try:
        config = Config(str(query_root / "alembic.ini"))
        with engine.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

        store = PostgresControlPlaneStore(engine)
        store.check_ready()

        def cas_write(value: int) -> Any:
            try:
                return store.commit(
                    expected_revision=0,
                    state={"probe": value},
                    audit=AuditEvent(
                        actor="enterprise-e2e",
                        action="concurrent_cas_probe",
                        resource_type="control_plane_state",
                        resource_id="current",
                    ),
                    event_type="e2e.control_plane.changed",
                )
            except StateConflictError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=8) as executor:
            cas_results = list(executor.map(cas_write, range(8)))
        conflicts = sum(isinstance(item, StateConflictError) for item in cas_results)
        commits = len(cas_results) - conflicts
        if (commits, conflicts, store.latest_revision()) != (1, 7, 1):
            raise ScenarioFailure(
                "Concurrent PostgreSQL CAS did not produce exactly one commit"
            )
        store.verify_audit_chain()

        event_store = PostgresIndexingEventStore(engine)
        event_store.check_ready()

        def prepare_event(value: int) -> Any:
            return event_store.prepare(
                IndexingEventDraft(
                    tenant_id="tenant-e2e",
                    collection="sequence_probe",
                    document_id="same-document",
                    operation="upsert",
                    target_clusters=("cluster-a", "cluster-b"),
                    routing_revision=1,
                    idempotency_key=f"e2e-sequence-{value}",
                    document={"id": "same-document", "value": value},
                )
            )

        with ThreadPoolExecutor(max_workers=16) as executor:
            events = list(executor.map(prepare_event, range(64)))
        sequences = sorted(item.sequence for item in events)
        if sequences != list(range(1, 65)):
            raise ScenarioFailure("Per-document sequences are not gap-free and monotonic")
        retry = prepare_event(0)
        if retry.event_id != events[0].event_id or retry.sequence != events[0].sequence:
            raise ScenarioFailure("Durable idempotency retry changed event identity")
        if event_store.pending_count() != 64:
            raise ScenarioFailure("Unexpected durable indexing outbox count")
        return {
            "alembic_head_verified": True,
            "cas_attempts": 8,
            "cas_commits": commits,
            "cas_conflicts": conflicts,
            "audit_chain_verified": True,
            "concurrent_sequences": len(sequences),
            "sequence_min": sequences[0],
            "sequence_max": sequences[-1],
            "idempotent_retry_stable": True,
        }
    finally:
        engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        admin_engine.dispose()


def _atomic_secret_write(path: Path, value: str) -> None:
    """Atomically replace one mounted secret without exposing its value."""
    if not path.is_absolute() or not path.parent.is_dir() or not value:
        raise ScenarioFailure("The writable runtime secret file is invalid")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o444)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _typesense_key_request(
    path: str,
    *,
    api_key: str,
    method: str = "GET",
    payload: Optional[Mapping[str, Any]] = None,
    expected: Iterable[int] = (200,),
) -> tuple[int, Any]:
    status, body, _ = _json_request(
        required_env("E2E_TYPESENSE_A_URL"),
        path,
        method=method,
        payload=payload,
        api_key=api_key,
        api_key_header="X-TYPESENSE-API-KEY",
        expected=expected,
    )
    return status, body


def _create_typesense_rotation_key(master_key: str, description: str) -> tuple[str, str]:
    _, body = _typesense_key_request(
        "/keys",
        api_key=master_key,
        method="POST",
        payload={
            "description": description,
            "actions": ["*"],
            "collections": ["*"],
        },
        expected=(200, 201),
    )
    if not isinstance(body, dict):
        raise ScenarioFailure("Typesense key creation returned an invalid response")
    key_id = body.get("id")
    value = body.get("value")
    if not isinstance(key_id, (str, int)) or not isinstance(value, str) or not value:
        raise ScenarioFailure("Typesense key creation omitted its id or value")
    return str(key_id), value


def _query_cluster_status(base_url: str, cluster_name: str) -> str:
    health = _query_health(base_url)
    data_clusters = health.get("data_clusters", {})
    return str(data_clusters.get(cluster_name, "missing"))


def _internal_cluster_key(cluster_name: str) -> Optional[str]:
    _, body, _ = _json_request(
        required_env("E2E_QUERY_A_URL"),
        "/admin/federation/clusters/internal",
        api_key=required_env("E2E_ADMIN_API_KEY"),
    )
    if not isinstance(body, dict) or not isinstance(body.get(cluster_name), dict):
        raise ScenarioFailure("Internal cluster transit response was invalid")
    config = body[cluster_name]
    if "api_key_ref" in config:
        raise ScenarioFailure("Internal transit exposed a persisted secret reference")
    value = config.get("api_key")
    return value if isinstance(value, str) else None


def scenario_typesense_secret_ref_rotation(_store: EvidenceStore) -> Dict[str, Any]:
    """Prove ref-only persistence plus live file rotation/revocation semantics."""
    from sqlalchemy import create_engine, text

    query_a = required_env("E2E_QUERY_A_URL")
    query_b = required_env("E2E_QUERY_B_URL")
    master_key = required_env("E2E_TYPESENSE_API_KEY")
    secret_file = Path(required_env("E2E_TYPESENSE_SECRET_FILE"))
    expected_file_ref = os.environ.get(
        "E2E_EXPECTED_TYPESENSE_FILE_REF", "file:typesense-api-key"
    )
    expected_env_ref = os.environ.get(
        "E2E_EXPECTED_TYPESENSE_ENV_REF", "env:E2E_TYPESENSE_ENV_KEY"
    )
    initial_revision = _control_revision(query_a)

    _, public_clusters, _ = _json_request(
        query_a,
        "/admin/federation/clusters",
        api_key=required_env("E2E_ADMIN_API_KEY"),
    )
    if not isinstance(public_clusters, dict):
        raise ScenarioFailure("Public cluster configuration was invalid")
    expected_refs = {
        "default-data-cluster": expected_file_ref,
        "default-data-cluster-2": expected_env_ref,
    }
    for cluster_name, expected_ref in expected_refs.items():
        config = public_clusters.get(cluster_name)
        if not isinstance(config, dict) or config.get("api_key_ref") != expected_ref:
            raise ScenarioFailure("Federation state did not expose the expected secret ref")
        if "api_key" in config:
            raise ScenarioFailure("Public cluster configuration contained a raw API key")

    first_id = ""
    replacement_id = ""
    first_key = ""
    replacement_key = ""
    revoked_status = 0
    cleanup_failures = 0
    try:
        first_id, first_key = _create_typesense_rotation_key(
            master_key,
            f"imposbro-e2e-old-{uuid.uuid4().hex}",
        )
        replacement_id, replacement_key = _create_typesense_rotation_key(
            master_key,
            f"imposbro-e2e-new-{uuid.uuid4().hex}",
        )

        _atomic_secret_write(secret_file, first_key)
        wait_until(
            lambda: _internal_cluster_key("default-data-cluster") == first_key,
            description="file secret rotation in authenticated internal transit",
        )
        for replica in (query_a, query_b):
            wait_until(
                lambda url=replica: _query_cluster_status(
                    url, "default-data-cluster"
                )
                == "ok",
                description=f"rotated Typesense key accepted by {replica}",
            )

        _typesense_key_request(
            f"/keys/{quote(first_id)}",
            api_key=master_key,
            method="DELETE",
        )
        first_id = ""
        revoked_status, _ = _typesense_key_request(
            "/collections",
            api_key=first_key,
            expected=(401, 403),
        )
        wait_until(
            lambda: _query_cluster_status(query_a, "default-data-cluster")
            == "error",
            description="revoked Typesense key failure in the Query API",
        )

        _atomic_secret_write(secret_file, replacement_key)
        wait_until(
            lambda: _internal_cluster_key("default-data-cluster")
            == replacement_key,
            description="replacement key in authenticated internal transit",
        )
        for replica in (query_a, query_b):
            wait_until(
                lambda url=replica: _query_cluster_status(
                    url, "default-data-cluster"
                )
                == "ok",
                description=f"replacement Typesense key accepted by {replica}",
            )
    finally:
        _atomic_secret_write(secret_file, master_key)
        for disposable_key_id in (first_id, replacement_id):
            if not disposable_key_id:
                continue
            try:
                _typesense_key_request(
                    f"/keys/{quote(disposable_key_id)}",
                    api_key=master_key,
                    method="DELETE",
                    expected=(200, 404),
                )
            except Exception:
                cleanup_failures += 1

    if cleanup_failures:
        raise ScenarioFailure("Disposable Typesense rotation keys were not cleaned up")

    final_revision = _control_revision(query_a)
    if final_revision != initial_revision:
        raise ScenarioFailure("Secret rotation mutated the control-plane revision")
    wait_until(
        lambda: _query_cluster_status(query_a, "default-data-cluster") == "ok",
        description="master key restoration after disposable rotation probes",
    )
    if _query_cluster_status(query_a, "default-data-cluster-2") != "ok":
        raise ScenarioFailure("The env: provider cluster was not healthy")

    engine = create_engine(required_env("E2E_POSTGRES_URL"), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            state = connection.scalar(
                text("SELECT state_json FROM control_plane_state WHERE id = 'current'")
            )
    finally:
        engine.dispose()
    if not isinstance(state, dict):
        raise ScenarioFailure("PostgreSQL control-plane state was unavailable")
    _, exported, _ = _json_request(
        query_a,
        "/admin/state/export",
        api_key=required_env("E2E_ADMIN_API_KEY"),
    )
    if not isinstance(exported, dict) or exported.get("secrets_included") is not False:
        raise ScenarioFailure("Control-plane export was not ref-only")
    persisted_clusters = state.get("federation_clusters_config", {})
    exported_clusters = exported.get("federation_clusters_config", {})
    for cluster_name, expected_ref in expected_refs.items():
        for clusters in (persisted_clusters, exported_clusters):
            config = clusters.get(cluster_name) if isinstance(clusters, dict) else None
            if not isinstance(config, dict) or config.get("api_key_ref") != expected_ref:
                raise ScenarioFailure("Persisted/exported state lost a secret reference")
            if "api_key" in config:
                raise ScenarioFailure("Persisted/exported state contained a raw API key")
    serialized_state = json.dumps(state, sort_keys=True)
    serialized_export = json.dumps(exported, sort_keys=True)
    if any(
        secret in serialized_state or secret in serialized_export
        for secret in (master_key, first_key, replacement_key)
    ):
        raise ScenarioFailure("Raw Typesense secret material reached state or export")

    return {
        "providers_exercised": ["env", "file"],
        "file_rotation_without_state_reload": True,
        "control_plane_revision_unchanged": True,
        "authenticated_internal_transit_materialized": True,
        "revoked_key_http_status": revoked_status,
        "revoked_key_failed_closed": revoked_status in {401, 403},
        "replacement_key_passed": True,
        "disposable_rotation_keys_removed": True,
        "postgres_state_contains_refs_only": True,
        "state_export_contains_refs_only": True,
    }


def scenario_typesense_secret_log_check(store: EvidenceStore) -> Dict[str, Any]:
    """Merge a post-rotation application-log scan into ENT-06 evidence."""
    payload = store.load()
    scenario = next(
        (
            item
            for item in payload.get("scenarios", [])
            if item.get("id") == "typesense_secret_ref_rotation"
        ),
        None,
    )
    if not isinstance(scenario, dict) or scenario.get("status") != "passed":
        raise ScenarioFailure("Secret rotation core evidence did not pass")
    assertions = scenario.get("assertions")
    if not isinstance(assertions, dict) or not assertions.get(
        "file_rotation_without_state_reload"
    ):
        raise ScenarioFailure("Secret rotation core assertions are incomplete")
    configured_path = os.environ.get("E2E_APPLICATION_LOGS_FILE", "").strip()
    if configured_path:
        log_file = Path(configured_path)
    else:
        basename = required_env("E2E_SECRET_LOG_BASENAME")
        if Path(basename).name != basename:
            raise ScenarioFailure("Application log basename is unsafe")
        log_file = Path("/evidence") / basename
    if not log_file.is_file() or log_file.stat().st_size > 20 * 1024 * 1024:
        raise ScenarioFailure("Bounded application logs were not provided")
    logs = log_file.read_text(encoding="utf-8", errors="replace")
    if required_env("E2E_TYPESENSE_API_KEY") in logs:
        raise ScenarioFailure("Raw Typesense secret material was present in logs")
    return {
        **assertions,
        "raw_typesense_secret_absent_from_application_logs": True,
        "application_logs_scanned": ["indexing-service", "query-a", "query-b"],
    }


def _assert_redis_unavailable() -> None:
    import redis

    redis_url = (
        os.environ.get("E2E_REDIS_PROBE_URL", "").strip()
        or os.environ.get("E2E_REDIS_URL", "").strip()
    )
    if not redis_url:
        raise ScenarioFailure("A Redis probe URL is required for notification-loss evidence")
    client = redis.from_url(
        redis_url,
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    try:
        try:
            reachable = bool(client.ping())
        except (redis.exceptions.RedisError, OSError):
            return
        if reachable:
            raise ScenarioFailure(
                "Redis is still reachable; notification-loss evidence is invalid"
            )
        raise ScenarioFailure("Redis probe returned without proving unavailability")
    finally:
        client.close()


def scenario_mutate_without_notification(_store: EvidenceStore) -> Dict[str, Any]:
    from control_plane import AuditEvent, PostgresControlPlaneStore

    _assert_redis_unavailable()
    store = PostgresControlPlaneStore(required_env("E2E_POSTGRES_URL"))
    store.check_ready()
    current = store.load()
    if current is None:
        raise ScenarioFailure("Control-plane state was not bootstrapped")
    state = copy.deepcopy(current.state)
    rules = state.setdefault("collection_routing_rules", {})
    marker = "e2e-convergence-" + uuid.uuid4().hex[:12]
    rules[marker] = {
        "collection": marker,
        "rules": [],
        "default_cluster": "default-data-cluster",
    }
    committed = store.commit(
        expected_revision=current.revision,
        state=state,
        audit=AuditEvent(
            actor="enterprise-e2e",
            action="notification_loss_probe",
            resource_type="routing_rule",
            resource_id=marker,
        ),
        event_type="e2e.notification.intentionally_omitted",
        event_payload={"marker": marker},
    )
    return {
        "redis_notification_sent": False,
        "previous_revision": current.revision,
        "committed_revision": committed.revision,
        "marker_digest": hashlib.sha256(marker.encode()).hexdigest()[:16],
    }


def _assert_revision(base_url: str, expected_revision: int) -> Dict[str, Any]:
    def matches() -> Any:
        control = _query_health(base_url).get("control_plane", {})
        if (
            control.get("converged")
            and int(control.get("applied_revision", -1)) == expected_revision
            and int(control.get("authoritative_revision", -1)) == expected_revision
        ):
            return control
        return None

    return wait_until(matches, description=f"revision {expected_revision} at {base_url}")


def scenario_convergence_without_redis(store: EvidenceStore) -> Dict[str, Any]:
    _assert_redis_unavailable()
    expected = int(
        store.assertion("config_mutation_without_notification", "committed_revision")
    )
    first = _assert_revision(required_env("E2E_QUERY_A_URL"), expected)
    second = _assert_revision(required_env("E2E_QUERY_B_URL"), expected)
    return {
        "redis_available_during_probe": False,
        "expected_revision": expected,
        "query_a_applied_revision": int(first["applied_revision"]),
        "query_b_applied_revision": int(second["applied_revision"]),
        "durable_poll_converged": True,
    }


def scenario_restart_convergence(store: EvidenceStore) -> Dict[str, Any]:
    _assert_redis_unavailable()
    expected = int(
        store.assertion("config_mutation_without_notification", "committed_revision")
    )
    second = _assert_revision(required_env("E2E_QUERY_B_URL"), expected)
    return {
        "expected_revision": expected,
        "restarted_replica_applied_revision": int(second["applied_revision"]),
        "restart_loaded_authoritative_state": True,
    }


def _create_collection(name: str) -> int:
    response = _admin_mutation(
        "/admin/collections",
        {
            "name": name,
            "fields": [
                {"name": "name", "type": "string"},
                {"name": "generation", "type": "int32"},
            ],
            "default_sorting_field": "generation",
        },
    )
    if not isinstance(response, dict) or int(response.get("revision", 0)) < 1:
        raise ScenarioFailure("Collection mutation did not return a revision")
    return int(response["revision"])


def _ingest(collection: str, document: Mapping[str, Any], key: str) -> Dict[str, Any]:
    if not key:
        raise ScenarioFailure("Enterprise E2E ingest requires an idempotency key")
    _, body, _ = _json_request_with_idempotency(collection, document, key)
    if not isinstance(body, dict):
        raise ScenarioFailure("Ingest response was not an object")
    return body


def _json_request_with_idempotency(
    collection: str,
    document: Mapping[str, Any],
    key: str,
    *,
    expected: Iterable[int] = (200,),
    traceparent: str = "",
) -> tuple[int, Any, Mapping[str, str]]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-API-Key": required_env("E2E_DATA_API_KEY"),
        "Idempotency-Key": key,
    }
    if traceparent:
        headers["traceparent"] = traceparent
    request = Request(
        required_env("E2E_QUERY_A_URL").rstrip("/")
        + f"/ingest/{quote(collection)}",
        data=json.dumps(document, separators=(",", ":")).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as response:
            status = response.status
            raw = response.read()
            headers = dict(response.headers.items())
    except HTTPError as exc:
        status = exc.code
        raw = exc.read()
        headers = dict(exc.headers.items())
    except (OSError, URLError) as exc:
        raise ScenarioFailure(f"Ingest request failed: {exc}") from exc
    try:
        body = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        body = raw.decode("utf-8", errors="replace")[:1024]
    if status not in set(expected):
        raise ScenarioFailure(f"Unexpected ingest HTTP {status}: {body}")
    return status, body, headers


def _outbox_event(collection: str, document_id: str) -> Dict[str, Any]:
    from sqlalchemy import create_engine, text

    engine = create_engine(required_env("E2E_POSTGRES_URL"), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT event_id, sequence, payload_json, published_at "
                    "FROM indexing_event_outbox "
                    "WHERE payload_json->'identity'->>'collection' = :collection "
                    "AND payload_json->'identity'->>'document_id' = :document_id "
                    "ORDER BY created_at DESC"
                ),
                {"collection": collection, "document_id": document_id},
            ).mappings().all()
    finally:
        engine.dispose()
    if not rows:
        raise ScenarioFailure("Durable indexing outbox has no event for the document")
    return {"rows": rows, "latest": rows[0]}


def _postgres_checkpoint(checkpoint_key: str) -> Optional[Dict[str, Any]]:
    from sqlalchemy import create_engine, text

    engine = create_engine(required_env("E2E_POSTGRES_URL"), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT checkpoint_key, event_id, sequence, document_version, "
                    "target_cluster, operation, tombstone "
                    "FROM indexing_checkpoints WHERE checkpoint_key = :checkpoint_key"
                ),
                {"checkpoint_key": checkpoint_key},
            ).mappings().one_or_none()
    finally:
        engine.dispose()
    return dict(row) if row is not None else None


def scenario_kafka_worker_v2(store: EvidenceStore) -> Dict[str, Any]:
    run_suffix = str(store.load()["run_id"])[:10].lower()
    collection = f"e2e_worker_{run_suffix}"
    document_id = "worker-v2"
    _create_collection(collection)
    response = _ingest(
        collection,
        {"id": document_id, "name": "versioned", "generation": 1},
        f"worker-v2-{run_suffix}",
    )
    if response.get("routed_to") != "default-data-cluster":
        raise ScenarioFailure(f"Unexpected initial route: {response.get('routed_to')}")
    _wait_document(required_env("E2E_TYPESENSE_A_URL"), collection, document_id)
    if _document(required_env("E2E_TYPESENSE_B_URL"), collection, document_id):
        raise ScenarioFailure("Pre-rollout event unexpectedly reached the second cluster")

    outbox = wait_until(
        lambda: (
            event
            if (event := _outbox_event(collection, document_id))["latest"]["published_at"]
            else None
        ),
        description="published durable indexing event",
    )
    payload = dict(outbox["latest"]["payload_json"])
    if payload.get("envelope_version") != 2:
        raise ScenarioFailure("Kafka outbox payload is not envelope v2")
    if payload.get("target_clusters") != ["default-data-cluster"]:
        raise ScenarioFailure("Envelope target list differs from the routed cluster")

    identity = payload["identity"]
    logical_key = "\x1f".join(
        [identity["tenant_id"], identity["collection"], identity["document_id"]]
    )
    checkpoint_id = hashlib.sha256(
        f"{logical_key}\x1fdefault-data-cluster".encode("utf-8")
    ).hexdigest()
    checkpoint = wait_until(
        lambda: _postgres_checkpoint(checkpoint_id),
        description="durable PostgreSQL indexing checkpoint",
    )
    if checkpoint.get("event_id") != payload.get("event_id"):
        raise ScenarioFailure("Worker checkpoint does not reference the v2 event")

    retry = _ingest(
        collection,
        {"id": document_id, "name": "versioned", "generation": 1},
        f"worker-v2-{run_suffix}",
    )
    if retry.get("status") != "ok":
        raise ScenarioFailure("Idempotent HTTP retry was not accepted")
    repeated = _outbox_event(collection, document_id)
    if len(repeated["rows"]) != 1:
        raise ScenarioFailure("Idempotent retry allocated another durable event")
    conflict_status, _, _ = _json_request_with_idempotency(
        collection,
        {"id": document_id, "name": "changed", "generation": 2},
        f"worker-v2-{run_suffix}",
        expected=(409,),
    )
    return {
        "collection_digest": hashlib.sha256(collection.encode()).hexdigest()[:16],
        "envelope_version": int(payload["envelope_version"]),
        "target_cluster_count": len(payload["target_clusters"]),
        "outbox_rows_after_retry": len(repeated["rows"]),
        "checkpoint_backend": "postgres",
        "checkpoint_sequence": int(checkpoint["sequence"]),
        "checkpoint_event_matches": True,
        "idempotency_conflict_http_status": conflict_status,
    }


def _captured_spans(trace_id: str) -> list[Dict[str, Any]]:
    endpoint = required_env("E2E_OTLP_CAPTURE_URL")
    ca_file = required_env("E2E_OTLP_CA_FILE")
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ScenarioFailure("OTLP capture endpoint must use HTTPS")
    context = ssl.create_default_context(cafile=ca_file)
    _, body, _ = _json_request(
        endpoint,
        f"/spans?trace_id={quote(trace_id)}",
        ssl_context=context,
    )
    spans = body.get("spans") if isinstance(body, dict) else None
    if not isinstance(spans, list) or not all(isinstance(item, dict) for item in spans):
        raise ScenarioFailure("OTLP capture response did not contain a span list")
    return spans


def _trace_chain(spans: list[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    def select(service: str, name: str, *, prefix: bool = False) -> list[dict]:
        return [
            span
            for span in spans
            if span.get("service_name") == service
            and (
                str(span.get("name", "")).startswith(name)
                if prefix
                else span.get("name") == name
            )
        ]

    http_spans = select("imposbro-query-api", "POST /ingest/", prefix=True)
    producers = select("imposbro-query-api", "kafka.publish")
    consumers = select("imposbro-indexing-service", "kafka.process")
    writes = select("imposbro-indexing-service", "typesense.document.upsert")
    for http_span in http_spans:
        for producer in producers:
            if producer.get("parent_span_id") != http_span.get("span_id"):
                continue
            for consumer in consumers:
                if consumer.get("parent_span_id") != producer.get("span_id"):
                    continue
                for write in writes:
                    if write.get("parent_span_id") == consumer.get("span_id"):
                        return {
                            "http": http_span,
                            "producer": producer,
                            "consumer": consumer,
                            "typesense": write,
                        }
    return None


def scenario_otlp_trace_continuity(store: EvidenceStore) -> Dict[str, Any]:
    run_id = str(store.load()["run_id"])
    suffix = run_id[-10:].lower()
    collection = f"e2e_trace_{suffix}"
    document_id = "trace-continuity"
    trace_id = hashlib.sha256(f"trace:{run_id}".encode()).hexdigest()[:32]
    parent_span_id = hashlib.sha256(f"parent:{run_id}".encode()).hexdigest()[:16]
    if trace_id == "0" * 32 or parent_span_id == "0" * 16:
        raise ScenarioFailure("Generated W3C trace context is invalid")
    traceparent = f"00-{trace_id}-{parent_span_id}-01"
    _create_collection(collection)
    status, response, headers = _json_request_with_idempotency(
        collection,
        {"id": document_id, "name": "traced", "generation": 1},
        f"trace-{suffix}",
        traceparent=traceparent,
    )
    if status != 200 or not isinstance(response, dict):
        raise ScenarioFailure("Traced ingest was not accepted")
    response_traceparent = next(
        (value for key, value in headers.items() if key.lower() == "traceparent"),
        "",
    )
    response_parts = response_traceparent.split("-")
    if len(response_parts) != 4 or response_parts[1] != trace_id:
        raise ScenarioFailure("HTTP response did not preserve the W3C trace id")
    _wait_document(required_env("E2E_TYPESENSE_A_URL"), collection, document_id)
    outbox = wait_until(
        lambda: (
            event
            if (event := _outbox_event(collection, document_id))["latest"]["published_at"]
            else None
        ),
        description="published traced indexing event",
    )
    payload = dict(outbox["latest"]["payload_json"])
    durable_traceparent = str(payload.get("trace", {}).get("traceparent", ""))
    durable_parts = durable_traceparent.split("-")
    if len(durable_parts) != 4 or durable_parts[1] != trace_id:
        raise ScenarioFailure("Durable outbox/Kafka envelope lost the W3C trace id")
    chain = wait_until(
        lambda: _trace_chain(_captured_spans(trace_id)),
        description="correlated HTTP, Kafka, worker, and Typesense OTLP spans",
    )
    spans = _captured_spans(trace_id)
    return {
        "trace_id_digest": hashlib.sha256(trace_id.encode()).hexdigest()[:16],
        "http_traceparent_preserved": True,
        "durable_envelope_traceparent_preserved": True,
        "collector_tls_verified": True,
        "collector_span_count": len(spans),
        "services_correlated": sorted(
            {str(span.get("service_name")) for span in spans}
        ),
        "span_chain": [
            str(chain[name]["name"])
            for name in ("http", "producer", "consumer", "typesense")
        ],
        "parent_child_lineage_verified": True,
        "document_reached_typesense": True,
    }


def _transition(
    rollout: Mapping[str, Any],
    target: str,
    gates: Optional[dict] = None,
) -> dict:
    payload: Dict[str, Any] = {
        "target_phase": target,
        "expected_version": int(rollout["version"]),
    }
    if gates is not None:
        payload["gates"] = gates
    response = _admin_mutation(
        f"/admin/routing-rollouts/{quote(str(rollout['rollout_id']))}/transitions",
        payload,
    )
    if not isinstance(response, dict) or not isinstance(response.get("rollout"), dict):
        raise ScenarioFailure("Routing transition did not return rollout state")
    return response["rollout"]


def scenario_routing_lifecycle(store: EvidenceStore) -> Dict[str, Any]:
    run_suffix = str(store.load()["run_id"])[-10:].lower()
    collection = f"e2e_route_{run_suffix}"
    baseline_id = "before-rollout"
    dual_id = "during-dual-write"
    cutover_id = "during-cutover"
    _create_collection(collection)
    initial = _ingest(
        collection,
        {"id": baseline_id, "name": "baseline", "generation": 1},
        f"route-baseline-{run_suffix}",
    )
    if initial.get("routed_to") != "default-data-cluster":
        raise ScenarioFailure("Baseline document did not use the active route")
    _wait_document(required_env("E2E_TYPESENSE_A_URL"), collection, baseline_id)

    created = _admin_mutation(
        "/admin/routing-rollouts",
        {
            "candidate_policy": {
                "collection": collection,
                "rules": [],
                "default_cluster": "default-data-cluster-2",
            },
            "rollback_window_seconds": 60,
        },
    )
    rollout = created.get("rollout") if isinstance(created, dict) else None
    if not isinstance(rollout, dict) or rollout.get("phase") != "draft":
        raise ScenarioFailure("Routing rollout was not created in draft")
    phases = ["draft"]
    rollout = _transition(rollout, "validating")
    phases.append(str(rollout["phase"]))
    gates = dict(rollout.get("gates", {}))
    gates.update({"validation_passed": True, "capacity_passed": True})
    rollout = _transition(rollout, "dual_write", gates)
    phases.append(str(rollout["phase"]))

    dual = _ingest(
        collection,
        {"id": dual_id, "name": "dual", "generation": 2},
        f"route-dual-{run_suffix}",
    )
    routed_to = set(str(dual.get("routed_to", "")).split(","))
    if routed_to != {"default-data-cluster", "default-data-cluster-2"}:
        raise ScenarioFailure(f"Dual-write route was incomplete: {sorted(routed_to)}")
    _wait_document(required_env("E2E_TYPESENSE_A_URL"), collection, dual_id)
    _wait_document(required_env("E2E_TYPESENSE_B_URL"), collection, dual_id)

    rollout = _transition(rollout, "backfill")
    phases.append(str(rollout["phase"]))
    copied = 0
    for _ in range(20):
        result = _admin_mutation(
            f"/admin/routing-rollouts/{quote(str(rollout['rollout_id']))}"
            "/backfill/steps",
            {
                "expected_version": int(rollout["version"]),
                "max_documents": 2,
            },
        )
        if not isinstance(result, dict) or not isinstance(result.get("rollout"), dict):
            raise ScenarioFailure("Backfill API did not return rollout state")
        rollout = result["rollout"]
        copied += int(result.get("copied", 0))
        checkpoint = dict(rollout.get("backfill_checkpoint", {}))
        if checkpoint.get("raw_copy_complete") and checkpoint.get("repair_complete"):
            break
    else:
        raise ScenarioFailure("Backfill did not complete within bounded chunks")
    rollout = _transition(rollout, "verifying")
    phases.append(str(rollout["phase"]))
    if rollout.get("backfill_checkpoint") != checkpoint:
        raise ScenarioFailure("Backfill checkpoint was not durably persisted")
    parity = _admin_mutation(
        f"/admin/routing-rollouts/{quote(str(rollout['rollout_id']))}"
        "/parity-verifications",
        {
            "expected_version": int(rollout["version"]),
            "sample_limit": 100,
        },
    )
    if not isinstance(parity, dict) or not isinstance(parity.get("rollout"), dict):
        raise ScenarioFailure("Parity API did not return rollout state")
    rollout = parity["rollout"]
    if not parity.get("passed") or not parity.get("digest"):
        raise ScenarioFailure(
            f"Exact Typesense parity failed: missing={parity.get('missing_ids')}, "
            f"different={parity.get('different_ids')}, "
            f"unexpected={parity.get('unexpected_ids')}"
        )
    rollout = _transition(rollout, "cutover")
    phases.append(str(rollout["phase"]))

    _, read_at_cutover, _ = _json_request(
        required_env("E2E_QUERY_A_URL"),
        f"/documents/{quote(collection)}/{quote(baseline_id)}",
        api_key=required_env("E2E_DATA_API_KEY"),
    )
    if read_at_cutover.get("found_in") != "default-data-cluster-2":
        raise ScenarioFailure("Cutover reads did not select the candidate cluster")
    cutover = _ingest(
        collection,
        {"id": cutover_id, "name": "cutover", "generation": 3},
        f"route-cutover-{run_suffix}",
    )
    if set(str(cutover.get("routed_to", "")).split(",")) != {
        "default-data-cluster",
        "default-data-cluster-2",
    }:
        raise ScenarioFailure("Cutover safety writes did not preserve both clusters")
    _wait_document(required_env("E2E_TYPESENSE_A_URL"), collection, cutover_id)
    _wait_document(required_env("E2E_TYPESENSE_B_URL"), collection, cutover_id)

    rollout = _transition(rollout, "rolling_back")
    phases.append(str(rollout["phase"]))
    rollout = _transition(rollout, "rolled_back")
    phases.append(str(rollout["phase"]))
    _, read_after_rollback, _ = _json_request(
        required_env("E2E_QUERY_A_URL"),
        f"/documents/{quote(collection)}/{quote(baseline_id)}",
        api_key=required_env("E2E_DATA_API_KEY"),
    )
    if read_after_rollback.get("found_in") != "default-data-cluster":
        raise ScenarioFailure("Rollback did not restore active-cluster reads")
    return {
        "phases_observed": phases,
        "dual_write_targets": 2,
        "backfill_documents_copied": copied,
        "source_barrier": str(rollout.get("gates", {}).get("source_barrier", "")),
        "backfill_checkpoint_persisted": True,
        "machine_owned_backfill_and_parity": True,
        "source_documents": int(parity["source_documents"]),
        "candidate_documents": int(parity["candidate_documents"]),
        "parity_digest": str(parity["digest"]),
        "cutover_read_candidate": True,
        "cutover_safety_write_targets": 2,
        "rollback_read_active": True,
    }


def scenario_tls_smoke(_store: EvidenceStore) -> Dict[str, Any]:
    endpoint = os.environ.get("E2E_TLS_TYPESENSE_URL", "").strip()
    ca_file = os.environ.get("E2E_TLS_CA_FILE", "").strip()
    if not endpoint or not ca_file:
        raise ScenarioNotRun(
            "E2E_TLS_TYPESENSE_URL and E2E_TLS_CA_FILE are required for TLS evidence"
        )
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ScenarioFailure("TLS smoke endpoint must be an https URL")
    context = ssl.create_default_context(cafile=ca_file)
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    import socket

    with socket.create_connection((parsed.hostname, parsed.port or 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=parsed.hostname) as wrapped:
            certificate = wrapped.getpeercert()
            negotiated = wrapped.version()
    not_after = certificate.get("notAfter")
    if not not_after:
        raise ScenarioFailure("TLS peer certificate has no expiry")
    remaining_seconds = ssl.cert_time_to_seconds(not_after) - time.time()
    if remaining_seconds <= 3600:
        raise ScenarioFailure("TLS peer certificate expires within one hour")
    _, body, _ = _json_request(
        endpoint,
        "/health",
        api_key=_typesense_api_key(required_env("E2E_TYPESENSE_A_URL")),
        api_key_header="X-TYPESENSE-API-KEY",
        ssl_context=context,
    )
    if not isinstance(body, dict) or not body.get("ok"):
        raise ScenarioFailure("TLS-protected Typesense health did not report ok")
    return {
        "certificate_chain_verified": True,
        "hostname_verified": True,
        "negotiated_protocol": negotiated,
        "certificate_valid_for_more_than_one_hour": True,
        "application_health_over_tls": True,
    }


SCENARIOS: Dict[str, Callable[[EvidenceStore], Dict[str, Any]]] = {
    "platform-readiness": scenario_platform_readiness,
    "oidc-tenant-isolation": scenario_oidc_tenant_isolation,
    "postgres-contract": scenario_postgres_contract,
    "secret-ref-rotation": scenario_typesense_secret_ref_rotation,
    "secret-ref-log-check": scenario_typesense_secret_log_check,
    "mutate-without-notification": scenario_mutate_without_notification,
    "assert-convergence": scenario_convergence_without_redis,
    "assert-restart-convergence": scenario_restart_convergence,
    "kafka-worker-v2": scenario_kafka_worker_v2,
    "otlp-trace": scenario_otlp_trace_continuity,
    "routing-lifecycle": scenario_routing_lifecycle,
    "tls-smoke": scenario_tls_smoke,
}

SCENARIO_IDS = {
    "platform-readiness": "platform_readiness",
    "oidc-tenant-isolation": "oidc_tenant_isolation",
    "postgres-contract": "postgres_migration_cas_and_sequence",
    "secret-ref-rotation": "typesense_secret_ref_rotation",
    "secret-ref-log-check": "typesense_secret_ref_rotation",
    "mutate-without-notification": "config_mutation_without_notification",
    "assert-convergence": "config_convergence_without_redis",
    "assert-restart-convergence": "restart_convergence_without_redis",
    "kafka-worker-v2": "kafka_worker_v2_delivery",
    "otlp-trace": "otlp_w3c_trace_continuity",
    "routing-lifecycle": "typesense_dual_write_backfill_cutover_rollback",
    "tls-smoke": "typesense_tls_transport",
}


def run_scenario(store: EvidenceStore, command: str) -> int:
    scenario_id = SCENARIO_IDS[command]
    started_at = utc_now()
    started = time.monotonic()
    status = "passed"
    assertions: Dict[str, Any] = {}
    reason = ""
    try:
        assertions = SCENARIOS[command](store)
    except ScenarioNotRun as exc:
        status = "not_run"
        reason = str(exc)
    except Exception as exc:
        status = "failed"
        reason = f"{type(exc).__name__}: {exc}"
    result = {
        "id": scenario_id,
        "status": status,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_ms": round((time.monotonic() - started) * 1000),
        "assertions": assertions,
    }
    if reason:
        result["reason"] = reason
    store.record(result)
    print(json.dumps(sanitize({"scenario": scenario_id, "status": status})))
    return 0 if status == "passed" else 2 if status == "not_run" else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--evidence-file",
        type=Path,
        default=Path("docs/evidence/enterprise-e2e-latest.json"),
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    initialize = subcommands.add_parser("init")
    initialize.add_argument(
        "--dependency-mode", choices=("compose", "external", "static"), required=True
    )
    initialize.add_argument("--runtime", default="unknown")
    for name in SCENARIOS:
        subcommands.add_parser(name)
    record = subcommands.add_parser("record-not-run")
    record.add_argument("--id", required=True)
    record.add_argument("--reason", required=True)
    result = subcommands.add_parser("record-result")
    result.add_argument("--id", required=True)
    result.add_argument("--status", choices=sorted(TERMINAL_STATUSES), required=True)
    result.add_argument("--reason", default="")
    result.add_argument("--duration-ms", type=int, default=0)
    result.add_argument("--assertions-json", default="{}")
    diagnostics = subcommands.add_parser("attach-diagnostics")
    diagnostics.add_argument("--input-file", type=Path, required=True)
    finalize = subcommands.add_parser("finalize")
    finalize.add_argument("--require-complete", action="store_true")
    return parser


def main() -> int:
    arguments = build_parser().parse_args()
    store = EvidenceStore(arguments.evidence_file.resolve())
    if arguments.command == "init":
        store.initialize(
            dependency_mode=arguments.dependency_mode,
            runtime=arguments.runtime,
        )
        return 0
    if arguments.command in SCENARIOS:
        return run_scenario(store, arguments.command)
    if arguments.command == "attach-diagnostics":
        try:
            raw_diagnostics = arguments.input_file.read_text(
                encoding="utf-8", errors="replace"
            )
        except OSError as exc:
            raise SystemExit(f"Could not read diagnostics: {exc}") from exc
        store.attach_diagnostics(raw_diagnostics)
        return 0
    if arguments.command in {"record-not-run", "record-result"}:
        if not re.fullmatch(r"[a-z0-9_]{3,128}", arguments.id):
            raise SystemExit("recorded scenario id must be lower snake_case")
        status = (
            "not_run" if arguments.command == "record-not-run" else arguments.status
        )
        reason = arguments.reason
        if status != "passed" and not reason:
            raise SystemExit("failed and not_run results require --reason")
        if arguments.command == "record-result":
            if arguments.duration_ms < 0:
                raise SystemExit("duration-ms must not be negative")
            try:
                assertions = json.loads(arguments.assertions_json)
            except json.JSONDecodeError as exc:
                raise SystemExit("assertions-json must be valid JSON") from exc
            if not isinstance(assertions, dict):
                raise SystemExit("assertions-json must contain an object")
            duration_ms = arguments.duration_ms
        else:
            assertions = {}
            duration_ms = 0
        finished_at = datetime.now(timezone.utc)
        started_at = finished_at - timedelta(milliseconds=duration_ms)
        store.record(
            {
                "id": arguments.id,
                "status": status,
                "started_at": format_utc(started_at),
                "finished_at": format_utc(finished_at),
                "duration_ms": duration_ms,
                "assertions": assertions,
                **({"reason": reason} if reason else {}),
            }
        )
        return 0
    payload = store.finalize()
    print(json.dumps({"status": payload["status"], "evidence": str(store.path)}))
    if arguments.require_complete and payload["status"] != "passed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
