"""Provider-neutral resolution for runtime-only Typesense credentials.

Secret references are deliberately small configuration locators.  The raw
value is resolved only at an infrastructure boundary and is never cached, so
mounted-secret rotation and revocation are visible without reloading control-
plane state.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Protocol


_ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SCHEME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MAX_REFERENCE_LENGTH = 1024
_MAX_SECRET_BYTES = 64 * 1024


class SecretResolutionError(RuntimeError):
    """A safe-to-report secret-reference validation or resolution failure."""


class SecretProvider(Protocol):
    """Resolve one provider-specific locator to a secret value."""

    def resolve(self, locator: str) -> str:
        """Return the current value for ``locator`` or fail closed."""


def parse_secret_reference(reference: str) -> tuple[str, str]:
    """Parse and validate the provider-neutral ``scheme:locator`` envelope."""
    if not isinstance(reference, str):
        raise SecretResolutionError("Secret reference must be a string")
    if not reference or reference != reference.strip():
        raise SecretResolutionError("Secret reference must be non-empty and trimmed")
    if len(reference) > _MAX_REFERENCE_LENGTH:
        raise SecretResolutionError("Secret reference is too long")
    scheme, separator, locator = reference.partition(":")
    if not separator or not _SCHEME_PATTERN.fullmatch(scheme) or not locator:
        raise SecretResolutionError(
            "Secret reference must use the 'provider:locator' format"
        )
    if any(character in locator for character in ("\x00", "\r", "\n")):
        raise SecretResolutionError("Secret reference locator contains invalid characters")
    return scheme, locator


def _validated_secret_value(value: str, *, provider: str) -> str:
    """Reject empty or unsafe provider results without echoing their contents."""
    normalized = value.rstrip("\r\n")
    if not normalized:
        raise SecretResolutionError(f"{provider} secret is empty")
    if "\x00" in normalized:
        raise SecretResolutionError(f"{provider} secret contains invalid data")
    return normalized


class EnvSecretProvider:
    """Resolve a value from an explicitly named process environment variable."""

    def __init__(self, environ: Mapping[str, str] | None = None):
        self._environ = os.environ if environ is None else environ

    def resolve(self, locator: str) -> str:
        if not _ENV_NAME_PATTERN.fullmatch(locator):
            raise SecretResolutionError("Environment secret locator is invalid")
        value = self._environ.get(locator)
        if value is None:
            raise SecretResolutionError("Environment secret is unavailable")
        return _validated_secret_value(value, provider="Environment")


class FileSecretProvider:
    """Resolve a relative file below one configured and traversal-safe root."""

    def __init__(self, root: str | os.PathLike[str], *, max_bytes: int = _MAX_SECRET_BYTES):
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._root = Path(root).expanduser()
        self._max_bytes = max_bytes

    def resolve(self, locator: str) -> str:
        relative = Path(locator)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise SecretResolutionError("File secret locator must stay below its root")
        try:
            root = self._root.resolve(strict=True)
            candidate = (root / relative).resolve(strict=True)
            candidate.relative_to(root)
            stat = candidate.stat()
        except (FileNotFoundError, NotADirectoryError):
            raise SecretResolutionError("File secret is unavailable") from None
        except (OSError, RuntimeError, ValueError):
            raise SecretResolutionError("File secret locator is not accessible") from None
        if not candidate.is_file():
            raise SecretResolutionError("File secret locator is not a regular file")
        if stat.st_size > self._max_bytes:
            raise SecretResolutionError("File secret exceeds the configured size limit")
        try:
            data = candidate.read_bytes()
        except OSError:
            raise SecretResolutionError("File secret could not be read") from None
        if len(data) > self._max_bytes:
            raise SecretResolutionError("File secret exceeds the configured size limit")
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError:
            raise SecretResolutionError("File secret must be UTF-8 text") from None
        return _validated_secret_value(value, provider="File")


class SecretResolver:
    """Route references to an immutable registry of named secret providers."""

    def __init__(self, providers: Mapping[str, SecretProvider]):
        normalized = dict(providers)
        if not normalized:
            raise ValueError("At least one secret provider is required")
        for name in normalized:
            if not _SCHEME_PATTERN.fullmatch(name):
                raise ValueError(f"Invalid secret provider name '{name}'")
        self._providers = MappingProxyType(normalized)

    @property
    def provider_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._providers))

    def validate_reference(self, reference: str) -> None:
        scheme, _ = parse_secret_reference(reference)
        if scheme not in self._providers:
            raise SecretResolutionError("Secret reference provider is not configured")

    def resolve(self, reference: str) -> str:
        scheme, locator = parse_secret_reference(reference)
        provider = self._providers.get(scheme)
        if provider is None:
            raise SecretResolutionError("Secret reference provider is not configured")
        return provider.resolve(locator)


def build_secret_resolver(file_root: str | os.PathLike[str]) -> SecretResolver:
    """Build the supported runtime registry without introducing provider globals."""
    return SecretResolver(
        {
            "env": EnvSecretProvider(),
            "file": FileSecretProvider(file_root),
        }
    )


def validate_cluster_secret_config(
    config: Mapping[str, object],
    *,
    allow_inline: bool,
    resolver: SecretResolver | None = None,
) -> None:
    """Enforce the exactly-one and deployment-profile cluster invariant."""
    api_key = config.get("api_key")
    api_key_ref = config.get("api_key_ref")
    has_inline = isinstance(api_key, str) and bool(api_key)
    has_reference = isinstance(api_key_ref, str) and bool(api_key_ref)
    if has_inline == has_reference:
        raise SecretResolutionError(
            "Cluster configuration requires exactly one of api_key_ref or api_key"
        )
    if has_inline and not allow_inline:
        raise SecretResolutionError(
            "Inline cluster API keys are allowed only in the development profile"
        )
    if has_reference:
        parse_secret_reference(str(api_key_ref))
        if resolver is not None:
            resolver.validate_reference(str(api_key_ref))


def materialize_cluster_secret(
    config: Mapping[str, object],
    *,
    allow_inline: bool,
    resolver: SecretResolver,
) -> str:
    """Resolve one validated cluster credential without mutating its config."""
    validate_cluster_secret_config(
        config,
        allow_inline=allow_inline,
        resolver=resolver,
    )
    api_key_ref = config.get("api_key_ref")
    if isinstance(api_key_ref, str) and api_key_ref:
        return resolver.resolve(api_key_ref)
    return _validated_secret_value(str(config["api_key"]), provider="Inline")
