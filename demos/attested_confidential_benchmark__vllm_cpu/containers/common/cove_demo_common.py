"""Shared helpers for attested confidential benchmark container entrypoints."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tarfile
import time
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
