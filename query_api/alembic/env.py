"""Alembic environment for the authoritative control-plane schema."""

from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool


QUERY_API_ROOT = Path(__file__).resolve().parents[1]
if str(QUERY_API_ROOT) not in sys.path:
    sys.path.insert(0, str(QUERY_API_ROOT))

from app.control_plane.schema import metadata  # noqa: E402


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = metadata


def _database_url() -> str:
    value = os.environ.get("CONTROL_PLANE_DATABASE_URL", "").strip()
    if value:
        return value
    configured = config.get_main_option("sqlalchemy.url").strip()
    if configured:
        return configured
    raise RuntimeError(
        "CONTROL_PLANE_DATABASE_URL or Alembic sqlalchemy.url is required"
    )


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    provided_connection = config.attributes.get("connection")
    if provided_connection is not None:
        _run_with_connection(provided_connection)
        return

    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        _run_with_connection(connection)


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
