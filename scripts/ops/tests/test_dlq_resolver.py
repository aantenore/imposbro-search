#!/usr/bin/env python3
"""Unit tests for the single-record DLQ resolver using fake Kafka clients."""

from __future__ import annotations

import base64
import copy
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional


OPS_DIR = Path(__file__).resolve().parents[1]
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import dlq_resolver as resolver_module


@dataclass(frozen=True)
class FakeTopicPartition:
    topic: str
    partition: int


@dataclass(frozen=True)
class FakeOffsetAndMetadata:
    offset: int
    metadata: Optional[str]


@dataclass
class FakeRecord:
    topic: str
    partition: int
    offset: int
    value: bytes
    key: Optional[bytes] = None


class FakeFuture:
    def __init__(self, log: list[str], *, error: Optional[Exception] = None):
        self.log = log
        self.error = error

    def get(self, timeout: float):
        self.log.append("get")
        if self.error is not None:
            raise self.error
        return SimpleNamespace(topic="imposbro_search_sharded_products", partition=2, offset=88)


class FakeProducer:
    def __init__(
        self,
        log: list[str],
        *,
        future_error: Optional[Exception] = None,
        flush_error: Optional[Exception] = None,
        on_flush=None,
    ) -> None:
        self.log = log
        self.future_error = future_error
        self.flush_error = flush_error
        self.on_flush = on_flush
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    def send(self, topic: str, **kwargs: Any) -> FakeFuture:
        self.log.append("send")
        self.sent.append({"topic": topic, **kwargs})
        return FakeFuture(self.log, error=self.future_error)

    def flush(self, timeout: float) -> None:
        self.log.append("flush")
        if self.on_flush is not None:
            self.on_flush()
        if self.flush_error is not None:
            raise self.flush_error

    def close(self) -> None:
        self.closed = True
        self.log.append("producer-close")


class FakeConsumer:
    def __init__(
        self,
        log: list[str],
        record: FakeRecord,
        *,
        beginning: int = 0,
        end: int = 10,
        committed: Optional[int] = 5,
    ) -> None:
        self.log = log
        self.record = record
        self.beginning = beginning
        self.end = end
        self.committed_offset = committed
        self.assigned: list[FakeTopicPartition] = []
        self.seek_call: Optional[tuple[FakeTopicPartition, int]] = None
        self.commit_calls: list[dict[FakeTopicPartition, FakeOffsetAndMetadata]] = []
        self.closed = False

    def partitions_for_topic(self, topic: str):
        return {self.record.partition} if topic == self.record.topic else None

    def beginning_offsets(self, partitions):
        return {partitions[0]: self.beginning}

    def end_offsets(self, partitions):
        return {partitions[0]: self.end}

    def committed(self, topic_partition: FakeTopicPartition):
        return self.committed_offset

    def assign(self, partitions) -> None:
        self.assigned = list(partitions)

    def seek(self, topic_partition: FakeTopicPartition, offset: int) -> None:
        self.seek_call = (topic_partition, offset)

    def poll(self, timeout_ms: int, max_records: int):
        return {self.assigned[0]: [self.record]}

    def commit(self, offsets) -> None:
        self.log.append("commit")
        copied = dict(offsets)
        self.commit_calls.append(copied)
        metadata = next(iter(copied.values()))
        self.committed_offset = metadata.offset

    def close(self) -> None:
        self.closed = True
        self.log.append("consumer-close")


