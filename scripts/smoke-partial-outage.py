#!/usr/bin/env python3
"""Runtime smoke test for federated search during a secondary data-cluster outage."""

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

from smoke_common import (
    VECTOR_SEARCH_PAYLOAD,
    auth_headers,
    create_vector_collection,
    delete_collection,
    ingest_vector_documents,
    load_dotenv,
    request,
    vector_ids,
    wait_for_degraded_ready,
    wait_for_ready,
    wait_for_vector_result,
)


DEFAULT_OUTAGE_SERVICES = (
    "typesense-data2-1",
    "typesense-data2-2",
    "typesense-data2-3",
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
        help="Retry timeout for readiness and search convergence",
    )
    parser.add_argument(
        "--outage-services",
        default=os.getenv("SMOKE_OUTAGE_SERVICES", ",".join(DEFAULT_OUTAGE_SERVICES)),
        help="Comma-separated docker compose services to stop during the outage",
    )
    parser.add_argument(
        "--keep-collection",
        action="store_true",
        help="Keep the temporary smoke collection for manual inspection",
    )
    return parser.parse_args()


def docker_compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], check=True)


def restart_services(services) -> None:
    docker_compose("up", "-d", *services)


def assert_partial_outage_result(result):
    ids = vector_ids(result)
    failed_clusters = result.get("failed_clusters", [])
    if result.get("partial") is not True:
        raise RuntimeError(f"Expected partial=true during outage, got {result}")
    if result.get("clusters_responded") != 1:
        raise RuntimeError(f"Expected exactly one responding cluster, got {result}")
    if "default-data-cluster-2" not in failed_clusters:
        raise RuntimeError(f"Expected default-data-cluster-2 failure, got {failed_clusters}")
    if ids[:2] != ["near", "far"]:
        raise RuntimeError(f"Expected ordered healthy-cluster hits, got {ids}")


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    args = parse_args()
    query_api_url = args.query_api_url.rstrip("/")
    outage_services = tuple(
        service.strip() for service in args.outage_services.split(",") if service.strip()
    )
    collection = f"outage_smoke_{int(time.time())}"
    admin_headers = auth_headers("admin")
    ingest_headers = auth_headers("ingest")
    search_headers = auth_headers("search")
    created = False
    services_stopped = False

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

        for status, doc_id, ingested in ingest_vector_documents(
            query_api_url, collection, ingest_headers
        ):
            print("ingest:", status, doc_id, f"routed_to={ingested.get('routed_to')}")

        baseline = wait_for_vector_result(
            query_api_url,
            collection,
            VECTOR_SEARCH_PAYLOAD,
            search_headers,
            args.timeout_seconds,
            partial=False,
        )
        print(
            "baseline-search:",
            f"clusters_responded={baseline.get('clusters_responded')}",
            f"partial={baseline.get('partial')}",
            f"ids={vector_ids(baseline)[:5]}",
        )

        print("stopping:", ",".join(outage_services))
        docker_compose("stop", *outage_services)
        services_stopped = True

        degraded = wait_for_degraded_ready(query_api_url, args.timeout_seconds)
        print("ready-degraded:", degraded)

        partial = wait_for_vector_result(
            query_api_url,
            collection,
            VECTOR_SEARCH_PAYLOAD,
            search_headers,
            args.timeout_seconds,
            partial=True,
        )
        assert_partial_outage_result(partial)
        print(
            "partial-search:",
            f"clusters_responded={partial.get('clusters_responded')}",
            f"failed_clusters={partial.get('failed_clusters')}",
            f"partial={partial.get('partial')}",
            f"ids={vector_ids(partial)[:5]}",
        )

        print("outage-smoke-ok:", collection)
        return 0
    finally:
        if services_stopped:
            try:
                print("restarting:", ",".join(outage_services))
                restart_services(outage_services)
                wait_for_ready(query_api_url, args.timeout_seconds)
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
                print(f"restart-warning: {exc}", file=sys.stderr)
        if created and not args.keep_collection:
            try:
                delete_collection(query_api_url, collection, admin_headers)
                print("cleanup:", collection)
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
                print(f"cleanup-warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
