"""Tests for Kafka publish payload shape."""

from services.kafka_producer import KafkaService


class FakeProducer:
    def __init__(self):
        self.sent = []
        self.flushed = False

    def send(self, topic, key, value):
        self.sent.append({"topic": topic, "key": key, "value": value})

    def flush(self):
        self.flushed = True


def test_publish_document_includes_request_id_when_present():
    producer = FakeProducer()
    service = KafkaService("localhost:9092", "imposbro")
    service._producer = producer

    service.publish_document(
        collection_name="products",
        document={"id": "doc-1", "name": "Product"},
        target_cluster="cluster-a",
        request_id="trace-123",
    )

    assert producer.sent == [
        {
            "topic": "imposbro_products",
            "key": b"doc-1",
            "value": {
                "target_cluster": "cluster-a",
                "collection": "products",
                "document": {"id": "doc-1", "name": "Product"},
                "request_id": "trace-123",
            },
        }
    ]
    assert producer.flushed is True


def test_publish_document_omits_empty_request_id_for_backward_compatibility():
    producer = FakeProducer()
    service = KafkaService("localhost:9092", "imposbro")
    service._producer = producer

    service.publish_document(
        collection_name="products",
        document={"id": "doc-1", "name": "Product"},
        target_cluster="cluster-a",
    )

    assert "request_id" not in producer.sent[0]["value"]


def test_publish_delete_document_includes_action_and_request_id():
    producer = FakeProducer()
    service = KafkaService("localhost:9092", "imposbro")
    service._producer = producer

    service.publish_delete_document(
        collection_name="products",
        document_id="doc-1",
        target_cluster="cluster-a",
        request_id="trace-123",
    )

    assert producer.sent == [
        {
            "topic": "imposbro_products",
            "key": b"doc-1",
            "value": {
                "action": "delete",
                "target_cluster": "cluster-a",
                "collection": "products",
                "document_id": "doc-1",
                "request_id": "trace-123",
            },
        }
    ]
    assert producer.flushed is True


def test_publish_delete_document_can_carry_tenant_filter():
    producer = FakeProducer()
    service = KafkaService("localhost:9092", "imposbro")
    service._producer = producer

    service.publish_delete_document(
        collection_name="products",
        document_id="doc-1",
        target_cluster="cluster-a",
        filter_by="(id:=doc-1) && tenant_id:=tenant-a",
    )

    assert producer.sent[0]["value"]["filter_by"] == (
        "(id:=doc-1) && tenant_id:=tenant-a"
    )
    assert "request_id" not in producer.sent[0]["value"]
