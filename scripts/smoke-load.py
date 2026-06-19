#!/usr/bin/env python3
"""Runtime load smoke for Kafka ingest, indexing worker, and federated search."""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from smoke_common import (
    auth_headers,
    create_collection,
    delete_collection,
    hit_ids,
    load_dotenv,
    request,
    wait_for_ready,
    wait_for_search_count,
)


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query-api-url",
        default=os.getenv("QUERY_API_URL", "http://localhost:8000"),
        help="Base URL for Query API",
    )
    parser.add_argument(
        "--documents",
        type=int,
        default=int(os.getenv("SMOKE_LOAD_DOCUMENTS", "75")),
        help="Number of documents to ingest",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("SMOKE_LOAD_CONCURRENCY", "8")),
        help="Concurrent ingest requests",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("SMOKE_TIMEOUT_SECONDS", "90")),
        help="Retry timeout for readiness and indexing convergence",
    )
    parser.add_argument(
        "--keep-collection",
        action="store_true",
        help="Keep the temporary smoke collection for manual inspection",
    )
    return parser.parse_args()


def build_schema(collection: str):
    return {
        "name": collection,
        "fields": [
            {"name": "title", "type": "string", "facet": False},
            {"name": "tenant", "type": "string", "facet": True},
            {"name": "rank", "type": "int32", "facet": False},
        ],
    }


def build_document(index: int, tenant: str):
    return {
        "id": f"doc-{index:05d}",
        "title": f"load smoke document {index:05d}",
        "tenant": tenant,
        "rank": index,
    }


def ingest_document(query_api_url: str, collection: str, headers, document):
    status, payload = request(
        "POST",
        f"{query_api_url}/ingest/{collection}",
        document,
        headers,
        timeout=30,
    )
    return status, payload.get("document_id")


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    args = parse_args()
    if args.documents < 1:
        raise SystemExit("--documents must be >= 1")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")

    query_api_url = args.query_api_url.rstrip("/")
    collection = f"load_smoke_{int(time.time())}"
    tenant = f"tenant_{int(time.time())}"
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

        status, created_payload = create_collection(
            query_api_url,
            build_schema(collection),
            admin_headers,
        )
        created = True
        print("collection:", status, collection, created_payload.get("message"))

        documents = [build_document(index, tenant) for index in range(args.documents)]
        started_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [
                executor.submit(
                    ingest_document,
                    query_api_url,
                    collection,
                    ingest_headers,
                    document,
                )
                for document in documents
            ]
            ingested_ids = []
            for future in as_completed(futures):
                status, document_id = future.result()
                if status != 200:
                    raise RuntimeError(f"Unexpected ingest status {status}")
                ingested_ids.append(document_id)
        ingest_elapsed = time.monotonic() - started_at
        print(
            "ingest-load:",
            f"documents={len(ingested_ids)}",
            f"concurrency={args.concurrency}",
            f"seconds={ingest_elapsed:.2f}",
        )

        search_payload = {
            "q": "load",
            "query_by": "title",
            "filter_by": f"tenant:={tenant}",
            "sort_by": "rank:asc",
            "offset": 0,
            "limit": min(args.documents, 250),
        }
        result = wait_for_search_count(
            query_api_url,
            collection,
            search_payload,
            search_headers,
            args.timeout_seconds,
            expected_found=args.documents,
            expected_first_id="doc-00000",
            partial=False,
        )
        ids = hit_ids(result)
        print(
            "search-load:",
            f"found={result.get('found')}",
            f"clusters_responded={result.get('clusters_responded')}",
            f"partial={result.get('partial')}",
            f"first_ids={ids[:5]}",
        )
        print("load-smoke-ok:", collection)
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
