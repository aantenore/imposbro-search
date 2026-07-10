#!/usr/bin/env python3
"""Enforce repository-local invariants for GitHub Actions supply-chain safety."""

from __future__ import annotations

import re
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIR = REPO_ROOT / ".github" / "workflows"
SHA_PIN = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"^\s*-?\s*uses:\s*([^\s#]+)", re.MULTILINE)
SERVICE_IMAGE = re.compile(r"^\s{8}image:\s*([^\s#]+)", re.MULTILINE)
DOCKER_FROM = re.compile(
    r"^\s*FROM(?:\s+--\S+)*\s+(?P<image>\S+)(?:\s+AS\s+(?P<stage>[A-Za-z0-9_.-]+))?\s*$",
    re.IGNORECASE | re.MULTILINE,
)


def fail(errors: list[str], path: Path, message: str) -> None:
    errors.append(f"{path.relative_to(REPO_ROOT)}: {message}")


def main() -> int:
    errors: list[str] = []
    workflows = sorted((*WORKFLOW_DIR.glob("*.yml"), *WORKFLOW_DIR.glob("*.yaml")))
    if not workflows:
        errors.append("No GitHub Actions workflows found")

    for path in workflows:
        text = path.read_text(encoding="utf-8")
        if "pull_request_target:" in text:
            fail(errors, path, "pull_request_target is forbidden for repository code execution")
        if not re.search(r"^permissions:\s*$", text, re.MULTILINE):
            fail(errors, path, "top-level least-privilege permissions block is required")
        if not re.search(r"^concurrency:\s*$", text, re.MULTILINE):
            fail(errors, path, "workflow-level concurrency is required")
        if re.search(r"run:\s*[^\n]*\$\{\{\s*secrets\.", text):
            fail(errors, path, "do not interpolate secrets directly into run commands")

        for use in USES.findall(text):
            if use.startswith("./"):
                continue
            if use.startswith("docker://"):
                if "@sha256:" not in use:
                    fail(errors, path, f"container action must be digest-pinned: {use}")
                continue
            if "@" not in use:
                fail(errors, path, f"action has no immutable ref: {use}")
                continue
            action, ref = use.rsplit("@", 1)
            if not SHA_PIN.fullmatch(ref):
                fail(errors, path, f"action must be pinned to a full commit SHA: {action}@{ref}")

        for image in SERVICE_IMAGE.findall(text):
            if "@sha256:" not in image:
                fail(errors, path, f"service container must be digest-pinned: {image}")

        for match in re.finditer(r"^  ([a-zA-Z0-9_-]+):\s*\n((?:^    .*\n?)*)", text, re.MULTILINE):
            job_name, body = match.groups()
            if "uses: ./" in body:
                continue
            if "runs-on:" in body and "timeout-minutes:" not in body:
                fail(errors, path, f"job {job_name!r} must define timeout-minutes")

    dockerfiles = sorted(
        path
        for path in REPO_ROOT.rglob("Dockerfile*")
        if not any(part in {".git", "node_modules", ".venv"} for part in path.parts)
    )
    if not dockerfiles:
        errors.append("No Dockerfiles found")
    for path in dockerfiles:
        stages: set[str] = set()
        matches = list(DOCKER_FROM.finditer(path.read_text(encoding="utf-8")))
        if not matches:
            fail(errors, path, "no valid FROM instruction found")
            continue
        for match in matches:
            image = match.group("image")
            if image not in stages and "@sha256:" not in image:
                fail(errors, path, f"external base image must be digest-pinned: {image}")
            if stage := match.group("stage"):
                stages.add(stage)

    if errors:
        print("Workflow policy validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(
        f"Validated {len(workflows)} workflow(s) and {len(dockerfiles)} Dockerfile(s): "
        "immutable dependencies and bounded permissions policy."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
