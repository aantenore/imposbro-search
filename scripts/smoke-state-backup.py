#!/usr/bin/env python3
"""Runtime smoke for control-plane state export, dry-run import, apply, and reconcile."""

import argparse
import os
import sys
import time
from pathlib import Path

from smoke_common import (
    auth_headers,
    create_collection,
    delete_collection,
    load_dotenv,
    request,
    wait_for_ready,
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
        default=int(os.getenv("SMOKE_TIMEOUT_SECONDS", "60")),
        help="Retry timeout for readiness",
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
            {"name": "region", "type": "string", "facet": True},
        ],
    }


def export_state(query_api_url: str, headers, *, include_secrets: bool):
    query = "?include_secrets=true" if include_secrets else ""
    _status, snapshot = request(
        "GET",
        f"{query_api_url}/admin/state/export{query}",
        headers=headers,
        timeout=30,
    )
    return snapshot


def import_state(query_api_url: str, headers, snapshot, *, apply: bool):
    query = "?apply=true" if apply else ""
    _status, result = request(
        "POST",
        f"{query_api_url}/admin/state/import{query}",
        snapshot,
        headers,
        timeout=30,
    )
    return result


def routing_map(query_api_url: str, headers):
    _status, payload = request(
        "GET",
        f"{query_api_url}/admin/routing-map",
        headers=headers,
        timeout=30,
    )
    return payload


def set_routing_rules(query_api_url: str, headers, collection: str, target_cluster: str):
    payload = {
        "collection": collection,
        "rules": [
            {
                "field": "region",
                "value": "dr",
                "cluster": target_cluster,
            }
        ],
        "default_cluster": "default",
    }
    _status, result = request(
        "POST",
        f"{query_api_url}/admin/routing-rules",
        payload,
        headers,
        timeout=30,
    )
    return result


def reconcile_collections(query_api_url: str, headers):
    _status, result = request(
        "POST",
        f"{query_api_url}/admin/collections/reconcile",
        headers=headers,
        timeout=60,
    )
    return result


def assert_snapshot_contains(snapshot, collection: str) -> None:
    if snapshot.get("version") != "imposbro.state.v1":
        raise RuntimeError(f"Unexpected snapshot version: {snapshot.get('version')}")
    if collection not in snapshot.get("collection_schemas", {}):
        raise RuntimeError(f"Snapshot is missing collection schema for {collection}")
    routing = snapshot.get("collection_routing_rules", {}).get(collection)
    if not routing:
        raise RuntimeError(f"Snapshot is missing routing rules for {collection}")
    rules = routing.get("rules", [])
    if not rules or rules[0].get("field") != "region":
        raise RuntimeError(f"Snapshot routing rule is unexpected: {routing}")


def assert_routing_restored(query_api_url: str, headers, collection: str) -> None:
    restored_map = routing_map(query_api_url, headers)
    restored = restored_map.get("collections", {}).get(collection)
    if not restored:
        raise RuntimeError(f"Restored routing map is missing {collection}: {restored_map}")
    rules = restored.get("rules", [])
    if not rules or rules[0].get("field") != "region":
        raise RuntimeError(f"Restored routing rule is unexpected: {restored}")

    _status, schema = request(
        "GET",
        f"{query_api_url}/admin/collections/{collection}",
        headers=headers,
        timeout=30,
    )
    field_names = {field.get("name") for field in schema.get("fields", [])}
    if {"title", "region"} - field_names:
        raise RuntimeError(f"Restored schema is unexpected: {schema}")


def assert_reconcile_recreated(result, collection: str) -> None:
    clusters = result.get("clusters", {})
    created_on = [
        name
        for name, report in clusters.items()
        if collection in report.get("created", [])
    ]
    existing_on = [
        name
        for name, report in clusters.items()
        if collection in report.get("existing", [])
    ]
    if not created_on and not existing_on:
        raise RuntimeError(f"Reconcile did not see {collection}: {result}")
    print(
        "reconcile:",
        f"desired={result.get('collections_desired')}",
        f"created_on={created_on}",
        f"existing_on={existing_on}",
    )


def choose_target_cluster(current_map) -> str:
    clusters = current_map.get("clusters", [])
    if not clusters:
        raise RuntimeError(f"No data clusters are registered: {current_map}")
    return clusters[-1]


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    args = parse_args()
    query_api_url = args.query_api_url.rstrip("/")
    admin_headers = auth_headers("admin")
    collection = f"state_smoke_{int(time.time())}"
    created = False

    try:
        ready = wait_for_ready(query_api_url, args.timeout_seconds)
        print(
            "ready:",
            ready.get("status"),
            f"clusters={ready.get('clusters')}",
            f"collections={ready.get('collections')}",
        )

        current_map = routing_map(query_api_url, admin_headers)
        target_cluster = choose_target_cluster(current_map)
        print("target-cluster:", target_cluster)

        status, created_payload = create_collection(
            query_api_url,
            build_schema(collection),
            admin_headers,
        )
        created = True
        print("collection:", status, collection, created_payload.get("message"))

        routing_result = set_routing_rules(
            query_api_url,
            admin_headers,
            collection,
            target_cluster,
        )
        print("routing:", routing_result.get("message"))

        masked_snapshot = export_state(
            query_api_url,
            admin_headers,
            include_secrets=False,
        )
        if masked_snapshot.get("secrets_included") is not False:
            raise RuntimeError("Masked export unexpectedly included secrets")
        masked_dry_run = import_state(
            query_api_url,
            admin_headers,
            masked_snapshot,
            apply=False,
        )
        print(
            "masked-dry-run:",
            f"dry_run={masked_dry_run.get('dry_run')}",
            f"importable={masked_dry_run.get('importable')}",
        )

        restore_snapshot = export_state(
            query_api_url,
            admin_headers,
            include_secrets=True,
        )
        if restore_snapshot.get("secrets_included") is not True:
            raise RuntimeError("Restore-ready export did not include secrets")
        assert_snapshot_contains(restore_snapshot, collection)

        dry_run = import_state(
            query_api_url,
            admin_headers,
            restore_snapshot,
            apply=False,
        )
        if dry_run.get("dry_run") is not True or dry_run.get("importable") is not True:
            raise RuntimeError(f"Restore-ready dry-run failed: {dry_run}")
        print("restore-dry-run:", dry_run.get("counts"))

        delete_collection(query_api_url, collection, admin_headers)
        created = False
        after_delete = routing_map(query_api_url, admin_headers)
        if collection in after_delete.get("collections", {}):
            raise RuntimeError(f"Delete did not remove routing state for {collection}")
        print("state-removed:", collection)

        applied = import_state(
            query_api_url,
            admin_headers,
            restore_snapshot,
            apply=True,
        )
        if applied.get("dry_run") is not False:
            raise RuntimeError(f"Apply did not restore state: {applied}")
        created = True
        print("restore-applied:", applied.get("counts"))

        assert_routing_restored(query_api_url, admin_headers, collection)
        reconcile_result = reconcile_collections(query_api_url, admin_headers)
        assert_reconcile_recreated(reconcile_result, collection)

        print("state-smoke-ok:", collection)
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
