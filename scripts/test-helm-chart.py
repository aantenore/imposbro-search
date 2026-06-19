#!/usr/bin/env python3
"""Validate Helm chart rendering and fail-fast guardrails."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path


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
    require_contains(manifest, "RATE_LIMIT_ENABLED")
    require_contains(manifest, "ImposbroQueryApiRateLimitBlocked")
    require_contains(manifest, "query_api_rate_limit_backend_errors_total")
    print(f"Rendered counts: {counts}")

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

    print("Helm chart validation passed.")


if __name__ == "__main__":
    main()
