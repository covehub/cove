#!/usr/bin/env python3

from __future__ import annotations

import json
import re

from attested_confidential_eval_common import (
    log,
    read_text,
    require_env,
    sha256_bytes,
    write_json,
)


SERVICE = "audit_agent"

SUSPICIOUS_PATTERNS: dict[str, list[str]] = {
    "eval_code": [
        r"requests\.(post|get|put|patch|delete)",
        r"urllib\.(request|parse)",
        r"socket\.",
        r"subprocess\.",
        r"os\.environ",
        r"base64\.",
        r"boto3",
        r"scp\b",
        r"ssh\b",
    ],
    "serving_patch": [
        r"requests\.(post|get|put|patch|delete)",
        r"urllib\.(request|parse)",
        r"socket\.",
        r"subprocess\.",
        r"os\.environ",
        r"open\(",
        r"base64\.",
        r"boto3",
        r"scp\b",
        r"ssh\b",
    ],
}


def _heuristic_findings(audit_kind: str, source: str) -> list[str]:
    findings: list[str] = []
    for pattern in SUSPICIOUS_PATTERNS.get(audit_kind, []):
        if re.search(pattern, source):
            findings.append(f"matched suspicious pattern: {pattern}")
    return findings


def _extract_json_object(text: str) -> dict[str, object] | None:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        return None
    try:
        value = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return value


def _llm_audit(audit_kind: str, source: str, model_id: str) -> dict[str, object] | None:
    try:
        from transformers import pipeline
    except Exception as exc:  # pragma: no cover - runtime dependency failure
        log(SERVICE, f"transformers import failed, falling back to heuristics: {exc}")
        return None

    prompt = (
        "You are a conservative code auditor.\n"
        "Review the following private artifact for obvious malicious behavior, data exfiltration, "
        "credential theft, or suspicious network access.\n"
        "Return strict JSON with keys: pass (boolean) and reasoning (string).\n\n"
        f"Artifact kind: {audit_kind}\n"
        "Artifact contents:\n"
        "```text\n"
        f"{source[:12000]}\n"
        "```\n"
    )
    try:
        generator = pipeline(
            task="text-generation",
            model=model_id,
            tokenizer=model_id,
            device=-1,
        )
        outputs = generator(
            prompt,
            max_new_tokens=160,
            do_sample=False,
            return_full_text=False,
        )
    except Exception as exc:  # pragma: no cover - runtime dependency failure
        log(SERVICE, f"LLM generation failed, falling back to heuristics: {exc}")
        return None

    if not outputs or not isinstance(outputs[0], dict):
        return None
    generated = str(outputs[0].get("generated_text", ""))
    parsed = _extract_json_object(generated)
    if parsed is None:
        log(SERVICE, "model response was not valid JSON; using heuristics")
    return parsed


def main() -> int:
    audit_kind = require_env("AUDIT_KIND")
    input_path = require_env("INPUT_PATH")
    result_path = require_env("RESULT_PATH")
    model_id = require_env("AUDIT_MODEL_ID")

    source = read_text(input_path)
    audited_sha256 = sha256_bytes(source.encode("utf-8"))
    findings = _heuristic_findings(audit_kind, source)
    heuristic_pass = not findings

    llm_result = _llm_audit(audit_kind, source, model_id)
    llm_used = llm_result is not None
    llm_pass = bool(llm_result.get("pass")) if llm_result is not None else True
    llm_reasoning = str(llm_result.get("reasoning", "")).strip() if llm_result is not None else ""

    passed = heuristic_pass and llm_pass
    if findings:
        reasoning = "; ".join(findings)
    elif llm_reasoning:
        reasoning = llm_reasoning
    else:
        reasoning = "No obvious malicious patterns were detected by the prototype audit checks."

    payload = {
        "audit_kind": audit_kind,
        "audited_sha256": audited_sha256,
        "heuristic_findings": findings,
        "llm_used": llm_used,
        "model_id": model_id,
        "pass": passed,
        "reasoning": reasoning,
    }
    write_json(result_path, payload)
    log(SERVICE, f"audited {audit_kind} at {input_path}: pass={passed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
