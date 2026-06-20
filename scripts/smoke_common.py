"""Shared helpers for IMPOSBRO runtime smoke tests."""

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path


VECTOR_DOCS = [
    {"id": "near", "title": "Near Vector", "embedding": [0.1, 0.2, 0.3]},
    {"id": "far", "title": "Far Vector", "embedding": [0.9, 0.9, 0.9]},
]

VECTOR_SEARCH_PAYLOAD = {
    "q": "*",
    "vector_query": "embedding:([0.1,0.2,0.3], k:10)",
    "exclude_fields": "embedding",
    "offset": 0,
    "limit": 10,
}


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def scope_matches(scopes, required_scope: str) -> bool:
    normalized = {str(scope).strip().lower() for scope in scopes}
    if "*" in normalized or required_scope in normalized:
        return True
    if required_scope in {"search", "ingest"} and (
        "data" in normalized or "data:*" in normalized
    ):
        return True
    if required_scope == "admin" and "admin:*" in normalized:
        return True
    return False


def scoped_key_for(required_scope: str):
    raw = os.getenv("SCOPED_API_KEYS", "").strip()
    if not raw:
        return None
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key", "")).strip()
        scopes = entry.get("scopes", [])
        if isinstance(scopes, str):
            scopes = [scope.strip() for scope in scopes.split(",")]
        if key and isinstance(scopes, list) and scope_matches(scopes, required_scope):
            return key
    return None


def auth_headers(scope: str):
    env_key = {
        "admin": "SMOKE_ADMIN_API_KEY",
        "search": "SMOKE_SEARCH_API_KEY",
        "ingest": "SMOKE_INGEST_API_KEY",
    }[scope]
    fallback_key = "ADMIN_API_KEY" if scope == "admin" else "DATA_API_KEY"
    key = os.getenv(env_key) or os.getenv(fallback_key) or scoped_key_for(scope)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-API-Key"] = key
    return headers


def _parse_response_body(raw: bytes):
    text = raw.decode("utf-8")
    return json.loads(text) if text else None


def request(method, url, payload=None, headers=None, timeout=15, ok_statuses=None):
    ok_statuses = set(ok_statuses or {200, 201, 202, 204})
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = _parse_response_body(resp.read())
            if resp.status not in ok_statuses:
                raise RuntimeError(f"{method} {url} -> {resp.status}: {payload}")
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            parsed = _parse_response_body(raw)
        except json.JSONDecodeError:
            parsed = raw.decode("utf-8")
        if exc.code in ok_statuses:
            return exc.code, parsed
        raise RuntimeError(f"{method} {url} -> {exc.code}: {parsed}") from exc


def wait_for_ready(query_api_url: str, timeout_seconds: int):
    deadline = time.monotonic() + timeout_seconds
    last_error = None
    while time.monotonic() < deadline:
        try:
            status, payload = request("GET", f"{query_api_url}/ready", timeout=5)
            if status == 200 and payload.get("status") == "healthy":
                return payload
            last_error = payload
        except Exception as exc:  # noqa: BLE001 - smoke scripts should report last failure.
            last_error = exc
        time.sleep(1)
    raise RuntimeError(f"Query API readiness did not converge: {last_error!r}")


def wait_for_degraded_ready(query_api_url: str, timeout_seconds: int):
    deadline = time.monotonic() + timeout_seconds
    last_payload = None
    while time.monotonic() < deadline:
        try:
            _status, payload = request(
                "GET",
                f"{query_api_url}/ready",
                timeout=5,
                ok_statuses={200, 503},
            )
            last_payload = payload
            if payload.get("status") == "degraded":
                return payload
        except Exception as exc:  # noqa: BLE001 - smoke scripts should report last failure.
            last_payload = exc
        time.sleep(1)
    raise RuntimeError(f"Degraded readiness did not converge: {last_payload!r}")


def wait_for_vector_result(
    query_api_url: str,
    collection: str,
    payload,
    headers,
    timeout_seconds: int,
    *,
    min_hits: int = 2,
    partial=None,
):
    deadline = time.monotonic() + timeout_seconds
    last_result = None
    while time.monotonic() < deadline:
        try:
            status, result = request(
                "POST",
                f"{query_api_url}/search/{collection}",
                payload,
                headers,
                timeout=30,
            )
            hits = result.get("hits", [])
            ids = [hit.get("document", {}).get("id") for hit in hits]
            last_result = {"status": status, "ids": ids, "result": result}
            partial_matches = partial is None or result.get("partial") is partial
            if (
                status == 200
                and len(ids) >= min_hits
                and ids[:1] == ["near"]
                and partial_matches
            ):
                return result
        except Exception as exc:  # noqa: BLE001 - smoke scripts should report last failure.
            last_result = exc
        time.sleep(1)
    raise RuntimeError(f"Vector search did not converge: {last_result!r}")


def hit_ids(result):
    return [hit.get("document", {}).get("id") for hit in result.get("hits", [])]


