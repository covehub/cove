"""NVIDIA confidential-computing GPU attestation (Hopper/Blackwell).

Collection reads the GPU's SPDM attestation report and device certificate
chain through NVML. The 32-byte SPDM nonce is ``sha256(report_data)``, so the
same Cove report-data labels used for TDX quotes bind GPU evidence too.

Verification submits the evidence to NVIDIA's Remote Attestation Service
(NRAS), which validates the device certificate chain, the report signature,
the nonce, and compares VBIOS/driver measurements against NVIDIA's signed
reference integrity manifests (RIMs). The returned EAT tokens are verified
locally against the NRAS JWKS before any claim is trusted.

A GPU-only attestation proves that a genuine NVIDIA GPU in CC mode with
NVIDIA-approved firmware produced the evidence for this nonce. It does NOT
measure the CPU-side workload (the node compose); that requires a CPU TEE
quote. Callers must opt in to accepting this format.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Any
from urllib import request as urllib_request

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.serialization import Encoding

from .common import RuntimeErrorBase, _http_request_bytes, http_get_json


NVIDIA_GPU_CC_ATTESTATION_FORMAT = "nvidia_gpu_cc_v1"
NRAS_GPU_ATTEST_URL = "https://nras.attestation.nvidia.com/v3/attest/gpu"
NRAS_JWKS_URL = "https://nras.attestation.nvidia.com/.well-known/jwks.json"
NRAS_ISSUER = "https://nras.attestation.nvidia.com"
_SUPPORTED_ARCHES = {"HOPPER", "BLACKWELL"}
_NVML_ARCH_NAMES = {9: "HOPPER", 10: "BLACKWELL"}
_REQUIRED_TRUE_GPU_CLAIMS = (
    "x-nvidia-gpu-attestation-report-cert-chain-validated",
    "x-nvidia-gpu-attestation-report-parsed",
    "x-nvidia-gpu-attestation-report-nonce-match",
    "x-nvidia-gpu-attestation-report-signature-verified",
    "x-nvidia-gpu-driver-rim-fetched",
    "x-nvidia-gpu-driver-rim-signature-verified",
    "x-nvidia-gpu-driver-rim-cert-validated",
    "x-nvidia-gpu-driver-rim-measurements-available",
    "x-nvidia-gpu-vbios-rim-fetched",
    "x-nvidia-gpu-vbios-rim-signature-verified",
    "x-nvidia-gpu-vbios-rim-cert-validated",
    "x-nvidia-gpu-vbios-rim-measurements-available",
    "x-nvidia-gpu-arch-check",
    "secboot",
)


class GpuAttestationError(RuntimeErrorBase):
    """Raised when GPU attestation collection or verification fails."""


@dataclass(frozen=True, slots=True)
class VerifiedGpu:
    ueid: str
    hwmodel: str
    driver_version: str
    vbios_version: str
    claims: dict[str, Any]


def gpu_nonce_for_report_data(report_data: bytes) -> bytes:
    return hashlib.sha256(report_data).digest()


def collect_nvidia_gpu_attestation_bundle(*, report_data: bytes) -> dict[str, Any]:
    try:
        import pynvml
    except ImportError as exc:  # pragma: no cover - depends on runtime environment
        raise GpuAttestationError(
            "nvidia-ml-py is required for nvidia_gpu_cc attestation mode "
            "(install cove-container-runtime[gpu])"
        ) from exc

    nonce = gpu_nonce_for_report_data(report_data)
    pynvml.nvmlInit()
    try:
        state = pynvml.nvmlSystemGetConfComputeState()
        if getattr(state, "ccFeature", 0) != pynvml.NVML_CC_SYSTEM_FEATURE_ENABLED:
            raise GpuAttestationError("GPU confidential computing is not enabled on this system")
        driver_version = _text(pynvml.nvmlSystemGetDriverVersion())
        evidence_list: list[dict[str, Any]] = []
        arch_names: set[str] = set()
        for index in range(pynvml.nvmlDeviceGetCount()):
            handle = pynvml.nvmlDeviceGetHandleByIndex(index)
            arch = _NVML_ARCH_NAMES.get(pynvml.nvmlDeviceGetArchitecture(handle))
            if arch is None:
                raise GpuAttestationError(f"GPU {index} architecture does not support CC attestation")
            arch_names.add(arch)
            report = pynvml.nvmlDeviceGetConfComputeGpuAttestationReport(handle, nonce)
            report_bytes = bytes(report.attestationReport[: report.attestationReportSize])
            cert = pynvml.nvmlDeviceGetConfComputeGpuCertificate(handle)
            chain_bytes = bytes(cert.attestationCertChain[: cert.attestationCertChainSize])
            evidence_list.append(
                {
                    "gpu_index": index,
                    "uuid": _text(pynvml.nvmlDeviceGetUUID(handle)),
                    "vbios_version": _text(pynvml.nvmlDeviceGetVbiosVersion(handle)),
                    "evidence": base64.b64encode(report_bytes).decode("ascii"),
                    "certificate": base64.b64encode(_device_chain_without_root(chain_bytes)).decode(
                        "ascii"
                    ),
                }
            )
    finally:
        pynvml.nvmlShutdown()

    if not evidence_list:
        raise GpuAttestationError("no NVIDIA GPUs found")
    if len(arch_names) != 1:
        raise GpuAttestationError("mixed GPU architectures are not supported")
    return {
        "format": NVIDIA_GPU_CC_ATTESTATION_FORMAT,
        "arch": arch_names.pop(),
        "nonce": nonce.hex(),
        "report_data": report_data.hex(),
        "driver_version": driver_version,
        "evidence_list": evidence_list,
    }


def verify_nvidia_gpu_attestation_bundle(
    attestation_bundle: dict[str, Any],
    *,
    expected_report_data: bytes,
) -> list[VerifiedGpu]:
    expected_nonce = gpu_nonce_for_report_data(expected_report_data).hex()
    if _string(attestation_bundle.get("nonce"), "attestation_bundle.nonce").lower() != expected_nonce:
        raise GpuAttestationError("attestation_bundle.nonce does not match sha256(expected report data)")
    arch = _string(attestation_bundle.get("arch"), "attestation_bundle.arch").upper()
    if arch not in _SUPPORTED_ARCHES:
        raise GpuAttestationError(f"unsupported GPU architecture: {arch}")
    raw_evidence = attestation_bundle.get("evidence_list")
    if not isinstance(raw_evidence, list) or not raw_evidence:
        raise GpuAttestationError("attestation_bundle.evidence_list must be a non-empty array")
    evidence_list = []
    for index, item in enumerate(raw_evidence):
        if not isinstance(item, dict):
            raise GpuAttestationError(f"attestation_bundle.evidence_list[{index}] must be an object")
        evidence_list.append(
            {
                "evidence": _string(item.get("evidence"), f"evidence_list[{index}].evidence"),
                "certificate": _string(item.get("certificate"), f"evidence_list[{index}].certificate"),
            }
        )

    # Submit with the nonce we derived, never the bundle's, so a replayed report fails nonce-match.
    response = _nras_post_json(
        {"nonce": expected_nonce, "arch": arch, "evidence_list": evidence_list}
    )
    return verify_nras_response(response, expected_nonce=expected_nonce, expected_gpu_count=len(evidence_list))


def verify_nras_response(
    response: Any,
    *,
    expected_nonce: str,
    expected_gpu_count: int,
    jwks: dict[str, Any] | None = None,
) -> list[VerifiedGpu]:
    # NRAS v3 returns [["JWT", <platform token>], {"GPU-0": <gpu token>, ...}].
    if (
        not isinstance(response, list)
        or len(response) != 2
        or not isinstance(response[0], list)
        or len(response[0]) != 2
        or not isinstance(response[1], dict)
    ):
        raise GpuAttestationError("NRAS returned an unexpected response shape")
    platform_token = _string(response[0][1], "NRAS platform token")
    gpu_tokens: dict[str, Any] = response[1]
    keys = jwks if jwks is not None else _fetch_jwks()

    platform_claims = _verify_jwt(platform_token, keys)
    _check_common_claims(platform_claims, expected_nonce, "platform token")
    if platform_claims.get("x-nvidia-overall-att-result") is not True:
        raise GpuAttestationError("NRAS overall attestation result is not true")
    submods = platform_claims.get("submods")
    if not isinstance(submods, dict) or set(submods) != set(gpu_tokens):
        raise GpuAttestationError("NRAS platform token submods do not match GPU tokens")
    if len(gpu_tokens) != expected_gpu_count:
        raise GpuAttestationError(
            f"NRAS attested {len(gpu_tokens)} GPUs but {expected_gpu_count} were submitted"
        )

    verified: list[VerifiedGpu] = []
    for name in sorted(gpu_tokens):
        token = _string(gpu_tokens[name], f"NRAS {name} token")
        digest_claim = submods[name]
        if (
            not isinstance(digest_claim, list)
            or len(digest_claim) != 2
            or digest_claim[0] != "DIGEST"
            or not isinstance(digest_claim[1], list)
            or digest_claim[1][:1] != ["SHA-256"]
        ):
            raise GpuAttestationError(f"NRAS submod {name} has an unexpected digest claim")
        if hashlib.sha256(token.encode("ascii")).hexdigest() != str(digest_claim[1][1]).lower():
            raise GpuAttestationError(f"NRAS {name} token does not match the platform token digest")
        claims = _verify_jwt(token, keys)
        _check_common_claims(claims, expected_nonce, f"{name} token")
        for claim in _REQUIRED_TRUE_GPU_CLAIMS:
            if claims.get(claim) is not True:
                raise GpuAttestationError(f"{name}: NRAS claim {claim} is not true")
        if claims.get("measres") != "success":
            raise GpuAttestationError(f"{name}: firmware measurements do not match NVIDIA golden values")
        if claims.get("dbgstat") != "disabled":
            raise GpuAttestationError(f"{name}: GPU debug mode is not disabled")
        verified.append(
            VerifiedGpu(
                ueid=str(claims.get("ueid", "")),
                hwmodel=str(claims.get("hwmodel", "")),
                driver_version=str(claims.get("x-nvidia-gpu-driver-version", "")),
                vbios_version=str(claims.get("x-nvidia-gpu-vbios-version", "")),
                claims=claims,
            )
        )
    return verified


def _check_common_claims(claims: dict[str, Any], expected_nonce: str, label: str) -> None:
    if claims.get("iss") != NRAS_ISSUER:
        raise GpuAttestationError(f"NRAS {label} has unexpected issuer")
    if str(claims.get("eat_nonce", "")).lower() != expected_nonce:
        raise GpuAttestationError(f"NRAS {label} nonce does not match expected nonce")


def _nras_post_json(payload: dict[str, Any]) -> Any:
    request = urllib_request.Request(
        NRAS_GPU_ATTEST_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    body = _http_request_bytes(request=request, timeout=60.0)
    try:
        return json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise GpuAttestationError("NRAS returned invalid JSON") from exc


def _fetch_jwks() -> dict[str, Any]:
    return http_get_json(url=NRAS_JWKS_URL, timeout=30.0)


def _verify_jwt(token: str, jwks: dict[str, Any]) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise GpuAttestationError("NRAS token is not a compact JWT")
    header = _b64url_json(parts[0], "NRAS token header")
    if header.get("alg") != "ES384":
        raise GpuAttestationError(f"NRAS token uses unsupported alg: {header.get('alg')}")
    kid = header.get("kid")
    key_entry = next(
        (key for key in jwks.get("keys", []) if isinstance(key, dict) and key.get("kid") == kid),
        None,
    )
    if key_entry is None:
        raise GpuAttestationError(f"NRAS signing key {kid!r} not found in JWKS")
    public_key = _jwk_public_key(key_entry)
    signature = _b64url_bytes(parts[2], "NRAS token signature")
    if len(signature) != 96:
        raise GpuAttestationError("NRAS ES384 signature must be 96 bytes")
    der_signature = encode_dss_signature(
        int.from_bytes(signature[:48], "big"),
        int.from_bytes(signature[48:], "big"),
    )
    try:
        public_key.verify(
            der_signature,
            f"{parts[0]}.{parts[1]}".encode("ascii"),
            ec.ECDSA(hashes.SHA384()),
        )
    except InvalidSignature as exc:
        raise GpuAttestationError("NRAS token signature is invalid") from exc
    return _b64url_json(parts[1], "NRAS token claims")


def _jwk_public_key(key_entry: dict[str, Any]) -> ec.EllipticCurvePublicKey:
    x5c = key_entry.get("x5c")
    if isinstance(x5c, list) and x5c:
        certificate = x509.load_der_x509_certificate(base64.b64decode(x5c[0]))
        public_key = certificate.public_key()
    elif key_entry.get("kty") == "EC" and key_entry.get("crv") == "P-384":
        public_key = ec.EllipticCurvePublicNumbers(
            int.from_bytes(_b64url_bytes(key_entry["x"], "jwk.x"), "big"),
            int.from_bytes(_b64url_bytes(key_entry["y"], "jwk.y"), "big"),
            ec.SECP384R1(),
        ).public_key()
    else:
        raise GpuAttestationError("NRAS JWKS key has no usable public key")
    if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
        public_key.curve, ec.SECP384R1
    ):
        raise GpuAttestationError("NRAS signing key is not a P-384 EC key")
    return public_key


def _device_chain_without_root(chain_bytes: bytes) -> bytes:
    # NVML returns the PEM chain leaf-first ending in the NVIDIA device root; NRAS wants it without the root.
    try:
        certificates = x509.load_pem_x509_certificates(chain_bytes)
    except ValueError as exc:
        raise GpuAttestationError("GPU certificate chain is not valid PEM") from exc
    if len(certificates) < 2:
        raise GpuAttestationError("GPU certificate chain is too short")
    return b"".join(cert.public_bytes(Encoding.PEM) for cert in certificates[:-1])


def _b64url_bytes(value: str, label: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise GpuAttestationError(f"{label} is not valid base64url") from exc


def _b64url_json(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(_b64url_bytes(value, label))
    except json.JSONDecodeError as exc:
        raise GpuAttestationError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise GpuAttestationError(f"{label} must be a JSON object")
    return decoded


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise GpuAttestationError(f"{label} must be a non-empty string")
    return value.strip()


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)
