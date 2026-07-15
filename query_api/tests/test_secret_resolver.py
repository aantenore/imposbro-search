"""Security and rotation tests for provider-neutral cluster secret references."""

from pathlib import Path

import pytest

from secret_resolver import (
    EnvSecretProvider,
    FileSecretProvider,
    SecretResolutionError,
    SecretResolver,
    materialize_cluster_secret,
    validate_cluster_secret_config,
)


def test_env_provider_resolves_current_value_without_caching():
    environment = {"DATA_CLUSTER_KEY": "first"}
    resolver = SecretResolver({"env": EnvSecretProvider(environment)})

    assert resolver.resolve("env:DATA_CLUSTER_KEY") == "first"
    environment["DATA_CLUSTER_KEY"] = "second"
    assert resolver.resolve("env:DATA_CLUSTER_KEY") == "second"
    del environment["DATA_CLUSTER_KEY"]
    with pytest.raises(SecretResolutionError, match="unavailable"):
        resolver.resolve("env:DATA_CLUSTER_KEY")


def test_file_provider_observes_atomic_rotation_and_revocation(tmp_path: Path):
    secret_file = tmp_path / "data-primary"
    secret_file.write_text("first\n", encoding="utf-8")
    resolver = SecretResolver({"file": FileSecretProvider(tmp_path)})

    assert resolver.resolve("file:data-primary") == "first"
    replacement = tmp_path / "replacement"
    replacement.write_text("second\n", encoding="utf-8")
    replacement.replace(secret_file)
    assert resolver.resolve("file:data-primary") == "second"
    secret_file.unlink()
    with pytest.raises(SecretResolutionError, match="unavailable"):
        resolver.resolve("file:data-primary")


def test_file_provider_rejects_traversal_and_symlink_escape(tmp_path: Path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("not-allowed", encoding="utf-8")
    (root / "escaped").symlink_to(outside)
    resolver = SecretResolver({"file": FileSecretProvider(root)})

    with pytest.raises(SecretResolutionError):
        resolver.resolve("file:../outside")
    with pytest.raises(SecretResolutionError):
        resolver.resolve("file:escaped")


def test_file_provider_rejects_oversized_and_non_utf8_values(tmp_path: Path):
    oversized = tmp_path / "oversized"
    oversized.write_text("x" * 9, encoding="utf-8")
    binary = tmp_path / "binary"
    binary.write_bytes(b"\xff")
    resolver = SecretResolver(
        {"file": FileSecretProvider(tmp_path, max_bytes=8)}
    )

    with pytest.raises(SecretResolutionError, match="size limit"):
        resolver.resolve("file:oversized")
    with pytest.raises(SecretResolutionError, match="UTF-8"):
        SecretResolver({"file": FileSecretProvider(tmp_path)}).resolve(
            "file:binary"
        )


def test_cluster_secret_policy_is_exactly_one_and_inline_is_development_only():
    resolver = SecretResolver({"env": EnvSecretProvider({"KEY": "resolved"})})

    with pytest.raises(SecretResolutionError, match="exactly one"):
        validate_cluster_secret_config({}, allow_inline=True)
    with pytest.raises(SecretResolutionError, match="exactly one"):
        validate_cluster_secret_config(
            {"api_key": "raw", "api_key_ref": "env:KEY"},
            allow_inline=True,
        )
    with pytest.raises(SecretResolutionError, match="development"):
        validate_cluster_secret_config(
            {"api_key": "raw"},
            allow_inline=False,
        )
    assert materialize_cluster_secret(
        {"api_key_ref": "env:KEY"},
        allow_inline=False,
        resolver=resolver,
    ) == "resolved"


def test_registry_fails_closed_for_unknown_provider():
    resolver = SecretResolver({"env": EnvSecretProvider({})})
    with pytest.raises(SecretResolutionError, match="not configured"):
        resolver.resolve("vault:path/to/key")
