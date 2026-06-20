#!/usr/bin/env python3
"""Benchmark a deployed IMPOSBRO Query API ingest and search path.

The harness is intentionally deployment-agnostic: point it at a Kubernetes
Ingress, port-forward, or Docker endpoint and provide credentials through the
same environment variables used by the smoke tests.
"""

import argparse
import json
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

from smoke_common import (
    auth_headers,
    create_collection,
    delete_collection,
    load_dotenv,
    request,
    wait_for_ready,
)


CATEGORY_COUNT = 10


def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw in (None, "") else int(raw)


def env_float(name: str):
    raw = os.getenv(name)
    return None if raw in (None, "") else float(raw)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def percentile(values, pct: float):
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    rank = max(0, min(len(ordered) - 1, math.ceil((pct / 100) * len(ordered)) - 1))
    return ordered[rank]


def latency_summary(values):
    if not values:
        return {
            "count": 0,
            "min_ms": None,
            "avg_ms": None,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "max_ms": None,
        }
    return {
        "count": len(values),
        "min_ms": round(min(values), 2),
        "avg_ms": round(mean(values), 2),
        "p50_ms": round(percentile(values, 50), 2),
        "p95_ms": round(percentile(values, 95), 2),
        "p99_ms": round(percentile(values, 99), 2),
        "max_ms": round(max(values), 2),
    }


def build_schema(collection: str):
    return {
        "name": collection,
        "fields": [
            {"name": "title", "type": "string", "facet": False},
            {"name": "body", "type": "string", "facet": False},
            {"name": "tenant", "type": "string", "facet": True},
            {"name": "category", "type": "string", "facet": True},
            {"name": "rank", "type": "int32", "facet": False},
        ],
    }


def build_document(index: int, tenant: str, seed: int):
    category = f"category-{index % CATEGORY_COUNT}"
    return {
        "id": f"bench-{seed}-{index:08d}",
        "title": f"benchmark document {index:08d}",
        "body": (
            "benchmark sustained workload "
            f"{category} shard-token-{index % 97} sequence-{index:08d}"
        ),
        "tenant": tenant,
        "category": category,
        "rank": index,
    }


def build_search_payload(
    tenant: str,
    term: str,
    per_page: int,
    *,
    category: str = None,
    sort_by: str = "rank:asc",
):
    filters = [f"tenant:={tenant}"]
    if category:
        filters.append(f"category:={category}")
    payload = {
        "q": term,
        "query_by": "title,body,category",
        "filter_by": " && ".join(filters),
        "offset": 0,
        "limit": per_page,
    }
    if sort_by:
        payload["sort_by"] = sort_by
    return payload


def time_call(fn, *args, **kwargs):
    started_at = time.perf_counter()
    payload = fn(*args, **kwargs)
    return (time.perf_counter() - started_at) * 1000, payload


def run_parallel(total: int, concurrency: int, call):
    latencies = []
    errors = []
    payloads = []
    started_at = time.monotonic()
    workers = max(1, min(concurrency, total))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(call, index) for index in range(total)]
        for future in as_completed(futures):
            try:
                latency_ms, payload = future.result()
                latencies.append(latency_ms)
                payloads.append(payload)
            except Exception as exc:  # noqa: BLE001 - benchmark must report every request failure.
                errors.append(str(exc)[:500])
    elapsed_seconds = time.monotonic() - started_at
    return {
        "successes": len(latencies),
        "errors": errors[:10],
        "error_count": len(errors),
        "elapsed_seconds": elapsed_seconds,
        "latencies_ms": latencies,
        "payloads": payloads,
    }


def ingest_one(query_api_url: str, collection: str, headers, document):
    status, payload = request(
        "POST",
        f"{query_api_url}/ingest/{collection}",
        document,
        headers,
        timeout=30,
    )
    if status != 200:
        raise RuntimeError(f"Unexpected ingest status {status}: {payload}")
    return payload


def search_one(query_api_url: str, collection: str, headers, payload):
    status, result = request(
        "POST",
        f"{query_api_url}/search/{collection}",
        payload,
        headers,
        timeout=30,
    )
    if status != 200:
        raise RuntimeError(f"Unexpected search status {status}: {result}")
    return result


