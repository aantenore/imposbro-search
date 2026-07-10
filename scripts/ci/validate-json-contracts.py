#!/usr/bin/env python3
"""Validate canonical JSON Schema contracts against their declared metaschema."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import SchemaError
from jsonschema.validators import validator_for


REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS_DIR = REPO_ROOT / "contracts"
TESTS_DIR = REPO_ROOT / "tests"


def main() -> int:
    paths = sorted(
        [*CONTRACTS_DIR.glob("*.schema.json"), *TESTS_DIR.glob("**/*.schema.json")]
    )
    if not paths:
        print(
            "No canonical JSON Schema contracts found in contracts/ or tests/",
            file=sys.stderr,
        )
        return 1

    errors: list[str] = []
    identifiers: set[str] = set()
    for path in paths:
        relative = path.relative_to(REPO_ROOT)
        try:
            schema = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append(f"{relative}: invalid JSON: {exc}")
            continue

        if not isinstance(schema, dict):
            errors.append(f"{relative}: schema root must be an object")
            continue
        dialect = schema.get("$schema")
        identifier = schema.get("$id")
        if not isinstance(dialect, str) or not dialect:
            errors.append(f"{relative}: non-empty $schema dialect is required")
            continue
        if not isinstance(identifier, str) or not identifier:
            errors.append(f"{relative}: non-empty canonical $id is required")
        elif identifier in identifiers:
            errors.append(f"{relative}: duplicate canonical $id {identifier!r}")
        else:
            identifiers.add(identifier)

        if schema.get("type") != "object":
            errors.append(f"{relative}: event contract root type must be object")
        if not isinstance(schema.get("properties"), dict):
            errors.append(f"{relative}: object contract must declare properties")
        if not isinstance(schema.get("required"), list) or not schema["required"]:
            errors.append(f"{relative}: object contract must declare required fields")

        try:
            validator = validator_for(schema, default=None)
            if validator is None:
                errors.append(f"{relative}: unsupported $schema dialect {dialect!r}")
                continue
            validator.check_schema(schema)
        except SchemaError as exc:
            errors.append(f"{relative}: invalid {dialect} schema: {exc.message}")

    if errors:
        print("JSON contract validation failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(f"Validated {len(paths)} canonical JSON Schema contract(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
