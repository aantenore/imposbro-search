#!/usr/bin/env python3
"""Atomic, redacted evidence writer for the disposable kind exercise."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SENSITIVE = re.compile(
    r"(?i)(password|secret|api[_-]?key|authorization|bearer|postgresql\+psycopg://)"
)


def atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_evidence(value: str) -> str:
    text = " ".join(value.strip().split())
    if len(text) > 1000:
        raise SystemExit("evidence text exceeds 1000 characters")
    if SENSITIVE.search(text):
        raise SystemExit("evidence text contains a sensitive marker")
    return text


def parse_tools(entries: list[str]) -> dict[str, str]:
    tools: dict[str, str] = {}
    for entry in entries:
        name, separator, version = entry.partition("=")
        if not separator or not name.strip() or not version.strip():
            raise SystemExit(f"invalid --tool entry: {entry!r}")
        tools[name.strip()] = safe_evidence(version)
    return tools


def command_init(args: argparse.Namespace) -> None:
    required = list(args.required_assertion)
    if not required:
        raise SystemExit("at least one --required-assertion is required")
    if len(required) != len(set(required)):
        raise SystemExit("required assertion IDs must be unique")
    if any(not re.fullmatch(r"[a-z0-9_]+", item) for item in required):
        raise SystemExit("required assertion IDs must match ^[a-z0-9_]+$")
    payload = {
        "schema_version": "1.0",
        "evidence_type": "kind-enterprise-smoke",
        "production_certification": False,
        "status": "running",
        "run": {
            "id": args.run_id,
            "started_at": args.started_at,
            "finished_at": None,
            "git": {"commit": args.commit, "dirty": args.dirty == "true"},
        },
        "environment": {
            "cluster_name": args.cluster_name,
            "namespace": args.namespace,
            "node_image": args.node_image,
            "tools": parse_tools(args.tool),
        },
        "required_assertions": required,
        "assertions": [],
        "cleanup": {
            "status": "pending",
            "cluster_deleted": False,
            "registry_deleted": False,
            "network_deleted": False,
        },
    }
    atomic_write(args.state, payload)


def command_assert(args: argparse.Namespace) -> None:
    payload = read_state(args.state)
    if args.assertion_id not in payload["required_assertions"]:
        raise SystemExit(f"unexpected assertion id: {args.assertion_id}")
    if any(item["id"] == args.assertion_id for item in payload["assertions"]):
        raise SystemExit(f"duplicate assertion id: {args.assertion_id}")
    payload["assertions"].append(
        {
            "id": args.assertion_id,
            "status": args.status,
            "duration_seconds": round(args.duration, 3),
            "evidence": safe_evidence(args.evidence),
        }
    )
    atomic_write(args.state, payload)


def command_finish(args: argparse.Namespace) -> None:
    payload = read_state(args.state)
    payload["run"]["finished_at"] = args.finished_at
    payload["cleanup"] = {
        "status": args.cleanup_status,
        "cluster_deleted": args.cluster_deleted == "true",
        "registry_deleted": args.registry_deleted == "true",
        "network_deleted": args.network_deleted == "true",
    }
    actual_ids = [item["id"] for item in payload["assertions"]]
    required_ids = payload["required_assertions"]
    exact_contract = actual_ids == required_ids
    all_passed = exact_contract and all(
        item["status"] == "passed" for item in payload["assertions"]
    )
    invalid_pass = args.status == "passed" and not all_passed
    payload["status"] = "failed" if invalid_pass else args.status
    atomic_write(args.state, payload)
    atomic_write(args.output, payload)
    if invalid_pass:
        missing = [item for item in required_ids if item not in actual_ids]
        unexpected = [item for item in actual_ids if item not in required_ids]
        non_passing = [
            item["id"] for item in payload["assertions"] if item["status"] != "passed"
        ]
        raise SystemExit(
            "refusing passed evidence: assertion contract mismatch "
            f"missing={missing} unexpected={unexpected} non_passing={non_passing}"
        )


def command_contains(args: argparse.Namespace) -> None:
    payload = read_state(args.state)
    if not any(item["id"] == args.assertion_id for item in payload["assertions"]):
        raise SystemExit(1)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser("init")
    initialize.add_argument("--state", type=Path, required=True)
    initialize.add_argument("--run-id", required=True)
    initialize.add_argument("--started-at", required=True)
    initialize.add_argument("--commit", required=True)
    initialize.add_argument("--dirty", choices=["true", "false"], required=True)
    initialize.add_argument("--cluster-name", required=True)
    initialize.add_argument("--namespace", required=True)
    initialize.add_argument("--node-image", required=True)
    initialize.add_argument("--tool", action="append", default=[])
    initialize.add_argument("--required-assertion", action="append", default=[])
    initialize.set_defaults(handler=command_init)

    assertion = commands.add_parser("assert")
    assertion.add_argument("--state", type=Path, required=True)
    assertion.add_argument("--id", dest="assertion_id", required=True)
    assertion.add_argument(
        "--status", choices=["passed", "failed", "not_run"], required=True
    )
    assertion.add_argument("--duration", type=float, required=True)
    assertion.add_argument("--evidence", required=True)
    assertion.set_defaults(handler=command_assert)

    contains = commands.add_parser("contains")
    contains.add_argument("--state", type=Path, required=True)
    contains.add_argument("--id", dest="assertion_id", required=True)
    contains.set_defaults(handler=command_contains)

    finish = commands.add_parser("finish")
    finish.add_argument("--state", type=Path, required=True)
    finish.add_argument("--output", type=Path, required=True)
    finish.add_argument(
        "--status", choices=["passed", "failed", "incomplete"], required=True
    )
    finish.add_argument("--finished-at", required=True)
    finish.add_argument(
        "--cleanup-status",
        choices=["completed", "retained", "failed"],
        required=True,
    )
    finish.add_argument("--cluster-deleted", choices=["true", "false"], required=True)
    finish.add_argument("--registry-deleted", choices=["true", "false"], required=True)
    finish.add_argument("--network-deleted", choices=["true", "false"], required=True)
    finish.set_defaults(handler=command_finish)
    return root


def main() -> int:
    args = parser().parse_args()
    args.handler(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
