import os
import sys
from pathlib import Path


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

import main
import consumer


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_fetch_cluster_configuration_uses_internal_endpoint_and_admin_key(monkeypatch):
    calls = []

    monkeypatch.setenv("ADMIN_API_KEY", "service-secret")
    monkeypatch.setattr(
        main,
        "create_typesense_client",
        lambda cluster_info: {"api_key": cluster_info["api_key"]},
    )

    def fake_get(url, headers, timeout):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return FakeResponse(
            {
                "cluster-a": {
                    "host": "typesense-a",
                    "port": 8108,
                    "api_key": "raw-key",
                },
                "default": {
                    "host": "Internal HA Cluster",
                    "port": 8108,
                    "api_key": "N/A",
                },
            }
        )

    monkeypatch.setattr(main.requests, "get", fake_get)

    clients = main.fetch_cluster_configuration("http://query-api:8000")

    assert calls == [
        {
            "url": "http://query-api:8000/admin/federation/clusters/internal",
            "headers": {"X-API-Key": "service-secret"},
            "timeout": 10,
        }
    ]
    assert clients == {"cluster-a": {"api_key": "raw-key"}}


def test_build_admin_headers_omits_empty_admin_key(monkeypatch):
    monkeypatch.delenv("ADMIN_API_KEY", raising=False)

    assert main.build_admin_headers() == {}


def test_create_kafka_consumer_refreshes_metadata_for_dynamic_topics(monkeypatch):
    created_kwargs = {}

    class FakeKafkaConsumer:
        def __init__(self, **kwargs):
            created_kwargs.update(kwargs)
            self.pattern = None

        def subscribe(self, pattern):
            self.pattern = pattern

    monkeypatch.setenv("KAFKA_METADATA_MAX_AGE_MS", "7000")
    monkeypatch.setattr(consumer, "KafkaConsumer", FakeKafkaConsumer)

    kafka_consumer = consumer.create_kafka_consumer(
        "kafka:29092", "imposbro_search_sharded"
    )

    assert created_kwargs["bootstrap_servers"] == "kafka:29092"
    assert created_kwargs["metadata_max_age_ms"] == 7000
    assert created_kwargs["enable_auto_commit"] is False
    assert kafka_consumer.pattern == "^imposbro_search_sharded_.*"


class FakeDocumentOperations:
    def __init__(self):
        self.upserted = []

    def upsert(self, document):
        self.upserted.append(document)


class FakeCollection:
    def __init__(self, documents):
        self.documents = documents


class FakeCollections:
    def __init__(self, documents):
        self.documents = documents

    def __getitem__(self, collection_name):
        return FakeCollection(self.documents)


class FakeTypesenseClient:
    def __init__(self):
        self.documents = FakeDocumentOperations()
        self.collections = FakeCollections(self.documents)


def test_process_message_upserts_to_target_cluster():
    client = FakeTypesenseClient()

    consumer.process_message(
        {
            "collection": "products",
            "target_cluster": "cluster-a",
            "document": {"id": "doc-1", "name": "Product"},
        },
        {"cluster-a": client},
    )

    assert client.documents.upserted == [{"id": "doc-1", "name": "Product"}]


def test_process_message_raises_when_target_cluster_is_missing():
    try:
        consumer.process_message(
            {
                "collection": "products",
                "target_cluster": "missing",
                "document": {"id": "doc-1", "name": "Product"},
            },
            {},
        )
    except RuntimeError as exc:
        assert "No client found for cluster 'missing'" in str(exc)
    else:
        raise AssertionError("Expected missing target cluster to fail processing")
