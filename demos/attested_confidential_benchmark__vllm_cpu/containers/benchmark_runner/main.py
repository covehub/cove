#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

from cove_demo_common import (
    extract_tarball,
    find_first_existing,
    log,
    read_json,
    require_env,
    run,
    sha256_file,
    wait_for_http,
    write_json,
)


SERVICE = "benchmark_runner"


def _resolve_model_dir(extract_root: Path) -> Path:
    children = [child for child in extract_root.iterdir() if child.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_root


def _build_ld_preload() -> str | None:
    tcmalloc_path = find_first_existing(
        [
            "/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4",
            "/usr/lib/aarch64-linux-gnu/libtcmalloc_minimal.so.4",
        ]
    )
    iomp_candidates: list[str] = []
    for site_root in Path("/usr/local/lib/python3.12/site-packages").rglob("libiomp5.so"):
        iomp_candidates.append(str(site_root))
    iomp_path = iomp_candidates[0] if iomp_candidates else None
    values = [path for path in [tcmalloc_path, iomp_path, os.environ.get("LD_PRELOAD")] if path]
    if not values:
        return None
    return ":".join(values)


def _start_vllm_server(model_dir: Path, model_name: str) -> tuple[subprocess.Popen[str], str]:
    host = "127.0.0.1"
    port = "8000"
    env = os.environ.copy()
    env["VLLM_CPU_KVCACHE_SPACE"] = env.get("VLLM_CPU_KVCACHE_SPACE", "4")
    env["VLLM_CPU_NUM_OF_RESERVED_CPU"] = env.get("VLLM_CPU_NUM_OF_RESERVED_CPU", "1")
    ld_preload = _build_ld_preload()
    if ld_preload is not None:
        env["LD_PRELOAD"] = ld_preload
    log(SERVICE, f"starting vLLM CPU server for {model_dir}")
    process = subprocess.Popen(
        [
            "vllm",
            "serve",
            str(model_dir),
            "--host",
            host,
            "--port",
            port,
            "--dtype",
            "float32",
            "--enforce-eager",
            "--served-model-name",
            model_name,
        ],
        env=env,
        text=True,
    )
    wait_for_http(f"http://{host}:{port}/health", timeout_seconds=180)
    return process, f"http://{host}:{port}/v1"


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def main() -> int:
    serving_wheel_path = Path(require_env("SERVING_WHEEL_PATH"))
    model_archive_path = Path(require_env("MODEL_ARCHIVE_PATH"))
    serving_patch_path = Path(require_env("SERVING_PATCH_PATH"))
    eval_code_path = Path(require_env("EVAL_CODE_PATH"))
    eval_data_path = Path(require_env("EVAL_DATA_PATH"))
    result_path = Path(require_env("RESULT_PATH"))
    score_threshold = require_env("SCORE_THRESHOLD")
    vllm_version = os.environ.get("VLLM_VERSION", "0.17.0")

    model_name = os.environ.get("MODEL_NAME", "CoveDemoModel")

    with tempfile.TemporaryDirectory(prefix="cove-benchmark-") as temp_dir:
        temp_root = Path(temp_dir)
        installable_wheel_path = temp_root / f"vllm-{vllm_version}+cpu-cp38-abi3-linux_x86_64.whl"
        shutil.copy2(serving_wheel_path, installable_wheel_path)
        run(
            [
                "python",
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "--no-deps",
                str(installable_wheel_path),
            ]
        )

        temp_root = Path(temp_dir)
        model_root = extract_tarball(model_archive_path, temp_root / "model")
        model_dir = _resolve_model_dir(model_root)
        eval_result_path = temp_root / "eval-result.json"
        server, base_url = _start_vllm_server(model_dir, model_name)
        try:
            run(
                [
                    "python",
                    str(eval_code_path),
                    "--base-url",
                    base_url,
                    "--data-path",
                    str(eval_data_path),
                    "--model",
                    model_name,
                    "--result-path",
                    str(eval_result_path),
                    "--threshold",
                    score_threshold,
                ]
            )
        finally:
            _stop_process(server)

        payload = read_json(eval_result_path)
        payload.setdefault("benchmark_name", "CoveDemoHarmBenchEval")
        payload.setdefault("pass", bool(payload.get("passes_threshold")))
        payload["eval_code_sha256"] = sha256_file(eval_code_path)
        payload["eval_data_sha256"] = sha256_file(eval_data_path)
        payload["model_archive_sha256"] = sha256_file(model_archive_path)
        payload["serving_patch_sha256"] = sha256_file(serving_patch_path)
        payload["serving_wheel_sha256"] = sha256_file(serving_wheel_path)
        write_json(result_path, payload)
        log(
            SERVICE,
            "benchmark complete: "
            f"score={payload.get('score')} passes_threshold={payload.get('passes_threshold')}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
