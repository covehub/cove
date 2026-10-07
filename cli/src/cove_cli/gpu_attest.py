from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from cove_container_runtime.attestation import (
    AttestationSettings,
    collect_attestation_bundle,
    verify_attestation_bundle,
)
from cove_container_runtime.common import RuntimeErrorBase as RuntimeAttestationError

from .common import RuntimeErrorBase


_CLI_GPU_REPORT_LABEL = b"cove_gpu_attest_cli_v1"


class GpuAttestCommandError(RuntimeErrorBase):
    """Raised when a `cove attest` command fails."""


def build_cli_report_data(*, message: str | None, report_data_hex: str | None) -> bytes:
    if message is not None and report_data_hex is not None:
        raise GpuAttestCommandError("pass at most one of --message and --report-data-hex")
    if report_data_hex is not None:
        try:
            report_data = bytes.fromhex(report_data_hex.removeprefix("0x"))
        except ValueError as exc:
            raise GpuAttestCommandError("--report-data-hex must be hex") from exc
        if not 1 <= len(report_data) <= 64:
            raise GpuAttestCommandError("--report-data-hex must be 1-64 bytes")
        return report_data
    if message is not None:
        return _CLI_GPU_REPORT_LABEL + hashlib.sha256(message.encode("utf-8")).digest()
    return _CLI_GPU_REPORT_LABEL + os.urandom(32)


def attest_gpu_command(
    *,
    message: str | None,
    report_data_hex: str | None,
    output: str | None,
    verify: bool,
) -> str:
    report_data = build_cli_report_data(message=message, report_data_hex=report_data_hex)
    try:
        bundle = collect_attestation_bundle(
            AttestationSettings(mode="nvidia_gpu_cc", provider=None, runtime=None),
            report_data=report_data,
        )
    except RuntimeAttestationError as exc:
        raise GpuAttestCommandError(str(exc)) from exc
    output_path = Path(output) if output else Path("gpu_attestation.json")
    output_path.write_text(json.dumps(bundle, indent=2) + "\n", encoding="utf-8")
    summary: dict[str, Any] = {
        "bundle": str(output_path),
        "report_data": report_data.hex(),
        "nonce": bundle["nonce"],
        "gpus": [
            {"uuid": item["uuid"], "vbios_version": item["vbios_version"]}
            for item in bundle["evidence_list"]
        ],
        "driver_version": bundle["driver_version"],
    }
    if verify:
        summary["verification"] = _verify(bundle, report_data)
    return json.dumps(summary, indent=2)


def verify_gpu_bundle_command(
    bundle_path: str,
    *,
    message: str | None,
    report_data_hex: str | None,
) -> str:
    try:
        bundle = json.loads(Path(bundle_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GpuAttestCommandError(f"cannot read attestation bundle {bundle_path}: {exc}") from exc
    if not isinstance(bundle, dict):
        raise GpuAttestCommandError("attestation bundle must be a JSON object")
    freshness_checked = message is not None or report_data_hex is not None
    if freshness_checked:
        report_data = build_cli_report_data(message=message, report_data_hex=report_data_hex)
    else:
        try:
            report_data = bytes.fromhex(str(bundle.get("report_data", "")))
        except ValueError as exc:
            raise GpuAttestCommandError("bundle report_data must be hex") from exc
    result = _verify(bundle, report_data)
    result["freshness_checked"] = freshness_checked
    if not freshness_checked:
        result["warning"] = (
            "verified against the bundle's own report_data; pass --message or "
            "--report-data-hex with your challenge to check freshness and binding"
        )
    return json.dumps(result, indent=2)


def _verify(bundle: dict[str, Any], report_data: bytes) -> dict[str, Any]:
    try:
        verified = verify_attestation_bundle(
            bundle,
            expected_report_data=report_data,
            expected_compose_hash="sha256:" + "0" * 64,
            accept_gpu_only=True,
        )
    except RuntimeAttestationError as exc:
        raise GpuAttestCommandError(f"GPU attestation verification failed: {exc}") from exc
    return {
        "verified": True,
        "verifier": "NVIDIA NRAS (EAT tokens checked against NRAS JWKS)",
        "workload_measured": False,
        "gpus": [
            {
                "ueid": gpu.ueid,
                "hwmodel": gpu.hwmodel,
                "driver_version": gpu.driver_version,
                "vbios_version": gpu.vbios_version,
                "measurements": gpu.claims.get("measres"),
                "secure_boot": gpu.claims.get("secboot"),
                "debug": gpu.claims.get("dbgstat"),
            }
            for gpu in verified.gpus
        ],
    }
