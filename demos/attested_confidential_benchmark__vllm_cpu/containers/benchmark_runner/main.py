#!/usr/bin/env python3
"""Run the attested benchmark node and write aggregate evaluation metrics."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

from inspect_ai import Task, eval as inspect_eval

from cove_demo_common import (
    extract_tarball,
    find_first_existing,
    log,
    require_env,
    run,
    sha256_file,
    wait_for_http,
    write_json,
)


SERVICE = "benchmark_runner"
TASK_FACTORY_NAME = "harmbench_direct_request"


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


def _validated_base_url(base_url: str) -> str:
    value = base_url.strip().rstrip("/")
    lowered = value.lower()
    if not lowered.startswith("http://"):
        raise ValueError("base URL must point to the local model endpoint")
    host_port, _, path = value[len("http://") :].partition("/")
    host, separator, port_text = host_port.partition(":")
    if host not in {"127.0.0.1", "localhost"} or separator != ":":
        raise ValueError("base URL must point to the local model endpoint")
    try:
        int(port_text)
    except ValueError as exc:
        raise ValueError("base URL must include a numeric local port") from exc
    api_prefix = "/" + path.strip("/")
    if api_prefix != "/v1":
        raise ValueError("base URL must point to the local OpenAI-compatible /v1 endpoint")
    return value


def _validated_model_name(model: str) -> str:
    if model != "CoveDemoModel":
        raise ValueError("eval must target the locally served CoveDemoModel")
    return model


def _validated_file(path: Path, *, suffix: str, description: str) -> Path:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{description} path must be a file")
    if resolved.suffix != suffix:
        raise ValueError(f"{description} path must end in {suffix}")
    return resolved


def _load_private_eval_module(eval_code_path: Path) -> ModuleType:
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location("bob_private_eval_code", eval_code_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load private eval artifact at {eval_code_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_private_task(eval_code_path: Path, data_path: Path) -> Task:
    # Bob's private artifact is intentionally imported here. It should define
    # the Inspect task/scorer only; this public runner owns execution policy.
    module = _load_private_eval_module(eval_code_path)
    task_factory = getattr(module, TASK_FACTORY_NAME, None)
    if not callable(task_factory):
        raise RuntimeError(
            f"private eval artifact must define callable {TASK_FACTORY_NAME}()"
        )
    return task_factory(data_path=str(data_path))


def _aggregate_inspect_log(log_object: object, *, benchmark_name: str, threshold: float) -> dict[str, object]:
    status = getattr(log_object, "status", None)
    if status != "success":
        raise RuntimeError(f"Inspect eval did not finish successfully: {status}")

    samples = getattr(log_object, "samples", None) or []
    attack_successes = 0
    for sample in samples:
        scores = getattr(sample, "scores", None) or {}
        if "refusal" not in scores:
            raise RuntimeError("Inspect eval sample is missing refusal score")
        score = scores["refusal"]
        if float(score.value) >= 1.0:
            attack_successes += 1

    total = len(samples)
    attack_success_rate = 0.0 if total == 0 else attack_successes / total
    refusal_rate = 1.0 - attack_success_rate
    return {
        "benchmark_name": benchmark_name,
        "pass": refusal_rate > threshold,
        "passes_threshold": refusal_rate > threshold,
        "score": refusal_rate,
        "successful_responses": total - attack_successes,
        "total_prompts": total,
        "attack_success_rate": attack_success_rate,
        "attack_successes": attack_successes,
        "refusal_rate": refusal_rate,
        "harmbench_method": "DirectRequest",
        "harmbench_split": "text_test",
        "inspect_display": "plain",
        "inspect_runner": True,
        "inspect_sandbox": None,
        "max_connections": 1,
        "model_provider": "openai-api",
        "model_service": "cove",
        "scoring_method": "refusal",
    }


def _evaluate_private_task(
    *,
    eval_code_path: Path,
    base_url: str,
    data_path: Path,
    model: str,
    threshold: float,
    request_timeout: float,
) -> dict[str, object]:
    eval_code_path = _validated_file(
        eval_code_path,
        suffix=".py",
        description="private eval code",
    )
    base_url = _validated_base_url(base_url)
    model = _validated_model_name(model)
    data_path = _validated_file(data_path, suffix=".jsonl", description="private eval data")
    task_obj = _load_private_task(eval_code_path, data_path)

    with tempfile.TemporaryDirectory(prefix="cove-inspect-eval-") as log_dir:
        logs = inspect_eval(
            task_obj,
            model=f"openai-api/{model}",
            model_base_url=base_url,
            model_args={"api_key": "EMPTY", "service": "cove"},
            sandbox=None,
            display="plain",
            log_dir=log_dir,
            max_connections=1,
            timeout=request_timeout,
            fail_on_error=True,
            log_samples=True,
        )
        if len(logs) != 1:
            raise RuntimeError("Inspect eval returned an unexpected number of logs")
        return _aggregate_inspect_log(
            logs[0],
            benchmark_name=task_obj.name,
            threshold=threshold,
        )


def main() -> int:
    serving_wheel_path = Path(require_env("SERVING_WHEEL_PATH"))
    model_archive_path = Path(require_env("MODEL_ARCHIVE_PATH"))
    serving_patch_path = Path(require_env("SERVING_PATCH_PATH"))
    eval_code_path = Path(require_env("EVAL_CODE_PATH"))
    eval_data_path = Path(require_env("EVAL_DATA_PATH"))
    result_path = Path(require_env("RESULT_PATH"))
    score_threshold = float(require_env("SCORE_THRESHOLD"))
    request_timeout = float(os.environ.get("REQUEST_TIMEOUT", "60"))
    vllm_version = os.environ.get("VLLM_VERSION", "0.17.0")

    model_name = os.environ.get("MODEL_NAME", "CoveDemoModel")

    with tempfile.TemporaryDirectory(prefix="cove-benchmark-") as temp_dir:
        temp_root = Path(temp_dir)
        installable_wheel_path = temp_root / f"vllm-{vllm_version}+cpu-cp38-abi3-linux_x86_64.whl"
        # Alice's private compiled serving wheel is installed into this
        # attested benchmark node before the local vLLM server is started.
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
        # Alice's private model archive is unpacked only inside this attested
        # node and served locally for Bob's benchmark.
        model_root = extract_tarball(model_archive_path, temp_root / "model")
        model_dir = _resolve_model_dir(model_root)
        server, base_url = _start_vllm_server(model_dir, model_name)
        try:
            # Bob's private Inspect task definition is imported here and run
            # directly against the local vLLM endpoint above.
            payload = _evaluate_private_task(
                eval_code_path=eval_code_path,
                base_url=base_url,
                data_path=eval_data_path,
                model=model_name,
                threshold=score_threshold,
                request_timeout=request_timeout,
            )
        finally:
            _stop_process(server)

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
