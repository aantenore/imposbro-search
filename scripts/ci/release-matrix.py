#!/usr/bin/env python3
"""Validate and emit the declarative release component matrix."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(__file__).with_name("release-components.json")
COMPONENT_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def main() -> int:
    try:
        matrix = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        print(f"Invalid release component configuration: {exc}", file=sys.stderr)
        return 1

    entries = matrix.get("include") if isinstance(matrix, dict) else None
    if not isinstance(entries, list) or not entries:
        print("release-components.json must contain a non-empty include list", file=sys.stderr)
        return 1

    errors: list[str] = []
    components: set[str] = set()
    contexts: set[str] = set()
    expected_keys = {"build_args", "component", "context"}
    for index, entry in enumerate(entries):
        label = f"include[{index}]"
        if not isinstance(entry, dict) or set(entry) != expected_keys:
            errors.append(f"{label} must contain exactly {sorted(expected_keys)}")
            continue
        component = entry["component"]
        context = entry["context"]
        build_args = entry["build_args"]
        if not isinstance(component, str) or not COMPONENT_PATTERN.fullmatch(component):
            errors.append(f"{label}.component must be a lowercase OCI-safe name")
        elif component in components:
            errors.append(f"duplicate component {component!r}")
        else:
            components.add(component)
        if not isinstance(context, str):
            errors.append(f"{label}.context must be a repository-relative path")
        else:
            pure_context = PurePosixPath(context)
            if pure_context.is_absolute() or ".." in pure_context.parts:
                errors.append(f"{label}.context cannot escape the repository")
            elif context in contexts:
                errors.append(f"duplicate build context {context!r}")
            elif not (REPO_ROOT / context / "Dockerfile").is_file():
                errors.append(f"{label}.context has no Dockerfile: {context!r}")
            else:
                contexts.add(context)
        if not isinstance(build_args, str):
            errors.append(f"{label}.build_args must be a string")
        else:
            for build_arg in build_args.splitlines():
                if not re.fullmatch(r"[A-Z_][A-Z0-9_]*=[^\r\n]*", build_arg):
                    errors.append(
                        f"{label}.build_args contains an invalid KEY=value entry"
                    )

    if errors:
        print("Release component configuration failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    print(json.dumps(matrix, separators=(",", ":"), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
