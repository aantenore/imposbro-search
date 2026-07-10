#!/usr/bin/env python3
"""Workspace wrapper for the packaged audit delivery worker."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "query_api" / "app"))

from control_plane.audit_delivery_worker import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
