from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_ROOT = REPO_ROOT / "demos_local/attested_confidential_benchmark__vllm_gpu_no_tee"
PREPARE_STATE = DEMO_ROOT / "scripts/prepare_state.py"
BENCHMARK_RUNNER = DEMO_ROOT / "runner/benchmark.py"
WRAPPER = DEMO_ROOT / "runner/local_node_wrapper.py"


def _load_benchmark_runner():
    import importlib.util

    spec = importlib.util.spec_from_file_location("no_tee_benchmark_under_test", BENCHMARK_RUNNER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_prepare_state_writes_encrypted_artifacts_and_local_keypair(tmp_path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    fixtures = {
        "alice_private_model.tar": b"model",
        "alice_private_serving_patch.diff": b"patch",
        "bob_private_eval_code.py": b"print('eval')\n",
        "bob_private_eval_data.jsonl": b'{"x": 1}\n',
    }
    for name, payload in fixtures.items():
        (inputs / name).write_bytes(payload)
    state = tmp_path / "state"

    subprocess.run(
        [
            sys.executable,
            str(PREPARE_STATE),
            "--state-root",
            str(state),
            "--inputs-root",
            str(inputs),
        ],
        check=True,
    )

    manifest = json.loads((state / "artifacts/manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["artifacts"]) == {
        "alice_private_model",
        "alice_private_serving_patch",
        "bob_private_eval_code",
        "bob_private_eval_data",
    }
    assert (state / "secrets/artifact_key.json").is_file()
    assert (state / "secrets/ratls_key/private.pem").is_file()
    assert (state / "secrets/ratls_key/certificate.pem").is_file()


def test_no_tee_runner_keeps_five_node_order_and_root_independence() -> None:
    module = _load_benchmark_runner()
    wrapper_text = WRAPPER.read_text(encoding="utf-8")

    assert module.NODE_ORDER == [
        "audit_serving_code",
        "compile_serving_code",
        "audit_eval_code",
        "model_benchmark",
        "model_deployment",
    ]
    assert '"compile_serving_code": NodeConfig' in wrapper_text
    assert 'dependencies=("audit_serving_code",)' not in wrapper_text
    assert 'dependencies=("compile_serving_code",)' not in wrapper_text
    assert "compile_serving_code requires passing serving audit" not in wrapper_text


def test_docker_command_runs_one_container(tmp_path) -> None:
    module = _load_benchmark_runner()
    command = module.docker_command(
        node="model_benchmark",
        state_root=tmp_path,
        run_id="run-test",
        tag="test-tag",
        image_prefix="registry.example/cove",
    )

    assert command[:3] == ["docker", "run", "--rm"]
    assert "--gpus" in command
    assert "COVE_LOCAL_NODE_ID=model_benchmark" in command
    assert command[-1] == (
        "registry.example/cove/cove-local-no-tee-vllm-gpu-benchmark_runner:test-tag"
    )


def test_ssh_runner_builds_remote_sequence_command(monkeypatch, tmp_path) -> None:
    module = _load_benchmark_runner()
    calls: list[list[str]] = []

    class Completed:
        returncode = 0

    def fake_run(command, *, check=False):
        calls.append(command)
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.run_ssh_sequence(
        SimpleNamespace(
            remote_demo_root="/workspace/cove/demo",
            remote_state_root="/workspace/cove-no-tee",
            run_id="run-test",
            tag="test-tag",
            image_prefix="registry.example/cove",
            detach_deployment=False,
            ssh_target="root@example",
            state_root=tmp_path,
            remote_executor="docker",
            remote_workload_root="/workspace/cove/workloads",
        )
    )

    assert result == 0
    assert calls[0][0] == "ssh"
    assert calls[0][1] == "root@example"
    assert "run-sequence --executor docker" in calls[0][2]


def test_process_command_runs_synced_workload(tmp_path) -> None:
    module = _load_benchmark_runner()
    workload_root = tmp_path / "containers"

    command, env, ref = module.process_command(
        node="audit_serving_code",
        state_root=tmp_path / "state",
        run_id="run-test",
        workload_root=workload_root,
    )

    assert command[-1].endswith("local_node_wrapper.py")
    assert env["COVE_LOCAL_NODE_ID"] == "audit_serving_code"
    assert env["COVE_LOCAL_WORKLOAD_ROOT"] == str(workload_root)
    assert ref == f"process:{workload_root / 'audit_agent/main.py'}"


def test_ssh_runner_can_target_remote_process_executor(monkeypatch, tmp_path) -> None:
    module = _load_benchmark_runner()
    calls: list[list[str]] = []

    class Completed:
        returncode = 0

    def fake_run(command, *, check=False):
        calls.append(command)
        return Completed()

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.run_ssh_sequence(
        SimpleNamespace(
            remote_demo_root="/logs/cove/demo",
            remote_state_root="/logs/cove-no-tee",
            run_id="run-test",
            tag="test-tag",
            image_prefix="",
            detach_deployment=True,
            ssh_target="root@example",
            state_root=tmp_path,
            remote_executor="process",
            remote_workload_root="/logs/cove/workloads",
        )
    )

    assert result == 0
    remote = calls[0][2]
    assert "run-sequence --executor process" in remote
    assert "--remote-workload-root" not in remote
    assert "--workload-root /logs/cove/workloads" in remote
    assert "--detach-deployment" in remote


def test_wrapper_marks_records_non_attested() -> None:
    text = WRAPPER.read_text(encoding="utf-8")

    assert 'STATE_VERSION = "cove_local_no_tee_v1"' in text
    assert '"attestation": {"type": "none"}' in text
    assert '"tee": False' in text


def test_collect_report_writes_summary_json_and_csv(monkeypatch, tmp_path) -> None:
    module = _load_benchmark_runner()
    state = tmp_path / "state"
    run_id = "run-test"
    for node in module.NODE_ORDER:
        node_root = state / "runs" / run_id / "nodes" / node
        node_root.mkdir(parents=True)
        (node_root / "record.json").write_text(
            json.dumps(
                {
                    "exit_code": 0,
                    "result": {
                        "benchmark_profile": {
                            "workload_seconds": 1.0,
                            "artifact_io_seconds": 2.0,
                            "startup_seconds": 3.0,
                            "teardown_seconds": 4.0,
                        },
                        "timings_seconds": {
                            "single_request_probe_seconds": 0.25,
                            "vllm_log_cuda_graph_observed_seconds": 0.5,
                            "vllm_log_model_load_observed_seconds": 0.75,
                        },
                        "gpu_name": "NVIDIA H200",
                    },
                    "wrapper_timings_seconds": {
                        "outer_container_wall_seconds": 10.0,
                    },
                }
            ),
            encoding="utf-8",
        )
        (node_root / "driver_record.json").write_text(
            json.dumps(
                {
                    "host_wall_seconds": 11.0,
                    "image_ref": "example/image:test",
                }
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(module, "_image_digest", lambda image: "example/image@sha256:abc")

    assert module.collect_report(SimpleNamespace(state_root=state, run_id=run_id, run_index="0")) == 0
    summary_path = state / "results" / run_id / "summary.json"
    csv_path = state / "results" / run_id / "summary.csv"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))

    assert csv_path.is_file()
    assert len(summary["rows"]) == 5
    assert summary["rows"][0]["workload_seconds"] == 1.0
    assert summary["rows"][0]["cuda_graph_seconds"] == 0.5
