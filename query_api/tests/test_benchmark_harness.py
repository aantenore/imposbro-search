"""Tests for the Kubernetes benchmark harness pure helpers."""

import argparse
import importlib.util
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

SPEC = importlib.util.spec_from_file_location(
    "benchmark_k8s",
    SCRIPTS_DIR / "benchmark-k8s.py",
)
benchmark = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(benchmark)


def test_latency_summary_uses_nearest_rank_percentiles():
    summary = benchmark.latency_summary([100, 10, 50, 200])

    assert summary["count"] == 4
    assert summary["min_ms"] == 10
    assert summary["avg_ms"] == 90
    assert summary["p50_ms"] == 50
    assert summary["p95_ms"] == 200
    assert summary["p99_ms"] == 200
    assert summary["max_ms"] == 200


def test_document_and_search_payload_are_tenant_scoped():
    document = benchmark.build_document(12, "tenant_a", 42)
    payload = benchmark.build_search_payload(
        "tenant_a",
        "benchmark",
        25,
        category="category-2",
    )

    assert document["id"] == "bench-42-00000012"
    assert document["tenant"] == "tenant_a"
    assert document["category"] == "category-2"
    assert payload["filter_by"] == "tenant:=tenant_a && category:=category-2"
    assert payload["query_by"] == "title,body,category"
    assert payload["limit"] == 25


def test_evaluate_slos_reports_threshold_violations():
    args = argparse.Namespace(
        min_ingest_docs_per_second=200.0,
        max_indexing_visible_seconds=30.0,
        max_search_p95_ms=500.0,
        max_search_error_rate=0.01,
        allow_partial=False,
    )
    summary = {
        "ingest": {"docs_per_second": 100.0},
        "indexing": {"visible_seconds": 42.0},
        "search": {
            "latency_ms": {"p95_ms": 900.0},
            "error_rate": 0.02,
            "partial_responses": 1,
        },
    }

    violations = benchmark.evaluate_slos(summary, args)

    assert len(violations) == 5
    assert any("ingest throughput" in item for item in violations)
    assert any("indexing visibility" in item for item in violations)
    assert any("search p95" in item for item in violations)
    assert any("search error rate" in item for item in violations)
    assert any("partial search responses" in item for item in violations)


def test_evaluate_slos_allows_configured_pass():
    args = argparse.Namespace(
        min_ingest_docs_per_second=50.0,
        max_indexing_visible_seconds=60.0,
        max_search_p95_ms=250.0,
        max_search_error_rate=0.01,
        allow_partial=True,
    )
    summary = {
        "ingest": {"docs_per_second": 100.0},
        "indexing": {"visible_seconds": 20.0},
        "search": {
            "latency_ms": {"p95_ms": 120.0},
            "error_rate": 0.0,
            "partial_responses": 1,
        },
    }

    assert benchmark.evaluate_slos(summary, args) == []


def test_run_parallel_collects_successes_and_errors():
    def call(index):
        if index == 2:
            raise RuntimeError("boom")
        return float(index), {"index": index}

    result = benchmark.run_parallel(4, 2, call)

    assert result["successes"] == 3
    assert result["error_count"] == 1
    assert result["errors"] == ["boom"]
    assert sorted(payload["index"] for payload in result["payloads"]) == [0, 1, 3]


def sample_summary():
    return {
        "status": "passed",
        "generated_at": "2026-06-20T00:00:00+00:00",
        "query_api_url": "https://api.example.com",
        "collection": "benchmark_20260620",
        "tenant": "tenant_a",
        "documents": 1000,
        "ingest": {
            "concurrency": 16,
            "successes": 1000,
            "error_count": 0,
            "errors": [],
            "elapsed_seconds": 10.5,
            "docs_per_second": 95.24,
            "latency_ms": {
                "p50_ms": 12.3,
                "p95_ms": 45.6,
                "p99_ms": 78.9,
            },
        },
        "indexing": {
            "visible_seconds": 24.2,
            "found": 1000,
            "clusters_responded": 2,
            "failed_clusters": [],
            "partial": False,
        },
        "search": {
            "requests": 200,
            "concurrency": 8,
            "successes": 200,
            "error_count": 0,
            "error_rate": 0.0,
            "errors": [],
            "partial_responses": 0,
            "found_min": 100,
            "found_max": 1000,
            "latency_ms": {
                "p50_ms": 18.1,
                "p95_ms": 52.4,
                "p99_ms": 90.0,
            },
        },
        "slo_violations": [],
    }


def test_render_markdown_report_summarizes_release_evidence():
    report = benchmark.render_markdown_report(sample_summary())

    assert "# IMPOSBRO Benchmark Report" in report
    assert "- Status: **PASSED**" in report
    assert "| Throughput | 95.24 docs/s |" in report
    assert "| Visible after | 24.2s |" in report
    assert "| Latency p95 | 52.4 ms |" in report
    assert "No SLO violations recorded." in report
    assert "Keep the JSON artifact beside this report" in report


def test_write_markdown_creates_parent_directories(tmp_path):
    output_path = tmp_path / "nested" / "benchmark.md"

    benchmark.write_markdown(str(output_path), sample_summary())

    assert output_path.exists()
    assert "benchmark_20260620" in output_path.read_text(encoding="utf-8")