class ResolverHarness:
    def __init__(
        self,
        *,
        wrapper: Optional[dict[str, Any]] = None,
        returned_offset: int = 5,
        committed: Optional[int] = 5,
        beginning: int = 0,
        end: int = 10,
        future_error: Optional[Exception] = None,
        flush_error: Optional[Exception] = None,
        concurrent_commit_after_flush: Optional[int] = None,
    ) -> None:
        self.log: list[str] = []
        self.payload = valid_event_payload()
        self.wrapper = valid_dlq_wrapper(self.payload) if wrapper is None else wrapper
        self.record = FakeRecord(
            topic="imposbro_search_sharded_dlq",
            partition=0,
            offset=returned_offset,
            value=json.dumps(self.wrapper, separators=(",", ":")).encode("utf-8"),
        )
        self.consumer = FakeConsumer(
            self.log,
            self.record,
            beginning=beginning,
            end=end,
            committed=committed,
        )
        self.producer = FakeProducer(
            self.log,
            future_error=future_error,
            flush_error=flush_error,
            on_flush=(
                None
                if concurrent_commit_after_flush is None
                else lambda: setattr(
                    self.consumer,
                    "committed_offset",
                    concurrent_commit_after_flush,
                )
            ),
        )
        self.consumer_creations: list[dict[str, Any]] = []
        self.producer_creations: list[dict[str, Any]] = []

        def consumer_factory(**kwargs: Any):
            self.consumer_creations.append(kwargs)
            return self.consumer

        def producer_factory(**kwargs: Any):
            self.producer_creations.append(kwargs)
            return self.producer

        bindings = resolver_module.KafkaBindings(
            Consumer=consumer_factory,
            Producer=producer_factory,
            TopicPartition=FakeTopicPartition,
            OffsetAndMetadata=FakeOffsetAndMetadata,
        )
        self.bindings = bindings
        config = resolver_module.ResolverConfig(
            bootstrap_servers=("kafka.internal:9093",),
            security_protocol="SASL_SSL",
            security_kwargs={"security_protocol": "SASL_SSL"},
            timeout_ms=5_000,
            max_record_bytes=1_048_576,
        )
        self.resolver = resolver_module.DlqResolver(
            config,
            bindings,
            now=lambda: "2026-07-10T12:00:00Z",
        )
        self.selection = resolver_module.Selection(
            dlq_topic="imposbro_search_sharded_dlq",
            partition=0,
            offset=5,
            expected_source_topic="imposbro_search_sharded_products",
        )

    def resolve(self, action: str, **kwargs: Any) -> dict[str, Any]:
        return self.resolver.resolve(
            action=action,
            selection=self.selection,
            operation_id="dlq-test-operation",
            **kwargs,
        )


def valid_event_payload() -> dict[str, Any]:
    return {
        "envelope_version": 2,
        "event_id": "evt:0123456789abcdef",
        "identity": {
            "tenant_id": "tenant-sensitive",
            "collection": "products",
            "document_id": "doc-sensitive",
        },
        "document_version": 7,
        "sequence": 7,
        "operation": "upsert",
        "routing_revision": 3,
        "rollout_id": None,
        "target_clusters": ["cluster-a", "cluster-b"],
        "occurred_at": "2026-07-10T10:00:00Z",
        "trace": {"request_id": "request-sensitive"},
        "document": {
            "id": "doc-sensitive",
            "name": "Ultra Secret Product Name",
        },
    }


def valid_dlq_wrapper(payload: dict[str, Any]) -> dict[str, Any]:
    key = b"tenant-sensitive\x1fproducts\x1fdoc-sensitive"
    return {
        "source_topic": "imposbro_search_sharded_products",
        "error": "MissingTargetClusterError",
        "error_message": "Sensitive dependency failure detail",
        "message": payload,
        "source_partition": 2,
        "source_offset": 41,
        "message_key": base64.b64encode(key).decode("ascii"),
        "message_key_encoding": "base64",
    }


