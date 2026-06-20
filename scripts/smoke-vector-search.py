#!/usr/bin/env python3
"""Runtime smoke test for vector search across Query API, Kafka, Typesense, and Admin UI proxy."""

import argparse
import os
import sys
import time
from pathlib import Path

from smoke_common import (
    VECTOR_SEARCH_PAYLOAD,
    auth_headers,
    create_vector_collection,
    delete_collection,
    delete_document,
    get_document,
    ingest_vector_documents_batch,
    load_dotenv,
    vector_ids,
    wait_for_document_not_found,
    wait_for_ready,
    wait_for_missing_id,
    wait_for_vector_result,
)


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

        status, created_payload = create_vector_collection(
            query_api_url, collection, admin_headers
        )
        created = True
        print("collection:", status, collection, created_payload.get("message"))

        status, batch_ingested = ingest_vector_documents_batch(
            query_api_url, collection, ingest_headers
        )
        for item in batch_ingested.get("items", []):
            print(
                "batch-ingest:",
                status,
                item.get("document_id"),
                f"routed_to={item.get('routed_to')}",
            )

        result = wait_for_vector_result(
            query_api_url,
            collection,
            VECTOR_SEARCH_PAYLOAD,
            search_headers,
            args.timeout_seconds,
            partial=False,
        )
        ids = vector_ids(result)
        print(
            "query-api-search:",
            f"found={result.get('found')}",
            f"clusters_responded={result.get('clusters_responded')}",
            f"partial={result.get('partial')}",
            f"ids={ids[:5]}",
        )

        status, retrieved = get_document(
            query_api_url,
            collection,
            "near",
            search_headers,
        )
        if retrieved.get("document", {}).get("id") != "near":
            raise RuntimeError(f"Unexpected retrieved document: {retrieved}")
        print(
            "get-document:",
            status,
            f"document_id={retrieved.get('document_id')}",
            f"found_in={retrieved.get('found_in')}",
        )

        if not args.skip_admin_ui:
            proxy_result = wait_for_vector_result(
                admin_ui_api_url,
                collection,
                VECTOR_SEARCH_PAYLOAD,
                search_headers,
                args.timeout_seconds,
                partial=False,
            )
            proxy_ids = vector_ids(proxy_result)
            print(
                "admin-ui-proxy-search:",
                f"found={proxy_result.get('found')}",
                f"clusters_responded={proxy_result.get('clusters_responded')}",
                f"partial={proxy_result.get('partial')}",
                f"ids={proxy_ids[:5]}",
            )

        status, deleted = delete_document(
            query_api_url,
            collection,
            "far",
            ingest_headers,
        )
        print(
            "delete:",
            status,
            f"document_id={deleted.get('document_id')}",
            f"routed_to={deleted.get('routed_to')}",
        )
        post_delete = wait_for_missing_id(
            query_api_url,
            collection,
            VECTOR_SEARCH_PAYLOAD,
            search_headers,
            args.timeout_seconds,
            missing_id="far",
            expected_first_id="near",
            partial=False,
        )
        print(
            "post-delete-search:",
            f"found={post_delete.get('found')}",
            f"ids={vector_ids(post_delete)[:5]}",
        )
        missing_payload = wait_for_document_not_found(
            query_api_url,
            collection,
            "far",
            search_headers,
            args.timeout_seconds,
        )
        print("post-delete-get:", missing_payload.get("detail"))

        print("smoke-ok:", collection)
        return 0
    finally:
        if created and not args.keep_collection:
            try:
                delete_collection(query_api_url, collection, admin_headers)
                print("cleanup:", collection)
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
                print(f"cleanup-warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
