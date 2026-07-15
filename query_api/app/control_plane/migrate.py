"""Serialized Alembic migration runner for the control-plane database."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from .postgres_store import PostgresControlPlaneStore


MIGRATION_LOCK_KEY = 4_809_724_653_421_027_190


def _positive_timeout() -> float:
    raw = os.environ.get("CONTROL_PLANE_MIGRATION_LOCK_TIMEOUT_SECONDS", "60")
    try:
        value = float(raw)
    except ValueError as exc:
        raise RuntimeError(
            "CONTROL_PLANE_MIGRATION_LOCK_TIMEOUT_SECONDS must be numeric"
        ) from exc
    if value <= 0:
        raise RuntimeError(
            "CONTROL_PLANE_MIGRATION_LOCK_TIMEOUT_SECONDS must be positive"
        )
    return value


def _database_url() -> str:
    value = os.environ.get("CONTROL_PLANE_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("CONTROL_PLANE_DATABASE_URL is required")
    return value


def _alembic_config() -> Config:
    query_api_root = Path(__file__).resolve().parents[2]
    return Config(str(query_api_root / "alembic.ini"))


def _acquire_postgres_lock(connection, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    while True:
        acquired = connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_key)"),
            {"lock_key": MIGRATION_LOCK_KEY},
        )
        if acquired:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Timed out waiting for the control-plane migration advisory lock"
            )
        time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))


def run_upgrade(target: str = "head") -> None:
    """Run one serialized forward migration using the configured database."""
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            _run_migration_with_lock(connection, command.upgrade, target)
    finally:
        engine.dispose()


def verify() -> None:
    """Fail unless the production store schema is at the supported revision."""
    database_url = _database_url()
    PostgresControlPlaneStore(database_url).check_ready()
    # Import lazily to keep the control-plane package independent from the
    # indexing adapter during normal application imports.
    from indexing_events import PostgresIndexingEventStore

    PostgresIndexingEventStore(database_url).check_ready()


def run_downgrade(target: str) -> None:
    """Run an explicitly authorized destructive rollback for disaster recovery."""
    if os.environ.get("CONTROL_PLANE_ALLOW_DESTRUCTIVE_DOWNGRADE") != "true":
        raise RuntimeError(
            "Set CONTROL_PLANE_ALLOW_DESTRUCTIVE_DOWNGRADE=true only after a "
            "verified backup and an approved rollback decision"
        )
    engine = create_engine(_database_url(), pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            if connection.dialect.name != "postgresql":
                raise RuntimeError("Destructive downgrade is supported only on PostgreSQL")
            _run_migration_with_lock(connection, command.downgrade, target)
    finally:
        engine.dispose()


def _run_migration_with_lock(connection, operation, target: str) -> None:
    """Run and durably commit one Alembic operation under a session lock.

    PostgreSQL advisory locks are session scoped, while SQLAlchemy 2 starts a
    transaction even for the lock SELECT.  That lock transaction must be
    committed before handing the same connection to Alembic; otherwise
    Alembic joins the outer transaction and a successful-looking process rolls
    all DDL back when the connection closes.
    """
    is_postgres = connection.dialect.name == "postgresql"
    lock_acquired = False
    if is_postgres:
        _acquire_postgres_lock(connection, _positive_timeout())
        lock_acquired = True
        connection.commit()

    try:
        config = _alembic_config()
        config.attributes["connection"] = connection
        operation(config, target)
        # Alembic may have joined a transaction opened by the supplied
        # connection.  Commit explicitly so process success means durable DDL.
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        if lock_acquired:
            # Unlock must execute outside any failed migration transaction and
            # itself be committed before a pooled connection can be reused.
            connection.rollback()
            unlocked = connection.scalar(
                text("SELECT pg_advisory_unlock(:lock_key)"),
                {"lock_key": MIGRATION_LOCK_KEY},
            )
            connection.commit()
            if unlocked is not True:
                raise RuntimeError("Control-plane migration advisory lock was not held")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    upgrade = subcommands.add_parser("upgrade")
    upgrade.add_argument("target", nargs="?", default="head")
    subcommands.add_parser("verify")
    downgrade = subcommands.add_parser("downgrade")
    downgrade.add_argument("target")
    arguments = parser.parse_args()

    if arguments.command == "upgrade":
        run_upgrade(arguments.target)
    elif arguments.command == "verify":
        verify()
    else:
        run_downgrade(arguments.target)


if __name__ == "__main__":
    main()
