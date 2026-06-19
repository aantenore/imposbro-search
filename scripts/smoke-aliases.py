#!/usr/bin/env python3
"""Runtime smoke for collection alias creation, search, switch, and cleanup."""

import argparse
import os
import sys
import time
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
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("SMOKE_TIMEOUT_SECONDS", "90")),
        help="Retry timeout for readiness and alias search convergence",
    )
    parser.add_argument(
        "--keep-collections",
        action="store_true",
        help="Keep the temporary smoke collections and alias for manual inspection",
    )
    return parser.parse_args()


def build_schema(collection: str):
    return {
        "name": collection,
        "fields": [
            {"name": "title", "type": "string", "facet": False},
            {"name": "marker", "type": "string", "facet": True},
            {"name": "rank", "type": "int32", "facet": False},
        ],
    }


def routing_map(query_api_url: str, headers):
    _status, payload = request(
        "GET",
        f"{query_api_url}/admin/routing-map",
        headers=headers,
        timeout=30,
    )
    return payload


def upsert_alias(query_api_url: str, headers, alias: str, collection: str, cluster: str):
    _status, payload = request(
        "PUT",
        (
            f"{query_api_url}/admin/aliases/{alias}"
            f"?collection_name={collection}&cluster_name={cluster}"
        ),
        headers=headers,
        timeout=30,
    )
    return payload


def delete_alias(query_api_url: str, headers, alias: str, cluster: str):
    return request(
        "DELETE",
        f"{query_api_url}/admin/aliases/{alias}?cluster_name={cluster}",
        headers=headers,
        timeout=30,
        ok_statuses={200, 404},
    )


def ingest_document(query_api_url: str, collection: str, headers, document):
    _status, payload = request(
        "POST",
        f"{query_api_url}/ingest/{collection}",
        document,
        headers,
        timeout=30,
    )
    return payload


def switch_alias_on_all_clusters(query_api_url, headers, alias, collection, clusters):
    for cluster in clusters:
        payload = upsert_alias(query_api_url, headers, alias, collection, cluster)
        print("alias-upsert:", cluster, payload.get("message"))


def wait_for_alias_marker(query_api_url, alias, headers, marker, expected_id, timeout):
    payload = {
        "q": "alias",
        "query_by": "title",
        "filter_by": f"marker:={marker}",
        "sort_by": "rank:asc",
        "offset": 0,
        "limit": 10,
    }
    result = wait_for_search_count(
        query_api_url,
        alias,
        payload,
        headers,
        timeout,
        expected_found=1,
        expected_first_id=expected_id,
        partial=False,
    )
    print(
        "alias-search:",
        f"marker={marker}",
        f"found={result.get('found')}",
        f"ids={hit_ids(result)}",
    )
    return result


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    args = parse_args()
    query_api_url = args.query_api_url.rstrip("/")
    stamp = int(time.time())
    alias = f"alias_smoke_{stamp}"
    collection_v1 = f"{alias}_v1"
    collection_v2 = f"{alias}_v2"
    admin_headers = auth_headers("admin")
    ingest_headers = auth_headers("ingest")
    search_headers = auth_headers("search")
    created_collections = []
    alias_clusters = []

    try:
        ready = wait_for_ready(query_api_url, args.timeout_seconds)
        print(
            "ready:",
            ready.get("status"),
            f"clusters={ready.get('clusters')}",
            f"collections={ready.get('collections')}",
        )

        current_map = routing_map(query_api_url, admin_headers)
        clusters = current_map.get("clusters") or []
        if not clusters:
            raise RuntimeError(f"No clusters available for alias smoke: {current_map}")

        for collection in (collection_v1, collection_v2):
            status, payload = create_collection(
                query_api_url,
                build_schema(collection),
                admin_headers,
            )
            created_collections.append(collection)
            print("collection:", status, collection, payload.get("message"))

        for collection, marker, document_id in (
            (collection_v1, "v1", "alias-v1"),
            (collection_v2, "v2", "alias-v2"),
        ):
            payload = ingest_document(
                query_api_url,
                collection,
                ingest_headers,
                {
                    "id": document_id,
                    "title": f"alias smoke {marker}",
                    "marker": marker,
                    "rank": 1,
                },
            )
            print("ingest:", collection, payload.get("document_id"), payload.get("routed_to"))

        switch_alias_on_all_clusters(
            query_api_url,
            admin_headers,
            alias,
            collection_v2,
            clusters,
        )
        alias_clusters = list(clusters)
        wait_for_alias_marker(
            query_api_url,
            alias,
            search_headers,
            "v2",
            "alias-v2",
            args.timeout_seconds,
        )

        switch_alias_on_all_clusters(
            query_api_url,
            admin_headers,
            alias,
            collection_v1,
            clusters,
        )
        wait_for_alias_marker(
            query_api_url,
            alias,
            search_headers,
            "v1",
            "alias-v1",
            args.timeout_seconds,
        )

        print("alias-smoke-ok:", alias)
        return 0
    finally:
        if not args.keep_collections:
            for cluster in alias_clusters:
                try:
                    delete_alias(query_api_url, admin_headers, alias, cluster)
                    print("cleanup-alias:", cluster, alias)
                except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
                    print(f"cleanup-alias-warning: {exc}", file=sys.stderr)
            for collection in reversed(created_collections):
                try:
                    delete_collection(query_api_url, collection, admin_headers)
                    print("cleanup-collection:", collection)
                except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
                    print(f"cleanup-collection-warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
