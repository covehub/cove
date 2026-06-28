"""Shared helpers for attested confidential benchmark container entrypoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


def env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


class TimingRecorder:
    def __init__(self, *, enabled: bool | None = None) -> None:
        self.enabled = (
            env_flag("COVE_DEMO_ENABLE_TIMING", default=False)
            if enabled is None
            else enabled
        )
        self._started_at = time.monotonic()
        self._started_at_iso = utc_now_iso()
        self.timings: dict[str, float] = {}

    def checkpoint(self, name: str, started_at: float) -> None:
        if self.enabled:
            self.timings[name] = round(time.monotonic() - started_at, 6)

    def mark_total(self, name: str = "total_wall_seconds") -> None:
        if self.enabled:
            self.timings[name] = round(time.monotonic() - self._started_at, 6)

    def add_to_payload(self, payload: dict[str, object]) -> None:
        if not self.enabled:
            return
        self.mark_total()
        payload["timings_seconds"] = dict(self.timings)
        payload["total_wall_seconds"] = self.timings["total_wall_seconds"]

    def summary(self) -> dict[str, float]:
        if not self.enabled:
            return {}
        self.mark_total()
        return dict(self.timings)

    @property
    def started_at_iso(self) -> str:
        return self._started_at_iso

    def add(self, name: str, seconds: float) -> None:
        if self.enabled:
            self.timings[name] = round(max(0.0, float(seconds)), 6)

    def merge(self, values: dict[str, float]) -> None:
        for key, value in values.items():
            self.add(key, value)


class timed_step:
    def __init__(self, timings: TimingRecorder, name: str) -> None:
        self.timings = timings
        self.name = name
        self.started_at = 0.0

    def __enter__(self) -> None:
        self.started_at = time.monotonic()

    def __exit__(self, _exc_type: object, _exc: object, _traceback: object) -> None:
        self.timings.checkpoint(self.name, self.started_at)


def _sum_timing_keys(timings: dict[str, float], keys: list[str]) -> float:
    return round(sum(float(timings.get(key, 0.0)) for key in keys), 6)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def write_runtime_metrics_payload(
    *,
    service_name: str,
    role: str,
    timing_started_at: str,
    summary: dict[str, float],
    benchmark_profile: dict[str, object] | None = None,
    extra: dict[str, object] | None = None,
) -> None:
    metrics_path = os.environ.get("COVE_RUNTIME_METRICS_PATH")
    if not metrics_path:
        return
    payload: dict[str, object] = {
        "schema_version": "cove_runtime_metrics_v1",
        "service_name": service_name,
        "role": role,
        "started_at": timing_started_at,
        "ended_at": utc_now_iso(),
        "wall_seconds": float(summary.get("total_wall_seconds", 0.0)),
        "phase_timings_seconds": {
            key: value
            for key, value in sorted(summary.items())
            if key != "total_wall_seconds"
        },
        "exit_code": 0,
        "ok": True,
    }
    if benchmark_profile is not None:
        payload["benchmark_profile"] = benchmark_profile
    if extra:
        payload.update(extra)
    write_json(metrics_path, payload)


def benchmark_profile_from_summary(
    summary: dict[str, float],
    *,
    workload_keys: list[str],
    startup_keys: list[str] | None = None,
    artifact_io_keys: list[str] | None = None,
    teardown_keys: list[str] | None = None,
    public_assets_already_cached: bool | None = None,
) -> dict[str, object]:
    profile: dict[str, object] = {
        "artifact_io_seconds": _sum_timing_keys(summary, artifact_io_keys or []),
        "outer_wall_seconds": float(summary.get("total_wall_seconds", 0.0)),
        "startup_seconds": _sum_timing_keys(summary, startup_keys or []),
        "teardown_seconds": _sum_timing_keys(summary, teardown_keys or []),
        "workload_seconds": _sum_timing_keys(summary, workload_keys),
        "workload_timing_keys": list(workload_keys),
        "startup_timing_keys": list(startup_keys or []),
        "artifact_io_timing_keys": list(artifact_io_keys or []),
        "teardown_timing_keys": list(teardown_keys or []),
    }
    if public_assets_already_cached is not None:
        profile["public_assets_already_cached"] = public_assets_already_cached
    return profile


def add_timing_metadata(
    payload: dict[str, object],
    timings: TimingRecorder,
    *,
    workload_keys: list[str],
    startup_keys: list[str] | None = None,
    artifact_io_keys: list[str] | None = None,
    teardown_keys: list[str] | None = None,
    public_assets_already_cached: bool | None = None,
) -> None:
    if not timings.enabled:
        return
    timings.mark_total()
    summary = dict(timings.timings)
    payload["timings_seconds"] = summary
    payload["total_wall_seconds"] = summary["total_wall_seconds"]
    payload["benchmark_profile"] = benchmark_profile_from_summary(
        summary,
        workload_keys=workload_keys,
        startup_keys=startup_keys,
        artifact_io_keys=artifact_io_keys,
        teardown_keys=teardown_keys,
        public_assets_already_cached=public_assets_already_cached,
    )
    write_runtime_metrics_payload(
        service_name=os.environ.get("COVE_SERVICE_NAME", "workload"),
        role=os.environ.get("COVE_SERVICE_ROLE", "workload"),
        timing_started_at=timings.started_at_iso,
        summary=summary,
        benchmark_profile=payload["benchmark_profile"],
    )


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def optional_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def ensure_parent(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def ensure_dir(path: str | Path) -> Path:
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def log(service: str, message: str) -> None:
    print(f"[{service}] {message}", flush=True)


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def write_text(path: str | Path, payload: str) -> None:
    ensure_parent(path)
    Path(path).write_text(payload, encoding="utf-8")


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(read_text(path))


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def extract_tarball(archive_path: str | Path, destination: str | Path) -> Path:
    destination_path = ensure_dir(destination)
    with tarfile.open(archive_path, "r:*") as tar:
        tar.extractall(destination_path)
    return destination_path


def copy_tree(src: str | Path, dst: str | Path) -> Path:
    target = Path(dst)
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(src, dst)
    return target


def run(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        text=True,
        capture_output=capture_output,
        check=True,
    )


class VllmLogObserver:
    def __init__(self) -> None:
        self._started_at = time.monotonic()
        self._events: dict[str, list[float]] = {
            "model_load": [],
            "cuda_graph": [],
            "torch_compile": [],
        }

    def observe(self, line: str) -> None:
        lowered = line.lower()
        elapsed = time.monotonic() - self._started_at
        if any(pattern in lowered for pattern in ("loading model weights", "loading weights", "model weights")):
            self._events["model_load"].append(elapsed)
        if any(pattern in lowered for pattern in ("cuda graph", "cudagraph", "cudagraphs")):
            self._events["cuda_graph"].append(elapsed)
        if "torch.compile" in lowered or "compilation" in lowered:
            self._events["torch_compile"].append(elapsed)

    def metrics(self) -> dict[str, float]:
        values: dict[str, float] = {}
        for name, events in self._events.items():
            if not events:
                continue
            values[f"vllm_log_{name}_first_seen_seconds"] = round(min(events), 6)
            values[f"vllm_log_{name}_last_seen_seconds"] = round(max(events), 6)
            values[f"vllm_log_{name}_observed_seconds"] = round(max(events) - min(events), 6)
        return values


def start_logged_process(
    command: list[str],
    *,
    env: dict[str, str],
    service: str,
    observer: VllmLogObserver | None = None,
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )

    def forward_logs() -> None:
        if process.stdout is None:
            return
        for line in process.stdout:
            if observer is not None:
                observer.observe(line)
            print(line, end="", flush=True)

    thread = threading.Thread(
        target=forward_logs,
        name=f"{service}-vllm-log-forwarder",
        daemon=True,
    )
    thread.start()
    return process


def wait_for_http(url: str, *, timeout_seconds: float, interval_seconds: float = 1.0) -> None:
    deadline = time.time() + timeout_seconds
    last_error = "unknown error"
    while time.time() < deadline:
        try:
            with urllib_request.urlopen(url, timeout=5) as response:
                if 200 <= response.status < 500:
                    return
        except urllib_error.URLError as exc:
            last_error = str(exc.reason)
        except urllib_error.HTTPError as exc:
            if exc.code < 500:
                return
            last_error = f"http {exc.code}"
        time.sleep(interval_seconds)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def run_openai_chat_probe(
    *,
    base_url: str,
    model: str,
    timeout_seconds: float = 60.0,
    extra_headers: dict[str, str] | None = None,
) -> dict[str, object]:
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": "Reply with exactly: cove benchmark probe",
            }
        ],
        "max_tokens": 8,
        "temperature": 0,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer EMPTY",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib_request.urlopen(request, timeout=timeout_seconds) as response:
        body = response.read()
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise RuntimeError("single request probe returned a non-object JSON payload")
    return {
        "status": "ok",
        "response_id": parsed.get("id", ""),
        "model": parsed.get("model", model),
    }


def find_first_existing(paths: list[str]) -> str | None:
    for path in paths:
        if Path(path).exists():
            return path
    return None


def _major_version(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.search(r"\d+", value)
    if match is None:
        return None
    return int(match.group(0))


def parse_nvidia_smi_versions(text: str) -> dict[str, str]:
    driver_match = re.search(r"Driver Version:\s*([0-9][0-9.]+)", text)
    cuda_match = re.search(r"CUDA Version:\s*([0-9][0-9.]+)", text)
    return {
        "driver_version": driver_match.group(1) if driver_match else "",
        "cuda_version": cuda_match.group(1) if cuda_match else "",
    }


def parse_nvidia_smi_query_line(text: str) -> dict[str, str]:
    line = next((candidate.strip() for candidate in text.splitlines() if candidate.strip()), "")
    name, separator, driver_version = line.partition(",")
    if not separator:
        return {"gpu_name": line, "driver_version": ""}
    return {
        "gpu_name": name.strip(),
        "driver_version": driver_version.strip(),
    }


def summarize_confidential_compute_status(text: str) -> str:
    labels = {
        "CC State",
        "CPU CC Capabilities",
        "GPU CC Capabilities",
        "CC GPUs Ready State",
        "Multi-GPU Mode",
    }
    values: list[str] = []
    for line in text.splitlines():
        label, separator, value = line.partition(":")
        if separator and label.strip() in labels:
            values.append(f"{label.strip()}: {value.strip()}")
    return "; ".join(values)


def _run_optional(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(args, text=True, capture_output=True, check=False)
    except FileNotFoundError:
        return None


def collect_gpu_status() -> dict[str, object]:
    status: dict[str, object] = {
        "gpu_cuda_available": False,
        "gpu_confidential_compute_status": "",
        "gpu_name": "",
        "gpu_nvidia_cuda_version": "",
        "gpu_nvidia_driver_version": "",
        "gpu_preflight_required": True,
        "gpu_torch_cuda_version": "",
    }

    try:
        import torch

        cuda_available = bool(torch.cuda.is_available())
        status["gpu_cuda_available"] = cuda_available
        status["gpu_torch_cuda_version"] = str(torch.version.cuda or "")
        if cuda_available:
            status["gpu_name"] = str(torch.cuda.get_device_name(0))
    except Exception as exc:  # pragma: no cover - depends on runtime host
        status["gpu_error"] = f"torch CUDA check failed: {exc}"

    nvidia_smi = _run_optional(["nvidia-smi"])
    if nvidia_smi is not None:
        versions = parse_nvidia_smi_versions(nvidia_smi.stdout + nvidia_smi.stderr)
        status["gpu_nvidia_driver_version"] = versions["driver_version"]
        status["gpu_nvidia_cuda_version"] = versions["cuda_version"]
        if nvidia_smi.returncode != 0:
            status["gpu_nvidia_smi_error"] = (nvidia_smi.stderr or nvidia_smi.stdout).strip()

    query = _run_optional(
        [
            "nvidia-smi",
            "--query-gpu=name,driver_version",
            "--format=csv,noheader,nounits",
        ]
    )
    if query is not None and query.returncode == 0:
        query_fields = parse_nvidia_smi_query_line(query.stdout)
        if query_fields["gpu_name"]:
            status["gpu_name"] = query_fields["gpu_name"]
        if query_fields["driver_version"]:
            status["gpu_nvidia_driver_version"] = query_fields["driver_version"]

    conf_compute = _run_optional(["nvidia-smi", "conf-compute", "-q"])
    if conf_compute is not None:
        summary = summarize_confidential_compute_status(
            conf_compute.stdout + conf_compute.stderr
        )
        status["gpu_confidential_compute_status"] = summary
        if conf_compute.returncode != 0:
            status["gpu_confidential_compute_error"] = (
                conf_compute.stderr or conf_compute.stdout
            ).strip()

    return status


def require_cuda_preflight(service: str) -> dict[str, object]:
    status = collect_gpu_status()
    required = os.environ.get("REQUIRE_CUDA", "1").strip().lower() not in {
        "0",
        "false",
        "no",
    }
    status["gpu_preflight_required"] = required
    if not required:
        status["gpu_preflight_passed"] = True
        log(service, "CUDA preflight skipped because REQUIRE_CUDA is disabled")
        return status

    failures: list[str] = []
    if status.get("gpu_cuda_available") is not True:
        failures.append("torch.cuda.is_available() is false")

    cuda_major = _major_version(status.get("gpu_nvidia_cuda_version")) or _major_version(
        status.get("gpu_torch_cuda_version")
    )
    if cuda_major != 13:
        failures.append(
            "CUDA major version must be 13 "
            f"(nvidia-smi={status.get('gpu_nvidia_cuda_version')!r}, "
            f"torch={status.get('gpu_torch_cuda_version')!r})"
        )

    driver_major = _major_version(status.get("gpu_nvidia_driver_version"))
    if driver_major is None or driver_major < 580:
        failures.append(
            "NVIDIA driver must be R580+ "
            f"(observed={status.get('gpu_nvidia_driver_version')!r})"
        )

    if failures:
        status["gpu_preflight_passed"] = False
        raise RuntimeError("CUDA preflight failed: " + "; ".join(failures))

    status["gpu_preflight_passed"] = True
    log(
        service,
        "CUDA preflight passed: "
        f"gpu={status.get('gpu_name')} "
        f"driver={status.get('gpu_nvidia_driver_version')} "
        f"cuda={status.get('gpu_nvidia_cuda_version') or status.get('gpu_torch_cuda_version')}",
    )
    return status


def wheel_install_filename(wheel_path: str | Path) -> str:
    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_name = next(
            name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")
        )
        wheel_name = next(
            name for name in wheel.namelist() if name.endswith(".dist-info/WHEEL")
        )
        metadata = Parser().parsestr(wheel.read(metadata_name).decode("utf-8"))
        wheel_metadata = Parser().parsestr(wheel.read(wheel_name).decode("utf-8"))

    package_name = str(metadata.get("Name", "")).strip()
    version = str(metadata.get("Version", "")).strip()
    tags = wheel_metadata.get_all("Tag") or []
    if not package_name or not version or not tags:
        raise ValueError(f"wheel metadata is incomplete for {wheel_path}")
    normalized_name = re.sub(r"[-_.]+", "_", package_name).strip("_")
    return f"{normalized_name}-{version}-{tags[0].strip()}.whl"


def install_private_wheel(wheel_path: str | Path, temp_root: str | Path) -> Path:
    installable_wheel_path = Path(temp_root) / wheel_install_filename(wheel_path)
    shutil.copy2(wheel_path, installable_wheel_path)
    run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--force-reinstall",
            "--no-deps",
            str(installable_wheel_path),
        ]
    )
    return installable_wheel_path
