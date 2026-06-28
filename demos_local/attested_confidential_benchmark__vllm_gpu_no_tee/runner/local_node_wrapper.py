#!/usr/bin/env python3
"""Run one no-TEE benchmark node against encrypted local state."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


STATE_VERSION = "cove_local_no_tee_v1"
DEFAULT_STATE_ROOT = "/workspace/cove-no-tee"


@dataclass(frozen=True)
class ArtifactBinding:
    artifact: str
    path: str


@dataclass(frozen=True)
class NodeConfig:
    image_name: str
    workload_dir: str
    service_name: str
    env: dict[str, str]
    inputs: tuple[ArtifactBinding, ...]
    dynamic_outputs: tuple[ArtifactBinding, ...] = ()
    dependencies: tuple[str, ...] = ()
    non_terminating: bool = False


NODE_CONFIGS: dict[str, NodeConfig] = {
    "audit_serving_code": NodeConfig(
        image_name="cove-local-no-tee-vllm-gpu-audit-agent",
        workload_dir="audit_agent",
        service_name="audit_agent",
        env={
            "AUDIT_KIND": "serving_patch",
            "AUDIT_DEVICE_MAP": "auto",
            "AUDIT_MODEL_ID": "Qwen/Qwen3.5-9B",
            "COVE_DEMO_ENABLE_TIMING": "1",
            "INPUT_PATH": "/workspace/input/alice_private_serving_patch.diff",
            "RESULT_PATH": "/workspace/output/audit_serving_result.json",
            "REQUIRE_CUDA": "1",
        },
        inputs=(
            ArtifactBinding(
                "alice_private_serving_patch",
                "/workspace/input/alice_private_serving_patch.diff",
            ),
        ),
    ),
    "compile_serving_code": NodeConfig(
        image_name="cove-local-no-tee-vllm-gpu-compile-serving-wheel",
        workload_dir="compile_serving_wheel",
        service_name="compile_serving_wheel",
        env={
            "COVE_DEMO_ENABLE_TIMING": "1",
            "OUTPUT_PATH": "/workspace/output/compiled_serving_wheel.whl",
            "PATCH_PATH": "/workspace/input/alice_private_serving_patch.diff",
            "RESULT_PATH": "/workspace/output/compile_result.json",
            "VLLM_GIT_SHA": "b31e9326a7d9394aab8c767f8ebe225c65594b60",
            "VLLM_SOURCE_ROOT": "/opt/vllm-src",
            "VLLM_VERSION": "0.17.0",
            "VLLM_WHEEL_PATH": "/opt/wheels/vllm-0.17.0+cu130-cp38-abi3-manylinux_2_35_x86_64.whl",
            "VLLM_WHEEL_FLAVOR": "cu130",
        },
        inputs=(
            ArtifactBinding(
                "alice_private_serving_patch",
                "/workspace/input/alice_private_serving_patch.diff",
            ),
        ),
        dynamic_outputs=(
            ArtifactBinding(
                "compiled_serving_wheel",
                "/workspace/output/compiled_serving_wheel.whl",
            ),
        ),
    ),
    "audit_eval_code": NodeConfig(
        image_name="cove-local-no-tee-vllm-gpu-audit-agent",
        workload_dir="audit_agent",
        service_name="audit_agent",
        env={
            "AUDIT_KIND": "eval_code",
            "AUDIT_DEVICE_MAP": "auto",
            "AUDIT_MODEL_ID": "Qwen/Qwen3.5-9B",
            "COVE_DEMO_ENABLE_TIMING": "1",
            "INPUT_PATH": "/workspace/input/bob_private_eval_code.py",
            "RESULT_PATH": "/workspace/output/audit_eval_result.json",
            "REQUIRE_CUDA": "1",
        },
        inputs=(
            ArtifactBinding("bob_private_eval_code", "/workspace/input/bob_private_eval_code.py"),
        ),
    ),
    "model_benchmark": NodeConfig(
        image_name="cove-local-no-tee-vllm-gpu-benchmark-runner",
        workload_dir="benchmark_runner",
        service_name="benchmark_runner",
        env={
            "COVE_DEMO_ENABLE_TIMING": "1",
            "EVAL_CODE_PATH": "/workspace/input/bob_private_eval_code.py",
            "EVAL_DATA_PATH": "/workspace/input/bob_private_eval_data.jsonl",
            "MODEL_ARCHIVE_PATH": "/workspace/input/alice_private_model.tar",
            "MODEL_NAME": "CoveDemoModel",
            "RESULT_PATH": "/workspace/output/benchmark_result.json",
            "REQUIRE_CUDA": "1",
            "SCORE_THRESHOLD": "0.40",
            "SERVING_PATCH_PATH": "/workspace/input/alice_private_serving_patch.diff",
            "SERVING_WHEEL_PATH": "/workspace/input/compiled_serving_wheel.whl",
            "VLLM_DEVICE": "cuda",
            "VLLM_DTYPE": "auto",
            "VLLM_GPU_MEMORY_UTILIZATION": "0.90",
            "VLLM_TENSOR_PARALLEL_SIZE": "1",
        },
        inputs=(
            ArtifactBinding("alice_private_model", "/workspace/input/alice_private_model.tar"),
            ArtifactBinding(
                "alice_private_serving_patch",
                "/workspace/input/alice_private_serving_patch.diff",
            ),
            ArtifactBinding("bob_private_eval_code", "/workspace/input/bob_private_eval_code.py"),
            ArtifactBinding("bob_private_eval_data", "/workspace/input/bob_private_eval_data.jsonl"),
            ArtifactBinding("compiled_serving_wheel", "/workspace/input/compiled_serving_wheel.whl"),
        ),
        dependencies=("audit_serving_code", "compile_serving_code", "audit_eval_code"),
    ),
    "model_deployment": NodeConfig(
        image_name="cove-local-no-tee-vllm-gpu-model-server",
        workload_dir="model_server",
        service_name="serve",
        env={
            "COVE_DEMO_ENABLE_TIMING": "1",
            "HOST": "0.0.0.0",
            "MODEL_ARCHIVE_PATH": "/workspace/input/alice_private_model.tar",
            "MODEL_NAME": "CoveDemoModel",
            "PORT": "8443",
            "REQUIRE_CUDA": "1",
            "SERVING_WHEEL_PATH": "/workspace/input/compiled_serving_wheel.whl",
            "TLS_CERT_PATH": "/workspace/cove-no-tee/secrets/ratls_key/certificate.pem",
            "TLS_KEY_PATH": "/workspace/cove-no-tee/secrets/ratls_key/private.pem",
            "VLLM_DEVICE": "cuda",
            "VLLM_DTYPE": "auto",
            "VLLM_GPU_MEMORY_UTILIZATION": "0.90",
            "VLLM_TENSOR_PARALLEL_SIZE": "1",
        },
        inputs=(
            ArtifactBinding("alice_private_model", "/workspace/input/alice_private_model.tar"),
            ArtifactBinding("compiled_serving_wheel", "/workspace/input/compiled_serving_wheel.whl"),
        ),
        dependencies=("compile_serving_code", "model_benchmark"),
        non_terminating=True,
    ),
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_bytes(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _state_root() -> Path:
    return Path(os.environ.get("COVE_LOCAL_STATE_ROOT", DEFAULT_STATE_ROOT)).resolve()


def _run_id() -> str:
    return os.environ.get("COVE_LOCAL_RUN_ID") or time.strftime("run-%Y%m%dT%H%M%SZ", time.gmtime())


def _node_id() -> str:
    node = os.environ.get("COVE_LOCAL_NODE_ID", "")
    if node not in NODE_CONFIGS:
        valid = ", ".join(sorted(NODE_CONFIGS))
        raise SystemExit(f"COVE_LOCAL_NODE_ID must be one of: {valid}")
    return node


def _load_key(state_root: Path) -> bytes:
    payload = _read_json(state_root / "secrets/artifact_key.json")
    key_b64 = payload.get("key_b64")
    if not isinstance(key_b64, str):
        raise SystemExit("missing local artifact key")
    return base64.b64decode(key_b64)


def _artifact_manifest_path(state_root: Path) -> Path:
    return state_root / "artifacts/manifest.json"


def _load_manifest(state_root: Path) -> dict[str, Any]:
    return _read_json(_artifact_manifest_path(state_root))


def _save_manifest(state_root: Path, manifest: dict[str, Any]) -> None:
    _write_json(_artifact_manifest_path(state_root), manifest)


def _decrypt_artifact(state_root: Path, manifest: dict[str, Any], name: str, output_path: Path) -> dict[str, Any]:
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, dict) or name not in artifacts:
        raise SystemExit(f"artifact {name!r} is not present in local state")
    meta = artifacts[name]
    encrypted_path = state_root / str(meta["ciphertext_path"])
    ciphertext = encrypted_path.read_bytes()
    nonce = base64.b64decode(str(meta["nonce_b64"]))
    plaintext = AESGCM(_load_key(state_root)).decrypt(nonce, ciphertext, None)
    observed_hash = _sha256_bytes(plaintext)
    expected_hash = str(meta["plaintext_hash"])
    if observed_hash != expected_hash:
        raise SystemExit(f"artifact {name!r} plaintext hash mismatch: {observed_hash} != {expected_hash}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(plaintext)
    return {
        "artifact": name,
        "plaintext_hash": observed_hash,
        "ciphertext_hash": _sha256_file(encrypted_path),
        "path": str(output_path),
    }


def _encrypt_artifact(state_root: Path, manifest: dict[str, Any], name: str, plaintext_path: Path) -> dict[str, Any]:
    key = _load_key(state_root)
    nonce = os.urandom(12)
    plaintext = plaintext_path.read_bytes()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    encrypted_rel = Path("artifacts/encrypted") / f"{name}.bin"
    encrypted_path = state_root / encrypted_rel
    encrypted_path.parent.mkdir(parents=True, exist_ok=True)
    encrypted_path.write_bytes(ciphertext)
    meta = {
        "ciphertext_hash": _sha256_file(encrypted_path),
        "ciphertext_path": encrypted_rel.as_posix(),
        "kind": "dynamic",
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "plaintext_hash": _sha256_bytes(plaintext),
        "updated_at": _now_iso(),
    }
    artifacts = manifest.setdefault("artifacts", {})
    if not isinstance(artifacts, dict):
        raise SystemExit("artifact manifest is invalid")
    artifacts[name] = meta
    _save_manifest(state_root, manifest)
    return {"artifact": name, **meta}


def _record_path(state_root: Path, run_id: str, node_id: str) -> Path:
    return state_root / "runs" / run_id / "nodes" / node_id / "record.json"


def _result_path_for(config: NodeConfig, output_dir: Path) -> Path | None:
    value = config.env.get("RESULT_PATH")
    if not value:
        return None
    return output_dir / Path(value).name


def _load_dependency_records(state_root: Path, run_id: str, config: NodeConfig) -> dict[str, Any]:
    records: dict[str, Any] = {}
    for dependency in config.dependencies:
        path = _record_path(state_root, run_id, dependency)
        if not path.exists():
            raise SystemExit(f"missing dependency record for {dependency}: {path}")
        record = _read_json(path)
        if record.get("exit_code") != 0:
            raise SystemExit(f"dependency {dependency} did not exit successfully")
        records[dependency] = record
    return records


def _validate_dependency_results(node_id: str, dependencies: dict[str, Any]) -> None:
    def result(dep: str) -> dict[str, Any]:
        return dependencies.get(dep, {}).get("result", {})

    if node_id == "model_benchmark":
        for dep in ("audit_serving_code", "audit_eval_code", "compile_serving_code"):
            if result(dep).get("pass") is not True:
                raise SystemExit(f"model_benchmark requires passing dependency {dep}")
    elif node_id == "model_deployment":
        if result("compile_serving_code").get("pass") is not True:
            raise SystemExit("model_deployment requires passing compile")
        if result("model_benchmark").get("passes_threshold") is not True:
            raise SystemExit("model_deployment requires passing benchmark threshold")


def _container_path_to_host(path: str, input_dir: Path, output_dir: Path, state_root: Path) -> Path:
    if path.startswith("/workspace/input/"):
        return input_dir / Path(path).name
    if path.startswith("/workspace/output/"):
        return output_dir / Path(path).name
    if path.startswith("/workspace/cove-no-tee/"):
        return state_root / Path(path.removeprefix("/workspace/cove-no-tee/"))
    return Path(path)


def _workload_command(config: NodeConfig) -> list[str]:
    explicit_main = os.environ.get("COVE_LOCAL_WORKLOAD_MAIN", "").strip()
    if explicit_main:
        return [sys.executable, explicit_main]
    workload_root = os.environ.get("COVE_LOCAL_WORKLOAD_ROOT", "").strip()
    if workload_root:
        return [sys.executable, str(Path(workload_root) / config.workload_dir / "main.py")]
    return [sys.executable, "/app/main.py"]


def _workload_pythonpath(config: NodeConfig) -> list[str]:
    paths: list[str] = []
    explicit_common = os.environ.get("COVE_LOCAL_WORKLOAD_COMMON_ROOT", "").strip()
    if explicit_common:
        paths.append(explicit_common)
    workload_root = os.environ.get("COVE_LOCAL_WORKLOAD_ROOT", "").strip()
    if workload_root:
        paths.append(str(Path(workload_root) / "common"))
        paths.append(str(Path(workload_root) / config.workload_dir))
    existing = os.environ.get("PYTHONPATH", "").strip()
    if existing:
        paths.append(existing)
    return paths


def _write_record(
    *,
    state_root: Path,
    run_id: str,
    node_id: str,
    config: NodeConfig,
    command: list[str],
    dependencies: dict[str, Any],
    exit_code: int | None,
    input_records: list[dict[str, Any]],
    output_records: list[dict[str, Any]],
    result_payload: dict[str, Any] | None,
    started_at_iso: str,
    started_at: float,
    wrapper_timings: dict[str, float],
) -> None:
    record = {
        "attestation": {"type": "none"},
        "command": command,
        "dependencies": dependencies,
        "ended_at": _now_iso() if exit_code is not None else None,
        "exit_code": exit_code,
        "inputs": input_records,
        "node_id": node_id,
        "non_terminating": config.non_terminating,
        "outputs": output_records,
        "record_version": STATE_VERSION,
        "result": result_payload or {},
        "run_id": run_id,
        "service_name": config.service_name,
        "started_at": started_at_iso,
        "tee": False,
        "wrapper_timings_seconds": {
            **wrapper_timings,
            "outer_container_wall_seconds": round(time.monotonic() - started_at, 6),
        },
    }
    _write_json(_record_path(state_root, run_id, node_id), record)


def _run_non_terminating(
    *,
    command: list[str],
    env: dict[str, str],
    on_startup: Any,
    timeout_seconds: float,
) -> tuple[int | None, dict[str, Any]]:
    process = subprocess.Popen(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_seconds
    result_payload: dict[str, Any] = {}
    serving = False
    while time.monotonic() < deadline:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                return process.returncode, result_payload
            time.sleep(0.1)
            continue
        print(line, end="", flush=True)
        if "startup timings:" in line:
            _, _, payload_text = line.partition("startup timings:")
            try:
                result_payload["startup"] = json.loads(payload_text.strip())
            except json.JSONDecodeError:
                result_payload["startup_parse_error"] = payload_text.strip()
        if "serving RA-TLS proxy" in line:
            serving = True
            break
    if not serving:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        return process.returncode, result_payload

    on_startup(result_payload)
    # Keep this container as the live endpoint and continue forwarding
    # model-server logs.
    for line in process.stdout:
        print(line, end="", flush=True)
    return process.wait(), result_payload


def main() -> int:
    state_root = _state_root()
    run_id = _run_id()
    node_id = _node_id()
    config = NODE_CONFIGS[node_id]
    started_at = time.monotonic()
    started_at_iso = _now_iso()
    node_root = state_root / "runs" / run_id / "nodes" / node_id
    input_dir = node_root / "input"
    output_dir = node_root / "output"
    input_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    manifest = _load_manifest(state_root)
    dependencies = _load_dependency_records(state_root, run_id, config)
    _validate_dependency_results(node_id, dependencies)

    wrapper_timings: dict[str, float] = {}
    step_started = time.monotonic()
    input_records = [
        _decrypt_artifact(
            state_root,
            manifest,
            binding.artifact,
            _container_path_to_host(binding.path, input_dir, output_dir, state_root),
        )
        for binding in config.inputs
    ]
    wrapper_timings["artifact_decrypt_seconds"] = round(time.monotonic() - step_started, 6)

    env = os.environ.copy()
    env.update(config.env)
    env["COVE_SERVICE_NAME"] = config.service_name
    env["COVE_SERVICE_ROLE"] = "workload"
    pythonpath = _workload_pythonpath(config)
    if pythonpath:
        env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    for key, value in config.env.items():
        env[key] = str(_container_path_to_host(value, input_dir, output_dir, state_root)) if value.startswith("/") else value

    command = _workload_command(config)
    result_payload: dict[str, Any] | None = None
    exit_code = 0
    output_records: list[dict[str, Any]] = []
    if config.non_terminating:
        def write_startup_record(startup_result: dict[str, Any]) -> None:
            _write_record(
                state_root=state_root,
                run_id=run_id,
                node_id=node_id,
                config=config,
                command=command,
                dependencies=dependencies,
                exit_code=None,
                input_records=input_records,
                output_records=output_records,
                result_payload=startup_result,
                started_at_iso=started_at_iso,
                started_at=started_at,
                wrapper_timings=wrapper_timings,
            )

        exit_code, result_payload = _run_non_terminating(
            command=command,
            env=env,
            on_startup=write_startup_record,
            timeout_seconds=float(os.environ.get("COVE_LOCAL_DEPLOYMENT_STARTUP_TIMEOUT", "600")),
        )
        _write_record(
            state_root=state_root,
            run_id=run_id,
            node_id=node_id,
            config=config,
            command=command,
            dependencies=dependencies,
            exit_code=None if exit_code is None else exit_code,
            input_records=input_records,
            output_records=output_records,
            result_payload=result_payload,
            started_at_iso=started_at_iso,
            started_at=started_at,
            wrapper_timings=wrapper_timings,
        )
        return 0 if exit_code is None else exit_code

    try:
        completed = subprocess.run(command, env=env, check=False)
        exit_code = completed.returncode
        result_path = _result_path_for(config, output_dir)
        if result_path is not None and result_path.exists():
            result_payload = _read_json(result_path)
    finally:
        step_started = time.monotonic()
        output_records = [
            _encrypt_artifact(
                state_root,
                manifest,
                binding.artifact,
                _container_path_to_host(binding.path, input_dir, output_dir, state_root),
            )
            for binding in config.dynamic_outputs
            if _container_path_to_host(binding.path, input_dir, output_dir, state_root).exists()
        ]
        wrapper_timings["artifact_encrypt_seconds"] = round(time.monotonic() - step_started, 6)

    _write_record(
        state_root=state_root,
        run_id=run_id,
        node_id=node_id,
        config=config,
        command=command,
        dependencies=dependencies,
        exit_code=exit_code,
        input_records=input_records,
        output_records=output_records,
        result_payload=result_payload,
        started_at_iso=started_at_iso,
        started_at=started_at,
        wrapper_timings=wrapper_timings,
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
