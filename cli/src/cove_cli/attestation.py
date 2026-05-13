from __future__ import annotations

import hashlib
from typing import Any

from .common import RuntimeErrorBase, canonical_json_bytes, http_post_json


PHALA_DSTACK_ATTESTATION_FORMAT = "phala_dstack_v1"
PHALA_DSTACK_VERIFY_URL = "https://cloud-api.phala.network/api/v1/attestations/verify"
_KEY_RELEASE_REPORT_LABEL = b"cove_key_release_v1"


class AttestationError(RuntimeErrorBase):
    """Raised when a key-release attestation is malformed or untrusted."""


def build_key_release_report_data(
    *,
    workflow_publisher_domain: str,
    workflow_id: str,
    node_id: str,
    compose_hash: str,
    artifact_provisioner_digest: str,
) -> bytes:
    return _labeled_report_data(
        label=_KEY_RELEASE_REPORT_LABEL,
        payload={
            "workflow_publisher_domain": workflow_publisher_domain,
            "workflow_id": workflow_id,
            "node_id": node_id,
            "compose_hash": compose_hash,
            "artifact_provisioner_digest": artifact_provisioner_digest,
        },
    )


def verify_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
) -> dict[str, Any]:
    attestation_format = _required_string(
        attestation_bundle.get("format"),
        "attestation_bundle.format",
    )
    if attestation_format == PHALA_DSTACK_ATTESTATION_FORMAT:
        _verify_phala_dstack_attestation_bundle(
            attestation_bundle,
            expected_report_data=expected_report_data,
        )
        return attestation_bundle
    raise AttestationError(f"unsupported attestation format: {attestation_format}")


def verify_phala_dstack_quote(quote_hex: str) -> str:
    response_payload = http_post_json(
        url=PHALA_DSTACK_VERIFY_URL,
        payload={"hex": quote_hex},
        timeout=30.0,
    )
    success = response_payload.get("success")
    quote_payload = response_payload.get("quote")
    if success is not True or not isinstance(quote_payload, dict):
        raise AttestationError("quote verification failed")
    if quote_payload.get("verified") is not True:
        raise AttestationError("quote verification did not verify successfully")
    body = quote_payload.get("body")
    if not isinstance(body, dict):
        raise AttestationError("quote verification response is missing quote.body")
    report_data = body.get("reportdata")
    if not isinstance(report_data, str) or not report_data.strip():
        raise AttestationError("quote verification response is missing body.reportdata")
    return report_data.strip()


def _labeled_report_data(*, label: bytes, payload: dict[str, Any]) -> bytes:
    payload_hash = hashlib.sha256(canonical_json_bytes(payload)).digest()
    return label + payload_hash


def _verify_phala_dstack_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
) -> None:
    quote = _required_string(attestation_bundle.get("quote"), "attestation_bundle.quote")
    report_data = _required_string(
        attestation_bundle.get("report_data"),
        "attestation_bundle.report_data",
    )
    if not _report_data_matches(report_data, expected_report_data):
        raise AttestationError("attestation_bundle.report_data does not match expected report data")

    verified_report_data = verify_phala_dstack_quote(quote)
    if not _report_data_matches(verified_report_data, expected_report_data):
        raise AttestationError("verified quote report data does not match expected report data")


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttestationError(f"{label} must be a non-empty string")
    return value.strip()


def _normalize_hex(value: str) -> str:
    lowered = value.strip().lower()
    if lowered.startswith("0x"):
        return lowered[2:]
    return lowered


def _report_data_matches(observed_hex: str, expected_report_data: bytes) -> bool:
    observed = _normalize_hex(observed_hex)
    expected = expected_report_data.hex()
    if observed == expected:
        return True
    if len(expected_report_data) < 64:
        return observed == expected_report_data.ljust(64, b"\0").hex()
    return False
