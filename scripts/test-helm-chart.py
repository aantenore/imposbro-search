#!/usr/bin/env python3
"""Validate Helm chart rendering and fail-fast guardrails."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
HELM = os.environ.get("HELM", "helm")
CHART_PATH = Path(os.environ.get("HELM_CHART_PATH", "helm"))
if not CHART_PATH.is_absolute():
    CHART_PATH = REPO_ROOT / CHART_PATH
RELEASE_NAME = os.environ.get("HELM_RELEASE", "imposbro-release")
TEST_VALUES = shlex.split(os.environ.get("HELM_TEST_VALUES", "-f helm/ci-values.yaml"))
RENDERED_OUTPUT = Path(
    os.environ.get("HELM_RENDERED_OUTPUT", "/tmp/imposbro-helm-rendered.yaml")
)


def run_helm(args: list[str], *, expect_success: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [HELM, *args],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if expect_success and result.returncode != 0:
        print(result.stdout, file=sys.stderr)
        raise SystemExit(result.returncode)
    if not expect_success and result.returncode == 0:
        print(result.stdout, file=sys.stderr)
        raise SystemExit(f"Expected Helm command to fail: {args}")
    return result


def render(*extra_args: str) -> str:
    result = run_helm(
        [
            "template",
            RELEASE_NAME,
            str(CHART_PATH),
            *TEST_VALUES,
            *extra_args,
        ]
    )
    return result.stdout


def count_kinds(manifest: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in re.finditer(r"^kind:\s+(.+)$", manifest, flags=re.MULTILINE):
        kind = match.group(1).strip()
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def iter_documents(manifest: str):
    for document in re.split(r"^---\s*$", manifest, flags=re.MULTILINE):
        document = document.strip()
        if document:
            yield document


def resource_name(document: str) -> str:
    match = re.search(r"^metadata:\n\s+name:\s+(.+)$", document, flags=re.MULTILINE)
    if not match:
        raise SystemExit(f"Rendered resource has no metadata.name:\n{document}")
    return match.group(1).strip().strip('"')


def find_document(manifest: str, kind: str, name: str) -> str:
    for document in iter_documents(manifest):
        if not re.search(rf"^kind:\s+{re.escape(kind)}$", document, flags=re.MULTILINE):
            continue
        if resource_name(document) == name:
            return document
    raise SystemExit(f"Expected rendered {kind}/{name} resource")


def resource_names(manifest: str, kind: str) -> set[str]:
    names = set()
    for document in iter_documents(manifest):
        if re.search(rf"^kind:\s+{re.escape(kind)}$", document, flags=re.MULTILINE):
            names.add(resource_name(document))
    return names


def configmap_value(configmap: str, key: str) -> str:
    match = re.search(
        rf"^\s+{re.escape(key)}:\s+\"([^\"]*)\"$",
        configmap,
        flags=re.MULTILINE,
    )
    if not match:
        raise SystemExit(f"Expected ConfigMap to contain {key!r}")
    return match.group(1)


def annotation_value(document: str, key: str) -> str:
    match = re.search(
        rf'^\s+{re.escape(key)}:\s+"?([^"\s]+)"?\s*$',
        document,
        flags=re.MULTILINE,
    )
    if not match:
        raise SystemExit(f"Expected resource to contain annotation {key!r}")
    return match.group(1)


def require_count(counts: dict[str, int], kind: str, expected: int) -> None:
    actual = counts.get(kind, 0)
    if actual != expected:
        raise SystemExit(f"Expected {expected} {kind} resource(s), rendered {actual}: {counts}")


def require_contains(manifest: str, needle: str) -> None:
    if needle not in manifest:
        raise SystemExit(f"Expected rendered manifest to contain {needle!r}")


def require_not_contains(manifest: str, needle: str) -> None:
    if needle in manifest:
        raise SystemExit(f"Rendered manifest unexpectedly contains {needle!r}")


def expect_failure(message: str, *extra_args: str) -> None:
    result = run_helm(
        [
            "template",
            RELEASE_NAME,
            str(CHART_PATH),
            *TEST_VALUES,
            *extra_args,
        ],
        expect_success=False,
    )
    if message not in result.stdout:
        print(result.stdout, file=sys.stderr)
        raise SystemExit(f"Expected Helm failure to mention {message!r}")


def main() -> None:
    print("==> Helm lint")
    lint = run_helm(["lint", str(CHART_PATH), *TEST_VALUES])
    print(lint.stdout.strip())

    print("==> Helm render: ci-values")
    manifest = render()
    RENDERED_OUTPUT.write_text(manifest, encoding="utf-8")
    counts = count_kinds(manifest)
    expected_counts = {
        "ConfigMap": 1,
        "Deployment": 3,
        "HorizontalPodAutoscaler": 2,
        "Ingress": 2,
        "NetworkPolicy": 3,
        "PodDisruptionBudget": 3,
        "PrometheusRule": 1,
        "ScaledObject": 1,
        "Secret": 1,
        "Service": 3,
        "ServiceAccount": 1,
        "ServiceMonitor": 2,
    }
    for kind, expected in expected_counts.items():
        require_count(counts, kind, expected)
    require_contains(manifest, 'ingressClassName: "nginx"')
    require_contains(manifest, "api.imposbro.example.com")
    require_contains(manifest, "admin.imposbro.example.com")
    require_contains(manifest, "REQUEST_ID_HEADER")
    require_contains(manifest, 'READINESS_POLICY: "serving"')
    require_contains(manifest, 'INTERNAL_STATE_PROTOCOL: "http"')
    require_contains(manifest, 'DEFAULT_DATA_CLUSTER_PROTOCOL: "http"')
    require_contains(manifest, 'DEFAULT_DATA2_CLUSTER_PROTOCOL: "http"')
    require_contains(manifest, "INGEST_BATCH_MAX_DOCUMENTS")
    require_contains(manifest, "RATE_LIMIT_ENABLED")
    require_contains(manifest, "ImposbroQueryApiRateLimitBlocked")
    require_contains(manifest, "query_api_rate_limit_backend_errors_total")
    require_contains(manifest, 'http_requests_total{status="5xx"}')
    require_contains(manifest, "http_request_duration_seconds_bucket")
    require_not_contains(manifest, "fastapi_requests")
    require_contains(manifest, 'INTERNAL_QUERY_API_ADMIN_API_KEY: "test-admin-key"')

    fullname = f"{RELEASE_NAME}-imposbro-search"
    configmap = find_document(manifest, "ConfigMap", f"{fullname}-config")
    secret = find_document(manifest, "Secret", f"{fullname}-secret")
    deployment_names = (
        f"{fullname}-query-api",
        f"{fullname}-admin-ui",
        f"{fullname}-indexing-service",
    )
    deployments = {
        name: find_document(manifest, "Deployment", name)
        for name in deployment_names
    }
    require_not_contains(configmap, "REDIS_URL")
    require_not_contains(configmap, "ADMIN_UI_PROXY_TRUSTED_VALUE")
    require_contains(secret, 'REDIS_URL: "redis://redis:6379"')
    require_contains(secret, 'ADMIN_UI_PROXY_TRUSTED_VALUE: "operator"')
    internal_query_api_url = configmap_value(configmap, "INTERNAL_QUERY_API_URL")
    internal_query_api_host = urlparse(internal_query_api_url).hostname
    services = resource_names(manifest, "Service")
    if internal_query_api_host not in services:
        raise SystemExit(
            "config.INTERNAL_QUERY_API_URL must target a rendered Service; "
            f"got host {internal_query_api_host!r}, rendered services {sorted(services)}"
        )

    secure_typesense = render(
        "--set",
        "config.INTERNAL_STATE_PROTOCOL=https",
        "--set",
        "config.DEFAULT_DATA_CLUSTER_PROTOCOL=https",
        "--set",
        "config.DEFAULT_DATA2_CLUSTER_PROTOCOL=https",
    )
    secure_typesense_configmap = find_document(
        secure_typesense,
        "ConfigMap",
        f"{fullname}-config",
    )
    for protocol_key in (
        "INTERNAL_STATE_PROTOCOL",
        "DEFAULT_DATA_CLUSTER_PROTOCOL",
        "DEFAULT_DATA2_CLUSTER_PROTOCOL",
    ):
        if configmap_value(secure_typesense_configmap, protocol_key) != "https":
            raise SystemExit(f"Expected {protocol_key} to render as https")

    print("==> Helm rollout checksums")
    config_checksums = {}
    secret_checksums = {}
    for name, deployment in deployments.items():
        config_checksums[name] = annotation_value(deployment, "checksum/config")
        secret_checksums[name] = annotation_value(deployment, "checksum/secret")
        if not re.fullmatch(r"[0-9a-f]{64}", config_checksums[name]):
            raise SystemExit(f"Deployment/{name} has an invalid config checksum")
        if not re.fullmatch(r"[0-9a-f]{64}", secret_checksums[name]):
            raise SystemExit(f"Deployment/{name} has an invalid secret checksum")

    changed_config = render("--set", "config.KAFKA_TOPIC_PREFIX=imposbro_checksum_test")
    changed_secret = render("--set", "config.DATA_API_KEY=rotated-data-key")
    annotated = render(
        "--set-json",
        'podAnnotations={"audit.imposbro.dev/enabled":"true"}',
    )
    for name in deployment_names:
        config_deployment = find_document(changed_config, "Deployment", name)
        secret_deployment = find_document(changed_secret, "Deployment", name)
        annotated_deployment = find_document(annotated, "Deployment", name)
        if annotation_value(config_deployment, "checksum/config") == config_checksums[name]:
            raise SystemExit(f"Deployment/{name} config checksum did not change")
        if annotation_value(config_deployment, "checksum/secret") != secret_checksums[name]:
            raise SystemExit(f"Deployment/{name} secret checksum changed with ConfigMap-only data")
        if annotation_value(secret_deployment, "checksum/secret") == secret_checksums[name]:
            raise SystemExit(f"Deployment/{name} secret checksum did not change")
        if annotation_value(secret_deployment, "checksum/config") != config_checksums[name]:
            raise SystemExit(f"Deployment/{name} config checksum changed with Secret-only data")
        require_contains(annotated_deployment, "audit.imposbro.dev/enabled")
        annotation_value(annotated_deployment, "checksum/config")
        annotation_value(annotated_deployment, "checksum/secret")

    query_api_policy = find_document(manifest, "NetworkPolicy", f"{fullname}-query-api")
    admin_ui_policy = find_document(manifest, "NetworkPolicy", f"{fullname}-admin-ui")
    indexing_policy = find_document(
        manifest,
        "NetworkPolicy",
        f"{fullname}-indexing-service-metrics",
    )
    require_contains(query_api_policy, "kubernetes.io/metadata.name: monitoring")
    require_contains(query_api_policy, "app.kubernetes.io/name: prometheus")
    require_contains(admin_ui_policy, "kubernetes.io/metadata.name: ingress-nginx")
    require_contains(admin_ui_policy, "app.kubernetes.io/name: ingress-nginx")
    require_contains(indexing_policy, "kubernetes.io/metadata.name: monitoring")
    require_contains(indexing_policy, "app.kubernetes.io/name: prometheus")

    print("==> Helm NetworkPolicy fail-closed rendering")
    deny_all_ingress = render(
        "--set-json",
        "networkPolicy.ingressController.from=[]",
        "--set-json",
        "networkPolicy.prometheus.from=[]",
        "--set",
        "networkPolicy.queryApi.allowAdminUi=false",
    )
    for name in (
        f"{fullname}-query-api",
        f"{fullname}-admin-ui",
        f"{fullname}-indexing-service-metrics",
    ):
        policy = find_document(deny_all_ingress, "NetworkPolicy", name)
        require_contains(policy, "ingress: []")
        require_not_contains(policy, "\n    - ports:")

    dashboard = (
        REPO_ROOT
        / "monitoring/grafana/provisioning/dashboards/imposbro-overview.json"
    ).read_text(encoding="utf-8")
    require_contains(dashboard, "http_requests_total")
    require_contains(dashboard, "http_request_duration_seconds_bucket")
    require_not_contains(dashboard, "fastapi_requests")
    print(f"Rendered counts: {counts}")

    print("==> Helm guardrail: credentialed Redis URL is secret-only")
    credentialed_redis = render("--set", "config.REDIS_URL=redis://:supersecret@redis:6379/0")
    credentialed_configmap = find_document(
        credentialed_redis,
        "ConfigMap",
        f"{fullname}-config",
    )
    credentialed_secret = find_document(
        credentialed_redis,
        "Secret",
        f"{fullname}-secret",
    )
    require_not_contains(credentialed_configmap, "REDIS_URL")
    require_not_contains(credentialed_configmap, "supersecret")
    require_contains(credentialed_secret, 'REDIS_URL: "redis://:supersecret@redis:6379/0"')

    print("==> Helm render: query-api ingress only")
    query_only = render("--set", "adminUi.ingress.enabled=false")
    require_count(count_kinds(query_only), "Ingress", 1)
    require_contains(query_only, "api.imposbro.example.com")
    require_not_contains(query_only, "admin.imposbro.example.com")

    print("==> Helm render: admin-ui ingress only")
    admin_only = render("--set", "queryApi.ingress.enabled=false")
    require_count(count_kinds(admin_only), "Ingress", 1)
    require_contains(admin_only, "admin.imposbro.example.com")
    require_not_contains(admin_only, "api.imposbro.example.com")

    print("==> Helm guardrail: query-api ingress requires hosts")
    expect_failure(
        "queryApi.ingress.hosts must contain at least one host",
        "--set-json",
        "queryApi.ingress.hosts=[]",
    )

    print("==> Helm guardrail: admin-ui ingress requires hosts")
    expect_failure(
        "adminUi.ingress.hosts must contain at least one host",
        "--set-json",
        "adminUi.ingress.hosts=[]",
    )

    print("==> Helm guardrail: request-id header is required")
    expect_failure(
        "config.REQUEST_ID_HEADER is required",
        "--set",
        "config.REQUEST_ID_HEADER=",
    )

    print("==> Helm guardrail: readiness policy must be supported")
    expect_failure(
        "config.READINESS_POLICY must be serving or strict",
        "--set",
        "config.READINESS_POLICY=unknown",
    )

    print("==> Helm guardrail: rollout checksum annotations are reserved")
    expect_failure(
        "podAnnotations must not override reserved checksum/config or checksum/secret annotations",
        "--set-json",
        'podAnnotations={"checksum/config":"operator-value"}',
    )

    print("==> Helm guardrail: Typesense protocols must be supported")
    for protocol_key in (
        "INTERNAL_STATE_PROTOCOL",
        "DEFAULT_DATA_CLUSTER_PROTOCOL",
        "DEFAULT_DATA2_CLUSTER_PROTOCOL",
    ):
        expect_failure(
            f"config.{protocol_key} must be http or https",
            "--set",
            f"config.{protocol_key}=ftp",
        )

    print("==> Helm guardrail: indexing HPA and KEDA are mutually exclusive")
    expect_failure(
        "indexingService.autoscaling.enabled and indexingService.keda.enabled cannot both be true",
        "--set",
        "indexingService.autoscaling.enabled=true",
    )

    print("==> Helm guardrail: rate-limit backend must be supported")
    expect_failure(
        "config.RATE_LIMIT_BACKEND must be redis or memory",
        "--set",
        "config.RATE_LIMIT_ENABLED=true",
        "--set",
        "config.RATE_LIMIT_BACKEND=sqlite",
    )

    print("==> Helm guardrail: memory rate-limit backend requires one replica")
    expect_failure(
        "config.RATE_LIMIT_BACKEND=memory is only supported for a single Query API replica",
        "--set",
        "config.RATE_LIMIT_ENABLED=true",
        "--set",
        "config.RATE_LIMIT_BACKEND=memory",
        "--set",
        "queryApi.replicaCount=2",
    )

    print("==> Helm guardrail: ingest batch size must be positive")
    expect_failure(
        "config.INGEST_BATCH_MAX_DOCUMENTS must be >= 1",
        "--set",
        "config.INGEST_BATCH_MAX_DOCUMENTS=0",
    )

    print("==> Helm guardrail: scoped-only admin auth needs worker internal key")
    expect_failure(
        "config.ADMIN_API_KEY or config.INTERNAL_QUERY_API_ADMIN_API_KEY is required for indexing service internal Query API admin calls",
        "--set",
        "config.ADMIN_API_KEY=",
        "--set",
        "config.INTERNAL_QUERY_API_ADMIN_API_KEY=",
        "--set-json",
        'config.SCOPED_API_KEYS=[{"name":"ops","key":"ops-secret","scopes":["admin"]}]',
    )

    print("==> Helm guardrail: distinct worker internal key must be accepted by Query API")
    expect_failure(
        "config.INTERNAL_QUERY_API_ADMIN_API_KEY must match config.ADMIN_API_KEY or be present in config.SCOPED_API_KEYS",
        "--set",
        "config.INTERNAL_QUERY_API_ADMIN_API_KEY=worker-secret",
    )

    print("==> Helm guardrail: trusted proxy header requires expected value")
    expect_failure(
        "config.ADMIN_UI_PROXY_TRUSTED_VALUE is required when the Admin UI proxy injects server-side API keys",
        "--set",
        "config.ADMIN_UI_PROXY_TRUSTED_VALUE=",
    )

    print("==> Helm guardrail: explicit Admin UI OIDC endpoints require JWKS")
    expect_failure(
        "config.ADMIN_UI_OIDC_ISSUER or explicit Admin UI OIDC authorization, token, and JWKS endpoints are required",
        "--set",
        "config.ADMIN_UI_OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
        "--set",
        "config.OIDC_ALGORITHMS=RS256",
        "--set",
        "config.ADMIN_UI_OIDC_CLIENT_ID=imposbro-admin-ui",
        "--set",
        "config.ADMIN_UI_SESSION_SECRET=admin-ui-session-secret-32-bytes-minimum",
        "--set",
        "config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT=https://idp.example.com/oauth2/authorize",
        "--set",
        "config.ADMIN_UI_OIDC_TOKEN_ENDPOINT=https://idp.example.com/oauth2/token",
    )

    print("==> Helm render: distinct worker internal key accepted via scoped key")
    scoped_worker = render(
        "--set",
        "config.ADMIN_API_KEY=",
        "--set",
        "config.INTERNAL_QUERY_API_ADMIN_API_KEY=worker-secret",
        "--set-json",
        'config.SCOPED_API_KEYS=[{"name":"worker","key":"worker-secret","scopes":["admin:internal"]}]',
    )
    require_contains(scoped_worker, "worker-secret")

    print("==> Helm guardrail: images must be digest-pinned")
    expect_failure(
        "queryApi.image must be pinned by digest",
        "--set",
        "queryApi.image=registry.example.com/imposbro-query-api:1.0.0",
    )

    print("==> Helm guardrail: placeholder image repositories are rejected")
    expect_failure(
        "adminUi.image must be a non-placeholder image reference",
        "--set",
        "adminUi.image=your-registry-user/imposbro-admin-ui@sha256:2222222222222222222222222222222222222222222222222222222222222222",
    )

    print("==> Helm guardrail: latest image tags are rejected even with digest")
    expect_failure(
        "indexingService.image must not use the mutable :latest tag",
        "--set",
        "indexingService.image=registry.example.com/imposbro-indexing-service:latest@sha256:3333333333333333333333333333333333333333333333333333333333333333",
    )

    print("==> Helm guardrail: chart secrets mode is required")
    expect_failure(
        "config.useSecret must be true",
        "--set",
        "config.useSecret=false",
    )

    print("==> Helm guardrail: Kafka bootstrap URL is required")
    expect_failure(
        "config.KAFKA_BROKER_URL is required",
        "--set",
        "config.KAFKA_BROKER_URL=",
    )

    print("==> Helm guardrail: Redis URL is required")
    expect_failure(
        "config.REDIS_URL is required",
        "--set",
        "config.REDIS_URL=",
    )

    print("==> Helm guardrail: Typesense state nodes are required")
    expect_failure(
        "config.INTERNAL_STATE_NODES is required",
        "--set",
        "config.INTERNAL_STATE_NODES=",
    )

    print("==> Helm guardrail: Typesense data nodes are required")
    expect_failure(
        "config.DEFAULT_DATA_CLUSTER_NODES is required",
        "--set",
        "config.DEFAULT_DATA_CLUSTER_NODES=",
    )

    print("==> Helm guardrail: Typesense state API key is required")
    expect_failure(
        "config.INTERNAL_STATE_API_KEY is required",
        "--set",
        "config.INTERNAL_STATE_API_KEY=",
    )

    print("==> Helm guardrail: Typesense data API key is required")
    expect_failure(
        "config.DEFAULT_DATA_CLUSTER_API_KEY is required",
        "--set",
        "config.DEFAULT_DATA_CLUSTER_API_KEY=",
    )

    print("==> Helm guardrail: trusted proxy header is required for key injection")
    expect_failure(
        "config.ADMIN_UI_PROXY_TRUSTED_HEADER is required when the Admin UI proxy injects server-side API keys",
        "--set",
        "config.ADMIN_UI_PROXY_TRUSTED_HEADER=",
    )

    print("==> Helm guardrail: OIDC issuer is required")
    expect_failure(
        "config.OIDC_ISSUER is required when OIDC_ENABLED is true",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
    )

    print("==> Helm guardrail: OIDC signing key source is required")
    expect_failure(
        "config.OIDC_JWKS_URL or config.OIDC_PUBLIC_KEY is required when OIDC_ENABLED is true",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=",
        "--set",
        "config.OIDC_PUBLIC_KEY=",
    )

    print("==> Helm guardrail: OIDC signing key sources are mutually exclusive")
    expect_failure(
        "config.OIDC_JWKS_URL and config.OIDC_PUBLIC_KEY are mutually exclusive",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
        "--set",
        "config.OIDC_PUBLIC_KEY=-----BEGIN PUBLIC KEY-----fake-----END PUBLIC KEY-----",
    )

    print("==> Helm guardrail: OIDC algorithms must be asymmetric")
    expect_failure(
        "config.OIDC_ALGORITHMS must use asymmetric algorithms",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
        "--set",
        "config.OIDC_ALGORITHMS=HS256",
    )

    print("==> Helm guardrail: Query API OIDC issuer must use HTTPS")
    expect_failure(
        "config.OIDC_ISSUER must use https://",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=http://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
    )

    print("==> Helm guardrail: Query API OIDC JWKS URL must use HTTPS")
    expect_failure(
        "config.OIDC_JWKS_URL must use https://",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=http://idp.example.com/.well-known/jwks.json",
    )

    print("==> Helm render: local-only insecure OIDC URLs can be explicitly allowed")
    insecure_local_oidc = render(
        "--set",
        "config.ALLOW_INSECURE_OIDC_URLS=true",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=http://idp.local/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=http://idp.local/.well-known/jwks.json",
    )
    require_contains(insecure_local_oidc, 'OIDC_ISSUER: "http://idp.local/"')

    print("==> Helm guardrail: Admin UI OIDC requires Query API OIDC")
    expect_failure(
        "config.OIDC_ENABLED=true is required when ADMIN_UI_OIDC_ENABLED is true",
        "--set",
        "config.ADMIN_UI_OIDC_ENABLED=true",
    )

    print("==> Helm guardrail: Admin UI OIDC session secret length is enforced")
    expect_failure(
        "config.ADMIN_UI_SESSION_SECRET must be at least 32 characters",
        "--set",
        "config.ADMIN_UI_OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
        "--set",
        "config.ADMIN_UI_OIDC_CLIENT_ID=imposbro-admin-ui",
        "--set",
        "config.ADMIN_UI_SESSION_SECRET=short",
        "--set",
        "config.ADMIN_UI_OIDC_ISSUER=https://idp.example.com/",
    )

    print("==> Helm guardrail: Admin UI explicit OIDC token endpoint needs authorization endpoint")
    expect_failure(
        "config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT is required when ADMIN_UI_OIDC_TOKEN_ENDPOINT is set",
        "--set",
        "config.ADMIN_UI_OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
        "--set",
        "config.ADMIN_UI_OIDC_CLIENT_ID=imposbro-admin-ui",
        "--set",
        "config.ADMIN_UI_SESSION_SECRET=admin-ui-session-secret-32-bytes-minimum",
        "--set",
        "config.ADMIN_UI_OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.ADMIN_UI_OIDC_TOKEN_ENDPOINT=https://idp.example.com/oauth2/token",
        "--set",
        "config.ADMIN_UI_OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
    )

    print("==> Helm guardrail: Admin UI OIDC JWKS without issuer needs explicit endpoints")
    expect_failure(
        "config.ADMIN_UI_OIDC_ISSUER or explicit Admin UI OIDC authorization, token, and JWKS endpoints are required",
        "--set",
        "config.ADMIN_UI_OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
        "--set",
        "config.ADMIN_UI_OIDC_CLIENT_ID=imposbro-admin-ui",
        "--set",
        "config.ADMIN_UI_SESSION_SECRET=admin-ui-session-secret-32-bytes-minimum",
        "--set",
        "config.ADMIN_UI_OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
    )

    print("==> Helm guardrail: Admin UI OIDC scopes include openid")
    expect_failure(
        "config.ADMIN_UI_OIDC_SCOPES must include openid",
        "--set",
        "config.ADMIN_UI_OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
        "--set",
        "config.ADMIN_UI_OIDC_CLIENT_ID=imposbro-admin-ui",
        "--set",
        "config.ADMIN_UI_SESSION_SECRET=admin-ui-session-secret-32-bytes-minimum",
        "--set",
        "config.ADMIN_UI_OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.ADMIN_UI_OIDC_SCOPES=profile email",
    )

    print("==> Helm guardrail: Admin UI OIDC endpoints must use HTTPS")
    expect_failure(
        "config.ADMIN_UI_OIDC_TOKEN_ENDPOINT must use https://",
        "--set",
        "config.ADMIN_UI_OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ENABLED=true",
        "--set",
        "config.OIDC_ISSUER=https://idp.example.com/",
        "--set",
        "config.OIDC_AUDIENCE=imposbro-api",
        "--set",
        "config.OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
        "--set",
        "config.ADMIN_UI_OIDC_CLIENT_ID=imposbro-admin-ui",
        "--set",
        "config.ADMIN_UI_SESSION_SECRET=admin-ui-session-secret-32-bytes-minimum",
        "--set",
        "config.ADMIN_UI_OIDC_AUTHORIZATION_ENDPOINT=https://idp.example.com/oauth2/authorize",
        "--set",
        "config.ADMIN_UI_OIDC_TOKEN_ENDPOINT=http://idp.example.com/oauth2/token",
        "--set",
        "config.ADMIN_UI_OIDC_JWKS_URL=https://idp.example.com/.well-known/jwks.json",
    )

    print("Helm chart validation passed.")


if __name__ == "__main__":
    main()
