#!/usr/bin/env python3
"""Fail-closed assertions over Kubernetes JSON read from stdin."""

from __future__ import annotations

import argparse
import json
import sys


COMPONENTS = {"query-api", "admin-ui", "indexing-service"}


def load() -> dict:
    return json.load(sys.stdin)


def assert_security(payload: dict) -> None:
    items = payload.get("items", [])
    if len(items) < 6:
        raise SystemExit(f"expected at least 6 application pods, found {len(items)}")
    observed: set[str] = set()
    for pod in items:
        metadata = pod.get("metadata", {})
        spec = pod.get("spec", {})
        name = metadata.get("name", "unknown")
        component = metadata.get("labels", {}).get("app.kubernetes.io/component")
        if component not in COMPONENTS:
            raise SystemExit(f"unexpected application component on {name}: {component}")
        observed.add(component)
        if spec.get("automountServiceAccountToken") is not False:
            raise SystemExit(f"{name} must disable service-account token automount")
        if spec.get("securityContext", {}).get("runAsNonRoot") is not True:
            raise SystemExit(f"{name} must enforce pod runAsNonRoot")
        containers = list(spec.get("initContainers", [])) + list(spec.get("containers", []))
        if not containers:
            raise SystemExit(f"{name} has no containers")
        for container in containers:
            security = container.get("securityContext", {})
            container_name = container.get("name", "unknown")
            if security.get("runAsNonRoot") is not True:
                raise SystemExit(f"{name}/{container_name} must enforce runAsNonRoot")
            if security.get("allowPrivilegeEscalation") is not False:
                raise SystemExit(
                    f"{name}/{container_name} must disable privilege escalation"
                )
            if security.get("readOnlyRootFilesystem") is not True:
                raise SystemExit(f"{name}/{container_name} must use a read-only root")
            if "ALL" not in security.get("capabilities", {}).get("drop", []):
                raise SystemExit(f"{name}/{container_name} must drop ALL capabilities")
            if "@sha256:" not in container.get("image", ""):
                raise SystemExit(f"{name}/{container_name} image is not digest pinned")
    if observed != COMPONENTS:
        raise SystemExit(f"missing application components: {sorted(COMPONENTS - observed)}")


def assert_migrations(payload: dict) -> None:
    items = payload.get("items", [])
    if len(items) != 2:
        raise SystemExit(f"expected 2 query pods, found {len(items)}")
    for pod in items:
        statuses = pod.get("status", {}).get("initContainerStatuses", [])
        migrations = [item for item in statuses if item.get("name") == "control-plane-migrate"]
        if len(migrations) != 1:
            raise SystemExit(f"migration init status missing for {pod['metadata']['name']}")
        terminated = migrations[0].get("state", {}).get("terminated", {})
        if terminated.get("exitCode") != 0 or terminated.get("reason") != "Completed":
            raise SystemExit(
                f"migration failed for {pod['metadata']['name']}: {terminated}"
            )


def assert_placement(payload: dict) -> None:
    placements: dict[str, set[str]] = {component: set() for component in COMPONENTS}
    for pod in payload.get("items", []):
        component = pod.get("metadata", {}).get("labels", {}).get(
            "app.kubernetes.io/component"
        )
        node = pod.get("spec", {}).get("nodeName")
        if component in placements and node:
            placements[component].add(node)
    failures = {key: sorted(value) for key, value in placements.items() if len(value) < 2}
    if failures:
        raise SystemExit(f"components are not spread across at least two nodes: {failures}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assertion", choices=["security", "migrations", "placement"])
    args = parser.parse_args()
    handlers = {
        "security": assert_security,
        "migrations": assert_migrations,
        "placement": assert_placement,
    }
    handlers[args.assertion](load())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
