from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .common import RuntimeErrorBase, canonical_json_bytes, http_post_json


PHALA_DSTACK_ATTESTATION_FORMAT = "phala_dstack_v1"
PHALA_DSTACK_VERIFY_URL = "https://cloud-api.phala.network/api/v1/attestations/verify"
_NODE_CERTIFICATE_REPORT_LABEL = b"cove_node_certificate_v1"
_KEY_RELEASE_REPORT_LABEL = b"cove_key_release_v1"
_RUNTIME_ARTIFACT_REPORT_LABEL = b"cove_runtime_artifact_v1"
_DSTACK_EVENT_TYPE = 134217729
_RTMR3_INDEX = 3
_RTMR_BYTES = 48
_REPORT_DATA_BYTES = 64


class AttestationError(RuntimeErrorBase):
    """Raised when runtime attestation collection or verification fails."""


@dataclass(frozen=True, slots=True)
class VerifiedAttestation:
    report_data: str
    rtmr3: str
    compose_event_payload: str


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

    client = DstackClient(timeout=30)
    quote = client.get_quote(report_data)
    bundle: dict[str, Any] = {
        "format": PHALA_DSTACK_ATTESTATION_FORMAT,
        "quote": quote.quote,
        "event_log": quote.event_log,
        "vm_config": quote.vm_config,
        "report_data": quote.report_data,
    }
    try:
        info = client.info()
    except Exception as exc:
        bundle["info_error"] = str(exc)
    else:
        if hasattr(info, "model_dump"):
            bundle["info"] = info.model_dump()
        else:  # pragma: no cover - compatibility fallback
            bundle["info"] = info.dict()
    return bundle


def verify_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
    expected_compose_hash: str,
    expected_deployed_compose_text: str | None = None,
) -> VerifiedAttestation:
    attestation_format = _required_string(attestation_bundle.get("format"), "attestation_bundle.format")
    if attestation_format == PHALA_DSTACK_ATTESTATION_FORMAT:
        return _verify_phala_dstack_attestation_bundle(
            attestation_bundle,
            expected_report_data=expected_report_data,
            expected_compose_hash=expected_compose_hash,
            expected_deployed_compose_text=expected_deployed_compose_text,
        )
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
    expected_compose_hash: str,
    expected_deployed_compose_text: str | None = None,
) -> VerifiedAttestation:
    return verify_phala_dstack_attestation_bundle(
        attestation_bundle,
        expected_report_data=expected_report_data,
        expected_compose_hash=expected_compose_hash,
        expected_deployed_compose_text=expected_deployed_compose_text,
    )


def verify_phala_dstack_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
    expected_compose_hash: str,
    expected_deployed_compose_text: str | None = None,
) -> VerifiedAttestation:
    quote = _required_string(attestation_bundle.get("quote"), "attestation_bundle.quote")
    report_data = _required_string(attestation_bundle.get("report_data"), "attestation_bundle.report_data")
    if not _report_data_matches(report_data, expected_report_data):
        raise AttestationError("attestation_bundle.report_data does not match expected report data")

    verified_quote = _verify_phala_tdx_quote(quote)
    verified_report_data = _hex_field(
        verified_quote.body,
        ("reportdata", "report_data"),
        "quote.body.reportdata",
        _REPORT_DATA_BYTES,
    )
    if not _report_data_matches(verified_report_data, expected_report_data):
        raise AttestationError("verified quote report data does not match expected report data")

    rtmr3 = _hex_field(
        verified_quote.body,
        ("rtmr3", "rt_mr3", "rtmr_3"),
        "quote.body.rtmr3",
        _RTMR_BYTES,
    )
    _require_tdx_measurement_fields(verified_quote.body)

    event_log = _parse_attested_event_log(attestation_bundle)
    replayed_rtmr3, compose_event_payload = _replay_rtmr3(event_log)
    if replayed_rtmr3.hex() != rtmr3:
        raise AttestationError(
            f"RTMR3 event log replay mismatch: {replayed_rtmr3.hex()} != {rtmr3}"
        )

    app_compose = _optional_app_compose(attestation_bundle)
    if app_compose is not None:
        app_compose_hash = hashlib.sha256(app_compose.encode("utf-8")).hexdigest()
        if compose_event_payload != app_compose_hash:
            raise AttestationError("RTMR3 compose-hash event does not match attested app_compose")
        if expected_deployed_compose_text is not None:
            _verify_app_compose_docker_compose(
                app_compose,
                expected_deployed_compose_text=expected_deployed_compose_text,
            )
    else:
        expected_hash = _normalize_sha256_literal(expected_compose_hash)
        if compose_event_payload != expected_hash:
            raise AttestationError("RTMR3 compose-hash event does not match expected compose hash")

    return VerifiedAttestation(
        report_data=verified_report_data,
        rtmr3=rtmr3,
        compose_event_payload=compose_event_payload,
    )


