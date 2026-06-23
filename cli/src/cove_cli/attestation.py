from __future__ import annotations

import hashlib
from typing import Any

from cove_container_runtime.attestation import (
    PHALA_DSTACK_ATTESTATION_FORMAT,
    PHALA_DSTACK_VERIFY_URL,
    verify_attestation_bundle as _verify_runtime_attestation_bundle,
    verify_phala_dstack_quote as _verify_runtime_phala_dstack_quote,
)
from cove_container_runtime.common import RuntimeErrorBase as RuntimeAttestationError

from .common import RuntimeErrorBase, canonical_json_bytes
_KEY_RELEASE_REPORT_LABEL = b"cove_key_release_v1"
_NODE_CERTIFICATE_REPORT_LABEL = b"cove_node_certificate_v1"


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


def build_node_certificate_report_data(
    *,
    certificate_body_hash: str,
    compose_hash: str,
) -> bytes:
    return _labeled_report_data(
        label=_NODE_CERTIFICATE_REPORT_LABEL,
        payload={
            "certificate_body_hash": certificate_body_hash,
            "generated_node_compose_hash": compose_hash,
        },
    )


def verify_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
    expected_compose_hash: str,
    expected_deployed_compose_text: str | None = None,
) -> dict[str, Any]:
    try:
        return _verify_runtime_attestation_bundle(
            attestation_bundle,
            expected_report_data=expected_report_data,
            expected_compose_hash=expected_compose_hash,
            expected_deployed_compose_text=expected_deployed_compose_text,
        )
    except RuntimeAttestationError as exc:
        raise AttestationError(str(exc)) from exc


def verify_phala_dstack_quote(quote_hex: str) -> str:
    try:
        return _verify_runtime_phala_dstack_quote(quote_hex)
    except RuntimeAttestationError as exc:
        raise AttestationError(str(exc)) from exc


def _labeled_report_data(*, label: bytes, payload: dict[str, Any]) -> bytes:
    payload_hash = hashlib.sha256(canonical_json_bytes(payload)).digest()
    return label + payload_hash
