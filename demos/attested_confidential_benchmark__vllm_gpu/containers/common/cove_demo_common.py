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
import time
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request


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
