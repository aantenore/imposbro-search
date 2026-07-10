"""Versioning, Problem Details, and generated OpenAPI contract checks."""

import json
from pathlib import Path

from api_contract import build_openapi_schema
from constants import API_VERSION_PREFIX
from main import app
from fastapi import HTTPException


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_versioned_not_found_uses_problem_details_and_correlation(client):
    response = client.get(
        f"{API_VERSION_PREFIX}/does-not-exist",
        headers={"X-Request-ID": "contract-request-1"},
    )

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["x-api-version"] == "1"
    assert response.json() == {
        "type": "https://imposbro.dev/problems/not_found",
        "title": "Not Found",
        "status": 404,
        "detail": "Not Found",
        "instance": f"{API_VERSION_PREFIX}/does-not-exist",
        "code": "not_found",
        "request_id": "contract-request-1",
        "api_version": "1",
    }


def test_legacy_error_shape_remains_compatible(client):
    response = client.get("/does-not-exist")

    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert response.headers["x-api-version"] == "1"


def test_legacy_api_alias_advertises_successor(client):
    response = client.get("/admin/federation/clusters")

    assert response.status_code == 200
    assert response.headers["deprecation"] == "true"
    assert response.headers["link"] == (
        '</api/v1/admin/federation/clusters>; rel="successor-version"'
    )
    versioned = client.get("/api/v1/admin/federation/clusters")
    assert versioned.status_code == 200
    assert versioned.json() == response.json()
    assert "deprecation" not in versioned.headers


def test_versioned_validation_has_stable_problem_shape(client):
    response = client.get("/api/v1/documents/invalid!name/doc-1")

    assert response.status_code == 422
    problem = response.json()
    assert problem["code"] == "validation_failed"
    assert problem["detail"] == "Request validation failed"
    assert problem["errors"]
    assert all(set(error) == {"location", "message", "code"} for error in problem["errors"])


def test_canonical_openapi_contains_only_v1_paths_and_unique_operation_ids(client):
    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    schema = response.json()
    assert schema["openapi"].startswith("3.1.")
    assert schema["info"]["version"] == "1.0.0"
    assert schema["info"]["x-api-major-version"] == 1
    assert schema["paths"]
    assert all(path.startswith("/api/v1/") for path in schema["paths"])
    operation_ids = []
    for path_item in schema["paths"].values():
        for method, operation in path_item.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_ids.append(operation["operationId"])
            assert operation["responses"]["default"]["content"][
                "application/problem+json"
            ]["schema"] == {"$ref": "#/components/schemas/ProblemDetail"}
    assert len(operation_ids) == len(set(operation_ids))


def test_canonical_openapi_matches_reviewed_baseline():
    baseline = json.loads(
        (REPO_ROOT / "contracts" / "openapi-v1.json").read_text(encoding="utf-8")
    )

    assert build_openapi_schema(app, versioned_only=True) == baseline


def test_versioned_internal_http_error_is_redacted(client):
    path = "/api/v1/__test_internal_error"

    async def fail():
        raise HTTPException(status_code=500, detail="password=do-not-leak")

    app.add_api_route(path, fail, methods=["GET"], include_in_schema=False)
    try:
        response = client.get(path)
    finally:
        app.router.routes.pop()

    assert response.status_code == 500
    assert response.json()["code"] == "internal_error"
    assert response.json()["detail"] == "An unexpected internal error occurred"
    assert "do-not-leak" not in response.text
