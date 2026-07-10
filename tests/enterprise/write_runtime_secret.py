#!/usr/bin/env python3
"""Initialize the mutable E2E secret volume without printing secret material."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def _integer_env(name: str) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw.isdigit():
        raise SystemExit(f"{name} must be a non-negative integer")
    return int(raw)


def main() -> int:
    raw_destination = os.environ.get("E2E_RUNTIME_SECRET_FILE", "").strip()
    if not raw_destination or not Path(raw_destination).is_absolute():
        raise SystemExit("E2E_RUNTIME_SECRET_FILE must be an absolute path")
    destination = Path(raw_destination).resolve()
    value = os.environ.get("E2E_RUNTIME_SECRET_VALUE", "")
    if not destination.name or not value or "\x00" in value:
        raise SystemExit("Runtime secret destination and value are required")
    destination.parent.mkdir(parents=True, exist_ok=True)
    uid = _integer_env("E2E_RUNTIME_SECRET_WRITER_UID")
    gid = _integer_env("E2E_RUNTIME_SECRET_WRITER_GID")
    os.chown(destination.parent, uid, gid)
    destination.parent.chmod(0o755)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, uid, gid)
        temporary.chmod(0o444)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
