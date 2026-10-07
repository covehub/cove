from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from cove_container_runtime import nvidia_gpu_attestation as gpu_module
from cove_container_runtime.attestation import (
    AttestationError,
    build_node_certificate_report_data,
    load_attestation_settings,
    verify_attestation_bundle,
)
from cove_container_runtime.nvidia_gpu_attestation import (
    NRAS_ISSUER,
    GpuAttestationError,
    gpu_nonce_for_report_data,
    verify_nras_response,
)


_KID = "test-kid"
_SIGNING_KEY = ec.generate_private_key(ec.SECP384R1())
_REPORT_DATA = build_node_certificate_report_data(
    certificate_body_hash="sha256:" + ("a" * 64),
    compose_hash="sha256:" + ("b" * 64),
)
_NONCE = gpu_nonce_for_report_data(_REPORT_DATA).hex()


def _b64url(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def _jwt(claims: dict, *, key: ec.EllipticCurvePrivateKey = _SIGNING_KEY, kid: str = _KID) -> str:
    header = _b64url(json.dumps({"alg": "ES384", "kid": kid}).encode())
    body = _b64url(json.dumps(claims).encode())
    der = key.sign(f"{header}.{body}".encode(), ec.ECDSA(hashes.SHA384()))
    r, s = decode_dss_signature(der)
    return f"{header}.{body}.{_b64url(r.to_bytes(48, 'big') + s.to_bytes(48, 'big'))}"


def _jwks() -> dict:
    numbers = _SIGNING_KEY.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "EC",
                "crv": "P-384",
                "kid": _KID,
                "x": _b64url(numbers.x.to_bytes(48, "big")),
                "y": _b64url(numbers.y.to_bytes(48, "big")),
            }
        ]
    }


def _gpu_claims(**overrides) -> dict:
    claims = {claim: True for claim in gpu_module._REQUIRED_TRUE_GPU_CLAIMS}
    claims.update(
        {
            "iss": NRAS_ISSUER,
            "eat_nonce": _NONCE,
            "measres": "success",
            "dbgstat": "disabled",
            "hwmodel": "GH100",
            "ueid": "1234",
            "x-nvidia-gpu-driver-version": "570.211.01",
            "x-nvidia-gpu-vbios-version": "96.00.74.00.11",
        }
    )
    claims.update(overrides)
    return claims


def _nras_response(gpu_claims: dict | None = None, **platform_overrides) -> list:
    gpu_token = _jwt(gpu_claims if gpu_claims is not None else _gpu_claims())
    platform = {
        "iss": NRAS_ISSUER,
        "eat_nonce": _NONCE,
        "x-nvidia-overall-att-result": True,
        "submods": {
            "GPU-0": ["DIGEST", ["SHA-256", hashlib.sha256(gpu_token.encode()).hexdigest()]]
        },
    }
    platform.update(platform_overrides)
    return [["JWT", _jwt(platform)], {"GPU-0": gpu_token}]


def _bundle() -> dict:
    return {
        "format": "nvidia_gpu_cc_v1",
        "arch": "HOPPER",
        "nonce": _NONCE,
        "report_data": _REPORT_DATA.hex(),
        "evidence_list": [{"evidence": "ZXZpZGVuY2U=", "certificate": "Y2VydA=="}],
    }


def test_verify_nras_response_accepts_valid_tokens() -> None:
    gpus = verify_nras_response(
        _nras_response(), expected_nonce=_NONCE, expected_gpu_count=1, jwks=_jwks()
    )
    assert [(gpu.hwmodel, gpu.driver_version) for gpu in gpus] == [("GH100", "570.211.01")]


@pytest.mark.parametrize(
    ("gpu_overrides", "message"),
    [
        ({"measres": "fail"}, "golden"),
        ({"dbgstat": "enabled"}, "debug"),
        ({"x-nvidia-gpu-attestation-report-signature-verified": False}, "signature-verified"),
        ({"eat_nonce": "00" * 32}, "nonce"),
        ({"iss": "https://evil.example"}, "issuer"),
    ],
)
def test_verify_nras_response_rejects_bad_gpu_claims(gpu_overrides, message) -> None:
    with pytest.raises(GpuAttestationError, match=message):
        verify_nras_response(
            _nras_response(_gpu_claims(**gpu_overrides)),
            expected_nonce=_NONCE,
            expected_gpu_count=1,
            jwks=_jwks(),
        )


def test_verify_nras_response_rejects_failed_overall_result() -> None:
    with pytest.raises(GpuAttestationError, match="overall"):
        verify_nras_response(
            _nras_response(**{"x-nvidia-overall-att-result": False}),
            expected_nonce=_NONCE,
            expected_gpu_count=1,
            jwks=_jwks(),
        )


def test_verify_nras_response_rejects_swapped_gpu_token() -> None:
    response = _nras_response()
    response[1]["GPU-0"] = _jwt(_gpu_claims(ueid="other"))
    with pytest.raises(GpuAttestationError, match="digest"):
        verify_nras_response(response, expected_nonce=_NONCE, expected_gpu_count=1, jwks=_jwks())


def test_verify_nras_response_rejects_foreign_signing_key() -> None:
    forged = _nras_response()
    forged[0][1] = _jwt(
        {"iss": NRAS_ISSUER, "eat_nonce": _NONCE, "x-nvidia-overall-att-result": True},
        key=ec.generate_private_key(ec.SECP384R1()),
    )
    with pytest.raises(GpuAttestationError, match="signature is invalid"):
        verify_nras_response(forged, expected_nonce=_NONCE, expected_gpu_count=1, jwks=_jwks())


def test_verify_attestation_bundle_requires_gpu_only_opt_in() -> None:
    with pytest.raises(AttestationError, match="explicitly accept GPU-only"):
        verify_attestation_bundle(
            _bundle(),
            expected_report_data=_REPORT_DATA,
            expected_compose_hash="sha256:" + ("b" * 64),
        )


def test_verify_attestation_bundle_accepts_gpu_bundle_when_opted_in(monkeypatch) -> None:
    submitted: dict = {}

    def fake_post(payload):
        submitted.update(payload)
        return _nras_response()

    monkeypatch.setattr(gpu_module, "_nras_post_json", fake_post)
    monkeypatch.setattr(gpu_module, "_fetch_jwks", _jwks)

    verified = verify_attestation_bundle(
        _bundle(),
        expected_report_data=_REPORT_DATA,
        expected_compose_hash="sha256:" + ("b" * 64),
        accept_gpu_only=True,
    )

    assert submitted["nonce"] == _NONCE
    assert submitted["arch"] == "HOPPER"
    assert verified.compose_event_payload == ""
    assert [gpu.ueid for gpu in verified.gpus] == ["1234"]


def test_verify_attestation_bundle_rejects_gpu_bundle_for_other_report_data() -> None:
    other_report_data = build_node_certificate_report_data(
        certificate_body_hash="sha256:" + ("c" * 64),
        compose_hash="sha256:" + ("b" * 64),
    )
    with pytest.raises(GpuAttestationError, match="nonce"):
        verify_attestation_bundle(
            _bundle(),
            expected_report_data=other_report_data,
            expected_compose_hash="sha256:" + ("b" * 64),
            accept_gpu_only=True,
        )


def test_load_attestation_settings_accepts_nvidia_gpu_cc_mode() -> None:
    settings = load_attestation_settings({"attestation": {"mode": "nvidia_gpu_cc"}})
    assert settings.mode == "nvidia_gpu_cc"
