from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "tests" / "ops" / "dr-evidence.schema.json"
EVIDENCE_PATH = REPO_ROOT / "tests" / "ops" / "fixtures" / "dr-evidence-v1.json"


def load_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = load_json(SCHEMA_PATH)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def errors(validator: Draft202012Validator, value: dict) -> list:
    return sorted(validator.iter_errors(value), key=lambda error: list(error.path))


def test_nonvacuous_versioned_dr_fixture_matches_v1_schema(
    validator: Draft202012Validator,
) -> None:
    evidence = load_json(EVIDENCE_PATH)

    assert errors(validator, evidence) == []
    assert evidence["retention"]["before"]["audit_rows"] >= 2
    assert evidence["retention"]["audit_chain_rows_verified"] >= 2


def test_failed_target_evidence_remains_representable(
    validator: Draft202012Validator,
) -> None:
    evidence = load_json(EVIDENCE_PATH)
    evidence["outcome"] = "failed"
    evidence["timing"]["rto"]["passed"] = False

    assert errors(validator, evidence) == []


@pytest.mark.parametrize(
    ("path", "invalid_value"),
    [
        (("scope", "production_certification"), True),
        (("backup", "artifact_sha256"), "not-a-sha256"),
        (("retention", "before", "audit_rows"), 0),
        (
            (
                "retention",
                "audit_chain_cryptographically_verified_after_restore",
            ),
            False,
        ),
        (("cleanup", "containers_and_volumes_destroyed"), False),
        (("provenance", "evidence_contains_credentials"), True),
        (("timing", "rto", "passed"), False),
    ],
)
def test_schema_rejects_false_or_vacuous_pass_assertions(
    validator: Draft202012Validator,
    path: tuple[str, ...],
    invalid_value: object,
) -> None:
    evidence = copy.deepcopy(load_json(EVIDENCE_PATH))
    target = evidence
    for component in path[:-1]:
        target = target[component]
    target[path[-1]] = invalid_value

    assert errors(validator, evidence), f"schema accepted invalid field {'.'.join(path)}"
