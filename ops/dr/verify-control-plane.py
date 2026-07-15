#!/usr/bin/env python3
"""Verify restored state integrity and the complete audit hash chain."""

from __future__ import annotations

import os

from control_plane.postgres_store import PostgresControlPlaneStore


database_url = os.environ.get("CONTROL_PLANE_DATABASE_URL", "").strip()
if not database_url:
    raise SystemExit("CONTROL_PLANE_DATABASE_URL is required")

store = PostgresControlPlaneStore(database_url)
store.check_ready()
if store.load() is None:
    raise SystemExit("control-plane state is missing")
store.verify_audit_chain()
print("control-plane state and audit chain verified")
