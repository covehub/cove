from __future__ import annotations

import base64
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from .common import RuntimeErrorBase, canonical_json_bytes, sha256_literal


OWNER_IDENTITY_VERSION = 2
OWNER_IDENTITY_PURPOSE = "cove_owner_identity_v2"
OWNER_KEY_RELEASE_RESPONSE_PURPOSE = "cove_owner_key_release_response_v1"
OWNER_RESPONSE_SIGNATURE_ALGORITHM_FIELD = "owner_response_signature_algorithm"
OWNER_RESPONSE_SIGNATURE_FIELD = "owner_response_signature"


class OwnerIdentityError(RuntimeErrorBase):
    """Raised when an owner identity document is malformed or untrusted."""


def owner_identity_signature_payload(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": OWNER_IDENTITY_VERSION,
        "purpose": OWNER_IDENTITY_PURPOSE,
        "owner_url": _required_string(document, "owner_url"),
        "owner_domain": _required_string(document, "owner_domain"),
        "owner_public_key_pem": _required_string(document, "owner_public_key_pem"),
        "owner_public_key_sha256": _required_string(document, "owner_public_key_sha256"),
        "not_before": _required_string(document, "not_before"),
        "not_after": _required_string(document, "not_after"),
    }


def verify_owner_identity_document(
    document: dict[str, Any],
    *,
    expected_owner_url: str | None = None,
    expected_owner_domain: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    version = document.get("version")
    if version != OWNER_IDENTITY_VERSION:
        raise OwnerIdentityError("owner identity version is unsupported")

    owner_url = _required_string(document, "owner_url").rstrip("/")
    if expected_owner_url is not None and owner_url != expected_owner_url.rstrip("/"):
        raise OwnerIdentityError(
            f"owner identity owner_url {owner_url!r} does not match expected {expected_owner_url.rstrip('/')!r}"
        )

    owner_domain = _required_string(document, "owner_domain")
    parsed_owner_url = urlparse(owner_url)
    if parsed_owner_url.scheme not in {"http", "https"} or not parsed_owner_url.hostname:
        raise OwnerIdentityError("owner identity owner_url must be an HTTP or HTTPS origin URL")
    if parsed_owner_url.hostname.lower() != owner_domain:
        raise OwnerIdentityError(
            f"owner identity owner_url hostname {parsed_owner_url.hostname!r} does not match owner_domain {owner_domain!r}"
        )
    if expected_owner_domain is not None and owner_domain != expected_owner_domain:
        raise OwnerIdentityError(
            f"owner identity owner_domain {owner_domain!r} does not match expected {expected_owner_domain!r}"
        )

    not_before = _parse_utc_datetime(_required_string(document, "not_before"), "not_before")
    not_after = _parse_utc_datetime(_required_string(document, "not_after"), "not_after")
    current_time = now or datetime.now(timezone.utc)
    if current_time < not_before:
        raise OwnerIdentityError("owner identity is not valid yet")
    if current_time > not_after:
        raise OwnerIdentityError("owner identity is expired")

    owner_public_key_pem = _required_string(document, "owner_public_key_pem")
    owner_public_key_hash = _required_string(document, "owner_public_key_sha256")
    observed_public_key_hash = sha256_literal(owner_public_key_pem.encode("utf-8"))
    if observed_public_key_hash != owner_public_key_hash:
        raise OwnerIdentityError("owner identity public key hash does not match owner_public_key_pem")
    public_key = load_pem_public_key(owner_public_key_pem.encode("utf-8"))
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise OwnerIdentityError("owner public key must be Ed25519")

    signature_algorithm = _required_string(document, "signature_algorithm")
    if signature_algorithm != "ed25519":
        raise OwnerIdentityError("owner identity signature_algorithm must be ed25519")
    try:
        signature = base64.b64decode(
            _required_string(document, "signature").encode("ascii"),
            validate=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise OwnerIdentityError("owner identity signature is not valid base64") from exc

    payload = owner_identity_signature_payload({**document, "owner_url": owner_url})
    try:
        public_key.verify(signature, canonical_json_bytes(payload))
    except Exception as exc:  # pragma: no cover - cryptography specifics vary
        raise OwnerIdentityError("owner identity signature verification failed") from exc

    return {**document, "owner_url": owner_url}


def owner_response_signature_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "purpose": OWNER_KEY_RELEASE_RESPONSE_PURPOSE,
        "payload": {
            key: value
            for key, value in payload.items()
            if key not in {OWNER_RESPONSE_SIGNATURE_ALGORITHM_FIELD, OWNER_RESPONSE_SIGNATURE_FIELD}
        },
    }


def verify_owner_signed_response_payload(
    payload: dict[str, Any],
    *,
    owner_identity: dict[str, Any],
) -> dict[str, Any]:
    verified_identity = verify_owner_identity_document(owner_identity)
    owner_public_key_pem = _required_string(verified_identity, "owner_public_key_pem")
    public_key = load_pem_public_key(owner_public_key_pem.encode("utf-8"))
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise OwnerIdentityError("owner public key must be Ed25519")
    signature_algorithm = _required_string(payload, OWNER_RESPONSE_SIGNATURE_ALGORITHM_FIELD)
    if signature_algorithm != "ed25519":
        raise OwnerIdentityError("owner response signature algorithm must be ed25519")
    try:
        signature = base64.b64decode(
            _required_string(payload, OWNER_RESPONSE_SIGNATURE_FIELD).encode("ascii"),
            validate=True,
        )
    except Exception as exc:  # pragma: no cover - defensive
        raise OwnerIdentityError("owner response signature is not valid base64") from exc
    try:
        public_key.verify(signature, canonical_json_bytes(owner_response_signature_payload(payload)))
    except Exception as exc:  # pragma: no cover - cryptography specifics vary
        raise OwnerIdentityError("owner response signature verification failed") from exc
    return payload


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise OwnerIdentityError(f"owner identity {key} must be a non-empty string")
    return value


def _parse_utc_datetime(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OwnerIdentityError(f"owner identity {label} must be an ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None:
        raise OwnerIdentityError(f"owner identity {label} must include a timezone")
    return parsed.astimezone(timezone.utc)
