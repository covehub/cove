from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .common import RuntimeErrorBase, canonical_json_bytes, http_post_json


PHALA_DSTACK_ATTESTATION_FORMAT = "phala_dstack_v1"
PHALA_DSTACK_VERIFY_URL = "https://cloud-api.phala.network/api/v1/attestations/verify"
_NODE_CERTIFICATE_REPORT_LABEL = b"cove_node_certificate_v1"
_KEY_RELEASE_REPORT_LABEL = b"cove_key_release_v1"
_RUNTIME_ARTIFACT_REPORT_LABEL = b"cove_runtime_artifact_v1"


class AttestationError(RuntimeErrorBase):
    """Raised when runtime attestation collection or verification fails."""


@dataclass(frozen=True, slots=True)
class AttestationSettings:
    mode: str
    provider: str | None
    runtime: str | None


def load_attestation_settings(config: dict[str, object]) -> AttestationSettings:
    raw_attestation = config.get("attestation")
    if raw_attestation is None:
        return AttestationSettings(mode="phala_dstack", provider="phala", runtime="dstack")
    if not isinstance(raw_attestation, dict):
        raise AttestationError("attestation must be an object when present")
    raw_mode = raw_attestation.get("mode", "phala_dstack")
    if not isinstance(raw_mode, str) or not raw_mode.strip():
        raise AttestationError("attestation.mode must be a non-empty string")
    mode = raw_mode.strip()
    if mode != "phala_dstack":
        raise AttestationError(f"unsupported attestation mode: {mode}")
    provider = _optional_string(raw_attestation.get("provider"), "attestation.provider")
    runtime = _optional_string(raw_attestation.get("runtime"), "attestation.runtime")
    return AttestationSettings(mode=mode, provider=provider, runtime=runtime)


def collect_attestation_bundle(
    settings: AttestationSettings,
    *,
    report_data: bytes,
) -> dict[str, Any]:
    if settings.mode != "phala_dstack":
        raise AttestationError(f"unsupported attestation mode: {settings.mode}")
    if settings.provider != "phala" or settings.runtime != "dstack":
        raise AttestationError(
            "phala_dstack attestation mode requires provider=phala and runtime=dstack"
        )
    try:
        from dstack_sdk import DstackClient
    except ImportError as exc:  # pragma: no cover - depends on runtime environment
        raise AttestationError(
            "dstack_sdk is required for phala_dstack attestation mode"
        ) from exc

    client = DstackClient()
    quote = client.get_quote(report_data)
    return {
        "format": PHALA_DSTACK_ATTESTATION_FORMAT,
        "quote": quote.quote,
        "event_log": quote.event_log,
        "vm_config": quote.vm_config,
        "report_data": quote.report_data,
        "info": client.info().model_dump(),
    }


def verify_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
) -> dict[str, Any]:
    attestation_format = _required_string(attestation_bundle.get("format"), "attestation_bundle.format")
    if attestation_format == PHALA_DSTACK_ATTESTATION_FORMAT:
        _verify_phala_dstack_attestation_bundle(
            attestation_bundle,
            expected_report_data=expected_report_data,
        )
        return attestation_bundle
    raise AttestationError(f"unsupported attestation format: {attestation_format}")


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


def build_runtime_artifact_report_data(
    *,
    workflow_id: str,
    node_id: str,
    compose_hash: str,
    artifact_name: str,
) -> bytes:
    return _labeled_report_data(
        label=_RUNTIME_ARTIFACT_REPORT_LABEL,
        payload={
            "workflow_id": workflow_id,
            "node_id": node_id,
            "compose_hash": compose_hash,
            "artifact_name": artifact_name,
        },
    )


def _labeled_report_data(*, label: bytes, payload: dict[str, Any]) -> bytes:
    payload_hash = hashlib.sha256(canonical_json_bytes(payload)).digest()
    return label + payload_hash

def _verify_phala_dstack_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
) -> None:
    quote = _required_string(attestation_bundle.get("quote"), "attestation_bundle.quote")
    report_data = _required_string(attestation_bundle.get("report_data"), "attestation_bundle.report_data")
    if not _report_data_matches(report_data, expected_report_data):
        raise AttestationError("attestation_bundle.report_data does not match expected report data")

    verified_report_data = verify_phala_dstack_quote(quote)
    if not _report_data_matches(verified_report_data, expected_report_data):
        raise AttestationError("verified quote report data does not match expected report data")


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


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AttestationError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttestationError(f"{label} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise AttestationError(f"{label} must be a non-empty string when present")
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
