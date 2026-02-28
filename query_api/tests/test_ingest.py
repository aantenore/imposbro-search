"""Tests for ingest endpoint validation and error handling."""
import os
import pytest
from unittest.mock import MagicMock

# Ensure test mode so we get mock federation
os.environ["TESTING"] = "1"


def test_ingest_requires_id(client):
    """POST /ingest/{collection} without 'id' in body returns 400."""
    r = client.post("/ingest/products", json={"name": "No ID"})
    assert r.status_code == 400
    assert "id" in r.json().get("detail", "").lower()


def test_ingest_accepts_valid_document(client):
    """POST /ingest/{collection} with valid document returns 200 and routed_to."""
    r = client.post(
        "/ingest/products",
        json={"id": "doc-1", "name": "Product One", "region": "EU"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "ok"
    assert data.get("document_id") == "doc-1"
    assert "routed_to" in data
