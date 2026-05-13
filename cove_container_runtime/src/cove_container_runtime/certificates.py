from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from .attestation import (
    build_node_certificate_report_data,
    collect_attestation_bundle,
    load_attestation_settings,
    verify_attestation_bundle,
)
from .common import RuntimeErrorBase, canonical_json_bytes, sha256_literal


class CertificateVerificationError(RuntimeErrorBase):
    """Raised when a node certificate is malformed or fails verification."""


@dataclass(frozen=True, slots=True)
class GeneratedKeypairMaterial:
    private_key_pem: str
    public_key_pem: str
    certificate_pem: str
    metadata: dict[str, Any]


def generate_ed25519_keypair_material(
    *,
    keypair_name: str,
    certificate_common_name: str,
) -> GeneratedKeypairMaterial:
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()

    private_key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_key_pem = public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")

    now = datetime.now(timezone.utc)
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, certificate_common_name)]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=7))
        .sign(private_key, algorithm=None)
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM).decode("utf-8")

    return GeneratedKeypairMaterial(
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        certificate_pem=certificate_pem,
        metadata={
            "name": keypair_name,
            "algorithm": "ed25519",
            "public_key_pem": public_key_pem,
            "public_key_hash": sha256_literal(public_key_pem.encode("utf-8")),
            "certificate_pem": certificate_pem,
            "certificate_hash": sha256_literal(certificate_pem.encode("utf-8")),
        },
    )


def build_node_certificate(
    *,
    workflow_id: str,
    node_name: str,
    generated_node_compose_hash: str,
    inputs: dict[str, Any],
    ephemeral_keypairs: dict[str, Any],
    results: dict[str, Any],
    outputs: dict[str, Any] | None = None,
    attestation_config: dict[str, object] | None = None,
) -> dict[str, Any]:
    certificate_body = {
        "workflow_id": workflow_id,
        "node_id": node_name,
        "generated_node_compose_hash": generated_node_compose_hash,
        "inputs": inputs,
        "ephemeral_keypairs": ephemeral_keypairs,
        "results": results,
        "outputs": outputs or {},
    }
    certificate_body_hash = sha256_literal(canonical_json_bytes(certificate_body))
    report_data = build_node_certificate_report_data(
        certificate_body_hash=certificate_body_hash,
        compose_hash=generated_node_compose_hash,
    )
    settings = load_attestation_settings(
        {"attestation": attestation_config} if attestation_config is not None else {}
    )
    attestation_bundle = collect_attestation_bundle(
        settings,
        report_data=report_data,
    )
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

def verify_node_certificate(
    certificate: dict[str, Any],
    *,
    expected_workflow_id: str | None = None,
    expected_node_name: str | None = None,
    expected_generated_node_compose_hash: str | None = None,
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

    _required_string(attestation_bundle.get("format"), "attestation_bundle.format")

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
        verify_attestation_bundle(
            attestation_bundle,
            expected_report_data=report_data,
        )
    except RuntimeErrorBase as exc:
        raise CertificateVerificationError(str(exc)) from exc

    return certificate


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CertificateVerificationError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CertificateVerificationError(f"{label} must be a non-empty string")
    return value.strip()