def wait_for_indexing(
    query_api_url: str,
    collection: str,
    headers,
    tenant: str,
    expected_documents: int,
    timeout_seconds: int,
    per_page: int,
):
    started_at = time.monotonic()
    deadline = started_at + timeout_seconds
    payload = build_search_payload(
        tenant,
        "benchmark",
        min(per_page, expected_documents, 250),
    )
    last_result = None
    while time.monotonic() < deadline:
        try:
            result = search_one(query_api_url, collection, headers, payload)
            last_result = result
            found = int(result.get("found", 0) or 0)
            if found >= expected_documents and result.get("partial") is False:
                return time.monotonic() - started_at, result
        except Exception as exc:  # noqa: BLE001 - retry until benchmark timeout.
            last_result = exc
        time.sleep(2)
    raise RuntimeError(
        "Indexed documents did not become searchable before timeout: "
        f"{last_result!r}"
    )


def evaluate_slos(summary, args):
    violations = []
    ingest_rate = summary["ingest"]["docs_per_second"]
    indexing_seconds = summary["indexing"]["visible_seconds"]
    search_p95 = summary["search"]["latency_ms"]["p95_ms"]
    search_error_rate = summary["search"]["error_rate"]

    if (
        args.min_ingest_docs_per_second is not None
        and ingest_rate < args.min_ingest_docs_per_second
    ):
        violations.append(
            "ingest throughput "
            f"{ingest_rate:.2f} docs/s < {args.min_ingest_docs_per_second:.2f} docs/s"
        )
    if (
        args.max_indexing_visible_seconds is not None
        and indexing_seconds > args.max_indexing_visible_seconds
    ):
        violations.append(
            "indexing visibility "
            f"{indexing_seconds:.2f}s > {args.max_indexing_visible_seconds:.2f}s"
        )
    if args.max_search_p95_ms is not None and search_p95 is not None:
        if search_p95 > args.max_search_p95_ms:
            violations.append(
                f"search p95 {search_p95:.2f}ms > {args.max_search_p95_ms:.2f}ms"
            )
    if (
        args.max_search_error_rate is not None
        and search_error_rate > args.max_search_error_rate
    ):
        violations.append(
            "search error rate "
            f"{search_error_rate:.4f} > {args.max_search_error_rate:.4f}"
        )
    if not args.allow_partial and summary["search"]["partial_responses"] > 0:
        violations.append(
            f"partial search responses observed: {summary['search']['partial_responses']}"
        )
    return violations


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--query-api-url",
        default=os.getenv("QUERY_API_URL", "http://localhost:8000"),
        help="Base URL for the Query API, Ingress, or port-forward",
    )
    parser.add_argument(
        "--collection",
        default=os.getenv("BENCHMARK_COLLECTION", ""),
        help="Collection name. Defaults to a timestamped temporary collection.",
    )
    parser.add_argument(
        "--tenant",
        default=os.getenv("BENCHMARK_TENANT", ""),
        help="Tenant marker used to isolate benchmark documents.",
    )
    parser.add_argument(
        "--documents",
        type=int,
        default=env_int("BENCHMARK_DOCUMENTS", 10000),
        help="Documents to ingest",
    )
    parser.add_argument(
        "--ingest-concurrency",
        type=int,
        default=env_int("BENCHMARK_INGEST_CONCURRENCY", 32),
        help="Concurrent ingest requests",
    )
    parser.add_argument(
        "--search-requests",
        type=int,
        default=env_int("BENCHMARK_SEARCH_REQUESTS", 500),
        help="Search requests to issue after indexing converges",
    )
    parser.add_argument(
        "--search-concurrency",
        type=int,
        default=env_int("BENCHMARK_SEARCH_CONCURRENCY", 16),
        help="Concurrent search requests",
    )
    parser.add_argument(
        "--per-page",
        type=int,
        default=env_int("BENCHMARK_SEARCH_PER_PAGE", 25),
        help="Search page size for benchmark queries",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=env_int("BENCHMARK_TIMEOUT_SECONDS", 900),
        help="Readiness and indexing convergence timeout",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=env_int("BENCHMARK_SEED", 20260620),
        help="Deterministic document ID seed",
    )
    parser.add_argument(
        "--output-json",
        default=os.getenv("BENCHMARK_OUTPUT_JSON", ""),
        help="Optional path for machine-readable benchmark summary",
    )
    parser.add_argument(
        "--output-markdown",
        default=os.getenv("BENCHMARK_OUTPUT_MARKDOWN", ""),
        help="Optional path for a human-readable benchmark report",
    )
    parser.add_argument(
        "--environment",
        default=os.getenv("BENCHMARK_ENVIRONMENT", ""),
        help="Environment name recorded in benchmark artifacts",
    )
    parser.add_argument(
        "--release",
        default=os.getenv("BENCHMARK_RELEASE", ""),
        help="Release, commit, or build identifier recorded in benchmark artifacts",
    )
    parser.add_argument(
        "--cluster-shape",
        default=os.getenv("BENCHMARK_CLUSTER_SHAPE", ""),
        help="Human-readable cluster shape recorded in benchmark artifacts",
    )
    parser.add_argument(
        "--helm-values-ref",
        default=os.getenv("BENCHMARK_HELM_VALUES_REF", ""),
        help="Reference to the Helm values or deployment config used for the run",
    )
    parser.add_argument(
        "--image-set",
        default=os.getenv("BENCHMARK_IMAGE_SET", ""),
        help="Image tags or digests used for the benchmarked deployment",
    )
    parser.add_argument(
        "--evidence-notes",
        default=os.getenv("BENCHMARK_EVIDENCE_NOTES", ""),
        help="Optional operator notes recorded in benchmark artifacts",
    )
    parser.add_argument(
        "--use-existing-collection",
        action="store_true",
        default=env_bool("BENCHMARK_USE_EXISTING_COLLECTION"),
        help="Skip collection create/delete and write into an existing schema",
    )
    parser.add_argument(
        "--keep-collection",
        action="store_true",
        default=env_bool("BENCHMARK_KEEP_COLLECTION"),
        help="Keep the temporary collection after the run",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        default=env_bool("BENCHMARK_ALLOW_PARTIAL"),
        help="Do not fail the run when search responses are partial",
    )
    parser.add_argument(
        "--min-ingest-docs-per-second",
        type=float,
        default=env_float("BENCHMARK_MIN_INGEST_DOCS_PER_SECOND"),
        help="Optional SLO: minimum ingest throughput",
    )
    parser.add_argument(
        "--max-indexing-visible-seconds",
        type=float,
        default=env_float("BENCHMARK_MAX_INDEXING_VISIBLE_SECONDS"),
        help="Optional SLO: max seconds until all docs are searchable",
    )
    parser.add_argument(
        "--max-search-p95-ms",
        type=float,
        default=env_float("BENCHMARK_MAX_SEARCH_P95_MS"),
        help="Optional SLO: max search latency p95 in milliseconds",
    )
    parser.add_argument(
        "--max-search-error-rate",
        type=float,
        default=env_float("BENCHMARK_MAX_SEARCH_ERROR_RATE"),
        help="Optional SLO: max failed search request ratio",
    )
    return parser.parse_args()


