from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
GPU_COMMON_PATH = (
    REPO_ROOT
    / "demos/attested_confidential_benchmark__vllm_gpu/containers/common/cove_demo_common.py"
)


def _load_gpu_common():
    spec = importlib.util.spec_from_file_location(
        "attested_benchmark_gpu_common_under_test",
        GPU_COMMON_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_parse_nvidia_smi_versions() -> None:
    module = _load_gpu_common()
    payload = (
        "| NVIDIA-SMI 580.95.05        Driver Version: 580.95.05        "
        "CUDA Version: 13.0     |\n"
    )

    assert module.parse_nvidia_smi_versions(payload) == {
        "driver_version": "580.95.05",
        "cuda_version": "13.0",
    }


def test_parse_nvidia_smi_query_line() -> None:
    module = _load_gpu_common()

    assert module.parse_nvidia_smi_query_line("NVIDIA H200, 580.95.05\n") == {
        "gpu_name": "NVIDIA H200",
        "driver_version": "580.95.05",
    }


def test_summarize_confidential_compute_status() -> None:
    module = _load_gpu_common()
    text = """
==============NVSMI CONF-COMPUTE LOG==============
    CC State                   : ON
    Multi-GPU Mode             : None
    CPU CC Capabilities        : INTEL TDX
    GPU CC Capabilities        : CC Capable
    CC GPUs Ready State        : Ready
"""

    assert module.summarize_confidential_compute_status(text) == (
        "CC State: ON; Multi-GPU Mode: None; CPU CC Capabilities: INTEL TDX; "
        "GPU CC Capabilities: CC Capable; CC GPUs Ready State: Ready"
    )


def test_require_cuda_preflight_accepts_cuda13_r580(monkeypatch) -> None:
    module = _load_gpu_common()
    monkeypatch.setattr(
        module,
        "collect_gpu_status",
        lambda: {
            "gpu_cuda_available": True,
            "gpu_name": "NVIDIA H200",
            "gpu_nvidia_cuda_version": "13.0",
            "gpu_nvidia_driver_version": "580.95.05",
            "gpu_torch_cuda_version": "13.0",
        },
    )

    status = module.require_cuda_preflight("test")

    assert status["gpu_preflight_passed"] is True


def test_require_cuda_preflight_rejects_cuda12_r570(monkeypatch) -> None:
    module = _load_gpu_common()
    monkeypatch.setattr(
        module,
        "collect_gpu_status",
        lambda: {
            "gpu_cuda_available": True,
            "gpu_name": "NVIDIA H200",
            "gpu_nvidia_cuda_version": "12.8",
            "gpu_nvidia_driver_version": "570.133.20",
            "gpu_torch_cuda_version": "12.8",
        },
    )

    with pytest.raises(RuntimeError, match="CUDA major version must be 13"):
        module.require_cuda_preflight("test")


def test_wheel_install_filename_uses_wheel_metadata(tmp_path) -> None:
    module = _load_gpu_common()
    wheel_path = tmp_path / "compiled_serving_wheel.whl"
    dist_info = "vllm-0.17.0+cu130.dist-info"
    with zipfile.ZipFile(wheel_path, "w") as wheel:
        wheel.writestr(
            f"{dist_info}/METADATA",
            "Metadata-Version: 2.4\nName: vllm\nVersion: 0.17.0+cu130\n",
        )
        wheel.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nTag: cp38-abi3-manylinux_2_35_x86_64\n",
        )

    assert (
        module.wheel_install_filename(wheel_path)
        == "vllm-0.17.0+cu130-cp38-abi3-manylinux_2_35_x86_64.whl"
    )
