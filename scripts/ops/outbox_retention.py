#!/usr/bin/env python3
"""Bounded, fail-closed retention for IMPOSBRO-owned PostgreSQL outboxes.

The default mode is dry-run.  Apply mode requires a reviewed policy file, its
exact SHA-256 digest, and an out-of-band confirmation value.  This tool never
reads or writes document payloads, never touches the audit chain, and never
deletes an unpublished row or the newest durable mutation for an identity.

Runtime dependency: psycopg 3 (the same driver used by Query API).
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import stat
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


EVIDENCE_SCHEMA = "imposbro.outbox-retention-evidence.v1"
POLICY_SCHEMA = "imposbro.outbox-retention-policy.v1"
SUPPORTED_SCHEMA_REVISIONS = {"0003_audit_delivery_deletion"}
SUPPORTED_TABLES = ("control_plane_outbox", "indexing_event_outbox")
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_POLICY_BYTES = 262_144


class RetentionError(RuntimeError):
    """Expected operational failure whose code is safe for evidence/stderr."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.public_message = message


class _DuplicateKey(ValueError):
    pass


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def strict_json_object(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateKey(key)
            value[key] = item
        return value

    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda _value: (_ for _ in ()).throw(
                ValueError("non-finite number")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey, ValueError) as exc:
        raise RetentionError("invalid_policy_json", "policy must be strict UTF-8 JSON") from exc
    if not isinstance(parsed, dict):
        raise RetentionError("invalid_policy_json", "policy must be a JSON object")
    return parsed


def require_exact_keys(value: Mapping[str, Any], expected: set[str], label: str) -> None:
    actual = set(value)
    if actual != expected:
        raise RetentionError(
            "invalid_policy_schema",
            f"{label} has missing or unsupported fields",
        )


def require_identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or not SAFE_IDENTIFIER.fullmatch(value):
        raise RetentionError(
            "invalid_policy_schema",
            f"{label} must be a safe 1-128 character identifier",
        )
    return value


def parse_utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise RetentionError(
            "invalid_policy_schema", f"{label} must be an RFC3339 UTC timestamp"
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise RetentionError(
            "invalid_policy_schema", f"{label} must be an RFC3339 UTC timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise RetentionError(
            "invalid_policy_schema", f"{label} must include a UTC timezone"
        )
    return parsed.astimezone(timezone.utc)


def require_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetentionError("invalid_policy_schema", f"{label} must be an integer")
    if value < minimum or value > maximum:
        raise RetentionError(
            "invalid_policy_schema", f"{label} must be between {minimum} and {maximum}"
        )
    return value


@dataclass(frozen=True)
class TablePolicy:
    retention_seconds: int
    max_unpublished: int
    held_identity_hashes: tuple[str, ...] = ()
    held_revisions: tuple[int, ...] = ()


@dataclass(frozen=True)
class LegalHoldSnapshot:
    snapshot_id: str
    captured_at: datetime
    expires_at: datetime
    complete: bool


@dataclass(frozen=True)
class RetentionPolicy:
    policy_id: str
    database_id: str
    database_name: str
    schema_revision: str
    not_before: datetime
    expires_at: datetime
    deny_all: bool
    legal_hold_snapshot: LegalHoldSnapshot
    allowed_tables: tuple[str, ...]
    tables: Mapping[str, TablePolicy]
    digest: str

    @property
    def confirmation(self) -> str:
        return f"APPLY:{self.database_id}:{self.policy_id}:{self.digest[:12]}"

    @classmethod
    def load(cls, path: Path) -> "RetentionPolicy":
        try:
            flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(path, flags)
        except FileNotFoundError as exc:
            raise RetentionError("policy_not_found", "policy file was not found") from exc
        except OSError as exc:
            raise RetentionError(
                "unsafe_policy_file", "policy must be a regular file, not a symbolic link"
            ) from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise RetentionError(
                    "unsafe_policy_file", "policy must be a regular file, not a symbolic link"
                )
            if details.st_mode & 0o077:
                raise RetentionError(
                    "unsafe_policy_permissions",
                    "policy file must not be accessible by group or other",
                )
            if details.st_uid not in {0, os.geteuid()}:
                raise RetentionError(
                    "unsafe_policy_owner", "policy must be owned by root or the retention process"
                )
            if details.st_size <= 0 or details.st_size > MAX_POLICY_BYTES:
                raise RetentionError(
                    "invalid_policy_size", "policy file size is outside the accepted range"
                )
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                raw = stream.read(MAX_POLICY_BYTES + 1)
            if len(raw) != details.st_size or len(raw) > MAX_POLICY_BYTES:
                raise RetentionError(
                    "policy_changed_during_read", "policy changed while it was being read"
                )
        finally:
            os.close(descriptor)
        digest = hashlib.sha256(raw).hexdigest()
        value = strict_json_object(raw)
        require_exact_keys(
            value,
            {
                "schema",
                "policy_id",
                "database_id",
                "database_name",
                "schema_revision",
                "not_before",
                "expires_at",
                "deny_all",
                "legal_hold_snapshot",
                "allowed_tables",
                "tables",
            },
            "policy",
        )
        if value["schema"] != POLICY_SCHEMA:
            raise RetentionError("unsupported_policy_schema", "policy schema is not supported")
        policy_id = require_identifier(value["policy_id"], "policy_id")
        database_id = require_identifier(value["database_id"], "database_id")
        database_name = require_identifier(value["database_name"], "database_name")
        revision = require_identifier(value["schema_revision"], "schema_revision")
        if revision not in SUPPORTED_SCHEMA_REVISIONS:
            raise RetentionError(
                "unsupported_schema_revision",
                "policy schema revision is not supported by this retention binary",
            )
        not_before = parse_utc(value["not_before"], "not_before")
        expires_at = parse_utc(value["expires_at"], "expires_at")
        if expires_at <= not_before:
            raise RetentionError(
                "invalid_policy_window", "policy expires_at must be after not_before"
            )
        if not isinstance(value["deny_all"], bool):
            raise RetentionError("invalid_policy_schema", "deny_all must be a boolean")
        snapshot_value = value["legal_hold_snapshot"]
        if not isinstance(snapshot_value, dict):
            raise RetentionError(
                "invalid_policy_schema", "legal_hold_snapshot must be an object"
            )
        require_exact_keys(
            snapshot_value,
            {"snapshot_id", "captured_at", "expires_at", "complete"},
            "legal_hold_snapshot",
        )
        snapshot = LegalHoldSnapshot(
            snapshot_id=require_identifier(
                snapshot_value["snapshot_id"], "legal_hold_snapshot.snapshot_id"
            ),
            captured_at=parse_utc(
                snapshot_value["captured_at"], "legal_hold_snapshot.captured_at"
            ),
            expires_at=parse_utc(
                snapshot_value["expires_at"], "legal_hold_snapshot.expires_at"
            ),
            complete=snapshot_value["complete"],
        )
        if not isinstance(snapshot.complete, bool):
            raise RetentionError(
                "invalid_policy_schema", "legal_hold_snapshot.complete must be a boolean"
            )
        if snapshot.expires_at <= snapshot.captured_at:
            raise RetentionError(
                "invalid_legal_hold_snapshot",
                "legal hold snapshot expiry must be after capture",
            )
        if not value["deny_all"] and not snapshot.complete:
            raise RetentionError(
                "incomplete_legal_hold_snapshot",
                "retention requires an explicitly complete legal hold snapshot",
            )
        allowed_raw = value["allowed_tables"]
        if (
            not isinstance(allowed_raw, list)
            or not allowed_raw
            or len(set(allowed_raw)) != len(allowed_raw)
            or any(item not in SUPPORTED_TABLES for item in allowed_raw)
        ):
            raise RetentionError(
                "invalid_policy_allowlist",
                "allowed_tables must be a non-empty unique allowlist of supported outboxes",
            )
        table_values = value["tables"]
        if not isinstance(table_values, dict) or set(table_values) != set(allowed_raw):
            raise RetentionError(
                "invalid_policy_schema",
                "tables must define exactly every allowlisted table",
            )

        parsed_tables: dict[str, TablePolicy] = {}
        for table in allowed_raw:
            item = table_values[table]
            if not isinstance(item, dict):
                raise RetentionError(
                    "invalid_policy_schema", f"tables.{table} must be an object"
                )
            if table == "control_plane_outbox":
                require_exact_keys(
                    item,
                    {"retention_seconds", "max_unpublished", "held_revisions"},
                    f"tables.{table}",
                )
                held = item["held_revisions"]
                if (
                    not isinstance(held, list)
                    or len(set(held)) != len(held)
                    or any(isinstance(entry, bool) or not isinstance(entry, int) or entry < 1 for entry in held)
                ):
                    raise RetentionError(
                        "invalid_policy_schema",
                        "held_revisions must contain unique positive integers",
                    )
                parsed_tables[table] = TablePolicy(
                    retention_seconds=require_int(
                        item["retention_seconds"],
                        f"tables.{table}.retention_seconds",
                        3600,
                        315_576_000,
                    ),
                    max_unpublished=require_int(
                        item["max_unpublished"],
                        f"tables.{table}.max_unpublished",
                        0,
                        10_000_000,
                    ),
                    held_revisions=tuple(sorted(held)),
                )
            else:
                require_exact_keys(
                    item,
                    {"retention_seconds", "max_unpublished", "held_identity_hashes"},
                    f"tables.{table}",
                )
                held = item["held_identity_hashes"]
                if (
                    not isinstance(held, list)
                    or len(set(held)) != len(held)
                    or any(not isinstance(entry, str) or not SHA256.fullmatch(entry) for entry in held)
                ):
                    raise RetentionError(
                        "invalid_policy_schema",
                        "held_identity_hashes must contain unique lowercase SHA-256 values",
                    )
                parsed_tables[table] = TablePolicy(
                    retention_seconds=require_int(
                        item["retention_seconds"],
                        f"tables.{table}.retention_seconds",
                        3600,
                        315_576_000,
                    ),
                    max_unpublished=require_int(
                        item["max_unpublished"],
                        f"tables.{table}.max_unpublished",
                        0,
                        10_000_000,
                    ),
                    held_identity_hashes=tuple(sorted(held)),
                )

        return cls(
            policy_id=policy_id,
            database_id=database_id,
            database_name=database_name,
            schema_revision=revision,
            not_before=not_before,
            expires_at=expires_at,
            deny_all=value["deny_all"],
            legal_hold_snapshot=snapshot,
            allowed_tables=tuple(allowed_raw),
            tables=parsed_tables,
            digest=digest,
        )


CONTROL_CANDIDATE_SQL = """
SELECT count(*), min(published_at), max(published_at)
FROM control_plane_outbox AS candidate
WHERE candidate.published_at IS NOT NULL
  AND candidate.published_at < %s
  AND candidate.revision <> ALL(%s)
  AND EXISTS (
    SELECT 1 FROM control_plane_outbox AS newer
    WHERE newer.revision > candidate.revision
  )
"""

INDEXING_CANDIDATE_SQL = """
SELECT count(*), min(published_at), max(published_at)
FROM indexing_event_outbox AS candidate
WHERE candidate.published_at IS NOT NULL
  AND candidate.published_at < %s
  AND candidate.identity_hash <> ALL(%s)
  AND EXISTS (
    SELECT 1 FROM indexing_event_outbox AS newer
    WHERE newer.identity_hash = candidate.identity_hash
      AND newer.sequence > candidate.sequence
  )
"""

CONTROL_DELETE_SQL = """
WITH candidates AS (
  SELECT candidate.id
  FROM control_plane_outbox AS candidate
  WHERE candidate.published_at IS NOT NULL
    AND candidate.published_at < %s
    AND candidate.revision <> ALL(%s)
    AND EXISTS (
      SELECT 1 FROM control_plane_outbox AS newer
      WHERE newer.revision > candidate.revision
    )
  ORDER BY candidate.published_at, candidate.id
  LIMIT %s
  FOR UPDATE OF candidate SKIP LOCKED
), deleted AS (
  DELETE FROM control_plane_outbox AS victim
  USING candidates
  WHERE victim.id = candidates.id
    AND victim.published_at IS NOT NULL
    AND EXISTS (
      SELECT 1 FROM control_plane_outbox AS newer
      WHERE newer.revision > victim.revision
    )
  RETURNING victim.published_at
)
SELECT count(*), min(published_at), max(published_at) FROM deleted
"""

INDEXING_DELETE_SQL = """
WITH candidates AS (
  SELECT candidate.event_id
  FROM indexing_event_outbox AS candidate
  WHERE candidate.published_at IS NOT NULL
    AND candidate.published_at < %s
    AND candidate.identity_hash <> ALL(%s)
    AND EXISTS (
      SELECT 1 FROM indexing_event_outbox AS newer
      WHERE newer.identity_hash = candidate.identity_hash
        AND newer.sequence > candidate.sequence
    )
  ORDER BY candidate.published_at, candidate.event_id
  LIMIT %s
  FOR UPDATE OF candidate SKIP LOCKED
), deleted AS (
  DELETE FROM indexing_event_outbox AS victim
  USING candidates
  WHERE victim.event_id = candidates.event_id
    AND victim.published_at IS NOT NULL
    AND EXISTS (
      SELECT 1 FROM indexing_event_outbox AS newer
      WHERE newer.identity_hash = victim.identity_hash
        AND newer.sequence > victim.sequence
    )
  RETURNING victim.published_at
)
SELECT count(*), min(published_at), max(published_at) FROM deleted
"""


@dataclass
class TableResult:
    table: str
    retention_seconds: int
    held_entries: int
    initial_unpublished: int
    candidates_before: int
    candidate_oldest: datetime | None
    candidate_newest: datetime | None
    deleted: int = 0
    deleted_oldest: datetime | None = None
    deleted_newest: datetime | None = None
    batches: int = 0
    remaining_candidates: int = 0

    def evidence(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "retention_seconds": self.retention_seconds,
            "legal_hold_entries": self.held_entries,
            "initial_unpublished": self.initial_unpublished,
            "candidates_before": self.candidates_before,
            "candidate_range": {
                "oldest": isoformat(self.candidate_oldest),
                "newest": isoformat(self.candidate_newest),
            },
            "deleted": self.deleted,
            "deleted_range": {
                "oldest": isoformat(self.deleted_oldest),
                "newest": isoformat(self.deleted_newest),
            },
            "batches": self.batches,
            "remaining_candidates": self.remaining_candidates,
        }


class PostgresRetention:
    def __init__(
        self,
        policy: RetentionPolicy,
        *,
        batch_size: int,
        max_batches: int,
        lock_timeout_ms: int,
        statement_timeout_ms: int,
    ) -> None:
        self.policy = policy
        self.batch_size = batch_size
        self.max_batches = max_batches
        self.lock_timeout_ms = lock_timeout_ms
        self.statement_timeout_ms = statement_timeout_ms

    def run(self, apply: bool) -> tuple[list[TableResult], bool, dict[str, Any]]:
        try:
            import psycopg
        except ImportError as exc:
            raise RetentionError(
                "missing_runtime_dependency", "psycopg 3 is required"
            ) from exc

        conninfo = os.environ.get("RETENTION_DATABASE_URL", "").strip()
        try:
            connection = psycopg.connect(conninfo, autocommit=True)
        except Exception as exc:
            raise RetentionError("database_connection_failed", "database connection failed") from exc

        locked = False
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT current_database(), clock_timestamp(), "
                    "(SELECT version_num FROM alembic_version LIMIT 1)"
                )
                database_name, database_now, revision = cursor.fetchone()
                if database_name != self.policy.database_name:
                    raise RetentionError(
                        "database_identity_mismatch",
                        "connected database does not match the reviewed policy",
                    )
                if revision != self.policy.schema_revision:
                    raise RetentionError(
                        "schema_revision_mismatch",
                        "database schema revision does not match the reviewed policy",
                    )
                if not self.policy.not_before <= database_now <= self.policy.expires_at:
                    raise RetentionError(
                        "policy_outside_validity_window",
                        "database clock is outside the reviewed policy validity window",
                    )
                snapshot = self.policy.legal_hold_snapshot
                if not snapshot.captured_at <= database_now <= snapshot.expires_at:
                    raise RetentionError(
                        "legal_hold_snapshot_outside_validity_window",
                        "database clock is outside the legal hold snapshot validity window",
                    )
                cursor.execute(
                    "SELECT pg_try_advisory_lock(hashtextextended(%s, 0))",
                    ("imposbro.outbox-retention.v1",),
                )
                locked = bool(cursor.fetchone()[0])
                if not locked:
                    raise RetentionError(
                        "retention_job_already_running",
                        "another retention job owns the database advisory lock",
                    )

            results: list[TableResult] = []
            incomplete = False
            for table in self.policy.allowed_tables:
                result = self._process_table(connection, table, database_now, apply)
                results.append(result)
                incomplete = incomplete or result.remaining_candidates > 0

            provenance = {
                "database_id": self.policy.database_id,
                "database_name_sha256": hashlib.sha256(database_name.encode("utf-8")).hexdigest(),
                "database_clock": isoformat(database_now),
                "schema_revision": revision,
            }
            return results, incomplete, provenance
        finally:
            if locked:
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
                            ("imposbro.outbox-retention.v1",),
                        )
                except Exception:
                    pass
            connection.close()

    def _counts(
        self, connection: Any, table: str, cutoff: datetime, table_policy: TablePolicy
    ) -> tuple[int, datetime | None, datetime | None, int]:
        if table == "control_plane_outbox":
            candidate_sql = CONTROL_CANDIDATE_SQL
            held: Sequence[Any] = list(table_policy.held_revisions)
        else:
            candidate_sql = INDEXING_CANDIDATE_SQL
            held = list(table_policy.held_identity_hashes)
        with connection.cursor() as cursor:
            cursor.execute(candidate_sql, (cutoff, held))
            count, oldest, newest = cursor.fetchone()
            cursor.execute(f"SELECT count(*) FROM {table} WHERE published_at IS NULL")
            unpublished = cursor.fetchone()[0]
        return int(count), oldest, newest, int(unpublished)

    def _process_table(
        self, connection: Any, table: str, database_now: datetime, apply: bool
    ) -> TableResult:
        table_policy = self.policy.tables[table]
        cutoff = database_now - timedelta(seconds=table_policy.retention_seconds)
        count, oldest, newest, unpublished = self._counts(
            connection, table, cutoff, table_policy
        )
        if unpublished > table_policy.max_unpublished:
            raise RetentionError(
                "unpublished_backlog_exceeds_policy",
                f"{table} unpublished backlog exceeds the reviewed policy limit",
            )
        held_entries = (
            len(table_policy.held_revisions)
            if table == "control_plane_outbox"
            else len(table_policy.held_identity_hashes)
        )
        result = TableResult(
            table=table,
            retention_seconds=table_policy.retention_seconds,
            held_entries=held_entries,
            initial_unpublished=unpublished,
            candidates_before=count,
            candidate_oldest=oldest,
            candidate_newest=newest,
            remaining_candidates=count,
        )
        if not apply:
            return result

        delete_sql = (
            CONTROL_DELETE_SQL
            if table == "control_plane_outbox"
            else INDEXING_DELETE_SQL
        )
        held: Sequence[Any] = (
            list(table_policy.held_revisions)
            if table == "control_plane_outbox"
            else list(table_policy.held_identity_hashes)
        )
        for _ in range(self.max_batches):
            _, _, _, current_unpublished = self._counts(
                connection, table, cutoff, table_policy
            )
            if (
                current_unpublished > result.initial_unpublished
                or current_unpublished > table_policy.max_unpublished
            ):
                raise RetentionError(
                    "unpublished_backlog_rising",
                    f"{table} unpublished backlog rose during retention",
                )
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('lock_timeout', %s, true), "
                        "set_config('statement_timeout', %s, true)",
                        (f"{self.lock_timeout_ms}ms", f"{self.statement_timeout_ms}ms"),
                    )
                    cursor.execute(delete_sql, (cutoff, held, self.batch_size))
                    deleted, oldest_deleted, newest_deleted = cursor.fetchone()
            deleted = int(deleted)
            if deleted == 0:
                break
            result.batches += 1
            result.deleted += deleted
            if result.deleted_oldest is None or (
                oldest_deleted is not None and oldest_deleted < result.deleted_oldest
            ):
                result.deleted_oldest = oldest_deleted
            if result.deleted_newest is None or (
                newest_deleted is not None and newest_deleted > result.deleted_newest
            ):
                result.deleted_newest = newest_deleted
        remaining, _, _, _ = self._counts(connection, table, cutoff, table_policy)
        result.remaining_candidates = remaining
        return result