def validate_args(args) -> None:
    if args.documents < 1:
        raise SystemExit("--documents must be >= 1")
    if args.ingest_concurrency < 1:
        raise SystemExit("--ingest-concurrency must be >= 1")
    if args.search_requests < 1:
        raise SystemExit("--search-requests must be >= 1")
    if args.search_concurrency < 1:
        raise SystemExit("--search-concurrency must be >= 1")
    if not 1 <= args.per_page <= 250:
        raise SystemExit("--per-page must be between 1 and 250")


def optional_text(args, name: str):
    value = getattr(args, name, "")
    return value or None


def build_run_metadata(args):
    return {
        "environment": optional_text(args, "environment"),
        "release": optional_text(args, "release"),
        "cluster_shape": optional_text(args, "cluster_shape"),
        "helm_values_ref": optional_text(args, "helm_values_ref"),
        "image_set": optional_text(args, "image_set"),
        "evidence_notes": optional_text(args, "evidence_notes"),
        "mode": {
            "use_existing_collection": bool(getattr(args, "use_existing_collection", False)),
            "keep_collection": bool(getattr(args, "keep_collection", False)),
            "allow_partial": bool(getattr(args, "allow_partial", False)),
        },
        "slo_thresholds": {
            "min_ingest_docs_per_second": getattr(args, "min_ingest_docs_per_second", None),
            "max_indexing_visible_seconds": getattr(args, "max_indexing_visible_seconds", None),
            "max_search_p95_ms": getattr(args, "max_search_p95_ms", None),
            "max_search_error_rate": getattr(args, "max_search_error_rate", None),
        },
    }


