"""
Pytest fixtures and configuration.
Uses TESTING=1 so the app uses mocked dependencies (no Kafka, Redis, Typesense).
"""
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# App is designed to run from query_api/app/ (see Dockerfile). Add app dir to path.
_query_api_app = Path(__file__).resolve().parent.parent / "app"
if str(_query_api_app) not in sys.path:
    sys.path.insert(0, str(_query_api_app))

# Enable test lifespan (mocked services) and set minimal env so Settings loads
os.environ.setdefault("TESTING", "1")
os.environ.setdefault("KAFKA_BROKER_URL", "localhost:9092")
os.environ.setdefault("KAFKA_TOPIC_PREFIX", "test")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("INTERNAL_STATE_NODES", "localhost")
os.environ.setdefault("INTERNAL_STATE_API_KEY", "test-key")
os.environ.setdefault("DEFAULT_DATA_CLUSTER_NODES", "localhost")
os.environ.setdefault("DEFAULT_DATA_CLUSTER_API_KEY", "test-key")
os.environ.setdefault("INTERNAL_QUERY_API_URL", "http://localhost:8000")

from main import app


@pytest.fixture
def client():
    # Context manager ensures lifespan runs so app.state is populated
    with TestClient(app) as c:
        yield c
