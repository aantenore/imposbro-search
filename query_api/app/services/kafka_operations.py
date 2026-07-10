"""Bounded Kafka consumer-lag probes used by routing safety gates."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping

from kafka import KafkaAdminClient, KafkaConsumer
from kafka.structs import TopicPartition


class KafkaOperationalProbeError(RuntimeError):
    """Raised when Kafka cannot provide authoritative cutover evidence."""


@dataclass(frozen=True)
class KafkaOperationalEvidence:
    consumer_lag: int
    unresolved_dlq: int
    topic_partitions: int
    dlq_partitions: int


def calculate_lag(
    end_offsets: Mapping[TopicPartition, int],
    committed_offsets: Mapping[TopicPartition, Any],
) -> int:
    """Return non-negative aggregate lag, treating missing commits as offset zero."""
    total = 0
    for partition, end_offset in end_offsets.items():
        metadata = committed_offsets.get(partition)
        committed = getattr(metadata, "offset", metadata)
        if committed is None or int(committed) < 0:
            committed = 0
        total += max(0, int(end_offset) - int(committed))
    return total


class KafkaOperationalProbe:
    def __init__(
        self,
        *,
        broker_url: str,
        connection_options: Dict[str, Any],
        consumer_group_id: str,
        dlq_group_id: str,
        timeout_ms: int,
    ):
        self.broker_url = broker_url
        self.connection_options = dict(connection_options)
        self.consumer_group_id = consumer_group_id
        self.dlq_group_id = dlq_group_id
        self.timeout_ms = timeout_ms

    def _topic_lag(self, consumer, admin, topic: str, group_id: str) -> tuple[int, int]:
        partitions = consumer.partitions_for_topic(topic)
        if not partitions:
            return 0, 0
        topic_partitions = [
            TopicPartition(topic, int(partition)) for partition in sorted(partitions)
        ]
        end_offsets = consumer.end_offsets(topic_partitions)
        committed = admin.list_consumer_group_offsets(
            group_id,
            partitions=topic_partitions,
        )
        return calculate_lag(end_offsets, committed), len(topic_partitions)

    def measure(
        self,
        *,
        topic: str,
        dlq_topic: str,
    ) -> KafkaOperationalEvidence:
        admin = None
        consumer = None
        try:
            common = {
                **self.connection_options,
                "bootstrap_servers": self.broker_url,
                "request_timeout_ms": self.timeout_ms,
                "api_version_auto_timeout_ms": self.timeout_ms,
            }
            base_client_id = common.get("client_id", "imposbro")
            admin = KafkaAdminClient(
                **{**common, "client_id": f"{base_client_id}-lag-admin"}
            )
            consumer = KafkaConsumer(
                **{
                    **common,
                    "client_id": f"{base_client_id}-lag-reader",
                    "enable_auto_commit": False,
                    "group_id": None,
                    "consumer_timeout_ms": self.timeout_ms,
                }
            )
            consumer_lag, topic_partitions = self._topic_lag(
                consumer,
                admin,
                topic,
                self.consumer_group_id,
            )
            unresolved_dlq, dlq_partitions = self._topic_lag(
                consumer,
                admin,
                dlq_topic,
                self.dlq_group_id,
            )
            return KafkaOperationalEvidence(
                consumer_lag=consumer_lag,
                unresolved_dlq=unresolved_dlq,
                topic_partitions=topic_partitions,
                dlq_partitions=dlq_partitions,
            )
        except Exception as exc:
            raise KafkaOperationalProbeError(
                "Kafka lag/DLQ evidence is unavailable"
            ) from exc
        finally:
            if consumer is not None:
                consumer.close()
            if admin is not None:
                admin.close()
