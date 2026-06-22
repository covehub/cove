from __future__ import annotations

import hashlib
import json

import pytest

from cove_cli.client_attestation import (
    ClientAttestationVerificationError,
    verify_client_attestation_bundle,
)


COMPOSE_HASH = "sha256:" + ("1" * 64)
EXPECTED_COMPOSE_TEXT = "services:\n  app:\n    image: example/app@sha256:" + ("2" * 64) + "\n"
EXPECTED_REPORT_DATA = b"cove-client-report-data"


def test_verify_client_attestation_replays_rtmr3_and_checks_compose(monkeypatch) -> None:
    app_compose = json.dumps(
        {"docker_compose_file": EXPECTED_COMPOSE_TEXT},
        sort_keys=True,
        separators=(",", ":"),
    )
    event_log, rtmr3 = _event_log_and_rtmr3(
        compose_payload=hashlib.sha256(app_compose.encode("utf-8")).hexdigest(),
    )
    _stub_phala_quote_verifier(monkeypatch, rtmr3=rtmr3)

    result = verify_client_attestation_bundle(
        {
            "format": "phala_dstack_v1",
            "quote": "tdx-quote",
            "report_data": EXPECTED_REPORT_DATA.hex(),
            "event_log": json.dumps(event_log),
            "info": {"tcb_info": {"app_compose": app_compose}},
        },
        expected_report_data=EXPECTED_REPORT_DATA,
        expected_compose_hash=COMPOSE_HASH,
        expected_deployed_compose_text=EXPECTED_COMPOSE_TEXT,
    )

    assert result.rtmr3 == rtmr3


def test_verify_client_attestation_prefers_tcb_event_log(monkeypatch) -> None:
    event_log, rtmr3 = _event_log_and_rtmr3(
        compose_payload=COMPOSE_HASH.removeprefix("sha256:"),
        include_empty_events=True,
    )
    lossy_event_log = [{**event, "digest": ""} for event in event_log]
    _stub_phala_quote_verifier(monkeypatch, rtmr3=rtmr3)

    result = verify_client_attestation_bundle(
        {
            "format": "phala_dstack_v1",
            "quote": "tdx-quote",
            "report_data": EXPECTED_REPORT_DATA.hex(),
            "event_log": json.dumps(lossy_event_log),
            "info": {"tcb_info": {"event_log": event_log}},
        },
        expected_report_data=EXPECTED_REPORT_DATA,
        expected_compose_hash=COMPOSE_HASH,
    )

    assert result.rtmr3 == rtmr3


def test_verify_client_attestation_rejects_bad_event_digest(monkeypatch) -> None:
    event_log, rtmr3 = _event_log_and_rtmr3(compose_payload=COMPOSE_HASH.removeprefix("sha256:"))
    event_log[0]["digest"] = "00" * 48
    _stub_phala_quote_verifier(monkeypatch, rtmr3=rtmr3)

    with pytest.raises(ClientAttestationVerificationError, match="event digest"):
        verify_client_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "tdx-quote",
                "report_data": EXPECTED_REPORT_DATA.hex(),
                "event_log": event_log,
            },
            expected_report_data=EXPECTED_REPORT_DATA,
            expected_compose_hash=COMPOSE_HASH,
        )


def test_verify_client_attestation_rejects_compose_mismatch(monkeypatch) -> None:
    event_log, rtmr3 = _event_log_and_rtmr3(compose_payload="3" * 64)
    _stub_phala_quote_verifier(monkeypatch, rtmr3=rtmr3)

    with pytest.raises(ClientAttestationVerificationError, match="compose-hash"):
        verify_client_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "tdx-quote",
                "report_data": EXPECTED_REPORT_DATA.hex(),
                "event_log": event_log,
            },
            expected_report_data=EXPECTED_REPORT_DATA,
            expected_compose_hash=COMPOSE_HASH,
        )


def test_verify_client_attestation_rejects_non_tdx_quote(monkeypatch) -> None:
    event_log, rtmr3 = _event_log_and_rtmr3(compose_payload=COMPOSE_HASH.removeprefix("sha256:"))
    monkeypatch.setattr(
        "cove_cli.client_attestation.http_post_json",
        lambda **_kwargs: _quote_response(rtmr3=rtmr3, tee_type="TEE_SGX"),
    )

    with pytest.raises(ClientAttestationVerificationError, match="not a TDX"):
        verify_client_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "tdx-quote",
                "report_data": EXPECTED_REPORT_DATA.hex(),
                "event_log": event_log,
            },
            expected_report_data=EXPECTED_REPORT_DATA,
            expected_compose_hash=COMPOSE_HASH,
        )


def _event_log_and_rtmr3(
    *,
    compose_payload: str,
    include_empty_events: bool = False,
) -> tuple[list[dict[str, object]], str]:
    events: list[dict[str, object]] = []
    digest = b"\0" * 48
    source_events: list[tuple[str, str]] = [
        ("compose-hash", compose_payload),
        ("instance-id", "4" * 64),
        ("key-provider", "5" * 64),
    ]
    if include_empty_events:
        source_events = [("system-preparing", ""), *source_events, ("system-ready", "")]
    for event_name, payload in source_events:
        event_digest = _event_digest(event_name=event_name, payload=payload)
        digest = hashlib.sha384(digest + event_digest).digest()
        events.append(
            {
                "imr": 3,
                "event_type": 134217729,
                "event": event_name,
                "event_payload": payload,
                "digest": event_digest.hex(),
            }
        )
    return events, digest.hex()


def _event_digest(*, event_name: str, payload: str) -> bytes:
    hasher = hashlib.sha384()
    hasher.update((134217729).to_bytes(4, "little"))
    hasher.update(b":")
    hasher.update(event_name.encode("utf-8"))
    hasher.update(b":")
    hasher.update(bytes.fromhex(payload))
    return hasher.digest()


def _stub_phala_quote_verifier(monkeypatch, *, rtmr3: str) -> None:
    monkeypatch.setattr(
        "cove_cli.client_attestation.http_post_json",
        lambda **_kwargs: _quote_response(rtmr3=rtmr3),
    )


def _quote_response(*, rtmr3: str, tee_type: str = "TEE_TDX") -> dict[str, object]:
    return {
        "success": True,
        "quote": {
            "verified": True,
            "header": {"tee_type": tee_type},
            "body": {
                "reportdata": EXPECTED_REPORT_DATA.ljust(64, b"\0").hex(),
                "mrtd": "a" * 96,
                "rtmr0": "b" * 96,
                "rtmr1": "c" * 96,
                "rtmr2": "d" * 96,
                "rtmr3": rtmr3,
            },
        },
    }
