from __future__ import annotations

import base64
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib import request as urllib_request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

from .common import canonical_json_bytes, sha256_literal
from .owner_identity import (
    OWNER_IDENTITY_VERSION,
    OwnerIdentityError,
    owner_identity_signature_payload,
    verify_owner_identity_document,
)

from .covehub import COVEHUB_USER_AGENT


DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DEFAULT_COVEHUB_SERVER_URL = "https://api.covehub.io"


def ensure_owner_domain(value: str, label: str = "owner domain") -> str:
    normalized = value.strip().lower()
    labels = normalized.split(".")
    if len(labels) < 2 or any(DNS_LABEL_RE.fullmatch(label_part) is None for label_part in labels):
        raise ValueError(
            f"{label} must be a lowercase DNS hostname with at least one dot"
        )
    return normalized


def normalize_owner_server_url(
    value: str | None = None,
) -> str:
    if value is None or not value.strip():
        raise ValueError("owner_server_url must be configured explicitly")
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("owner_server_url must be an http or https origin URL")
    owner_domain_from_url(normalized)
    return normalized


def owner_domain_from_url(owner_url: str) -> str:
    parsed = urlparse(owner_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("owner_server_url must be an http or https origin URL")
    return ensure_owner_domain(parsed.hostname)


def ensure_owner_signing_key_material(
    *,
    private_key_path: Path,
    public_key_path: Path,
) -> None:
    if private_key_path.exists() and public_key_path.exists():
        return
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    public_key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    private_key_path.chmod(0o600)
    public_key_path.chmod(0o644)


def build_owner_identity_document(
    *,
    owner_url: str,
    owner_private_key_path: Path,
    owner_public_key_path: Path,
) -> dict[str, object]:
    owner_private_key = _load_owner_private_key(owner_private_key_path)
    owner_public_key_pem = owner_public_key_path.read_text(encoding="utf-8")
    owner_url = normalize_owner_server_url(owner_url)
    owner_domain = owner_domain_from_url(owner_url)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    document: dict[str, object] = {
        "version": OWNER_IDENTITY_VERSION,
        "owner_url": owner_url,
        "owner_domain": owner_domain,
        "owner_public_key_pem": owner_public_key_pem,
        "owner_public_key_sha256": sha256_literal(owner_public_key_pem.encode("utf-8")),
        "not_before": _format_utc(now - timedelta(minutes=1)),
        "not_after": _format_utc(now + timedelta(days=3650)),
        "signature_algorithm": "ed25519",
    }
    signature_payload = owner_identity_signature_payload(document)
    signature = owner_private_key.sign(canonical_json_bytes(signature_payload))
    document["signature"] = base64.b64encode(signature).decode("ascii")
    return document


def fetch_owner_identity_document(
    *,
    owner_url: str,
    expected_owner_domain: str | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    owner_url = normalize_owner_server_url(owner_url)
    identity_url = f"{owner_url.rstrip('/')}/identity"
    request = urllib_request.Request(
        identity_url,
        headers={
            "Accept": "application/json",
            "User-Agent": COVEHUB_USER_AGENT,
        },
        method="GET",
    )
    with urllib_request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise OwnerIdentityError(f"owner identity endpoint returned a non-object payload: {identity_url}")
    return verify_owner_identity_document(
        payload,
        expected_owner_url=owner_url,
        expected_owner_domain=expected_owner_domain or owner_domain_from_url(owner_url),
    )


def fetch_owner_identity_for_write(
    *,
    owner_url: str,
    owner_private_key_path: Path,
    expected_owner_domain: str | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    identity = fetch_owner_identity_document(
        owner_url=owner_url,
        expected_owner_domain=expected_owner_domain,
        timeout=timeout,
    )
    served_public_key_pem = identity.get("owner_public_key_pem")
    if not isinstance(served_public_key_pem, str) or not served_public_key_pem.strip():
        raise OwnerIdentityError("served owner identity public key must be a non-empty string")
    served_public_key = load_pem_public_key(served_public_key_pem.encode("utf-8"))
    if not isinstance(served_public_key, ed25519.Ed25519PublicKey):
        raise OwnerIdentityError("served owner identity public key must be Ed25519")
    local_public_key = _load_owner_private_key(owner_private_key_path).public_key()
    if _raw_owner_public_key(local_public_key) != _raw_owner_public_key(served_public_key):
        raise OwnerIdentityError(
            "served owner identity public key does not match the local owner signing key"
        )
    return identity


def _raw_owner_public_key(public_key: ed25519.Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _load_owner_private_key(path: Path) -> ed25519.Ed25519PrivateKey:
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError(f"owner private key at {path} must be Ed25519")
    return key


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
