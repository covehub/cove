#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import urllib.request
from dataclasses import asdict, dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


SA_BENCH_DIR = Path("/opt/sa-bench")
DEFAULT_RESULT_DIR = Path("/logs")
VLLM_HOST = "127.0.0.1"
VLLM_PORT = 8000
RESULT_PORT = 8080


@dataclass(frozen=True)
class BenchmarkProfile:
    name: str
    model_id: str
    served_model_name: str
    dtype: str
    max_model_len: int
    gpu_memory_utilization: float
    isl: int
    osl: int
    concurrencies: str
    req_rate: str
    random_range_ratio: float
    num_prompts_mult: int
    num_warmup_mult: int
    random_num_workers: int
    vllm_extra_args: str


PROFILES: dict[str, BenchmarkProfile] = {
    "smoke": BenchmarkProfile(
        name="smoke",
        model_id="Qwen/Qwen3-0.6B",
        served_model_name="bench-model",
        dtype="bfloat16",
        max_model_len=4096,
        gpu_memory_utilization=0.90,
        isl=128,
        osl=128,
        concurrencies="1x4x8",
        req_rate="inf",
        random_range_ratio=0.8,
        num_prompts_mult=2,
        num_warmup_mult=1,
        random_num_workers=0,
        vllm_extra_args="",
    ),
    "qwen32b-bf16-1k1k": BenchmarkProfile(
        name="qwen32b-bf16-1k1k",
        model_id="Qwen/Qwen3-32B",
        served_model_name="bench-model",
        dtype="bfloat16",
        max_model_len=4096,
        gpu_memory_utilization=0.90,
        isl=1024,
        osl=1024,
        concurrencies="1x4x16x32x64",
        req_rate="inf",
        random_range_ratio=0.8,
        num_prompts_mult=10,
        num_warmup_mult=2,
        random_num_workers=0,
        vllm_extra_args="",
    ),
    "qwen32b-bf16-8k1k": BenchmarkProfile(
        name="qwen32b-bf16-8k1k",
        model_id="Qwen/Qwen3-32B",
        served_model_name="bench-model",
        dtype="bfloat16",
        max_model_len=12288,
        gpu_memory_utilization=0.90,
        isl=8192,
        osl=1024,
        concurrencies="1x2x4x8x16",
        req_rate="inf",
        random_range_ratio=0.8,
        num_prompts_mult=10,
        num_warmup_mult=2,
        random_num_workers=0,
        vllm_extra_args="",
    ),
    "qwen32b-fp8-1k1k": BenchmarkProfile(
        name="qwen32b-fp8-1k1k",
        model_id="Qwen/Qwen3-32B-FP8",
        served_model_name="bench-model",
        dtype="auto",
        max_model_len=4096,
        gpu_memory_utilization=0.90,
        isl=1024,
        osl=1024,
        concurrencies="1x4x16x32x64",
        req_rate="inf",
        random_range_ratio=0.8,
        num_prompts_mult=10,
        num_warmup_mult=2,
        random_num_workers=0,
        vllm_extra_args="",
    ),
    "gptoss120b-mxfp4-smoke": BenchmarkProfile(
        name="gptoss120b-mxfp4-smoke",
        model_id="openai/gpt-oss-120b",
        served_model_name="bench-model",
        dtype="auto",
        max_model_len=4096,
        gpu_memory_utilization=0.95,
        isl=128,
        osl=128,
        concurrencies="1x2",
        req_rate="inf",
        random_range_ratio=0.0,
        num_prompts_mult=2,
        num_warmup_mult=1,
        random_num_workers=0,
        vllm_extra_args="--max-num-batched-tokens 1024",
    ),
    "gptoss120b-mxfp4-1k1k": BenchmarkProfile(
        name="gptoss120b-mxfp4-1k1k",
        model_id="openai/gpt-oss-120b",
        served_model_name="bench-model",
        dtype="auto",
        max_model_len=4096,
        gpu_memory_utilization=0.95,
        isl=1024,
        osl=1024,
        concurrencies="1x4x8x16",
        req_rate="inf",
        random_range_ratio=0.8,
        num_prompts_mult=10,
        num_warmup_mult=2,
        random_num_workers=0,
        vllm_extra_args="--max-num-batched-tokens 1024",
    ),
}


def log(message: str) -> None:
    print(f"[phala-h200-sa-bench] {message}", flush=True)


