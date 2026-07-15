#!/usr/bin/env python3
"""Reject backward-incompatible changes to the released OpenAPI v1 surface."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
PREVIOUS = REPO_ROOT / "contracts" / "openapi-v1.previous.json"
CURRENT = REPO_ROOT / "contracts" / "openapi-v1.json"
HTTP_METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _error(errors: list[str], location: str, message: str) -> None:
    errors.append(f"{location}: {message}")


def _schema_compatible(old: Any, new: Any, location: str, errors: list[str]) -> None:
    if not isinstance(old, dict):
        if old != new:
            _error(errors, location, "schema value changed")
        return
    if not isinstance(new, dict):
        _error(errors, location, "schema object was removed")
        return

    for keyword in ("type", "format", "$ref", "const", "pattern"):
        if keyword in old and new.get(keyword) != old[keyword]:
            _error(errors, location, f"{keyword} changed")

    if "enum" in old:
        old_enum = set(json.dumps(item, sort_keys=True) for item in old["enum"])
        new_enum = set(json.dumps(item, sort_keys=True) for item in new.get("enum", []))
        if not old_enum.issubset(new_enum):
            _error(errors, location, "enum values were removed")

    old_required = set(old.get("required", []))
    new_required = set(new.get("required", []))
    if added := sorted(new_required - old_required):
        _error(errors, location, f"new required fields: {', '.join(added)}")

    old_properties = old.get("properties", {})
    new_properties = new.get("properties", {})
    if isinstance(old_properties, dict):
        if not isinstance(new_properties, dict):
            _error(errors, location, "properties were removed")
        else:
            for name, old_property in old_properties.items():
                child = f"{location}.properties.{name}"
                if name not in new_properties:
                    _error(errors, child, "property was removed")
                else:
                    _schema_compatible(old_property, new_properties[name], child, errors)

    if "items" in old:
        _schema_compatible(old["items"], new.get("items"), f"{location}.items", errors)

    for minimum in ("minimum", "exclusiveMinimum", "minLength", "minItems", "minProperties"):
        if minimum in new and (minimum not in old or new[minimum] > old[minimum]):
            _error(errors, location, f"{minimum} became stricter")
    for maximum in ("maximum", "exclusiveMaximum", "maxLength", "maxItems", "maxProperties"):
        if maximum in new and (maximum not in old or new[maximum] < old[maximum]):
            _error(errors, location, f"{maximum} became stricter")

    old_additional = old.get("additionalProperties", True)
    new_additional = new.get("additionalProperties", True)
    if old_additional is not False and new_additional is False:
        _error(errors, location, "additional properties are no longer accepted")

    for composition in ("allOf", "anyOf", "oneOf", "not"):
        if composition in old and new.get(composition) != old[composition]:
            _error(errors, location, f"{composition} changed; review as a new API major")


def _parameters(item: dict[str, Any], operation: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for parameter in [*item.get("parameters", []), *operation.get("parameters", [])]:
        if isinstance(parameter, dict) and "name" in parameter and "in" in parameter:
            result[(str(parameter["name"]), str(parameter["in"]))] = parameter
    return result


def _operation_compatible(
    path: str,
    method: str,
    old_item: dict[str, Any],
    new_item: dict[str, Any],
    errors: list[str],
) -> None:
    old = old_item[method]
    new = new_item[method]
    location = f"paths.{path}.{method}"
    if old.get("operationId") != new.get("operationId"):
        _error(errors, location, "operationId changed")
    if old.get("security", []) != new.get("security", []):
        _error(errors, location, "security requirements changed")

    old_parameters = _parameters(old_item, old)
    new_parameters = _parameters(new_item, new)
    for identity, old_parameter in old_parameters.items():
        parameter_location = f"{location}.parameters.{identity[1]}:{identity[0]}"
        new_parameter = new_parameters.get(identity)
        if new_parameter is None:
            _error(errors, parameter_location, "parameter was removed")
            continue
        if not old_parameter.get("required", False) and new_parameter.get("required", False):
            _error(errors, parameter_location, "optional parameter became required")
        for keyword in ("style", "explode", "allowEmptyValue"):
            if keyword in old_parameter and new_parameter.get(keyword) != old_parameter[keyword]:
                _error(errors, parameter_location, f"{keyword} changed")
        _schema_compatible(
            old_parameter.get("schema", {}),
            new_parameter.get("schema", {}),
            f"{parameter_location}.schema",
            errors,
        )
    for identity, new_parameter in new_parameters.items():
        if identity not in old_parameters and new_parameter.get("required", False):
            _error(errors, location, f"new required parameter {identity[1]}:{identity[0]}")

    old_body = old.get("requestBody")
    new_body = new.get("requestBody")
    if old_body is not None:
        if not isinstance(new_body, dict):
            _error(errors, f"{location}.requestBody", "request body was removed")
        else:
            if not old_body.get("required", False) and new_body.get("required", False):
                _error(errors, f"{location}.requestBody", "request body became required")
            for media_type, old_media in old_body.get("content", {}).items():
                new_media = new_body.get("content", {}).get(media_type)
                if new_media is None:
                    _error(errors, f"{location}.requestBody.{media_type}", "media type was removed")
                else:
                    _schema_compatible(
                        old_media.get("schema", {}),
                        new_media.get("schema", {}),
                        f"{location}.requestBody.{media_type}.schema",
                        errors,
                    )

    for status, old_response in old.get("responses", {}).items():
        response_location = f"{location}.responses.{status}"
        new_response = new.get("responses", {}).get(status)
        if new_response is None:
            _error(errors, response_location, "response was removed")
            continue
        for media_type, old_media in old_response.get("content", {}).items():
            new_media = new_response.get("content", {}).get(media_type)
            if new_media is None:
                _error(errors, f"{response_location}.{media_type}", "media type was removed")
            else:
                _schema_compatible(
                    old_media.get("schema", {}),
                    new_media.get("schema", {}),
                    f"{response_location}.{media_type}.schema",
                    errors,
                )


def compatibility_errors(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    old_paths = old.get("paths", {})
    new_paths = new.get("paths", {})
    for path, old_item in old_paths.items():
        new_item = new_paths.get(path)
        if not isinstance(new_item, dict):
            _error(errors, f"paths.{path}", "path was removed")
            continue
        for method in HTTP_METHODS.intersection(old_item):
            if method not in new_item:
                _error(errors, f"paths.{path}.{method}", "operation was removed")
            else:
                _operation_compatible(path, method, old_item, new_item, errors)

    old_schemas = old.get("components", {}).get("schemas", {})
    new_schemas = new.get("components", {}).get("schemas", {})
    for name, old_schema in old_schemas.items():
        if name not in new_schemas:
            _error(errors, f"components.schemas.{name}", "schema was removed")
        else:
            _schema_compatible(
                old_schema,
                new_schemas[name],
                f"components.schemas.{name}",
                errors,
            )
    return errors


def main() -> int:
    if not PREVIOUS.exists() or not CURRENT.exists():
        print("Both previous and current OpenAPI v1 contracts are required.", file=sys.stderr)
        return 1
    old = json.loads(PREVIOUS.read_text(encoding="utf-8"))
    new = json.loads(CURRENT.read_text(encoding="utf-8"))
    errors = compatibility_errors(old, new)
    if errors:
        print("Backward-incompatible OpenAPI v1 changes detected:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("OpenAPI v1 is backward compatible with the previous release baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
