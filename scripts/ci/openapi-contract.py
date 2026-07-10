#!/usr/bin/env python3
"""Compare or intentionally regenerate the canonical Query API v1 contract."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
QUERY_APP = REPO_ROOT / "query_api" / "app"
CONTRACT_PATH = REPO_ROOT / "contracts" / "openapi-v1.json"


def _configure_import() -> None:
    if str(QUERY_APP) not in sys.path:
        sys.path.insert(0, str(QUERY_APP))
    defaults = {
        "TESTING": "1",
        "KAFKA_BROKER_URL": "localhost:9092",
        "KAFKA_TOPIC_PREFIX": "contract",
        "REDIS_URL": "redis://localhost:6379",
        "INTERNAL_STATE_NODES": "localhost",
        "INTERNAL_STATE_API_KEY": "contract-key",
        "DEFAULT_DATA_CLUSTER_NODES": "localhost",
        "DEFAULT_DATA_CLUSTER_API_KEY": "contract-key",
        "INTERNAL_QUERY_API_URL": "http://localhost:8000",
        "ALLOW_UNAUTHENTICATED_ADMIN": "true",
        "ALLOW_UNAUTHENTICATED_DATA": "true",
    }
    for name, value in defaults.items():
        os.environ.setdefault(name, value)


def _canonical_json() -> str:
    _configure_import()
    from api_contract import build_openapi_schema
    from main import app

    schema = build_openapi_schema(app, versioned_only=True)
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--write",
        action="store_true",
        help="Intentionally replace the reviewed baseline.",
    )
    arguments = parser.parse_args()
    generated = _canonical_json()
    if arguments.write:
        CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONTRACT_PATH.write_text(generated, encoding="utf-8")
        print(f"Wrote {CONTRACT_PATH.relative_to(REPO_ROOT)}")
        return 0
    if not CONTRACT_PATH.exists():
        print(
            f"Missing {CONTRACT_PATH.relative_to(REPO_ROOT)}; run with --write and review it.",
            file=sys.stderr,
        )
        return 1
    current = CONTRACT_PATH.read_text(encoding="utf-8")
    if current != generated:
        print(
            "OpenAPI v1 changed. Preserve backward compatibility, then run "
            "scripts/ci/openapi-contract.py --write and review the diff.",
            file=sys.stderr,
        )
        return 1
    print("OpenAPI v1 matches the reviewed baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