def env_text(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return default if value is None or value == "" else int(value)


def env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return default if value is None or value == "" else float(value)


def selected_profile() -> BenchmarkProfile:
    profile_name = env_text("BENCH_PROFILE", "smoke")
    if profile_name not in PROFILES:
        valid = ", ".join(sorted(PROFILES))
        raise RuntimeError(f"unknown BENCH_PROFILE={profile_name!r}; valid profiles: {valid}")
    base = PROFILES[profile_name]
    return BenchmarkProfile(
        name=base.name,
        model_id=env_text("MODEL_ID", base.model_id),
        served_model_name=env_text("SERVED_MODEL_NAME", base.served_model_name),
        dtype=env_text("DTYPE", base.dtype),
        max_model_len=env_int("MAX_MODEL_LEN", base.max_model_len),
        gpu_memory_utilization=env_float("GPU_MEMORY_UTILIZATION", base.gpu_memory_utilization),
        isl=env_int("ISL", base.isl),
        osl=env_int("OSL", base.osl),
        concurrencies=env_text("CONCURRENCIES", base.concurrencies),
        req_rate=env_text("REQ_RATE", base.req_rate),
        random_range_ratio=env_float("RANDOM_RANGE_RATIO", base.random_range_ratio),
        num_prompts_mult=env_int("NUM_PROMPTS_MULT", base.num_prompts_mult),
        num_warmup_mult=env_int("NUM_WARMUP_MULT", base.num_warmup_mult),
        random_num_workers=env_int("RANDOM_NUM_WORKERS", base.random_num_workers),
        vllm_extra_args=env_text("VLLM_EXTRA_ARGS", base.vllm_extra_args),
    )


def command_output(command: list[str]) -> str | None:
    try:
        completed = subprocess.run(
            command,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    return completed.stdout.strip()


def gpu_metadata() -> list[dict[str, str]]:
    output = command_output(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if not output:
        return []
    records = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) >= 3:
            records.append(
                {
                    "name": parts[0],
                    "memory_total_mb": parts[1],
                    "driver_version": parts[2],
                }
            )
    return records


def cuda_version() -> str | None:
    output = command_output(["nvidia-smi"])
    if not output:
        return None
    marker = "CUDA Version:"
    if marker not in output:
        return None
    return output.split(marker, 1)[1].split("|", 1)[0].strip()


def vllm_version() -> str | None:
    return command_output(["python3", "-c", "import vllm; print(vllm.__version__)"])


def clipped(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[-limit:]


def cuda_preflight() -> None:
    if env_text("SKIP_CUDA_PREFLIGHT", "0").lower() in {"1", "true", "yes"}:
        log("skipping CUDA preflight")
        return

    code = r"""
import json
import os
import traceback

try:
    import vllm.env_override  # noqa: F401
    import torch

    payload = {
        "cuda_available": torch.cuda.is_available(),
        "cuda_device_count": torch.cuda.device_count(),
        "ld_library_path": os.environ.get("LD_LIBRARY_PATH"),
        "torch_cuda": torch.version.cuda,
        "torch_version": torch.__version__,
        "vllm_cuda_compatibility_path": os.environ.get("VLLM_CUDA_COMPATIBILITY_PATH"),
        "vllm_enable_cuda_compatibility": os.environ.get("VLLM_ENABLE_CUDA_COMPATIBILITY"),
    }
    if not payload["cuda_available"]:
        raise RuntimeError("torch.cuda.is_available() returned false")
    payload["devices"] = [
        {
            "index": index,
            "name": torch.cuda.get_device_name(index),
            "capability": torch.cuda.get_device_capability(index),
        }
        for index in range(payload["cuda_device_count"])
    ]
    print(json.dumps(payload, sort_keys=True))
except Exception as exc:
    print(
        json.dumps(
            {
                "error": str(exc),
                "traceback": traceback.format_exc(),
                "vllm_cuda_compatibility_path": os.environ.get(
                    "VLLM_CUDA_COMPATIBILITY_PATH"
                ),
                "vllm_enable_cuda_compatibility": os.environ.get(
                    "VLLM_ENABLE_CUDA_COMPATIBILITY"
                ),
            },
            sort_keys=True,
        )
    )
    raise
"""
    completed = subprocess.run(
        ["python3", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        if completed.stdout.strip():
            log("CUDA preflight stdout: " + clipped(completed.stdout.strip()))
        if completed.stderr.strip():
            log("CUDA preflight stderr: " + clipped(completed.stderr.strip()))
        raise RuntimeError(
            "CUDA preflight failed before vLLM start. This usually means the "
            "container CUDA stack cannot use the host NVIDIA driver; on "
            "datacenter GPUs keep VLLM_ENABLE_CUDA_COMPATIBILITY=1 and set "
            "VLLM_CUDA_COMPATIBILITY_PATH=/usr/local/cuda/compat, or use a "
            "host driver/image pair that supports this CUDA version."
        )
    if completed.stdout.strip():
        log("CUDA preflight passed: " + clipped(completed.stdout.strip()))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_serve_command(profile: BenchmarkProfile) -> list[str]:
    command = [
        "vllm",
        "serve",
        profile.model_id,
        "--served-model-name",
        profile.served_model_name,
        "--host",
        VLLM_HOST,
        "--port",
        str(VLLM_PORT),
        "--gpu-memory-utilization",
        str(profile.gpu_memory_utilization),
        "--max-model-len",
        str(profile.max_model_len),
    ]
    if profile.dtype != "auto":
        command.extend(["--dtype", profile.dtype])
    command.extend(shlex.split(profile.vllm_extra_args))
    return command


def build_sa_bench_command(
    profile: BenchmarkProfile,
    concurrency: int,
    result_dir: Path,
    *,
    save_result: bool,
) -> list[str]:
    prompts_mult = profile.num_prompts_mult if save_result else profile.num_warmup_mult
    num_prompts = max(1, concurrency * max(1, prompts_mult))
    command = [
        "python3",
        "-u",
        str(SA_BENCH_DIR / "benchmark_serving.py"),
        "--model",
        profile.model_id,
        "--served-model-name",
        profile.served_model_name,
        "--tokenizer",
        profile.model_id,
        "--host",
        VLLM_HOST,
        "--port",
        str(VLLM_PORT),
        "--backend",
        "vllm",
        "--endpoint",
        "/v1/completions",
        "--disable-tqdm",
        "--dataset-name",
        "random",
        "--num-prompts",
        str(num_prompts),
        "--random-input-len",
        str(profile.isl),
        "--random-output-len",
        str(profile.osl),
        "--random-range-ratio",
        str(profile.random_range_ratio),
        "--random-num-workers",
        str(profile.random_num_workers),
        "--ignore-eos",
        "--request-rate",
        profile.req_rate if save_result else "250",
        "--percentile-metrics",
        "ttft,tpot,itl,e2el",
        "--metric-percentiles",
        "50,90,95,99",
        "--max-concurrency",
        str(concurrency),
        "--trust-remote-code",
    ]
    if save_result:
        command.extend(
            [
                "--save-result",
                "--result-dir",
                str(result_dir),
                "--result-filename",
                f"results_concurrency_{concurrency}_gpus_1.json",
            ]
        )
    command.extend(shlex.split(env_text("SA_BENCH_EXTRA_ARGS", "")))
    return command


def wait_for_vllm(process: subprocess.Popen[Any], timeout_seconds: int = 3600) -> None:
    health_url = f"http://{VLLM_HOST}:{VLLM_PORT}/health"
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        exit_code = process.poll()
        if exit_code is not None:
            raise RuntimeError(f"vLLM exited before becoming healthy: exit_code={exit_code}")
        try:
            with urllib.request.urlopen(health_url, timeout=5) as response:
                if 200 <= response.status < 300:
                    log("vLLM health endpoint is ready")
                    return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(5)
    raise RuntimeError(f"vLLM did not become healthy at {health_url}: {last_error}")


def run_checked(command: list[str]) -> None:
    log("$ " + shlex.join(command))
    subprocess.run(command, check=True)


def stop_process(process: subprocess.Popen[Any] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=30)


def parse_concurrencies(value: str) -> list[int]:
    result = []
    for item in value.replace(",", "x").split("x"):
        item = item.strip()
        if item:
            result.append(int(item))
    if not result:
        raise RuntimeError("at least one concurrency is required")
    return result


class ResultsHandler(SimpleHTTPRequestHandler):
    result_root: Path

    def log_message(self, fmt: str, *args: object) -> None:
        log("result-server " + (fmt % args))

    def translate_path(self, path: str) -> str:
        parsed = urlparse(path)
        request_path = unquote(parsed.path)
        if request_path in {"", "/"}:
            request_path = "/results/"
        if request_path == "/healthz":
            return str(self.result_root / ".healthz")
        if request_path == "/results":
            relative = ""
        elif request_path.startswith("/results/"):
            relative = request_path[len("/results/") :]
        else:
            relative = "__missing__"
        safe_parts = [part for part in relative.split("/") if part not in {"", ".", ".."}]
        return str(self.result_root.joinpath(*safe_parts))

    def do_GET(self) -> None:  # noqa: N802
        if urlparse(self.path).path == "/healthz":
            body = b'{"status":"ok"}\n'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()


def serve_results(result_dir: Path) -> None:
    handler = partial(ResultsHandler)
    ResultsHandler.result_root = result_dir
    server = ThreadingHTTPServer(("0.0.0.0", RESULT_PORT), handler)
    log(f"serving results at http://0.0.0.0:{RESULT_PORT}/results/")
    server.serve_forever()


def metadata(profile: BenchmarkProfile, serve_command: list[str]) -> dict[str, Any]:
    return {
        "benchmark_profile": asdict(profile),
        "bench_image_ref": os.environ.get("BENCH_IMAGE_REF") or os.environ.get("IMAGE_REF"),
        "cuda_compatibility": {
            "enabled": os.environ.get("VLLM_ENABLE_CUDA_COMPATIBILITY"),
            "path": os.environ.get("VLLM_CUDA_COMPATIBILITY_PATH"),
        },
        "cuda_version": cuda_version(),
        "gpus": gpu_metadata(),
        "phala": {
            "app_id": os.environ.get("PHALA_APP_ID"),
            "cvm_id": os.environ.get("PHALA_CVM_ID"),
            "cvm_name": os.environ.get("PHALA_CVM_NAME"),
            "instance_type": os.environ.get("PHALA_INSTANCE_TYPE"),
        },
        "serve_command": serve_command,
        "timestamp_unix": time.time(),
        "vllm_version": vllm_version(),
    }


def run_benchmark(profile: BenchmarkProfile, result_dir: Path) -> None:
    serve_command = build_serve_command(profile)
    write_json(result_dir / "environment.json", metadata(profile, serve_command))
    cuda_preflight()

    vllm_process: subprocess.Popen[Any] | None = None
    try:
        log("starting vLLM")
        log("$ " + shlex.join(serve_command))
        vllm_process = subprocess.Popen(serve_command)
        wait_for_vllm(vllm_process)

        concurrencies = parse_concurrencies(profile.concurrencies)
        sa_result_dir = result_dir / f"sa-bench_isl_{profile.isl}_osl_{profile.osl}"
        sa_result_dir.mkdir(parents=True, exist_ok=True)

        for concurrency in concurrencies:
            if profile.num_warmup_mult > 0:
                log(f"warmup concurrency={concurrency}")
                run_checked(
                    build_sa_bench_command(
                        profile,
                        concurrency,
                        sa_result_dir,
                        save_result=False,
                    )
                )
            log(f"benchmark concurrency={concurrency}")
            run_checked(
                build_sa_bench_command(
                    profile,
                    concurrency,
                    sa_result_dir,
                    save_result=True,
                )
            )

        run_checked(["python3", str(SA_BENCH_DIR / "rollup.py"), str(result_dir)])
        rollup_path = result_dir / "benchmark-rollup.json"
        if rollup_path.exists():
            log("benchmark rollup:")
            print(rollup_path.read_text(encoding="utf-8"), flush=True)
        write_json(result_dir / "status.json", {"status": "complete", "profile": profile.name})
    finally:
        stop_process(vllm_process)


def dry_run(profile: BenchmarkProfile, result_dir: Path) -> None:
    serve_command = build_serve_command(profile)
    concurrencies = parse_concurrencies(profile.concurrencies)
    sa_result_dir = result_dir / f"sa-bench_isl_{profile.isl}_osl_{profile.osl}"
    payload = {
        "profile": asdict(profile),
        "serve_command": serve_command,
        "benchmark_commands": [
            build_sa_bench_command(profile, concurrency, sa_result_dir, save_result=True)
            for concurrency in concurrencies
        ],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Phala H200 vLLM SA-Bench")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    profile = selected_profile()
    result_dir = Path(env_text("RESULT_DIR", str(DEFAULT_RESULT_DIR)))

    if args.dry_run or env_text("DRY_RUN", "0") in {"1", "true", "yes"}:
        dry_run(profile, result_dir)
        return 0

    result_dir.mkdir(parents=True, exist_ok=True)

    try:
        run_benchmark(profile, result_dir)
    except Exception as exc:
        log(f"benchmark failed: {exc}")
        write_json(
            result_dir / "failure.json",
            {
                "error": str(exc),
                "profile": profile.name,
                "status": "failed",
                "timestamp_unix": time.time(),
            },
        )
    serve_results(result_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
