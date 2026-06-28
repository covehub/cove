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


def test_vllm_log_observer_extracts_model_and_cuda_graph_events(monkeypatch) -> None:
    module = _load_gpu_common()
    times = iter([100.0, 101.0, 105.0, 108.0, 109.0])
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))

    observer = module.VllmLogObserver()
    observer.observe("Loading model weights took 3.2 GiB")
    observer.observe("Capturing CUDA graph for decoding")
    observer.observe("CUDA graph capture finished")
    metrics = observer.metrics()

    assert metrics["vllm_log_model_load_observed_seconds"] == 0.0
    assert metrics["vllm_log_cuda_graph_observed_seconds"] == 3.0


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


def test_timing_recorder_is_env_gated(monkeypatch) -> None:
    module = _load_gpu_common()
    monkeypatch.setenv("COVE_DEMO_ENABLE_TIMING", "1")
    recorder = module.TimingRecorder()
    payload = {}

    with module.timed_step(recorder, "example_seconds"):
        pass
    recorder.add_to_payload(payload)

    assert payload["total_wall_seconds"] >= 0
    assert payload["timings_seconds"]["example_seconds"] >= 0


def test_add_timing_metadata_builds_benchmark_profile(monkeypatch) -> None:
    module = _load_gpu_common()
    monkeypatch.setenv("COVE_DEMO_ENABLE_TIMING", "1")
    recorder = module.TimingRecorder()
    recorder.timings = {
        "load_seconds": 1.25,
        "run_seconds": 2.5,
        "cleanup_seconds": 0.25,
    }
    payload = {}

    module.add_timing_metadata(
        payload,
        recorder,
        workload_keys=["run_seconds"],
        startup_keys=["load_seconds"],
        teardown_keys=["cleanup_seconds"],
        public_assets_already_cached=True,
    )

    assert payload["benchmark_profile"]["workload_seconds"] == 2.5
    assert payload["benchmark_profile"]["startup_seconds"] == 1.25
    assert payload["benchmark_profile"]["teardown_seconds"] == 0.25
    assert payload["benchmark_profile"]["outer_wall_seconds"] == payload["total_wall_seconds"]
    assert payload["benchmark_profile"]["public_assets_already_cached"] is True