def verify_phala_dstack_quote(quote_hex: str) -> str:
    verified_quote = _verify_phala_tdx_quote(quote_hex)
    return _hex_field(
        verified_quote.body,
        ("reportdata", "report_data"),
        "quote.body.reportdata",
        _REPORT_DATA_BYTES,
    )


@dataclass(frozen=True, slots=True)
class _VerifiedQuote:
    header: dict[str, Any]
    body: dict[str, Any]


def _verify_phala_tdx_quote(quote_hex: str) -> _VerifiedQuote:
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
    header = _required_mapping(quote_payload.get("header"), "quote.header")
    body = quote_payload.get("body")
    if not isinstance(body, dict):
        raise AttestationError("quote verification response is missing quote.body")
    tee_type = _required_string(header.get("tee_type"), "quote.header.tee_type")
    if "tdx" not in tee_type.lower():
        raise AttestationError(f"verified quote is not a TDX quote: {tee_type}")
    return _VerifiedQuote(header=header, body=body)


def _require_tdx_measurement_fields(body: dict[str, Any]) -> None:
    _hex_field(body, ("mrtd", "mr_td"), "quote.body.mrtd", _RTMR_BYTES)
    _hex_field(body, ("rtmr0", "rt_mr0", "rtmr_0"), "quote.body.rtmr0", _RTMR_BYTES)
    _hex_field(body, ("rtmr1", "rt_mr1", "rtmr_1"), "quote.body.rtmr1", _RTMR_BYTES)
    _hex_field(body, ("rtmr2", "rt_mr2", "rtmr_2"), "quote.body.rtmr2", _RTMR_BYTES)