def build_summary(
    args,
    query_api_url: str,
    collection: str,
    tenant: str,
    ingest_result,
    indexing_seconds: float,
    indexing_result,
    search_result,
):
    ingest_elapsed = max(ingest_result["elapsed_seconds"], 0.001)
    search_total = search_result["successes"] + search_result["error_count"]
    search_error_rate = search_result["error_count"] / max(search_total, 1)
    search_payloads = search_result["payloads"]
    partial_responses = sum(1 for payload in search_payloads if payload.get("partial"))
    found_values = [
        int(payload.get("found", 0) or 0)
        for payload in search_payloads
        if isinstance(payload, dict)
    ]
    return {
        "query_api_url": query_api_url,
        "collection": collection,
        "tenant": tenant,
        "documents": args.documents,
        "metadata": build_run_metadata(args),
        "ingest": {
            "concurrency": args.ingest_concurrency,
            "successes": ingest_result["successes"],
            "error_count": ingest_result["error_count"],
            "errors": ingest_result["errors"],
            "elapsed_seconds": round(ingest_result["elapsed_seconds"], 3),
            "docs_per_second": round(ingest_result["successes"] / ingest_elapsed, 2),
            "latency_ms": latency_summary(ingest_result["latencies_ms"]),
        },
        "indexing": {
            "visible_seconds": round(indexing_seconds, 3),
            "found": indexing_result.get("found"),
            "clusters_responded": indexing_result.get("clusters_responded"),
            "failed_clusters": indexing_result.get("failed_clusters", []),
            "partial": indexing_result.get("partial"),
        },
        "search": {
            "requests": args.search_requests,
            "concurrency": args.search_concurrency,
            "successes": search_result["successes"],
            "error_count": search_result["error_count"],
            "error_rate": round(search_error_rate, 6),
            "errors": search_result["errors"],
            "partial_responses": partial_responses,
            "found_min": min(found_values) if found_values else None,
            "found_max": max(found_values) if found_values else None,
            "latency_ms": latency_summary(search_result["latencies_ms"]),
        },
    }


def print_summary(summary, violations):
    print(
        "ingest:",
        f"documents={summary['documents']}",
        f"successes={summary['ingest']['successes']}",
        f"errors={summary['ingest']['error_count']}",
        f"docs_per_second={summary['ingest']['docs_per_second']}",
        f"p95_ms={summary['ingest']['latency_ms']['p95_ms']}",
    )
    print(
        "indexing:",
        f"visible_seconds={summary['indexing']['visible_seconds']}",
        f"found={summary['indexing']['found']}",
        f"partial={summary['indexing']['partial']}",
    )
    print(
        "search:",
        f"requests={summary['search']['requests']}",
        f"successes={summary['search']['successes']}",
        f"errors={summary['search']['error_count']}",
        f"error_rate={summary['search']['error_rate']}",
        f"p95_ms={summary['search']['latency_ms']['p95_ms']}",
        f"partial_responses={summary['search']['partial_responses']}",
    )
    if violations:
        print("slo-violations:")
        for violation in violations:
            print(f"- {violation}")
    else:
        print("slo: passed")


