#!/usr/bin/env python3
"""Multi-instance smoke for Query API and indexing worker rolling behavior."""

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from smoke_common import (
    auth_headers,
    create_collection,
    delete_collection,
    load_dotenv,
    request,
    wait_for_ready,
    wait_for_search_count,
)


CONSUMER_GROUP = "imposbro_federated_indexing_group"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query-api-url",
        default=os.getenv("QUERY_API_URL", "http://localhost:8000"),
        help="Base URL for the load-balanced Query API",
    )
    parser.add_argument(
        "--documents",
        type=int,
        default=int(os.getenv("SMOKE_SCALE_DOCUMENTS", "90")),
        help="Number of documents to ingest during rolling restart",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=int(os.getenv("SMOKE_SCALE_CONCURRENCY", "12")),
        help="Concurrent ingest requests",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=int(os.getenv("SMOKE_TIMEOUT_SECONDS", "120")),
        help="Retry timeout for readiness, ingest retries, lag, and convergence",
    )
    parser.add_argument(
        "--lag-budget",
        type=int,
        default=int(os.getenv("SMOKE_LAG_BUDGET", "0")),
        help="Maximum acceptable Kafka consumer-group lag after recovery",
    )
    parser.add_argument(
        "--keep-collection",
        action="store_true",
        help="Keep the temporary smoke collection for manual inspection",
    )
    return parser.parse_args()


def run_compose(*args, check=True):
    result = subprocess.run(
        ["docker", "compose", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def container_ids(service: str):
    result = run_compose("ps", "-q", service)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def restart_container(container_id: str):
    result = subprocess.run(
        ["docker", "restart", "--time", "20", container_id],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())


def kafka_lag():
    result = run_compose(
        "exec",
        "-T",
        "kafka",
        "/opt/bitnami/kafka/bin/kafka-consumer-groups.sh",
        "--bootstrap-server",
        "kafka:9092",
        "--describe",
        "--group",
        CONSUMER_GROUP,
        check=False,
    )
    if result.returncode != 0:
        return None, result.stderr.strip() or result.stdout.strip()

    total = 0
    rows = 0
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 6 or parts[0] != CONSUMER_GROUP:
            continue
        try:
            total += int(parts[5])
        except ValueError:
            continue
        rows += 1
    if rows == 0:
        return None, result.stdout.strip()
    return total, result.stdout.strip()


def wait_for_lag_budget(timeout_seconds: int, budget: int):
    deadline = time.monotonic() + timeout_seconds
    last_detail = None
    while time.monotonic() < deadline:
        lag, detail = kafka_lag()
        last_detail = detail
        if lag is None:
            time.sleep(2)
            continue
        print("kafka-lag:", lag)
        if lag <= budget:
            return lag
        time.sleep(2)
    raise RuntimeError(f"Kafka lag did not recover to <= {budget}: {last_detail}")


def build_schema(collection: str):
    return {
        "name": collection,
        "fields": [
            {"name": "title", "type": "string", "facet": False},
            {"name": "tenant", "type": "string", "facet": True},
            {"name": "rank", "type": "int32", "facet": False},
        ],
        "default_sorting_field": "rank",
    }


def build_document(index: int, tenant: str):
    return {
        "id": f"scale-{index:05d}",
        "title": f"scale smoke document {index:05d}",
        "tenant": tenant,
        "rank": index,
    }


def ingest_with_retry(query_api_url: str, collection: str, headers, document, deadline):
    last_error = None
    while time.monotonic() < deadline:
        try:
            status, payload = request(
                "POST",
                f"{query_api_url}/ingest/{collection}",
                document,
                headers,
                timeout=10,
            )
            if status == 200:
                return payload.get("document_id")
            last_error = f"status={status}"
        except Exception as exc:  # noqa: BLE001 - retries report the final error.
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(f"Ingest retry budget exhausted: {last_error!r}")


def restart_services_during_ingest(query_api_url: str, timeout_seconds: int):
    for service in ("query_api", "indexing_service"):
        ids = container_ids(service)
        if not ids:
            raise RuntimeError(f"No containers found for service {service}")
        for container_id in ids:
            print("rolling-restart:", service, container_id[:12])
            restart_container(container_id)
            wait_for_ready(query_api_url, timeout_seconds)


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    args = parse_args()
    if args.documents < 1:
        raise SystemExit("--documents must be >= 1")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")

    query_api_url = args.query_api_url.rstrip("/")
    collection = f"scale_smoke_{int(time.time())}"
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
        status, payload = create_collection(
            query_api_url,
            build_schema(collection),
            admin_headers,
        )
        created = True
        print("collection:", status, collection, payload.get("message"))

        documents = [build_document(index, tenant) for index in range(args.documents)]
        deadline = time.monotonic() + args.timeout_seconds
        started_at = time.monotonic()
        with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
            futures = [
                executor.submit(
                    ingest_with_retry,
                    query_api_url,
                    collection,
                    ingest_headers,
                    document,
                    deadline,
                )
                for document in documents
            ]
            restart_services_during_ingest(query_api_url, args.timeout_seconds)
            ingested_ids = [future.result() for future in as_completed(futures)]

        print(
            "ingest-rolling:",
            f"documents={len(ingested_ids)}",
            f"concurrency={args.concurrency}",
            f"seconds={time.monotonic() - started_at:.2f}",
        )
        wait_for_lag_budget(args.timeout_seconds, args.lag_budget)

        search_payload = {
            "q": "scale",
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
            expected_first_id="scale-00000",
            partial=False,
        )
        print(
            "scale-search:",
            f"found={result.get('found')}",
            f"responded={result.get('clusters_responded')}/{result.get('clusters_queried')}",
            f"partial={result.get('partial')}",
        )
        print("scale-smoke-ok:", collection)
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
