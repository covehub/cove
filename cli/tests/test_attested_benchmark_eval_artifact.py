from __future__ import annotations

import ast
import hashlib
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_ROOT = REPO_ROOT / "demos/attested_confidential_benchmark__vllm_cpu"
PREPARE_INPUTS = DEMO_ROOT / "scripts/prepare_demo_inputs.py"
RUNTIME_EVAL = DEMO_ROOT / "runtime_inputs/bob_private_eval_code.py"
BENCHMARK_MAIN = DEMO_ROOT / "containers/benchmark_runner/main.py"
WORKFLOW = DEMO_ROOT / "workflow/workflow.cove.yaml"
PYTHON_MODULES = [
    DEMO_ROOT / "scripts/render_workflow.py",
    DEMO_ROOT / "scripts/prepare_demo_inputs.py",
    DEMO_ROOT / "containers/model_server/main.py",
    DEMO_ROOT / "containers/compile_serving_wheel/main.py",
    DEMO_ROOT / "containers/common/cove_demo_common.py",
    DEMO_ROOT / "containers/benchmark_runner/main.py",
    DEMO_ROOT / "containers/audit_agent/main.py",
]


def _eval_template() -> str:
    module = ast.parse(PREPARE_INPUTS.read_text(encoding="utf-8"))
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "EVAL_CODE_TEMPLATE":
                return ast.literal_eval(node.value)
    raise AssertionError("EVAL_CODE_TEMPLATE not found")


def test_private_eval_artifact_only_defines_inspect_task() -> None:
    code = _eval_template()

    compile(code, str(RUNTIME_EVAL), "exec")
    assert "from inspect_ai import Task, task" in code
    assert "def harmbench_direct_request(data_path: str) -> Task:" in code
    assert "solver=generate(max_tokens=96, temperature=0)" in code
    assert '@scorer(metrics=[], name="refusal")' in code
    assert "def refusal():" in code
    assert "not proper HarmBench" in code
    assert "cais/HarmBench-Llama-2-13b-cls" in code
    assert "extra 13B model" in code
    assert "check for refusal strings" in code
    assert '"private_eval": True' not in code
    assert '"harmbench_method":' not in code
    assert '"scoring_method":' not in code
    assert "inspect_eval" not in code
    assert "argparse" not in code
    assert "def main() -> int:" not in code
    assert "HTTPConnection" not in code
    assert "direct_request_solver" not in code
    assert "mockllm" not in code


def test_benchmark_main_imports_private_task_and_invokes_inspect() -> None:
    code = BENCHMARK_MAIN.read_text(encoding="utf-8")

    compile(code, str(BENCHMARK_MAIN), "exec")
    assert "spec_from_file_location" in code
    assert 'TASK_FACTORY_NAME = "harmbench_direct_request"' in code
    assert "from inspect_ai import Task, eval as inspect_eval" in code
    assert 'model=f"openai-api/{model}"' in code
    assert "model_base_url=base_url" in code
    assert 'model_args={"api_key": "EMPTY", "service": "cove"}' in code
    assert "display=\"plain\"" in code
    assert "sandbox=None" in code
    assert "max_connections=1" in code
    assert "run_private_eval.py" not in code


def test_eval_artifact_hash_matches_template() -> None:
    code = _eval_template()
    digest = "sha256:" + hashlib.sha256(code.encode("utf-8")).hexdigest()

    assert digest in WORKFLOW.read_text(encoding="utf-8")
    if RUNTIME_EVAL.exists():
        assert RUNTIME_EVAL.read_text(encoding="utf-8") == code


def test_attested_benchmark_python_modules_have_docstrings() -> None:
    for path in PYTHON_MODULES:
        module = ast.parse(path.read_text(encoding="utf-8"))
        assert ast.get_docstring(module), f"{path} is missing a module docstring"