class DlqResolverTests(unittest.TestCase):
    def test_inspect_never_commits_and_can_observe_non_next_offset(self):
        harness = ResolverHarness(committed=4)

        evidence = harness.resolve("inspect")

        self.assertEqual(evidence["outcome"], "inspected")
        self.assertFalse(evidence["offset_state"]["resolution_order_eligible"])
        self.assertEqual(harness.consumer.commit_calls, [])
        self.assertEqual(harness.producer_creations, [])

    def test_dry_run_validates_replay_without_send_or_commit(self):
        harness = ResolverHarness()

        evidence = harness.resolve("dry-run")

        self.assertEqual(evidence["outcome"], "dry_run")
        self.assertTrue(evidence["replay_validation"]["eligible"])
        self.assertFalse(evidence["commit"]["performed"])
        self.assertEqual(harness.producer_creations, [])
        self.assertEqual(harness.consumer.commit_calls, [])
        consumer_config = harness.consumer_creations[0]
        self.assertEqual(
            consumer_config["group_id"],
            resolver_module.RESOLVER_GROUP_ID,
        )
        self.assertFalse(consumer_config["enable_auto_commit"])
        self.assertEqual(consumer_config["auto_offset_reset"], "none")
        self.assertEqual(consumer_config["max_poll_records"], 1)

    def test_replay_default_acks_and_flushes_but_does_not_commit(self):
        harness = ResolverHarness()
        original_payload = copy.deepcopy(harness.payload)

        evidence = harness.resolve(
            "replay",
            confirm="REPLAY:imposbro_search_sharded_dlq:0:5",
        )

        self.assertEqual(evidence["outcome"], "replayed_uncommitted")
        self.assertEqual(harness.log[:3], ["send", "get", "flush"])
        self.assertNotIn("commit", harness.log)
        self.assertEqual(harness.producer_creations[0]["acks"], "all")
        self.assertEqual(harness.producer_creations[0]["retries"], 5)
        self.assertEqual(
            harness.producer_creations[0]["max_in_flight_requests_per_connection"],
            1,
        )
        self.assertNotIn("enable_idempotence", harness.producer_creations[0])
        self.assertEqual(harness.producer.sent[0]["value"], original_payload)
        self.assertEqual(harness.payload, original_payload)
        header_names = [name for name, _value in harness.producer.sent[0]["headers"]]
        self.assertIn("imposbro-recovery-protocol", header_names)
        self.assertIn("imposbro-dlq-offset", header_names)
        headers = dict(harness.producer.sent[0]["headers"])
        self.assertEqual(headers["imposbro-dlq-offset"], b"5")
        self.assertEqual(headers["imposbro-source-offset"], b"41")

    def test_replay_ack_and_flush_happen_before_exact_offset_commit(self):
        harness = ResolverHarness()

        evidence = harness.resolve(
            "replay",
            commit=True,
            confirm="REPLAY-AND-COMMIT:imposbro_search_sharded_dlq:0:5",
        )

        self.assertLess(harness.log.index("send"), harness.log.index("get"))
        self.assertLess(harness.log.index("get"), harness.log.index("flush"))
        self.assertLess(harness.log.index("flush"), harness.log.index("commit"))
        commit = harness.consumer.commit_calls[0]
        metadata = next(iter(commit.values()))
        self.assertEqual(metadata.offset, 6)
        self.assertEqual(evidence["commit"]["offset"], 6)
        self.assertEqual(evidence["outcome"], "replayed_and_committed")

    def test_replay_ack_failure_never_flushes_or_commits(self):
        harness = ResolverHarness(future_error=RuntimeError("broker detail"))

        with self.assertRaisesRegex(
            resolver_module.ResolverError, "offset was not committed"
        ) as raised:
            harness.resolve(
                "replay",
                commit=True,
                confirm="REPLAY-AND-COMMIT:imposbro_search_sharded_dlq:0:5",
            )

        self.assertEqual(raised.exception.code, "replay_ack_failed")
        self.assertNotIn("flush", harness.log)
        self.assertNotIn("commit", harness.log)
        self.assertEqual(harness.consumer.commit_calls, [])

    def test_replay_flush_failure_never_commits(self):
        harness = ResolverHarness(flush_error=RuntimeError("flush detail"))

        with self.assertRaises(resolver_module.ResolverError) as raised:
            harness.resolve(
                "replay",
                commit=True,
                confirm="REPLAY-AND-COMMIT:imposbro_search_sharded_dlq:0:5",
            )

        self.assertEqual(raised.exception.code, "replay_ack_failed")
        self.assertIn("flush", harness.log)
        self.assertNotIn("commit", harness.log)
        self.assertEqual(harness.consumer.commit_calls, [])

    def test_concurrent_group_advance_cannot_be_rewound_by_stale_commit(self):
        harness = ResolverHarness(concurrent_commit_after_flush=7)

        with self.assertRaises(resolver_module.ResolverError) as raised:
            harness.resolve(
                "replay",
                commit=True,
                confirm="REPLAY-AND-COMMIT:imposbro_search_sharded_dlq:0:5",
            )

        self.assertEqual(raised.exception.code, "concurrent_offset_change")
        self.assertEqual(harness.consumer.commit_calls, [])
        self.assertEqual(harness.consumer.committed_offset, 7)

    def test_record_returned_for_different_offset_fails_closed(self):
        harness = ResolverHarness(returned_offset=6)

        with self.assertRaises(resolver_module.ResolverError) as raised:
            harness.resolve("inspect")

        self.assertEqual(raised.exception.code, "exact_record_mismatch")
        self.assertEqual(harness.consumer.commit_calls, [])

    def test_replay_cannot_skip_an_earlier_unresolved_offset(self):
        harness = ResolverHarness(committed=4)

        with self.assertRaises(resolver_module.ResolverError) as raised:
            harness.resolve(
                "replay",
                confirm="REPLAY:imposbro_search_sharded_dlq:0:5",
            )

        self.assertEqual(raised.exception.code, "partition_order_violation")
        self.assertEqual(harness.producer_creations, [])
        self.assertEqual(harness.consumer.commit_calls, [])

    def test_malformed_dlq_wrapper_never_commits(self):
        wrapper = valid_dlq_wrapper(valid_event_payload())
        del wrapper["source_offset"]
        harness = ResolverHarness(wrapper=wrapper)

        with self.assertRaises(resolver_module.ResolverError) as raised:
            harness.resolve(
                "disposition",
                commit=True,
                reason="invalid-wrapper",
                approver="security-team",
                confirm="DISPOSITION-AND-COMMIT:imposbro_search_sharded_dlq:0:5",
            )

        self.assertEqual(raised.exception.code, "malformed_dlq_wrapper")
        self.assertEqual(harness.consumer.commit_calls, [])

    def test_source_topic_must_match_explicit_operator_expectation(self):
        wrapper = valid_dlq_wrapper(valid_event_payload())
        wrapper["source_topic"] = "unexpected_topic"
        harness = ResolverHarness(wrapper=wrapper)

        with self.assertRaises(resolver_module.ResolverError) as raised:
            harness.resolve("inspect")

        self.assertEqual(raised.exception.code, "source_topic_mismatch")
        self.assertEqual(harness.consumer.commit_calls, [])

    def test_replay_rejects_key_that_does_not_match_v2_identity(self):
        wrapper = valid_dlq_wrapper(valid_event_payload())
        wrapper["message_key"] = base64.b64encode(b"wrong-key").decode("ascii")
        harness = ResolverHarness(wrapper=wrapper)

        with self.assertRaises(resolver_module.ResolverError) as raised:
            harness.resolve("dry-run")

        self.assertEqual(raised.exception.code, "replay_key_mismatch")
        self.assertEqual(harness.consumer.commit_calls, [])

    def test_disposition_requires_explicit_commit_and_exact_confirmation(self):
        harness = ResolverHarness()

        with self.assertRaises(resolver_module.ResolverError) as missing_commit:
            harness.resolve(
                "disposition",
                reason="obsolete-after-tombstone",
                approver="data-owner",
                confirm="DISPOSITION-AND-COMMIT:imposbro_search_sharded_dlq:0:5",
            )
        self.assertEqual(missing_commit.exception.code, "explicit_commit_required")

        with self.assertRaises(resolver_module.ResolverError) as bad_confirmation:
            harness.resolve(
                "disposition",
                commit=True,
                reason="obsolete-after-tombstone",
                approver="data-owner",
                confirm="wrong",
            )
        self.assertEqual(bad_confirmation.exception.code, "confirmation_mismatch")
        self.assertEqual(harness.consumer.commit_calls, [])

        with self.assertRaises(resolver_module.ResolverError) as invalid_approval:
            harness.resolve(
                "disposition",
                commit=True,
                reason="contains unsafe whitespace",
                approver="data-owner",
                confirm="DISPOSITION-AND-COMMIT:imposbro_search_sharded_dlq:0:5",
            )
        self.assertEqual(invalid_approval.exception.code, "invalid_disposition")
        self.assertEqual(harness.consumer.commit_calls, [])

    def test_approved_disposition_commits_without_replay(self):
        harness = ResolverHarness()

        evidence = harness.resolve(
            "disposition",
            commit=True,
            reason="obsolete-after-tombstone",
            approver="data-owner",
            confirm="DISPOSITION-AND-COMMIT:imposbro_search_sharded_dlq:0:5",
        )

        self.assertEqual(evidence["outcome"], "disposition_committed")
        self.assertEqual(evidence["commit"]["offset"], 6)
        self.assertEqual(evidence["disposition"]["reason"], "obsolete-after-tombstone")
        self.assertEqual(harness.producer_creations, [])

    def test_evidence_contains_digests_not_payload_key_or_error_message(self):
        harness = ResolverHarness()

        evidence = harness.resolve("inspect")
        serialized = json.dumps(evidence, sort_keys=True)

        self.assertNotIn("tenant-sensitive", serialized)
        self.assertNotIn("doc-sensitive", serialized)
        self.assertNotIn("Ultra Secret Product Name", serialized)
        self.assertNotIn("Sensitive dependency failure detail", serialized)
        self.assertTrue(evidence["redaction"]["payload_included"] is False)
        self.assertRegex(evidence["record"]["message_sha256"], r"^[0-9a-f]{64}$")


