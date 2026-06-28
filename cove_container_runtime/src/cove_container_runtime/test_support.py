from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from typing import Any

from .attestation import build_node_certificate_report_data
from .certificates import CertificateVerificationError
from .common import RuntimeErrorBase, canonical_json_bytes, sha256_literal


MOCK_ATTESTATION_FORMAT = "mock_tdx_v1"


def build_mock_attestation_bundle(report_data: bytes) -> dict[str, Any]:
    report_data_hex = report_data.hex()
    quoted_report_data_hash = _sha256_literal(report_data)
    return {
        "format": MOCK_ATTESTATION_FORMAT,
        "quote": f"mock-tdx-quote:{quoted_report_data_hash}",
        "event_log": [
            {
                "event": "mock_attestation",
                "report_data_sha256": quoted_report_data_hash,
            }
        ],
        "report_data": report_data_hex,
        "info": {"mock": True},
    }


def verify_mock_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
) -> dict[str, Any]:
    expected_report_data_hash = _sha256_literal(expected_report_data)
    quote = _required_string(attestation_bundle.get("quote"), "attestation_bundle.quote")
    expected_quote = f"mock-tdx-quote:{expected_report_data_hash}"
    if quote != expected_quote:
        raise RuntimeErrorBase("mock quote does not match expected report data")

    report_data = _required_string(attestation_bundle.get("report_data"), "attestation_bundle.report_data")
    if _normalize_hex(report_data) != expected_report_data.hex():
        raise RuntimeErrorBase("mock attestation report_data does not match expected report data")

    event_log = attestation_bundle.get("event_log")
    if not isinstance(event_log, list) or not event_log:
        raise RuntimeErrorBase("attestation_bundle.event_log must be a non-empty list")
    first_event = _required_mapping(event_log[0], "attestation_bundle.event_log[0]")
    event_hash = _required_string(
        first_event.get("report_data_sha256"),
        "attestation_bundle.event_log[0].report_data_sha256",
    )
    if event_hash != expected_report_data_hash:
        raise RuntimeErrorBase("mock event log does not match expected report data")
    return attestation_bundle


