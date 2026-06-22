from __future__ import annotations

import importlib.util
import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
AUDIT_AGENT_PATH = (
    REPO_ROOT
    / "demos/attested_confidential_benchmark__vllm_cpu/containers/audit_agent/main.py"
)
COMMON_PATH = (
    REPO_ROOT / "demos/attested_confidential_benchmark__vllm_cpu/containers/common"
)


def _load_audit_agent(monkeypatch):
    monkeypatch.syspath_prepend(str(COMMON_PATH))
    spec = importlib.util.spec_from_file_location(
        "attested_benchmark_audit_agent_under_test",
        AUDIT_AGENT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_env(monkeypatch, input_path: Path, result_path: Path) -> None:
    monkeypatch.setenv("AUDIT_KIND", "serving_patch")
    monkeypatch.setenv("INPUT_PATH", str(input_path))
    monkeypatch.setenv("RESULT_PATH", str(result_path))
    monkeypatch.setenv("AUDIT_MODEL_ID", "Qwen/Qwen3.5-9B")


def test_audit_agent_uses_qwen_result_without_heuristic_fallback(tmp_path, monkeypatch):
    module = _load_audit_agent(monkeypatch)
    input_path = tmp_path / "serving.patch"
    result_path = tmp_path / "audit_result.json"
    input_path.write_text("import os\nimport subprocess\nprint(os.environ)\n")
    _set_env(monkeypatch, input_path, result_path)

    def qwen_pass(audit_kind: str, source: str, model_id: str) -> dict[str, object]:
        assert audit_kind == "serving_patch"
        assert "subprocess" in source
        assert model_id == "Qwen/Qwen3.5-9B"
        return {"pass": True, "reasoning": "Qwen accepted the patch."}

    monkeypatch.setattr(module, "_llm_audit", qwen_pass)

    assert module.main() == 0
    payload = json.loads(result_path.read_text())

    assert payload["llm_used"] is True
    assert payload["pass"] is True
    assert payload["model_id"] == "Qwen/Qwen3.5-9B"
    assert payload["reasoning"] == "Qwen accepted the patch."
    assert "heuristic_findings" not in payload


def test_audit_agent_records_failed_qwen_audit(tmp_path, monkeypatch):
    module = _load_audit_agent(monkeypatch)
    input_path = tmp_path / "serving.patch"
    result_path = tmp_path / "audit_result.json"
    input_path.write_text("print('hello')\n")
    _set_env(monkeypatch, input_path, result_path)

    def qwen_fail(_audit_kind: str, _source: str, _model_id: str) -> dict[str, object]:
        raise module.AuditFailure("model did not load")

    monkeypatch.setattr(module, "_llm_audit", qwen_fail)

    assert module.main() == 0
    payload = json.loads(result_path.read_text())

    assert payload["llm_used"] is False
    assert payload["pass"] is False
    assert payload["reasoning"] == "Qwen audit failed: model did not load"


def test_extracts_qwen_json_after_prose_and_code_braces(monkeypatch):
    module = _load_audit_agent(monkeypatch)
    generated = """
The user wants me to audit the patch. Consider this code shape first:

```python
def example():
    return {"not": "the audit result"}
```

{"pass": true, "reasoning": "No model swap or exfiltration behavior found."}
"""

    parsed = module._extract_json_object(generated)

    assert parsed == {
        "pass": True,
        "reasoning": "No model swap or exfiltration behavior found.",
    }


def test_render_qwen_chat_disables_thinking_when_supported(monkeypatch):
    module = _load_audit_agent(monkeypatch)

    class Tokenizer:
        def __init__(self):
            self.enable_thinking = None

        def apply_chat_template(self, _messages, **kwargs):
            self.enable_thinking = kwargs.get("enable_thinking")
            return "<assistant>"

    tokenizer = Tokenizer()

    assert module._render_qwen_chat(tokenizer, []) == "<assistant>"
    assert tokenizer.enable_thinking is False


def test_audit_policy_allows_private_model_rename_and_checks_eval_exfiltration(
    monkeypatch,
):
    module = _load_audit_agent(monkeypatch)

    serving_policy = module.AUDIT_POLICIES["serving_patch"]
    eval_policy = module.AUDIT_POLICIES["eval_code"]

    assert "rename Qwen2-derived classes" in serving_policy
    assert "not a failure by itself" in serving_policy
    assert "memory-corruption risk" in serving_policy
    assert "model weights" in eval_policy
    assert "external network calls" in eval_policy
