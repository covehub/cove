from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from cove_container_runtime.attestation import (
    PHALA_DSTACK_ATTESTATION_FORMAT,
    verify_attestation_bundle,
)
from cove_container_runtime.common import RuntimeErrorBase


class QuoteVerificationError(ValueError):
    """Raised when runtime attestation headers are invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeAttestation:
    format: str
    quote: str
    event_log: str | None
    info: dict[str, Any] | None
    node_id: str
    compose_hash: str
    report_data: str
    expected_report_data: bytes


class QuoteVerifier(Protocol):
    def verify(self, attestation: RuntimeAttestation) -> None:
        """Validate a runtime attestation."""


class PhalaDstackQuoteVerifier:
    def verify(self, attestation: RuntimeAttestation) -> None:
        if attestation.format != PHALA_DSTACK_ATTESTATION_FORMAT:
            raise QuoteVerificationError("phala_dstack attestation format is invalid")
        try:
            verify_attestation_bundle(
                {
                    "format": attestation.format,
                    "quote": attestation.quote,
                    "event_log": attestation.event_log,
                    "info": attestation.info,
                    "report_data": attestation.report_data,
                },
                expected_report_data=attestation.expected_report_data,
                expected_compose_hash=attestation.compose_hash,
            )
        except RuntimeErrorBase as exc:
            raise QuoteVerificationError(str(exc)) from exc


def build_quote_verifier(mode: str) -> QuoteVerifier:
    if mode != "phala_dstack":
        raise ValueError(f"unsupported quote verifier mode: {mode}")
    return PhalaDstackQuoteVerifier()
