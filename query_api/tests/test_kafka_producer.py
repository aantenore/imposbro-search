"""Tests for Kafka publish payload shape and durable replay."""

import pytest

from indexing_events import InMemoryIndexingEventStore
from services.kafka_producer import KafkaService


class FakeProducer:
    def __init__(self):
        self.sent = []
        self.flushed = False

    def send(self, topic, key, value):
        self.sent.append({"topic": topic, "key": key, "value": value})

    def flush(self):
        self.flushed = True


class FailingProducer(FakeProducer):
    def __init__(self, failures=1):
        super().__init__()
        self.failures = failures

    def send(self, topic, key, value):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("broker unavailable")
        super().send(topic, key, value)


def test_publish_document_includes_request_id_when_present():
    producer = FakeProducer()
    service = KafkaService("localhost:9092", "imposbro")
    service._producer = producer

    service.publish_document(
        collection_name="products",
        document={"id": "doc-1", "name": "Product"},
        target_clusters=["cluster-a", "cluster-b"],
        tenant_id="tenant-a",
        document_version=7,
        sequence=9,
        routing_revision=3,
        rollout_id="rollout-1",
        idempotency_key="retry-key-123",
        request_id="trace-123",
        traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    )

    assert len(producer.sent) == 1
    sent = producer.sent[0]
    assert sent["topic"] == "imposbro_products"
    assert sent["key"] == b"tenant-a\x1fproducts\x1fdoc-1"
    assert sent["value"]["envelope_version"] == 2
    assert sent["value"]["identity"] == {
        "tenant_id": "tenant-a",
        "collection": "products",
        "document_id": "doc-1",
    }
    assert sent["value"]["target_clusters"] == ["cluster-a", "cluster-b"]
    assert sent["value"]["document_version"] == 7
    assert sent["value"]["sequence"] == 9
    assert sent["value"]["routing_revision"] == 3
    assert sent["value"]["rollout_id"] == "rollout-1"
    assert sent["value"]["trace"] == {
        "request_id": "trace-123",
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    }
    assert sent["value"]["document"] == {"id": "doc-1", "name": "Product"}
    assert sent["value"]["event_id"].startswith("evt:")
    assert producer.flushed is True


def test_publish_document_event_id_is_stable_for_idempotent_retry():
    producer = FakeProducer()
    service = KafkaService("localhost:9092", "imposbro")
    service._producer = producer

    kwargs = {
        "collection_name": "products",
        "document": {"id": "doc-1", "name": "Product"},
        "target_clusters": ["cluster-a"],
        "tenant_id": "tenant-a",
        "document_version": 1,
        "sequence": 1,
        "routing_revision": 1,
        "idempotency_key": "retry-key-123",
    }
    service.publish_document(**kwargs, request_id="request-a")
    service.publish_document(**kwargs, request_id="request-b")

    assert producer.sent[0]["value"]["event_id"] == producer.sent[1]["value"]["event_id"]
    assert producer.sent[0]["value"]["trace"] != producer.sent[1]["value"]["trace"]


def test_durable_retry_returns_original_event_without_republishing():
    producer = FakeProducer()
    store = InMemoryIndexingEventStore()
    service = KafkaService(
        "localhost:9092",
        "imposbro",
        event_store=store,
    )
    service._producer = producer
    kwargs = {
        "collection_name": "products",
        "document": {"id": "doc-1", "name": "Product"},
        "target_clusters": ["cluster-a"],
        "tenant_id": "tenant-a",
        "document_version": 999,
        "sequence": 999,
        "routing_revision": 3,
        "idempotency_key": "durable-retry",
    }

    first_id = service.publish_document(**kwargs, request_id="request-a")
    second_id = service.publish_document(
        **{**kwargs, "target_clusters": ["cluster-b"], "routing_revision": 4},
        request_id="request-b",
    )

    assert second_id == first_id
    assert len(producer.sent) == 1
    assert producer.sent[0]["value"]["document_version"] == 1
    assert producer.sent[0]["value"]["sequence"] == 1
    assert producer.sent[0]["value"]["routing_revision"] == 3
    assert producer.sent[0]["value"]["target_clusters"] == ["cluster-a"]
    assert store.pending_count() == 0