def vector_ids(result):
    return hit_ids(result)


def create_vector_collection(query_api_url: str, collection: str, admin_headers):
    schema = {
        "name": collection,
        "fields": [
            {"name": "title", "type": "string", "facet": False},
            {"name": "embedding", "type": "float[]", "facet": False, "num_dim": 3},
        ],
    }
    return create_collection(query_api_url, schema, admin_headers)


def create_collection(query_api_url: str, schema, admin_headers):
    return request(
        "POST",
        f"{query_api_url}/admin/collections",
        schema,
        admin_headers,
        timeout=30,
    )


def delete_collection(query_api_url: str, collection: str, admin_headers) -> None:
    request(
        "DELETE",
        f"{query_api_url}/admin/collections/{collection}",
        headers=admin_headers,
        timeout=30,
    )


def delete_document(
    query_api_url: str,
    collection: str,
    document_id: str,
    ingest_headers,
):
    return request(
        "DELETE",
        f"{query_api_url}/documents/{collection}/{document_id}",
        headers=ingest_headers,
        timeout=30,
    )


def get_document(
    query_api_url: str,
    collection: str,
    document_id: str,
    search_headers,
    *,
    ok_statuses=None,
):
    return request(
        "GET",
        f"{query_api_url}/documents/{collection}/{document_id}",
        headers=search_headers,
        timeout=30,
        ok_statuses=ok_statuses,
    )


def ingest_vector_documents(query_api_url: str, collection: str, ingest_headers):
    results = []
    for doc in VECTOR_DOCS:
        status, ingested = request(
            "POST",
            f"{query_api_url}/ingest/{collection}",
            doc,
            ingest_headers,
        )
        results.append((status, doc["id"], ingested))
    return results


def ingest_vector_documents_batch(query_api_url: str, collection: str, ingest_headers):
    status, ingested = request(
        "POST",
        f"{query_api_url}/ingest/{collection}/batch",
        {"documents": VECTOR_DOCS},
        ingest_headers,
        timeout=30,
    )
    if ingested.get("accepted") != len(VECTOR_DOCS):
        raise RuntimeError(f"Batch vector ingest rejected documents: {ingested}")
    return status, ingested


def wait_for_search_count(
    query_api_url: str,
    collection: str,
    payload,
    headers,
    timeout_seconds: int,
    *,
    expected_found: int,
    expected_first_id: str,
    partial=False,
):
    deadline = time.monotonic() + timeout_seconds
    last_result = None
    while time.monotonic() < deadline:
        try:
            status, result = request(
                "POST",
                f"{query_api_url}/search/{collection}",
                payload,
                headers,
                timeout=30,
            )
            hits = result.get("hits", [])
            ids = [hit.get("document", {}).get("id") for hit in hits]
            last_result = {
                "status": status,
                "found": result.get("found"),
                "ids": ids,
            }
            partial_matches = result.get("partial") is partial
            if (
                status == 200
                and result.get("found", 0) >= expected_found
                and ids[:1] == [expected_first_id]
                and partial_matches
            ):
                return result
        except Exception as exc:  # noqa: BLE001 - smoke scripts should report last failure.
            last_result = exc
        time.sleep(1)
    raise RuntimeError(f"Search count did not converge: {last_result!r}")


def wait_for_missing_id(
    query_api_url: str,
    collection: str,
    payload,
    headers,
    timeout_seconds: int,
    *,
    missing_id: str,
    expected_first_id: str,
    partial=False,
):
    deadline = time.monotonic() + timeout_seconds
    last_result = None
    while time.monotonic() < deadline:
        try:
            status, result = request(
                "POST",
                f"{query_api_url}/search/{collection}",
                payload,
                headers,
                timeout=30,
            )
            ids = hit_ids(result)
            last_result = {
                "status": status,
                "found": result.get("found"),
                "ids": ids,
            }
            partial_matches = result.get("partial") is partial
            if (
                status == 200
                and ids[:1] == [expected_first_id]
                and missing_id not in ids
                and partial_matches
            ):
                return result
        except Exception as exc:  # noqa: BLE001 - smoke scripts should report last failure.
            last_result = exc
        time.sleep(1)
    raise RuntimeError(f"Deleted document remained searchable: {last_result!r}")


def wait_for_document_not_found(
    query_api_url: str,
    collection: str,
    document_id: str,
    headers,
    timeout_seconds: int,
):
    deadline = time.monotonic() + timeout_seconds
    last_result = None
    while time.monotonic() < deadline:
        try:
            status, payload = get_document(
                query_api_url,
                collection,
                document_id,
                headers,
                ok_statuses={200, 404},
            )
            last_result = {"status": status, "payload": payload}
            if status == 404:
                return payload
        except Exception as exc:  # noqa: BLE001 - smoke scripts should report last failure.
            last_result = exc
        time.sleep(1)
    raise RuntimeError(f"Document lookup still found {document_id}: {last_result!r}")