def build_mock_certificate(
    *,
    workflow_id: str,
    node_name: str,
    generated_node_compose_hash: str,
    inputs: dict[str, Any],
    ephemeral_keypairs: dict[str, Any],
    results: dict[str, Any],
    outputs: dict[str, Any] | None = None,
    dependencies: dict[str, Any] | None = None,
    runtime_metrics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    certificate_body = {
        "workflow_id": workflow_id,
        "node_id": node_name,
        "generated_node_compose_hash": generated_node_compose_hash,
        "inputs": inputs,
        "ephemeral_keypairs": ephemeral_keypairs,
        "results": results,
        "outputs": outputs or {},
        "dependencies": dependencies or {},
        "runtime_metrics": runtime_metrics or {},
    }
    certificate_body_hash = sha256_literal(canonical_json_bytes(certificate_body))
    report_data = build_node_certificate_report_data(
        certificate_body_hash=certificate_body_hash,
        compose_hash=generated_node_compose_hash,
    )
    attestation_bundle = build_mock_attestation_bundle(report_data)
    return {
        "certificate_body": certificate_body,
        "certificate_body_hash": certificate_body_hash,
        "attestation_bundle": {
            **attestation_bundle,
            "quoted_certificate_body_hash": certificate_body_hash,
            "generated_node_compose_hash": generated_node_compose_hash,
            "node_id": node_name,
        },
    }


def verify_mock_certificate(
    certificate: dict[str, Any],
    *,
    expected_workflow_id: str | None = None,
    expected_node_name: str | None = None,
    expected_generated_node_compose_hash: str | None = None,
    expected_dependency_compose_hashes: Mapping[str, str] | None = None,
    expected_dependency_edges: Mapping[str, Sequence[str]] | None = None,
    _seen: frozenset[str] | None = None,
) -> dict[str, Any]:
    certificate_body = _required_mapping(certificate.get("certificate_body"), "certificate_body")
    attestation_bundle = _required_mapping(
        certificate.get("attestation_bundle"),
        "attestation_bundle",
    )
    certificate_body_hash = _required_string(
        certificate.get("certificate_body_hash"),
        "certificate_body_hash",
    )
    observed_body_hash = sha256_literal(canonical_json_bytes(certificate_body))
    if observed_body_hash != certificate_body_hash:
        raise CertificateVerificationError(
            "certificate_body_hash does not match the canonical certificate body"
        )

    node_name = _required_string(certificate_body.get("node_id"), "certificate_body.node_id")
    if _seen is None:
        _seen = frozenset()
    if node_name in _seen:
        raise CertificateVerificationError(
            f"certificate dependency graph contains a cycle at node {node_name!r}"
        )
    seen = _seen | {node_name}
    if expected_node_name is not None and node_name != expected_node_name:
        raise CertificateVerificationError(
            f"certificate node id {node_name!r} does not match expected node {expected_node_name!r}"
        )
    workflow_id = _required_string(
        certificate_body.get("workflow_id"),
        "certificate_body.workflow_id",
    )
    if expected_workflow_id is not None and workflow_id != expected_workflow_id:
        raise CertificateVerificationError(
            f"certificate workflow id {workflow_id!r} does not match expected workflow {expected_workflow_id!r}"
        )

    attestation_format = _required_string(
        attestation_bundle.get("format"),
        "attestation_bundle.format",
    )
    if attestation_format != MOCK_ATTESTATION_FORMAT:
        raise CertificateVerificationError(
            f"unsupported attestation format: {attestation_format}"
        )

    quoted_hash = _required_string(
        attestation_bundle.get("quoted_certificate_body_hash"),
        "attestation_bundle.quoted_certificate_body_hash",
    )
    if quoted_hash != certificate_body_hash:
        raise CertificateVerificationError(
            "quoted certificate body hash does not match certificate_body_hash"
        )

    attested_node_id = _required_string(
        attestation_bundle.get("node_id"),
        "attestation_bundle.node_id",
    )
    if attested_node_id != node_name:
        raise CertificateVerificationError(
            "attestation node_id does not match certificate body node_id"
        )

    compose_hash = _required_string(
        certificate_body.get("generated_node_compose_hash"),
        "certificate_body.generated_node_compose_hash",
    )
    attested_compose_hash = _required_string(
        attestation_bundle.get("generated_node_compose_hash"),
        "attestation_bundle.generated_node_compose_hash",
    )
    if attested_compose_hash != compose_hash:
        raise CertificateVerificationError(
            "attestation compose hash does not match certificate body compose hash"
        )
    if expected_dependency_compose_hashes is not None:
        expected_compose_for_node = expected_dependency_compose_hashes.get(node_name)
        if expected_compose_for_node is None:
            raise CertificateVerificationError(
                f"certificate node {node_name!r} is not expected by the workflow bundle"
            )
        if compose_hash != expected_compose_for_node:
            raise CertificateVerificationError(
                f"certificate for node {node_name!r} has stale or mismatched compose hash"
            )
    if (
        expected_generated_node_compose_hash is not None
        and compose_hash != expected_generated_node_compose_hash
    ):
        raise CertificateVerificationError(
            "certificate compose hash does not match the expected generated compose hash"
        )
    report_data = build_node_certificate_report_data(
        certificate_body_hash=certificate_body_hash,
        compose_hash=compose_hash,
    )
    try:
        verify_mock_attestation_bundle(
            attestation_bundle,
            expected_report_data=report_data,
        )
    except RuntimeErrorBase as exc:
        raise CertificateVerificationError(str(exc)) from exc

    dependencies = _required_mapping(
        certificate_body.get("dependencies"),
        "certificate_body.dependencies",
    )
    if expected_dependency_edges is not None:
        expected_direct = set(_expected_dependency_names(expected_dependency_edges, node_name))
        observed_direct = set(dependencies)
        missing = sorted(expected_direct - observed_direct)
        unexpected = sorted(observed_direct - expected_direct)
        if missing:
            raise CertificateVerificationError(
                f"certificate for node {node_name!r} is missing dependency certificates: {', '.join(missing)}"
            )
        if unexpected:
            raise CertificateVerificationError(
                f"certificate for node {node_name!r} has unexpected dependency certificates: {', '.join(unexpected)}"
            )

    for dependency_name, dependency_certificate in dependencies.items():
        if not isinstance(dependency_name, str) or not dependency_name.strip():
            raise CertificateVerificationError(
                "certificate_body.dependencies keys must be non-empty node names"
            )
        if not isinstance(dependency_certificate, dict):
            raise CertificateVerificationError(
                f"certificate_body.dependencies.{dependency_name} must be a certificate object"
            )
        expected_dependency_compose_hash = (
            expected_dependency_compose_hashes.get(dependency_name)
            if expected_dependency_compose_hashes is not None
            else None
        )
        if expected_dependency_compose_hashes is not None and expected_dependency_compose_hash is None:
            raise CertificateVerificationError(
                f"dependency certificate {dependency_name!r} is not expected by the workflow bundle"
            )
        verify_mock_certificate(
            dependency_certificate,
            expected_workflow_id=workflow_id,
            expected_node_name=dependency_name,
            expected_generated_node_compose_hash=expected_dependency_compose_hash,
            expected_dependency_compose_hashes=expected_dependency_compose_hashes,
            expected_dependency_edges=expected_dependency_edges,
            _seen=seen,
        )

    return certificate


def _expected_dependency_names(
    expected_dependency_edges: Mapping[str, Sequence[str]],
    node_name: str,
) -> list[str]:
    raw_dependencies = expected_dependency_edges.get(node_name, ())
    dependencies: list[str] = []
    for dependency in raw_dependencies:
        if not isinstance(dependency, str) or not dependency.strip():
            raise CertificateVerificationError(
                f"expected dependency edge for node {node_name!r} is invalid"
            )
        dependencies.append(dependency.strip())
    return dependencies


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CertificateVerificationError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CertificateVerificationError(f"{label} must be a non-empty string")
    return value.strip()


def _normalize_hex(value: str) -> str:
    lowered = value.strip().lower()
    if lowered.startswith("0x"):
        return lowered[2:]
    return lowered


def _sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
