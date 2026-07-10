"""Kafka operational gate calculation tests."""

from kafka.structs import OffsetAndMetadata, TopicPartition

from services.kafka_operations import calculate_lag


def test_lag_uses_committed_offsets_and_treats_missing_commit_as_zero():
    first = TopicPartition("events", 0)
    second = TopicPartition("events", 1)

    lag = calculate_lag(
        {first: 10, second: 7},
        {first: OffsetAndMetadata(6, None)},
    )

    assert lag == 11


def test_lag_never_becomes_negative_after_log_truncation_or_race():
    partition = TopicPartition("events", 0)

    assert calculate_lag(
        {partition: 5},
        {partition: OffsetAndMetadata(8, None)},
    ) == 0
