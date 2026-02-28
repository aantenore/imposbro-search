"""
Integration tests: require live Kafka, Redis, and Typesense (e.g. docker-compose up).

Run unit tests only (default):
  pytest tests -v -m "not integration"

Run integration tests (with stack running):
  pytest tests -v -m integration

Run all tests including integration:
  pytest tests -v
"""
import os
import pytest

# Skip entire module if INTEGRATION=1 is not set (avoid accidental runs in CI)
pytestmark = pytest.mark.integration

_INTEGRATION = os.environ.get("INTEGRATION", "").lower() in ("1", "true", "yes")


@pytest.mark.skipif(not _INTEGRATION, reason="Set INTEGRATION=1 to run integration tests")
def test_health_with_real_services():
    """Hit /health with real backend; expects redis/kafka/state available."""
    import requests
    base = os.environ.get("QUERY_API_URL", "http://localhost:8000")
    r = requests.get(f"{base}/health", timeout=5)
    assert r.status_code == 200
    data = r.json()
    assert "status" in data
    assert "service" in data