def test_durable_publish_failure_is_replayed_from_outbox():
    producer = FailingProducer()
    store = InMemoryIndexingEventStore()
    service = KafkaService(
        "localhost:9092",
        "imposbro",
        event_store=store,
    )
    service._producer = producer

    with pytest.raises(RuntimeError, match="broker unavailable"):
        service.publish_document(
            collection_name="products",
            document={"id": "doc-1", "name": "Product"},
            target_clusters=["cluster-a"],
            tenant_id="tenant-a",
            document_version=1,
            sequence=1,
            routing_revision=1,
            idempotency_key="durable-failure",
        )

    assert store.pending_count() == 1
    pending = store.list_pending()[0]
    assert pending.publish_attempts == 1
    assert pending.last_error == "broker unavailable"
    assert service.publish_pending() == 1
    assert store.pending_count() == 0
    assert producer.sent[0]["value"]["event_id"] == pending.event_id


def test_publish_delete_document_includes_action_and_request_id():
    producer = FakeProducer()
    service = KafkaService("localhost:9092", "imposbro")
    service._producer = producer

    service.publish_delete_document(
        collection_name="products",
        document_id="doc-1",
        target_clusters=["cluster-a", "cluster-b"],
        tenant_id="tenant-a",
        document_version=8,
        sequence=10,
        routing_revision=4,
        idempotency_key="delete-key-123",
        request_id="trace-123",
        traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    )

    sent = producer.sent[0]
    assert sent["key"] == b"tenant-a\x1fproducts\x1fdoc-1"
    assert sent["value"]["operation"] == "delete"
    assert sent["value"]["target_clusters"] == ["cluster-a", "cluster-b"]
    assert sent["value"]["trace"] == {
        "request_id": "trace-123",
        "traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    }
    assert producer.flushed is True


def test_publish_delete_document_can_carry_tenant_filter():
    producer = FakeProducer()
    service = KafkaService("localhost:9092", "imposbro")
    service._producer = producer

    service.publish_delete_document(
        collection_name="products",
        document_id="doc-1",
        target_clusters=["cluster-a"],
        tenant_id="tenant-a",
        document_version=2,
        sequence=2,
        routing_revision=1,
        idempotency_key="delete-key-123",
        filter_by="(id:=doc-1) && tenant_id:=tenant-a",
    )

    assert producer.sent[0]["value"]["delete_filter"] == (
        "(id:=doc-1) && tenant_id:=tenant-a"
    )
    assert producer.sent[0]["value"]["trace"] == {
        "request_id": "",
        "traceparent": "",
    }


def test_kafka_security_options_are_fail_closed_and_include_tls_sasl():
    service = KafkaService(
        "kafka:9093",
        "imposbro",
        security_protocol="SASL_SSL",
        sasl_mechanism="SCRAM-SHA-512",
        sasl_username="query",
        sasl_password="secret",
        ssl_cafile="/tls/ca.crt",
        ssl_check_hostname=True,
    )

    assert service._connection_options() == {
        "security_protocol": "SASL_SSL",
        "client_id": "imposbro-query-api",
        "sasl_mechanism": "SCRAM-SHA-512",
        "sasl_plain_username": "query",
        "sasl_plain_password": "secret",
        "ssl_check_hostname": True,
        "ssl_cafile": "/tls/ca.crt",
    }

    invalid = KafkaService(
        "kafka:9093",
        "imposbro",
        security_protocol="SASL_SSL",
    )
    try:
        invalid._connection_options()
    except ValueError as exc:
        assert "username and password" in str(exc)
    else:
        raise AssertionError("missing Kafka SASL credentials must fail closed")