def write_evidence(path: Path, value: Mapping[str, Any]) -> tuple[str, Path]:
    checksum_path = Path(f"{path}.sha256")
    if path.suffix != ".json":
        raise RetentionError("invalid_evidence_path", "evidence path must end in .json")
    if not path.parent.is_dir():
        raise RetentionError("invalid_evidence_path", "evidence parent directory does not exist")
    if path.exists() or checksum_path.exists():
        raise RetentionError(
            "evidence_exists", "evidence or checksum already exists; refusing to overwrite"
        )

    body = (
        json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
        + "\n"
    ).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    staged: list[Path] = []
    published: list[Path] = []
    try:
        for target, content in (
            (path, body),
            (checksum_path, f"{digest}  {path.name}\n".encode("ascii")),
        ):
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            staged_path = Path(name)
            staged.append(staged_path)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(staged_path, target)
                published.append(target)
            finally:
                staged_path.unlink(missing_ok=True)
        return digest, checksum_path
    except Exception:
        for target in published:
            target.unlink(missing_ok=True)
        raise


def positive_int(raw: str, minimum: int, maximum: int, label: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{label} must be an integer") from exc
    if value < minimum or value > maximum:
        raise argparse.ArgumentTypeError(
            f"{label} must be between {minimum} and {maximum}"
        )
    return value


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(
        description="Dry-run or apply bounded retention to owned PostgreSQL outboxes."
    )
    value.add_argument("--policy", required=True, type=Path)
    value.add_argument("--evidence", required=True, type=Path)
    value.add_argument(
        "--apply", action="store_true", help="delete eligible rows; default is dry-run"
    )
    value.add_argument(
        "--policy-sha256",
        default="",
        help="reviewed policy digest; mandatory with --apply",
    )
    value.add_argument(
        "--batch-size",
        type=lambda raw: positive_int(raw, 1, 10_000, "batch-size"),
        default=500,
    )
    value.add_argument(
        "--max-batches",
        type=lambda raw: positive_int(raw, 1, 100_000, "max-batches"),
        default=1_000,
    )
    value.add_argument(
        "--lock-timeout-ms",
        type=lambda raw: positive_int(raw, 100, 60_000, "lock-timeout-ms"),
        default=2_000,
    )
    value.add_argument(
        "--statement-timeout-ms",
        type=lambda raw: positive_int(raw, 1_000, 300_000, "statement-timeout-ms"),
        default=30_000,
    )
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    started = utc_now()
    monotonic_started = time.monotonic()
    policy: RetentionPolicy | None = None
    outcome = "failed"
    error_code: str | None = None
    results: list[TableResult] = []
    provenance: dict[str, Any] = {}
    exit_code = 1
    try:
        policy = RetentionPolicy.load(args.policy)
        if policy.deny_all:
            raise RetentionError(
                "policy_denies_retention", "reviewed policy has deny_all enabled"
            )
        if args.apply:
            supplied_digest = args.policy_sha256.strip().lower()
            if not SHA256.fullmatch(supplied_digest) or not hmac.compare_digest(
                supplied_digest, policy.digest
            ):
                raise RetentionError(
                    "policy_digest_mismatch",
                    "--policy-sha256 must match the reviewed policy exactly",
                )
            if os.environ.get("RETENTION_CONFIRMATION", "") != policy.confirmation:
                raise RetentionError(
                    "confirmation_mismatch",
                    "RETENTION_CONFIRMATION does not match the reviewed target and policy",
                )
        runner = PostgresRetention(
            policy,
            batch_size=args.batch_size,
            max_batches=args.max_batches,
            lock_timeout_ms=args.lock_timeout_ms,
            statement_timeout_ms=args.statement_timeout_ms,
        )
        results, incomplete, provenance = runner.run(args.apply)
        if args.apply and incomplete:
            outcome = "partial"
            error_code = "bounded_run_incomplete"
            exit_code = 2
        else:
            outcome = "success"
            exit_code = 0
    except RetentionError as exc:
        error_code = exc.code
        print(f"ERROR [{exc.code}]: {exc.public_message}", file=sys.stderr)
    except Exception:
        error_code = "unexpected_retention_failure"
        print("ERROR [unexpected_retention_failure]: retention failed", file=sys.stderr)

    completed = utc_now()
    evidence = {
        "schema": EVIDENCE_SCHEMA,
        "outcome": outcome,
        "mode": "apply" if args.apply else "dry-run",
        "error_code": error_code,
        "policy": {
            "id": policy.policy_id if policy else None,
            "sha256": policy.digest if policy else None,
            "deny_all": policy.deny_all if policy else None,
            "allowed_tables": list(policy.allowed_tables) if policy else [],
            "valid_from": isoformat(policy.not_before) if policy else None,
            "valid_until": isoformat(policy.expires_at) if policy else None,
            "legal_hold_snapshot": {
                "id": policy.legal_hold_snapshot.snapshot_id if policy else None,
                "captured_at": isoformat(policy.legal_hold_snapshot.captured_at) if policy else None,
                "valid_until": isoformat(policy.legal_hold_snapshot.expires_at) if policy else None,
                "complete": policy.legal_hold_snapshot.complete if policy else None,
            },
        },
        "provenance": provenance,
        "safety": {
            "dry_run_default": True,
            "published_rows_only": True,
            "newest_mutation_per_identity_preserved": True,
            "audit_chain_touched": False,
            "payloads_recorded": False,
            "bounded_batch_size": args.batch_size,
            "bounded_max_batches": args.max_batches,
            "lock_timeout_ms": args.lock_timeout_ms,
            "statement_timeout_ms": args.statement_timeout_ms,
        },
        "tables": [result.evidence() for result in results],
        "timing": {
            "started_at": isoformat(started),
            "completed_at": isoformat(completed),
            "duration_ms": round((time.monotonic() - monotonic_started) * 1_000),
        },
        "production_certification": False,
    }
    try:
        digest, checksum_path = write_evidence(args.evidence, evidence)
        print(f"Evidence: {args.evidence} (sha256={digest})", file=sys.stderr)
        print(f"Checksum: {checksum_path}", file=sys.stderr)
    except RetentionError as exc:
        print(f"ERROR [{exc.code}]: {exc.public_message}", file=sys.stderr)
        return 1
    except Exception:
        print("ERROR [evidence_write_failed]: could not write retention evidence", file=sys.stderr)
        return 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