def _parse_attested_event_log(attestation_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    info = attestation_bundle.get("info")
    if isinstance(info, dict):
        tcb_info = info.get("tcb_info")
        if isinstance(tcb_info, dict) and tcb_info.get("event_log") is not None:
            return _parse_event_log(tcb_info.get("event_log"), "info.tcb_info.event_log")
    return _parse_event_log(
        attestation_bundle.get("event_log"),
        "attestation_bundle.event_log",
    )


def _parse_event_log(value: Any, label: str) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as exc:
            raise AttestationError(f"{label} is not valid JSON") from exc
    else:
        decoded = value
    if not isinstance(decoded, list):
        raise AttestationError(f"{label} must be a JSON array")
    events: list[dict[str, Any]] = []
    for index, event in enumerate(decoded):
        if not isinstance(event, dict):
            raise AttestationError(f"{label}[{index}] must be an object")
        events.append(event)
    return events


def _replay_rtmr3(event_log: list[dict[str, Any]]) -> tuple[bytes, str]:
    digest = b"\0" * _RTMR_BYTES
    observed_any = False
    compose_event_payloads: list[str] = []
    for event in event_log:
        imr = _required_int(event.get("imr"), "event_log[].imr")
        if imr != _RTMR3_INDEX:
            continue
        observed_any = True
        event_type = _required_int(event.get("event_type"), "event_log[].event_type")
        if event_type != _DSTACK_EVENT_TYPE:
            raise AttestationError(f"RTMR3 event has unsupported event_type: {event_type}")
        event_name = _required_string(event.get("event"), "event_log[].event")
        event_payload = _required_text(event.get("event_payload"), "event_log[].event_payload")
        payload_bytes = _hex_bytes(event_payload, "event_log[].event_payload")
        expected_event_digest = _event_digest(
            event_type=event_type,
            event_name=event_name,
            event_payload=payload_bytes,
        )
        event_digest = _bytes_field(event.get("digest"), "event_log[].digest", _RTMR_BYTES)
        if event_digest != expected_event_digest:
            raise AttestationError("RTMR3 event digest does not match event payload")
        digest = hashlib.sha384(digest + event_digest).digest()
        if event_name == "compose-hash":
            compose_event_payloads.append(_normalize_hex(event_payload))

    if not observed_any:
        raise AttestationError("attestation_bundle.event_log has no RTMR3 events")
    if len(compose_event_payloads) != 1:
        raise AttestationError(
            "attestation_bundle.event_log must contain exactly one RTMR3 compose-hash event"
        )
    return digest, compose_event_payloads[0]


def _event_digest(
    *,
    event_type: int,
    event_name: str,
    event_payload: bytes,
) -> bytes:
    hasher = hashlib.sha384()
    hasher.update(event_type.to_bytes(4, "little"))
    hasher.update(b":")
    hasher.update(event_name.encode("utf-8"))
    hasher.update(b":")
    hasher.update(event_payload)
    return hasher.digest()


def _optional_app_compose(attestation_bundle: dict[str, Any]) -> str | None:
    info = attestation_bundle.get("info")
    if not isinstance(info, dict):
        return None
    tcb_info = info.get("tcb_info")
    if not isinstance(tcb_info, dict):
        return None
    app_compose = tcb_info.get("app_compose")
    if isinstance(app_compose, str) and app_compose.strip():
        return app_compose
    return None


def _verify_app_compose_docker_compose(
    app_compose: str,
    *,
    expected_deployed_compose_text: str,
) -> None:
    try:
        payload = json.loads(app_compose)
    except json.JSONDecodeError as exc:
        raise AttestationError("attested app_compose is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise AttestationError("attested app_compose must be a JSON object")
    docker_compose_file = payload.get("docker_compose_file")
    if not isinstance(docker_compose_file, str) or not docker_compose_file:
        raise AttestationError("attested app_compose is missing docker_compose_file")
    if docker_compose_file != expected_deployed_compose_text:
        raise AttestationError("attested docker_compose_file does not match expected deployed compose")


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AttestationError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AttestationError(f"{label} must be a non-empty string")
    return value.strip()


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise AttestationError(f"{label} must be a string")
    return value.strip()


def _required_int(value: Any, label: str) -> int:
    if not isinstance(value, int):
        raise AttestationError(f"{label} must be an integer")
    return value


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


def _hex_field(
    payload: dict[str, Any],
    keys: tuple[str, ...],
    label: str,
    expected_length: int,
) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            decoded = _hex_bytes(value, label)
            if len(decoded) != expected_length:
                raise AttestationError(f"{label} must be {expected_length} bytes")
            return _normalize_hex(value)
    raise AttestationError(f"{label} must be a non-empty hex string")


def _bytes_field(value: Any, label: str, expected_length: int) -> bytes:
    if isinstance(value, str):
        decoded = _hex_bytes(value, label)
    elif isinstance(value, list) and all(isinstance(item, int) for item in value):
        try:
            decoded = bytes(value)
        except ValueError as exc:
            raise AttestationError(f"{label} must contain byte values") from exc
    else:
        raise AttestationError(f"{label} must be hex or a byte array")
    if len(decoded) != expected_length:
        raise AttestationError(f"{label} must be {expected_length} bytes")
    return decoded


def _hex_bytes(value: str, label: str) -> bytes:
    normalized = _normalize_hex(value)
    try:
        return bytes.fromhex(normalized)
    except ValueError as exc:
        raise AttestationError(f"{label} must be hex") from exc


def _normalize_sha256_literal(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    if len(normalized) != 64:
        raise AttestationError("expected compose hash must be sha256")
    _hex_bytes(normalized, "expected compose hash")
    return normalized


def _report_data_matches(observed_hex: str, expected_report_data: bytes) -> bool:
    observed = _normalize_hex(observed_hex)
    expected = expected_report_data.hex()
    if observed == expected:
        return True
    if len(expected_report_data) < 64:
        return observed == expected_report_data.ljust(64, b"\0").hex()
    return False
