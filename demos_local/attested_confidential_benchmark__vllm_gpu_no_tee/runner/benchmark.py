#!/usr/bin/env python3
"""Host-side runner for the local no-TEE GPU benchmark."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


NODE_ORDER = [
    "audit_serving_code",
    "compile_serving_code",
    "audit_eval_code",
    "model_benchmark",
    "model_deployment",
]

NODE_IMAGE_COMPONENTS = {
    "audit_serving_code": "audit_agent",
    "compile_serving_code": "compile_serving_wheel",
    "audit_eval_code": "audit_agent",
    "model_benchmark": "benchmark_runner",
    "model_deployment": "model_server",
}

WORKLOAD_DIR_COMPONENTS = {
    "audit_serving_code": "audit_agent",
    "compile_serving_code": "compile_serving_wheel",
    "audit_eval_code": "audit_agent",
    "model_benchmark": "benchmark_runner",
    "model_deployment": "model_server",
}

REPORT_COLUMNS = [
    "mode",
    "node",
    "container",
    "run_index",
    "host_wall_seconds",
    "sidecar_wall_seconds",
    "main_service_wall_seconds",
    "workload_seconds",
    "model_load_seconds",
    "cuda_graph_seconds",
    "single_request_seconds",
    "artifact_io_seconds",
    "startup_seconds",
    "teardown_seconds",
    "gpu_name",
    "gpu_nvidia_driver_version",
    "gpu_nvidia_cuda_version",
    "image_ref",
    "image_digest",
    "exit_code",
]


def image_ref(*, node: str, tag: str, image_prefix: str = "") -> str:
    name = f"cove-local-no-tee-vllm-gpu-{NODE_IMAGE_COMPONENTS[node]}"
    if image_prefix:
        return f"{image_prefix.rstrip('/')}/{name}:{tag}"
    return f"{name}:{tag}"


def default_workload_root() -> Path:
    repo_root = Path(__file__).resolve().parents[3]
    return repo_root / "demos/attested_confidential_benchmark__vllm_gpu/containers"


def process_ref(*, node: str, workload_root: Path) -> str:
    main_path = workload_root / WORKLOAD_DIR_COMPONENTS[node] / "main.py"
    return f"process:{main_path}"


def docker_command(
    *,
    node: str,
    state_root: Path,
    run_id: str,
    tag: str,
    image_prefix: str = "",
    detach: bool = False,
) -> list[str]:
    command = [
        "docker",
        "run",
        "--rm",
        "--gpus",
        "all",
        "-e",
        f"COVE_LOCAL_NODE_ID={node}",
        "-e",
        f"COVE_LOCAL_RUN_ID={run_id}",
        "-e",
        "COVE_LOCAL_STATE_ROOT=/workspace/cove-no-tee",
        "-e",
        "HF_HOME=/workspace/cove-no-tee/cache/huggingface",
        "-v",
        f"{state_root.resolve()}:/workspace/cove-no-tee",
    ]
    if node == "model_deployment":
        command.extend(["-p", "18443:8443"])
    if detach:
        command.append("-d")
    command.append(image_ref(node=node, tag=tag, image_prefix=image_prefix))
    return command


def run_docker_sequence(args: argparse.Namespace) -> int:
    for node in NODE_ORDER:
        detach = bool(args.detach_deployment and node == "model_deployment")
        command = docker_command(
            node=node,
            state_root=args.state_root,
            run_id=args.run_id,
            tag=args.tag,
            image_prefix=args.image_prefix,
            detach=detach,
        )
        print(shlex.join(command), flush=True)
        started = time.monotonic()
        completed = subprocess.run(command, check=False)
        wall = round(time.monotonic() - started, 6)
        driver_path = args.state_root / "runs" / args.run_id / "nodes" / node / "driver_record.json"
        driver_path.parent.mkdir(parents=True, exist_ok=True)
        driver_path.write_text(
            json.dumps(
                {
                    "node": node,
                    "mode": "no_tee",
                    "host_wall_seconds": wall,
                    "exit_code": completed.returncode,
                    "image_ref": command[-1],
                    "detached": detach,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if completed.returncode != 0:
            return completed.returncode
    return 0


def _driver_record_path(state_root: Path, run_id: str, node: str) -> Path:
    return state_root / "runs" / run_id / "nodes" / node / "driver_record.json"


def _record_path(state_root: Path, run_id: str, node: str) -> Path:
    return state_root / "runs" / run_id / "nodes" / node / "record.json"


def _write_driver_record(
    *,
    state_root: Path,
    run_id: str,
    node: str,
    host_wall_seconds: float,
    exit_code: int | None,
    image_ref_value: str,
    detached: bool,
    pid: int | None = None,
    log_path: Path | None = None,
) -> None:
    driver_path = _driver_record_path(state_root, run_id, node)
    driver_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "node": node,
        "mode": "no_tee",
        "host_wall_seconds": host_wall_seconds,
        "exit_code": exit_code,
        "image_ref": image_ref_value,
        "detached": detached,
        "executor": "process",
    }
    if pid is not None:
        payload["pid"] = pid
    if log_path is not None:
        payload["log_path"] = str(log_path)
    driver_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _wait_for_startup_record(
    *,
    state_root: Path,
    run_id: str,
    node: str,
    process: subprocess.Popen[str],
    timeout_seconds: float,
) -> int | None:
    deadline = time.monotonic() + timeout_seconds
    path = _record_path(state_root, run_id, node)
    while time.monotonic() < deadline:
        if path.exists():
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                record = {}
            result = record.get("result") if isinstance(record.get("result"), dict) else {}
            if "startup" in result or record.get("non_terminating") is True:
                return None
        returncode = process.poll()
        if returncode is not None:
            return returncode
        time.sleep(2)
    process.terminate()
    try:
        return process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=10)


def process_command(
    *,
    node: str,
    state_root: Path,
    run_id: str,
    workload_root: Path,
) -> tuple[list[str], dict[str, str], str]:
    wrapper = Path(__file__).resolve().with_name("local_node_wrapper.py")
    env = os.environ.copy()
    env.update(
        {
            "COVE_LOCAL_NODE_ID": node,
            "COVE_LOCAL_RUN_ID": run_id,
            "COVE_LOCAL_STATE_ROOT": str(state_root),
            "COVE_LOCAL_WORKLOAD_ROOT": str(workload_root),
            "HF_HOME": str(state_root / "cache/huggingface"),
            "PYTHONUNBUFFERED": "1",
        }
    )
    return [sys.executable, str(wrapper)], env, process_ref(node=node, workload_root=workload_root)


def run_process_sequence(args: argparse.Namespace) -> int:
    state_root = args.state_root.resolve()
    workload_root = args.workload_root.resolve()
    for node in NODE_ORDER:
        detach = bool(args.detach_deployment and node == "model_deployment")
        command, env, ref = process_command(
            node=node,
            state_root=state_root,
            run_id=args.run_id,
            workload_root=workload_root,
        )
        print(shlex.join(command), flush=True)
        started = time.monotonic()
        if detach:
            log_path = state_root / "runs" / args.run_id / "nodes" / node / "process.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_handle = log_path.open("a", encoding="utf-8")
            process = subprocess.Popen(
                command,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            exit_code = _wait_for_startup_record(
                state_root=state_root,
                run_id=args.run_id,
                node=node,
                process=process,
                timeout_seconds=args.deployment_startup_timeout,
            )
            log_handle.close()
            wall = round(time.monotonic() - started, 6)
            _write_driver_record(
                state_root=state_root,
                run_id=args.run_id,
                node=node,
                host_wall_seconds=wall,
                exit_code=exit_code,
                image_ref_value=ref,
                detached=True,
                pid=process.pid,
                log_path=log_path,
            )
            if exit_code is not None:
                return exit_code
            continue

        completed = subprocess.run(command, env=env, check=False)
        wall = round(time.monotonic() - started, 6)
        _write_driver_record(
            state_root=state_root,
            run_id=args.run_id,
            node=node,
            host_wall_seconds=wall,
            exit_code=completed.returncode,
            image_ref_value=ref,
            detached=False,
        )
        if completed.returncode != 0:
            return completed.returncode
    return 0


def run_ssh_sequence(args: argparse.Namespace) -> int:
    remote_demo_root = args.remote_demo_root.rstrip("/")
    remote_args = [
        "python3",
        f"{remote_demo_root}/runner/benchmark.py",
        "run-sequence",
        "--executor",
        args.remote_executor,
        "--state-root",
        args.remote_state_root,
        "--run-id",
        args.run_id,
        "--tag",
        args.tag,
    ]
    if args.image_prefix:
        remote_args.extend(["--image-prefix", args.image_prefix])
    if args.detach_deployment:
        remote_args.append("--detach-deployment")
    if args.remote_executor == "process":
        remote_args.extend(["--workload-root", args.remote_workload_root])
    command = ["ssh", args.ssh_target, shlex.join(remote_args)]
    print(shlex.join(command), flush=True)
    return subprocess.run(command, check=False).returncode


def pull_images(args: argparse.Namespace) -> int:
    seen: set[str] = set()
    for node in NODE_ORDER:
        ref = image_ref(node=node, tag=args.tag, image_prefix=args.image_prefix)
        if ref in seen:
            continue
        seen.add(ref)
        completed = subprocess.run(["docker", "pull", ref], check=False)
        if completed.returncode != 0:
            return completed.returncode
    return 0


def _load_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _profile_from_result(result: dict[str, Any]) -> dict[str, Any]:
    profile = result.get("benchmark_profile")
    if isinstance(profile, dict):
        return profile
    startup = result.get("startup")
    if isinstance(startup, dict) and isinstance(startup.get("benchmark_profile"), dict):
        return startup["benchmark_profile"]
    return {}


def _timings_from_result(result: dict[str, Any]) -> dict[str, Any]:
    timings = result.get("timings_seconds")
    if isinstance(timings, dict):
        return timings
    startup = result.get("startup")
    if isinstance(startup, dict) and isinstance(startup.get("timings_seconds"), dict):
        return startup["timings_seconds"]
    return {}


def _image_digest(image: str) -> str:
    if not image or image.startswith("process:"):
        return ""
    completed = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{index .RepoDigests 0}}"],
        check=False,
        capture_output=True,
        text=True,
    ) if shutil.which("docker") else None
    if completed is None:
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def collect_report(args: argparse.Namespace) -> int:
    results_root = args.state_root / "results" / args.run_id
    raw_root = results_root / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    for node in NODE_ORDER:
        node_root = args.state_root / "runs" / args.run_id / "nodes" / node
        record = _load_json(node_root / "record.json") or {}
        driver = _load_json(node_root / "driver_record.json") or {}
        if record:
            shutil.copy2(node_root / "record.json", raw_root / f"{node}.json")
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        startup = result.get("startup") if isinstance(result.get("startup"), dict) else {}
        profile = _profile_from_result(result)
        timings = _timings_from_result(result)
        wrapper = record.get("wrapper_timings_seconds")
        if not isinstance(wrapper, dict):
            wrapper = {}
        image = str(driver.get("image_ref") or "")
        row = {
            "mode": "no_tee",
            "node": node,
            "container": NODE_IMAGE_COMPONENTS[node],
            "run_index": args.run_index,
            "host_wall_seconds": driver.get("host_wall_seconds"),
            "sidecar_wall_seconds": 0,
            "main_service_wall_seconds": wrapper.get("outer_container_wall_seconds"),
            "workload_seconds": profile.get("workload_seconds"),
            "model_load_seconds": timings.get("audit_model_load_seconds")
            or timings.get("vllm_log_model_load_observed_seconds"),
            "cuda_graph_seconds": timings.get("vllm_log_cuda_graph_observed_seconds"),
            "single_request_seconds": timings.get("single_request_probe_seconds"),
            "artifact_io_seconds": profile.get("artifact_io_seconds"),
            "startup_seconds": profile.get("startup_seconds"),
            "teardown_seconds": profile.get("teardown_seconds"),
            "gpu_name": result.get("gpu_name") or startup.get("gpu_name", ""),
            "gpu_nvidia_driver_version": result.get("gpu_nvidia_driver_version")
            or startup.get("gpu_nvidia_driver_version", ""),
            "gpu_nvidia_cuda_version": result.get("gpu_nvidia_cuda_version")
            or startup.get("gpu_nvidia_cuda_version", ""),
            "image_ref": image,
            "image_digest": _image_digest(image) if image else "",
            "exit_code": record.get("exit_code"),
        }
        rows.append(row)

    summary = {"mode": "no_tee", "run_id": args.run_id, "rows": rows}
    (results_root / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    with (results_root / "summary.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in REPORT_COLUMNS})
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)

    pull = subcommands.add_parser("pull-images")
    pull.add_argument("--tag", default="no-tee-gpu")
    pull.add_argument("--image-prefix", default=os.environ.get("RUNPOD_DOCKER_IMAGE_PREFIX", ""))
    pull.set_defaults(func=pull_images)

    run = subcommands.add_parser("run-sequence")
    run.add_argument("--executor", choices=["docker", "process", "ssh"], default="docker")
    run.add_argument("--state-root", type=Path, required=True)
    run.add_argument("--remote-state-root", default="/workspace/cove-no-tee")
    run.add_argument("--run-id", required=True)
    run.add_argument("--tag", default="no-tee-gpu")
    run.add_argument("--image-prefix", default=os.environ.get("RUNPOD_DOCKER_IMAGE_PREFIX", ""))
    run.add_argument("--workload-root", type=Path, default=default_workload_root())
    run.add_argument("--ssh-target", default=os.environ.get("COVE_RUNPOD_SSH_TARGET", ""))
    run.add_argument("--remote-executor", choices=["docker", "process"], default="docker")
    run.add_argument(
        "--remote-demo-root",
        default=os.environ.get(
            "COVE_RUNPOD_DEMO_ROOT",
            "/workspace/cove/demos_local/attested_confidential_benchmark__vllm_gpu_no_tee",
        ),
    )
    run.add_argument(
        "--remote-workload-root",
        default="/workspace/cove/demos/attested_confidential_benchmark__vllm_gpu/containers",
    )
    run.add_argument("--deployment-startup-timeout", type=float, default=900.0)
    run.add_argument("--detach-deployment", action="store_true")
    run.set_defaults(
        func=lambda args: (
            run_ssh_sequence(args)
            if args.executor == "ssh"
            else run_process_sequence(args)
            if args.executor == "process"
            else run_docker_sequence(args)
        )
    )

    collect = subcommands.add_parser("collect-report")
    collect.add_argument("--state-root", type=Path, required=True)
    collect.add_argument("--run-id", required=True)
    collect.add_argument("--run-index", default="0")
    collect.set_defaults(func=collect_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "executor", None) == "ssh" and not args.ssh_target:
        parser.error("--ssh-target or COVE_RUNPOD_SSH_TARGET is required for --executor ssh")
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