class ResolverConfigTests(unittest.TestCase):
    def test_security_protocol_is_explicit_and_plaintext_needs_opt_in(self):
        with self.assertRaises(resolver_module.ResolverError) as missing:
            resolver_module.ResolverConfig.from_env(
                {"KAFKA_BOOTSTRAP_SERVERS": "kafka:9092"}
            )
        self.assertEqual(missing.exception.code, "missing_transport_config")

        with self.assertRaises(resolver_module.ResolverError) as insecure:
            resolver_module.ResolverConfig.from_env(
                {
                    "KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
                    "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
                }
            )
        self.assertEqual(insecure.exception.code, "insecure_transport_not_approved")

    def test_sasl_tls_fails_closed_without_ca_and_does_not_echo_password(self):
        password = "do-not-log-this-password"
        with self.assertRaises(resolver_module.ResolverError) as raised:
            resolver_module.ResolverConfig.from_env(
                {
                    "KAFKA_BOOTSTRAP_SERVERS": "kafka:9093",
                    "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
                    "KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
                    "KAFKA_SASL_USERNAME": "resolver",
                    "KAFKA_SASL_PASSWORD": password,
                }
            )

        self.assertEqual(raised.exception.code, "invalid_transport_config")
        self.assertNotIn(password, raised.exception.public_message)

    def test_valid_sasl_tls_configuration_uses_secret_without_logging_it(self):
        with tempfile.TemporaryDirectory() as directory:
            ca = Path(directory) / "ca.pem"
            ca.write_text("fake-ca-for-config-validation", encoding="utf-8")
            config = resolver_module.ResolverConfig.from_env(
                {
                    "KAFKA_BOOTSTRAP_SERVERS": "kafka-1:9093,kafka-2:9093",
                    "KAFKA_SECURITY_PROTOCOL": "SASL_SSL",
                    "KAFKA_SASL_MECHANISM": "SCRAM-SHA-512",
                    "KAFKA_SASL_USERNAME": "resolver",
                    "KAFKA_SASL_PASSWORD": "secret-value",
                    "KAFKA_SSL_CAFILE": str(ca),
                }
            )

        self.assertEqual(config.bootstrap_servers, ("kafka-1:9093", "kafka-2:9093"))
        self.assertTrue(config.security_kwargs["ssl_check_hostname"])
        self.assertEqual(config.security_kwargs["sasl_plain_password"], "secret-value")


