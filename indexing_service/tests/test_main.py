import os
import re
import sys
from pathlib import Path


_app_dir = Path(__file__).resolve().parent.parent / "app"
if str(_app_dir) not in sys.path:
    sys.path.insert(0, str(_app_dir))

import main
import consumer
import metrics
import typesense


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
    monkeypatch.delenv("INTERNAL_QUERY_API_ADMIN_API_KEY", raising=False)

    assert main.build_admin_headers() == {}


def test_build_admin_headers_prefers_internal_query_api_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "legacy-admin-secret")
    monkeypatch.setenv("INTERNAL_QUERY_API_ADMIN_API_KEY", "worker-admin-secret")

    assert main.build_admin_headers() == {"X-API-Key": "worker-admin-secret"}


def test_build_admin_headers_falls_back_to_admin_api_key(monkeypatch):
    monkeypatch.setenv("ADMIN_API_KEY", "legacy-admin-secret")
    monkeypatch.delenv("INTERNAL_QUERY_API_ADMIN_API_KEY", raising=False)

    assert main.build_admin_headers() == {"X-API-Key": "legacy-admin-secret"}


def test_create_typesense_client_defaults_legacy_http_and_supports_https(monkeypatch):
    created_configs = []
    monkeypatch.setattr(
        main.typesense,
        "Client",
        lambda config: created_configs.append(config) or object(),
    )

    main.create_typesense_client(
        {"host": "legacy-a,legacy-b", "port": 8108, "api_key": "legacy-key"}
    )
    main.create_typesense_client(
        {
            "host": "secure-a",
            "port": 443,
            "protocol": "https",
            "api_key": "secure-key",
        }
    )

    assert [node["protocol"] for node in created_configs[0]["nodes"]] == [
        "http",
        "http",
    ]
    assert created_configs[1]["nodes"] == [
        {"host": "secure-a", "port": 443, "protocol": "https"}
    ]


def test_create_typesense_client_rejects_invalid_protocol():
    try:
        main.create_typesense_client(
            {
                "host": "typesense-a",
                "port": 8108,
                "protocol": "ftp",
                "api_key": "secret",
            }
        )
    except ValueError as exc:
        assert "protocol" in str(exc).lower()
    else:
        raise AssertionError("Expected invalid Typesense protocol to be rejected")


def test_start_metrics_server_respects_disabled_env(monkeypatch):
    calls = []

    monkeypatch.setenv("INDEXING_METRICS_ENABLED", "false")
    monkeypatch.setattr(metrics, "start_http_server", lambda port: calls.append(port))

    assert metrics.start_metrics_server_from_env() is False
    assert calls == []


def test_start_metrics_server_uses_configured_port(monkeypatch):
    calls = []

    monkeypatch.setenv("INDEXING_METRICS_ENABLED", "true")
    monkeypatch.setenv("INDEXING_METRICS_PORT", "19108")
    monkeypatch.setattr(metrics, "start_http_server", lambda port: calls.append(port))

    assert metrics.start_metrics_server_from_env() is True
    assert calls == [19108]


def test_start_metrics_server_is_best_effort(monkeypatch):
    monkeypatch.setenv("INDEXING_METRICS_ENABLED", "true")
    monkeypatch.setenv("INDEXING_METRICS_PORT", "19108")

    def fail_to_start(_port):
        raise OSError("already in use")

    monkeypatch.setattr(metrics, "start_http_server", fail_to_start)

    assert metrics.start_metrics_server_from_env() is False


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
    assert kafka_consumer.pattern == "^imposbro_search_sharded_(?!dlq$).*"


def test_topic_subscription_pattern_excludes_dlq_topic():
    pattern = consumer.build_topic_subscription_pattern("imposbro_search_sharded")

    assert re.match(pattern, "imposbro_search_sharded_products")
    assert not re.match(pattern, "imposbro_search_sharded_dlq")


