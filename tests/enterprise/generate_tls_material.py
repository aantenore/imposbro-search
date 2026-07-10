#!/usr/bin/env python3
"""Generate a short-lived private CA and service TLS certificate for E2E only."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def write(path: Path, payload: bytes, mode: int) -> None:
    path.write_bytes(payload)
    path.chmod(mode)


def base64url_uint(value: int) -> str:
    raw = value.to_bytes(max(1, (value.bit_length() + 7) // 8), "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def main() -> None:
    output = Path(os.environ.get("E2E_TLS_OUTPUT_DIR", "/tls"))
    output.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "IMPOSBRO E2E CA")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=False,
                key_cert_sign=True,
                key_agreement=False,
                content_commitment=False,
                data_encipherment=False,
                encipher_only=False,
                decipher_only=False,
                crl_sign=True,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "typesense-tls")]
    )
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("typesense-tls"),
                    x509.DNSName("otel-capture"),
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    write(output / "ca.crt", ca_cert.public_bytes(serialization.Encoding.PEM), 0o644)
    write(
        output / "server.crt",
        server_cert.public_bytes(serialization.Encoding.PEM),
        0o644,
    )
    write(
        output / "server.key",
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o600,
    )
    oidc_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    oidc_public_der = oidc_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    oidc_key_id = hashlib.sha256(oidc_public_der).hexdigest()[:32]
    oidc_numbers = oidc_key.public_key().public_numbers()
    jwks = {
        "keys": [
            {
                "alg": "RS256",
                "e": base64url_uint(oidc_numbers.e),
                "kid": oidc_key_id,
                "kty": "RSA",
                "n": base64url_uint(oidc_numbers.n),
                "use": "sig",
            }
        ]
    }
    oidc_private_path = output / "oidc-private.pem"
    write(
        oidc_private_path,
        oidc_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        0o600,
    )
    try:
        reader_uid = int(os.environ.get("E2E_TLS_READER_UID", str(os.getuid())))
        reader_gid = int(os.environ.get("E2E_TLS_READER_GID", str(os.getgid())))
    except ValueError as exc:
        raise RuntimeError("E2E TLS reader uid/gid must be integers") from exc
    if reader_uid < 0 or reader_gid < 0:
        raise RuntimeError("E2E TLS reader uid/gid must not be negative")
    os.chown(oidc_private_path, reader_uid, reader_gid)
    write(
        output / "oidc-jwks.json",
        (json.dumps(jwks, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        ),
        0o644,
    )


if __name__ == "__main__":
    main()
