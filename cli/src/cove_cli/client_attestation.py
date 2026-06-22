from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from .attestation import PHALA_DSTACK_ATTESTATION_FORMAT, PHALA_DSTACK_VERIFY_URL
from .common import RuntimeErrorBase, http_post_json


_DSTACK_EVENT_TYPE = 134217729
_RTMR3_INDEX = 3
_RTMR_BYTES = 48
_REPORT_DATA_BYTES = 64


class ClientAttestationVerificationError(RuntimeErrorBase):
    """Raised when client-side TDX quote verification fails closed."""


@dataclass(frozen=True, slots=True)
class VerifiedClientAttestation:
    report_data: str
    rtmr3: str
    compose_event_payload: str


def verify_client_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
    expected_compose_hash: str,
    expected_deployed_compose_text: str | None = None,
) -> VerifiedClientAttestation:
    attestation_format = _required_string(
        attestation_bundle.get("format"),
        "attestation_bundle.format",
    )
    if attestation_format != PHALA_DSTACK_ATTESTATION_FORMAT:
        raise ClientAttestationVerificationError(
            f"unsupported attestation format: {attestation_format}"
        )

    quote = _required_string(attestation_bundle.get("quote"), "attestation_bundle.quote")
    bundle_report_data = _required_string(
        attestation_bundle.get("report_data"),
        "attestation_bundle.report_data",
    )
    if not _report_data_matches(bundle_report_data, expected_report_data):
        raise ClientAttestationVerificationError(
            "attestation_bundle.report_data does not match expected report data"
        )

    verified_quote = _verify_phala_tdx_quote(quote)
    verified_report_data = _hex_field(
        verified_quote.body,
        ("reportdata", "report_data"),
        "quote.body.reportdata",
        _REPORT_DATA_BYTES,
    )
    if not _report_data_matches(verified_report_data, expected_report_data):
        raise ClientAttestationVerificationError(
            "verified quote report data does not match expected report data"
        )

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
        raise ClientAttestationVerificationError(
            f"RTMR3 event log replay mismatch: {replayed_rtmr3.hex()} != {rtmr3}"
        )

    app_compose = _optional_app_compose(attestation_bundle)
    if app_compose is not None:
        app_compose_hash = hashlib.sha256(app_compose.encode("utf-8")).hexdigest()
        if compose_event_payload != app_compose_hash:
            raise ClientAttestationVerificationError(
                "RTMR3 compose-hash event does not match attested app_compose"
            )
        if expected_deployed_compose_text is not None:
            _verify_app_compose_docker_compose(
                app_compose,
                expected_deployed_compose_text=expected_deployed_compose_text,
            )
    else:
        expected_hash = _normalize_sha256_literal(expected_compose_hash)
        if compose_event_payload != expected_hash:
            raise ClientAttestationVerificationError(
                "RTMR3 compose-hash event does not match expected compose hash"
            )

    return VerifiedClientAttestation(
        report_data=verified_report_data,
        rtmr3=rtmr3,
        compose_event_payload=compose_event_payload,
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
    if response_payload.get("success") is not True:
        raise ClientAttestationVerificationError("Phala TDX quote verification failed")
    quote_payload = response_payload.get("quote")
    if not isinstance(quote_payload, dict):
        raise ClientAttestationVerificationError("quote verification response is missing quote")
    if quote_payload.get("verified") is not True:
        raise ClientAttestationVerificationError(
            "quote verification did not verify successfully"
        )
    header = _required_mapping(quote_payload.get("header"), "quote.header")
    body = _required_mapping(quote_payload.get("body"), "quote.body")
    tee_type = _required_string(header.get("tee_type"), "quote.header.tee_type")
    if "tdx" not in tee_type.lower():
        raise ClientAttestationVerificationError(
            f"verified quote is not a TDX quote: {tee_type}"
        )
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
            raise ClientAttestationVerificationError(
                f"{label} is not valid JSON"
            ) from exc
    else:
        decoded = value
    if not isinstance(decoded, list):
        raise ClientAttestationVerificationError(
            f"{label} must be a JSON array"
        )
    events: list[dict[str, Any]] = []
    for index, event in enumerate(decoded):
        if not isinstance(event, dict):
            raise ClientAttestationVerificationError(
                f"{label}[{index}] must be an object"
            )
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
            raise ClientAttestationVerificationError(
                f"RTMR3 event has unsupported event_type: {event_type}"
            )
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
            raise ClientAttestationVerificationError(
                "RTMR3 event digest does not match event payload"
            )
        digest = hashlib.sha384(digest + event_digest).digest()
        if event_name == "compose-hash":
            compose_event_payloads.append(_normalize_hex(event_payload))

    if not observed_any:
        raise ClientAttestationVerificationError(
            "attestation_bundle.event_log has no RTMR3 events"
        )
    if len(compose_event_payloads) != 1:
        raise ClientAttestationVerificationError(
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
        raise ClientAttestationVerificationError("attested app_compose is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ClientAttestationVerificationError("attested app_compose must be a JSON object")
    docker_compose_file = payload.get("docker_compose_file")
    if not isinstance(docker_compose_file, str) or not docker_compose_file:
        raise ClientAttestationVerificationError(
            "attested app_compose is missing docker_compose_file"
        )
    if docker_compose_file != expected_deployed_compose_text:
        raise ClientAttestationVerificationError(
            "attested docker_compose_file does not match expected deployed compose"
        )


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ClientAttestationVerificationError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClientAttestationVerificationError(f"{label} must be a non-empty string")
    return value.strip()


def _required_text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ClientAttestationVerificationError(f"{label} must be a string")
    return value.strip()


def _required_int(value: Any, label: str) -> int:
    if not isinstance(value, int):
        raise ClientAttestationVerificationError(f"{label} must be an integer")
    return value


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
                raise ClientAttestationVerificationError(
                    f"{label} must be {expected_length} bytes"
                )
            return _normalize_hex(value)
    raise ClientAttestationVerificationError(f"{label} must be a non-empty hex string")


def _bytes_field(value: Any, label: str, expected_length: int) -> bytes:
    if isinstance(value, str):
        decoded = _hex_bytes(value, label)
    elif isinstance(value, list) and all(isinstance(item, int) for item in value):
        try:
            decoded = bytes(value)
        except ValueError as exc:
            raise ClientAttestationVerificationError(f"{label} must contain byte values") from exc
    else:
        raise ClientAttestationVerificationError(f"{label} must be hex or a byte array")
    if len(decoded) != expected_length:
        raise ClientAttestationVerificationError(f"{label} must be {expected_length} bytes")
    return decoded


def _hex_bytes(value: str, label: str) -> bytes:
    normalized = _normalize_hex(value)
    try:
        return bytes.fromhex(normalized)
    except ValueError as exc:
        raise ClientAttestationVerificationError(f"{label} must be hex") from exc


def _normalize_hex(value: str) -> str:
    lowered = value.strip().lower()
    if lowered.startswith("0x"):
        return lowered[2:]
    return lowered


def _normalize_sha256_literal(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith("sha256:"):
        normalized = normalized.removeprefix("sha256:")
    if len(normalized) != 64:
        raise ClientAttestationVerificationError("expected compose hash must be sha256")
    _hex_bytes(normalized, "expected compose hash")
    return normalized


def _report_data_matches(observed_hex: str, expected_report_data: bytes) -> bool:
    observed = _normalize_hex(observed_hex)
    expected = expected_report_data.hex()
    if observed == expected:
        return True
    if len(expected_report_data) < _REPORT_DATA_BYTES:
        return observed == expected_report_data.ljust(_REPORT_DATA_BYTES, b"\0").hex()
    return False
