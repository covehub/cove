from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from urllib import request as urllib_request

from cove_container_runtime.common import canonical_json_bytes, sha256_literal
from cove_container_runtime.owner_identity import (
    OwnerIdentityError,
    verify_owner_identity_document,
)
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_public_key


WRITE_AUTH_PURPOSE = "covehub_domain_write_v1"
WRITE_SIGNATURE_ALGORITHM = "ed25519"
WRITE_TIMESTAMP_SKEW = timedelta(minutes=5)


class WriteAuthError(ValueError):
    """Raised when a Covehub domain write proof is missing or invalid."""


def verify_domain_write(
    *,
    method: str,
    path: str,
    payload: bytes,
    owner_domain: str,
    headers,
    now: datetime | None = None,
) -> None:
    identity = _owner_identity_from_header(
        _required_header(headers, "x-cove-owner-identity")
    )
    try:
        verified_identity = verify_owner_identity_document(
            identity,
            expected_owner_domain=owner_domain,
            now=now,
        )
    except OwnerIdentityError as exc:
        raise WriteAuthError(str(exc)) from exc

    served_identity = _fetch_current_owner_identity(
        owner_url=_required_string(verified_identity, "owner_url"),
        owner_domain=owner_domain,
        now=now,
    )
    identity_hash = sha256_literal(canonical_json_bytes(verified_identity))
    request_public_key = _load_owner_public_key(verified_identity)
    served_public_key = _load_owner_public_key(served_identity)
    if _raw_public_key_bytes(request_public_key) != _raw_public_key_bytes(served_public_key):
        raise WriteAuthError("owner identity public key does not match the current served identity")

    timestamp = _parse_timestamp(_required_header(headers, "x-cove-write-timestamp"))
    current_time = now or datetime.now(timezone.utc)
    if abs(current_time - timestamp) > WRITE_TIMESTAMP_SKEW:
        raise WriteAuthError("write signature timestamp is outside the allowed freshness window")

    signature_algorithm = _required_header(headers, "x-cove-write-signature-algorithm")
    if signature_algorithm != WRITE_SIGNATURE_ALGORITHM:
        raise WriteAuthError("write signature algorithm must be ed25519")
    signature = _decode_base64(_required_header(headers, "x-cove-write-signature"), "write signature")

    payload_hash = sha256_literal(payload)
    signature_payload = domain_write_signature_payload(
        method=method,
        path=path,
        payload_hash=payload_hash,
        owner_domain=owner_domain,
        owner_identity_hash=identity_hash,
        timestamp=_format_utc(timestamp),
    )
    try:
        served_public_key.verify(signature, canonical_json_bytes(signature_payload))
    except Exception as exc:  # pragma: no cover - cryptography specifics vary
        raise WriteAuthError("write signature verification failed") from exc


def domain_write_signature_payload(
    *,
    method: str,
    path: str,
    payload_hash: str,
    owner_domain: str,
    owner_identity_hash: str,
    timestamp: str,
) -> dict[str, object]:
    return {
        "purpose": WRITE_AUTH_PURPOSE,
        "method": method.upper(),
        "path": path,
        "payload_hash": payload_hash,
        "owner_domain": owner_domain,
        "owner_identity_hash": owner_identity_hash,
        "timestamp": timestamp,
    }


def _fetch_current_owner_identity(
    *,
    owner_url: str,
    owner_domain: str,
    now: datetime | None,
) -> dict[str, object]:
    request = urllib_request.Request(
        f"{owner_url.rstrip('/')}/identity",
        headers={"Accept": "application/json", "User-Agent": "covehub-server/0.2.0"},
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise WriteAuthError(f"failed to fetch current owner identity: {exc}") from exc
    if not isinstance(payload, dict):
        raise WriteAuthError("owner identity endpoint returned a non-object payload")
    try:
        return verify_owner_identity_document(
            payload,
            expected_owner_url=owner_url,
            expected_owner_domain=owner_domain,
            now=now,
        )
    except OwnerIdentityError as exc:
        raise WriteAuthError(str(exc)) from exc


def _load_owner_public_key(identity: dict[str, object]) -> ed25519.Ed25519PublicKey:
    public_key = load_pem_public_key(
        _required_string(identity, "owner_public_key_pem").encode("utf-8")
    )
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise WriteAuthError("owner public key must be Ed25519")
    return public_key


def _raw_public_key_bytes(public_key: ed25519.Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _owner_identity_from_header(value: str) -> dict[str, object]:
    try:
        decoded = _decode_base64url(value).decode("utf-8")
        payload = json.loads(decoded)
    except Exception as exc:
        raise WriteAuthError("owner identity header must be base64url JSON") from exc
    if not isinstance(payload, dict):
        raise WriteAuthError("owner identity header must decode to a JSON object")
    return payload


def _required_header(headers, name: str) -> str:
    value = headers.get(name)
    if not isinstance(value, str) or not value.strip():
        raise WriteAuthError(f"missing {name} header")
    return value.strip()


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WriteAuthError(f"owner identity {key} must be a non-empty string")
    return value.strip()


def _decode_base64(value: str, label: str) -> bytes:
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        raise WriteAuthError(f"{label} must be valid base64") from exc


def _decode_base64url(value: str) -> bytes:
    encoded = value.encode("ascii")
    padding = b"=" * (-len(encoded) % 4)
    return base64.urlsafe_b64decode(encoded + padding)


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise WriteAuthError("write timestamp must be ISO-8601 UTC") from exc
    if parsed.tzinfo is None:
        raise WriteAuthError("write timestamp must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0)


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
