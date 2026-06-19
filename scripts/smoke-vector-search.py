#!/usr/bin/env python3
"""Runtime smoke test for vector search across Query API, Kafka, Typesense, and Admin UI proxy."""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


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


def request(method, url, payload=None, headers=None, timeout=15):
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = raw
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


def wait_for_vector_result(query_api_url: str, collection: str, payload, headers, timeout_seconds: int):
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
            if status == 200 and len(ids) >= 2 and ids[0] == "near":
                return result
        except Exception as exc:  # noqa: BLE001 - smoke scripts should report last failure.
            last_result = exc
        time.sleep(1)
    raise RuntimeError(f"Vector search did not converge: {last_result!r}")


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query-api-url",
        default=os.getenv("QUERY_API_URL", "http://localhost:8000"),
        help="Base URL for Query API",
    )
    parser.add_argument(
        "--admin-ui-api-url",
        default=os.getenv("ADMIN_UI_API_URL", "http://localhost:3001/api"),
        help="Base URL for Admin UI /api proxy",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("SMOKE_TIMEOUT_SECONDS", "60")),
        help="Retry timeout for readiness and indexing convergence",
    )
    parser.add_argument(
        "--skip-admin-ui",
        action="store_true",
        default=os.getenv("SMOKE_SKIP_ADMIN_UI", "").lower() in {"1", "true", "yes"},
        help="Skip Admin UI proxy verification",
    )
    parser.add_argument(
        "--keep-collection",
        action="store_true",
        help="Keep the temporary smoke collection for manual inspection",
    )
    return parser.parse_args()


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    args = parse_args()
    query_api_url = args.query_api_url.rstrip("/")
    admin_ui_api_url = args.admin_ui_api_url.rstrip("/")
    collection = f"vector_smoke_{int(time.time())}"
    admin_headers = auth_headers("admin")
    ingest_headers = auth_headers("ingest")
    search_headers = auth_headers("search")
    created = False

    try:
        ready = wait_for_ready(query_api_url, args.timeout_seconds)
        print(
            "ready:",
            ready.get("status"),
            f"clusters={ready.get('clusters')}",
            f"collections={ready.get('collections')}",
        )

        schema = {
            "name": collection,
            "fields": [
                {"name": "title", "type": "string", "facet": False},
                {"name": "embedding", "type": "float[]", "facet": False, "num_dim": 3},
            ],
        }
        status, created_payload = request(
            "POST",
            f"{query_api_url}/admin/collections",
            schema,
            admin_headers,
            timeout=30,
        )
        created = True
        print("collection:", status, collection, created_payload.get("message"))

        for doc in [
            {"id": "near", "title": "Near Vector", "embedding": [0.1, 0.2, 0.3]},
            {"id": "far", "title": "Far Vector", "embedding": [0.9, 0.9, 0.9]},
        ]:
            status, ingested = request(
                "POST",
                f"{query_api_url}/ingest/{collection}",
                doc,
                ingest_headers,
            )
            print("ingest:", status, doc["id"], f"routed_to={ingested.get('routed_to')}")

        search_payload = {
            "q": "*",
            "vector_query": "embedding:([0.1,0.2,0.3], k:10)",
            "exclude_fields": "embedding",
            "offset": 0,
            "limit": 10,
        }
        result = wait_for_vector_result(
            query_api_url,
            collection,
            search_payload,
            search_headers,
            args.timeout_seconds,
        )
        ids = [hit.get("document", {}).get("id") for hit in result.get("hits", [])]
        print(
            "query-api-search:",
            f"found={result.get('found')}",
            f"clusters_responded={result.get('clusters_responded')}",
            f"partial={result.get('partial')}",
            f"ids={ids[:5]}",
        )

        if not args.skip_admin_ui:
            proxy_result = wait_for_vector_result(
                admin_ui_api_url,
                collection,
                search_payload,
                search_headers,
                args.timeout_seconds,
            )
            proxy_ids = [
                hit.get("document", {}).get("id")
                for hit in proxy_result.get("hits", [])
            ]
            print(
                "admin-ui-proxy-search:",
                f"found={proxy_result.get('found')}",
                f"clusters_responded={proxy_result.get('clusters_responded')}",
                f"partial={proxy_result.get('partial')}",
                f"ids={proxy_ids[:5]}",
            )

        print("smoke-ok:", collection)
        return 0
    finally:
        if created and not args.keep_collection:
            try:
                request(
                    "DELETE",
                    f"{query_api_url}/admin/collections/{collection}",
                    headers=admin_headers,
                    timeout=30,
                )
                print("cleanup:", collection)
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
                print(f"cleanup-warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