def write_json(path: str, summary) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def markdown_value(value, suffix: str = "") -> str:
    """Format benchmark values for stable Markdown output."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
    else:
        text = str(value)
    return f"{text}{suffix}"


def markdown_text(value) -> str:
    if value in (None, ""):
        return "n/a"
    return str(value).replace("\n", " ").replace("|", "\\|")


def render_markdown_report(summary) -> str:
    """Render a compact benchmark report suitable for release evidence."""
    ingest_latency = summary["ingest"]["latency_ms"]
    search_latency = summary["search"]["latency_ms"]
    violations = summary.get("slo_violations", [])
    status = str(summary.get("status", "unknown")).upper()
    metadata = summary.get("metadata", {})
    mode = metadata.get("mode", {})
    slo_thresholds = metadata.get("slo_thresholds", {})

    lines = [
        "# IMPOSBRO Benchmark Report",
        "",
        f"- Status: **{status}**",
        f"- Generated at: {summary.get('generated_at', 'not recorded')}",
        f"- Query API URL: `{summary.get('query_api_url', 'n/a')}`",
        f"- Collection: `{summary.get('collection', 'n/a')}`",
        f"- Tenant: `{summary.get('tenant', 'n/a')}`",
        f"- Documents: {markdown_value(summary.get('documents'))}",
        "",
        "## Run Metadata",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Environment | {markdown_text(metadata.get('environment'))} |",
        f"| Release | {markdown_text(metadata.get('release'))} |",
        f"| Cluster shape | {markdown_text(metadata.get('cluster_shape'))} |",
        f"| Helm values | {markdown_text(metadata.get('helm_values_ref'))} |",
        f"| Images | {markdown_text(metadata.get('image_set'))} |",
        f"| Use existing collection | {markdown_value(mode.get('use_existing_collection'))} |",
        f"| Keep collection | {markdown_value(mode.get('keep_collection'))} |",
        f"| Allow partial responses | {markdown_value(mode.get('allow_partial'))} |",
        f"| Notes | {markdown_text(metadata.get('evidence_notes'))} |",
        "",
        "## SLO Thresholds",
        "",
        "| Threshold | Value |",
        "| --- | ---: |",
        f"| Min ingest throughput | {markdown_value(slo_thresholds.get('min_ingest_docs_per_second'), ' docs/s')} |",
        f"| Max indexing visibility | {markdown_value(slo_thresholds.get('max_indexing_visible_seconds'), 's')} |",
        f"| Max search p95 | {markdown_value(slo_thresholds.get('max_search_p95_ms'), ' ms')} |",
        f"| Max search error rate | {markdown_value(slo_thresholds.get('max_search_error_rate'))} |",
        "",
        "## Ingest",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Concurrency | {markdown_value(summary['ingest']['concurrency'])} |",
        f"| Successes | {markdown_value(summary['ingest']['successes'])} |",
        f"| Errors | {markdown_value(summary['ingest']['error_count'])} |",
        f"| Elapsed | {markdown_value(summary['ingest']['elapsed_seconds'], 's')} |",
        f"| Throughput | {markdown_value(summary['ingest']['docs_per_second'], ' docs/s')} |",
        f"| Latency p50 | {markdown_value(ingest_latency['p50_ms'], ' ms')} |",
        f"| Latency p95 | {markdown_value(ingest_latency['p95_ms'], ' ms')} |",
        f"| Latency p99 | {markdown_value(ingest_latency['p99_ms'], ' ms')} |",
        "",
        "## Indexing Visibility",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Visible after | {markdown_value(summary['indexing']['visible_seconds'], 's')} |",
        f"| Found | {markdown_value(summary['indexing']['found'])} |",
        f"| Clusters responded | {markdown_value(summary['indexing']['clusters_responded'])} |",
        f"| Partial | {markdown_value(summary['indexing']['partial'])} |",
        f"| Failed clusters | {', '.join(summary['indexing']['failed_clusters']) or 'none'} |",
        "",
        "## Search",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Requests | {markdown_value(summary['search']['requests'])} |",
        f"| Concurrency | {markdown_value(summary['search']['concurrency'])} |",
        f"| Successes | {markdown_value(summary['search']['successes'])} |",
        f"| Errors | {markdown_value(summary['search']['error_count'])} |",
        f"| Error rate | {markdown_value(summary['search']['error_rate'])} |",
        f"| Partial responses | {markdown_value(summary['search']['partial_responses'])} |",
        f"| Found min | {markdown_value(summary['search']['found_min'])} |",
        f"| Found max | {markdown_value(summary['search']['found_max'])} |",
        f"| Latency p50 | {markdown_value(search_latency['p50_ms'], ' ms')} |",
        f"| Latency p95 | {markdown_value(search_latency['p95_ms'], ' ms')} |",
        f"| Latency p99 | {markdown_value(search_latency['p99_ms'], ' ms')} |",
        "",
        "## SLO Result",
        "",
    ]

    if violations:
        lines.extend([f"- {violation}" for violation in violations])
    else:
        lines.append("No SLO violations recorded.")

    errors = summary["ingest"].get("errors", []) + summary["search"].get("errors", [])
    lines.extend(["", "## Request Errors", ""])
    if errors:
        lines.extend([f"- `{error}`" for error in errors])
    else:
        lines.append("No request errors recorded.")

    lines.extend([
        "",
        "## Evidence Notes",
        "",
        "- Keep the JSON artifact beside this report for machine comparison.",
        "- Fill missing run metadata before publishing production-sized results.",
        "",
    ])
    return "\n".join(lines)


def write_markdown(path: str, summary) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_markdown_report(summary), encoding="utf-8")


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    args = parse_args()
    validate_args(args)

    query_api_url = args.query_api_url.rstrip("/")
    timestamp = int(time.time())
    collection = args.collection or f"benchmark_{timestamp}"
    tenant = args.tenant or f"tenant_{args.seed}_{timestamp}"
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

        if not args.use_existing_collection:
            status, created_payload = create_collection(
                query_api_url,
                build_schema(collection),
                admin_headers,
            )
            created = True
            print("collection:", status, collection, created_payload.get("message"))

        documents = [
            build_document(index, tenant, args.seed) for index in range(args.documents)
        ]
        ingest_result = run_parallel(
            args.documents,
            args.ingest_concurrency,
            lambda index: time_call(
                ingest_one,
                query_api_url,
                collection,
                ingest_headers,
                documents[index],
            ),
        )
        if ingest_result["error_count"]:
            raise RuntimeError(f"Ingest failures: {ingest_result['errors']}")

        indexing_seconds, indexing_result = wait_for_indexing(
            query_api_url,
            collection,
            search_headers,
            tenant,
            args.documents,
            args.timeout_seconds,
            args.per_page,
        )

        search_payloads = [
            build_search_payload(
                tenant,
                "benchmark",
                args.per_page,
                category=f"category-{index % CATEGORY_COUNT}"
                if index % 2 == 0
                else None,
            )
            for index in range(args.search_requests)
        ]
        search_result = run_parallel(
            args.search_requests,
            args.search_concurrency,
            lambda index: time_call(
                search_one,
                query_api_url,
                collection,
                search_headers,
                search_payloads[index],
            ),
        )

        summary = build_summary(
            args,
            query_api_url,
            collection,
            tenant,
            ingest_result,
            indexing_seconds,
            indexing_result,
            search_result,
        )
        violations = evaluate_slos(summary, args)
        summary["slo_violations"] = violations
        summary["status"] = "failed" if violations or search_result["error_count"] else "passed"
        summary["generated_at"] = datetime.now(timezone.utc).isoformat()

        print_summary(summary, violations)
        if args.output_json:
            write_json(args.output_json, summary)
            print("summary-json:", args.output_json)
        if args.output_markdown:
            write_markdown(args.output_markdown, summary)
            print("summary-markdown:", args.output_markdown)

        return 1 if summary["status"] == "failed" else 0
    finally:
        if created and not args.keep_collection:
            try:
                delete_collection(query_api_url, collection, admin_headers)
                print("cleanup:", collection)
            except Exception as exc:  # noqa: BLE001 - cleanup is best effort.
                print(f"cleanup-warning: {exc}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