def test_run_consumer_quarantines_invalid_json_and_commits_offset(monkeypatch):
    sent = []
    commits = []
    closed = []
    source_topic = "imposbro_search_sharded_products"

    class FakeMessage:
        topic = source_topic
        partition = 0
        offset = 41
        value = b"{not-json"

    class FakeConsumer:
        def __init__(self):
            self.polled = False

        def poll(self, timeout_ms):
            if self.polled:
                consumer.shutdown_requested = True
                return {}
            self.polled = True
            return {consumer.TopicPartition(source_topic, 0): [FakeMessage()]}

        def commit(self, offsets):
            commits.append(offsets)
            consumer.shutdown_requested = True

        def close(self):
            closed.append("consumer")

    class FakeDlqProducer:
        def send(self, topic, value):
            sent.append({"topic": topic, "value": value})

        def flush(self):
            sent.append({"flushed": True})

        def close(self):
            closed.append("producer")

    monkeypatch.setenv("KAFKA_BROKER_URL", "kafka:29092")
    monkeypatch.setenv("KAFKA_TOPIC_PREFIX", "imposbro_search_sharded")
    monkeypatch.setattr(consumer, "shutdown_requested", False)
    monkeypatch.setattr(
        consumer,
        "create_kafka_consumer",
        lambda kafka_broker_url, topic_prefix: FakeConsumer(),
    )
    monkeypatch.setattr(
        consumer,
        "create_dlq_producer",
        lambda kafka_broker_url: FakeDlqProducer(),
    )

    consumer.run_consumer({})

    assert sent[0]["topic"] == "imposbro_search_sharded_dlq"
    assert sent[0]["value"]["source_topic"] == source_topic
    assert sent[0]["value"]["error"] == "InvalidKafkaMessageError"
    assert sent[0]["value"]["message"] == "{not-json"
    assert sent[1] == {"flushed": True}

    committed_offsets = commits[0]
    topic_partition = consumer.TopicPartition(source_topic, 0)
    assert list(committed_offsets) == [topic_partition]
    assert committed_offsets[topic_partition].offset == 42
    assert closed == ["consumer", "producer"]


class FakeDocumentOperations:
    def __init__(self):
        self.upserted = []
        self.deleted_ids = []
        self.delete_filters = []
        self.delete_result = {"num_deleted": 1}
        self.raise_not_found = False

    def upsert(self, document):
        self.upserted.append(document)

    def __getitem__(self, document_id):
        return FakeDocumentReference(self, document_id)

    def delete(self, params):
        self.delete_filters.append(params)
        if self.raise_not_found:
            raise typesense.exceptions.ObjectNotFound("not found")
        return self.delete_result


class FakeDocumentReference:
    def __init__(self, documents, document_id):
        self.documents = documents
        self.document_id = document_id

    def delete(self):
        if self.documents.raise_not_found:
            raise typesense.exceptions.ObjectNotFound("not found")
        self.documents.deleted_ids.append(self.document_id)


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


def sample_value(name, labels):
    return metrics.REGISTRY.get_sample_value(name, labels) or 0


def test_process_message_upserts_to_target_cluster():
    client = FakeTypesenseClient()
    labels = {"collection": "products", "target_cluster": "cluster-a"}
    before = sample_value("indexing_documents_indexed_total", labels)

    consumer.process_message(
        {
            "collection": "products",
            "target_cluster": "cluster-a",
            "document": {"id": "doc-1", "name": "Product"},
        },
        {"cluster-a": client},
    )

    assert client.documents.upserted == [{"id": "doc-1", "name": "Product"}]
    assert sample_value("indexing_documents_indexed_total", labels) == before + 1


def test_process_message_accepts_request_id_metadata():
    client = FakeTypesenseClient()

    consumer.process_message(
        {
            "collection": "products",
            "target_cluster": "cluster-a",
            "request_id": "trace-123",
            "document": {"id": "doc-1", "name": "Product"},
        },
        {"cluster-a": client},
    )

    assert client.documents.upserted == [{"id": "doc-1", "name": "Product"}]


def test_process_message_without_target_cluster_uses_default_data_cluster():
    client = FakeTypesenseClient()

    consumer.process_message(
        {
            "collection": "products",
            "document": {"id": "doc-1", "name": "Product"},
        },
        {"default-data-cluster": client},
    )

    assert client.documents.upserted == [{"id": "doc-1", "name": "Product"}]


def test_process_message_deletes_document_by_id():
    client = FakeTypesenseClient()
    labels = {
        "collection": "products",
        "target_cluster": "cluster-a",
        "result": "deleted",
    }
    before = sample_value("indexing_documents_deleted_total", labels)

    consumer.process_message(
        {
            "action": "delete",
            "collection": "products",
            "target_cluster": "cluster-a",
            "document_id": "doc-1",
        },
        {"cluster-a": client},
    )

    assert client.documents.deleted_ids == ["doc-1"]
    assert sample_value("indexing_documents_deleted_total", labels) == before + 1


