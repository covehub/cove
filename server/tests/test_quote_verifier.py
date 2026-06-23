from __future__ import annotations

import pytest

from cove_container_runtime.common import RuntimeErrorBase
from cove_server.quote_verifier import (
    PhalaDstackQuoteVerifier,
    QuoteVerificationError,
    RuntimeAttestation,
    build_quote_verifier,
)


def test_build_quote_verifier_requires_phala_dstack() -> None:
    assert isinstance(build_quote_verifier("phala_dstack"), PhalaDstackQuoteVerifier)

    with pytest.raises(ValueError, match="unsupported quote verifier mode"):
        build_quote_verifier("mock")


def test_phala_dstack_quote_verifier_delegates_to_shared_attestation_verifier(
    monkeypatch,
) -> None:
    expected_report_data = b"verified-report-data"
    captured: dict[str, object] = {}

    def fake_verify_attestation_bundle(
        attestation_bundle,
        *,
        expected_report_data,
        expected_compose_hash,
    ):
        captured["attestation_bundle"] = attestation_bundle
        captured["expected_report_data"] = expected_report_data
        captured["expected_compose_hash"] = expected_compose_hash
        return attestation_bundle

    monkeypatch.setattr(
        "cove_server.quote_verifier.verify_attestation_bundle",
        fake_verify_attestation_bundle,
    )
    verifier = build_quote_verifier("phala_dstack")

    verifier.verify(
        RuntimeAttestation(
            format="phala_dstack_v1",
            quote="phala-quote",
            event_log="[]",
            info={"tcb_info": {"event_log": []}},
            node_id="benchmark_model_node",
            compose_hash="sha256:" + ("a" * 64),
            report_data=expected_report_data.hex(),
            expected_report_data=expected_report_data,
        )
    )

    assert captured == {
        "attestation_bundle": {
            "format": "phala_dstack_v1",
            "quote": "phala-quote",
            "event_log": "[]",
            "info": {"tcb_info": {"event_log": []}},
            "report_data": expected_report_data.hex(),
        },
        "expected_report_data": expected_report_data,
        "expected_compose_hash": "sha256:" + ("a" * 64),
    }


def test_phala_dstack_quote_verifier_rejects_invalid_format() -> None:
    verifier = build_quote_verifier("phala_dstack")

    with pytest.raises(QuoteVerificationError, match="format is invalid"):
        verifier.verify(
            RuntimeAttestation(
                format="mock_tdx_v1",
                quote="mock-quote",
                event_log=None,
                info=None,
                node_id="benchmark_model_node",
                compose_hash="sha256:" + ("a" * 64),
                report_data="00",
                expected_report_data=b"expected",
            )
        )


def test_phala_dstack_quote_verifier_wraps_shared_verifier_errors(monkeypatch) -> None:
    monkeypatch.setattr(
        "cove_server.quote_verifier.verify_attestation_bundle",
        lambda _attestation_bundle, *, expected_report_data, expected_compose_hash: _raise_runtime_error(
            f"bad quote for {expected_report_data.hex()}"
        ),
    )
    verifier = build_quote_verifier("phala_dstack")

    with pytest.raises(QuoteVerificationError, match="bad quote"):
        verifier.verify(
            RuntimeAttestation(
                format="phala_dstack_v1",
                quote="phala-quote",
                event_log=None,
                info=None,
                node_id="benchmark_model_node",
                compose_hash="sha256:" + ("a" * 64),
                report_data="00",
                expected_report_data=b"expected",
            )
        )


def _raise_runtime_error(message: str):
    raise RuntimeErrorBase(message)