class EvidenceWriterTests(unittest.TestCase):
    def test_evidence_path_is_reserved_and_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            first = resolver_module.EvidenceWriter(path)
            first.reserve({"outcome": "started"})
            first.finalize({"outcome": "complete", "payload": None})

            second = resolver_module.EvidenceWriter(path)
            with self.assertRaises(resolver_module.ResolverError) as raised:
                second.reserve({"outcome": "started"})

            self.assertEqual(raised.exception.code, "evidence_exists")
            self.assertEqual(json.loads(path.read_text())["outcome"], "complete")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_cli_dry_run_writes_final_redacted_evidence(self):
        harness = ResolverHarness()
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "dry-run.json"
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                result = resolver_module.main(
                    [
                        "dry-run",
                        "--dlq-topic",
                        "imposbro_search_sharded_dlq",
                        "--partition",
                        "0",
                        "--offset",
                        "5",
                        "--expected-source-topic",
                        "imposbro_search_sharded_products",
                        "--evidence",
                        str(evidence_path),
                    ],
                    environ={
                        "KAFKA_BOOTSTRAP_SERVERS": "kafka:9092",
                        "KAFKA_SECURITY_PROTOCOL": "PLAINTEXT",
                        "DLQ_RESOLVER_ALLOW_INSECURE_KAFKA": "true",
                    },
                    bindings=harness.bindings,
                )

            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            serialized = json.dumps(evidence)
            self.assertEqual(result, 0)
            self.assertEqual(evidence["outcome"], "dry_run")
            self.assertNotIn("tenant-sensitive", serialized)
            self.assertIn("redacted evidence", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