def test_process_message_deletes_by_filter_for_tenant_safe_event():
    client = FakeTypesenseClient()

    consumer.process_message(
        {
            "action": "delete",
            "collection": "products",
            "target_cluster": "cluster-a",
            "document_id": "doc-1",
            "filter_by": "(id:=doc-1) && tenant_id:=tenant-a",
        },
        {"cluster-a": client},
    )

    assert client.documents.deleted_ids == []
    assert client.documents.delete_filters == [
        {"filter_by": "(id:=doc-1) && tenant_id:=tenant-a"}
    ]


def test_process_message_treats_missing_delete_as_idempotent_noop():
    client = FakeTypesenseClient()
    client.documents.raise_not_found = True
    labels = {
        "collection": "products",
        "target_cluster": "cluster-a",
        "result": "not_found",
    }
    before = sample_value("indexing_documents_deleted_total", labels)

    consumer.process_message(
        {
            "action": "delete",
            "collection": "products",
            "target_cluster": "cluster-a",
            "document_id": "doc-1",
        },
        {"cluster-a": client},
    )

    assert client.documents.deleted_ids == []
    assert sample_value("indexing_documents_deleted_total", labels) == before + 1


def test_process_message_rejects_unknown_action():
    try:
        consumer.process_message(
            {
                "action": "archive",
                "collection": "products",
                "target_cluster": "cluster-a",
                "document": {"id": "doc-1", "name": "Product"},
            },
            {"cluster-a": FakeTypesenseClient()},
        )
    except ValueError as exc:
        assert "Unsupported indexing action 'archive'" in str(exc)
    else:
        raise AssertionError("Expected unknown action to fail processing")


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


def test_process_message_with_retries_refreshes_missing_cluster():
    client = FakeTypesenseClient()
    clients = {}
    refresh_calls = []
    labels = {
        "collection": "products",
        "target_cluster": "cluster-a",
        "error": "MissingTargetClusterError",
    }
    before = sample_value("indexing_processing_retries_total", labels)

    def refresh_clients():
        refresh_calls.append(True)
        return {"cluster-a": client}

    consumer.process_message_with_retries(
        {
            "collection": "products",
            "target_cluster": "cluster-a",
            "document": {"id": "doc-1", "name": "Product"},
        },
        clients,
        refresh_clients=refresh_clients,
        dlq_producer=None,
        source_topic="imposbro_search_sharded_products",
        topic_prefix="imposbro_search_sharded",
        max_attempts=2,
    )

    assert refresh_calls == [True]
    assert clients == {"cluster-a": client}
    assert client.documents.upserted == [{"id": "doc-1", "name": "Product"}]
    assert sample_value("indexing_processing_retries_total", labels) == before + 1


def test_process_message_with_retries_quarantines_poison_message_to_dlq(monkeypatch):
    sent = []
    retry_labels = {
        "collection": "products",
        "target_cluster": "missing",
        "error": "MissingTargetClusterError",
    }
    dlq_labels = {
        "source_topic": "imposbro_search_sharded_products",
        "error": "MissingTargetClusterError",
    }
    retry_before = sample_value("indexing_processing_retries_total", retry_labels)
    dlq_before = sample_value("indexing_dlq_messages_total", dlq_labels)

    class FakeDlqProducer:
        def send(self, topic, value):
            sent.append({"topic": topic, "value": value})

        def flush(self):
            sent.append({"flushed": True})

    monkeypatch.setattr(consumer.time, "sleep", lambda _seconds: None)

    consumer.process_message_with_retries(
        {
            "collection": "products",
            "target_cluster": "missing",
            "document": {"id": "doc-1", "name": "Product"},
        },
        {},
        refresh_clients=None,
        dlq_producer=FakeDlqProducer(),
        source_topic="imposbro_search_sharded_products",
        topic_prefix="imposbro_search_sharded",
        max_attempts=2,
    )

    assert sent[0]["topic"] == "imposbro_search_sharded_dlq"
    assert sent[0]["value"]["source_topic"] == "imposbro_search_sharded_products"
    assert sent[0]["value"]["error"] == "MissingTargetClusterError"
    assert sent[1] == {"flushed": True}
    assert sample_value("indexing_processing_retries_total", retry_labels) == retry_before + 1
    assert sample_value("indexing_dlq_messages_total", dlq_labels) == dlq_before + 1
