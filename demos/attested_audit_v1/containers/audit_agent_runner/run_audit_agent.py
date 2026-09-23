#!/usr/bin/env python
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from openai import OpenAI


AUDIT_AGENT_VERSION = "attested-audit-v1.simple-json.1"
RAW_RESPONSE_PREVIEW_CHARS = 2000


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def sha256_prefixed(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def read_text(path: Path, *, limit: int = 12000) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > limit:
        return text[:limit] + "\n... [truncated]"
    return text


def wait_for_health(base_url: str, timeout_seconds: int) -> None:
    health_url = re.sub(r"/v1/?$", "", base_url.rstrip("/")) + "/health"
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            request = Request(health_url, method="GET")
            with urlopen(request, timeout=5) as response:
                if 200 <= response.status < 300:
                    return
        except URLError as exc:
            last_error = str(exc)
        except Exception as exc:  # pragma: no cover - defensive runtime path
            last_error = str(exc)
        time.sleep(2)
    raise RuntimeError(f"model server did not become healthy at {health_url}: {last_error}")


def extract_json_object(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    candidates = [stripped]
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        candidates.insert(0, fenced.group(1))
    braces = re.search(r"(\{.*\})", stripped, flags=re.DOTALL)
    if braces:
        candidates.append(braces.group(1))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def coerce_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized == "true":
            return True
        if normalized == "false":
            return False
    return None


def preview_text(text: str, *, limit: int = RAW_RESPONSE_PREVIEW_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


DEFAULT_POLICIES = {
    "serving_patch": (
        "Audit this private vLLM serving-code patch for security bugs in the "
        "serving path. Focus on arbitrary code execution, unsafe native or FFI "
        "code, unsafe deserialization, hidden shell or subprocess behavior, "
        "external network calls, credential or artifact exfiltration, and "
        "filesystem access unrelated to loading or serving the model."
    ),
    "eval_code": (
        "Audit that this private Inspect evaluation code only defines the "
        "benchmark task and scorer. It may read private eval data to build "
        "samples and score model outputs supplied by Inspect. It must not "
        "read, copy, upload, print, or otherwise exfiltrate model weights, "
        "compiled runtime bundles, credentials, raw prompts, raw responses, "
        "or private data; must not make external network calls; and must not "
        "use hidden shell, subprocess, or unrelated filesystem behavior."
    ),
}


def build_prompt(policy: dict[str, Any], artifact_kind: str, artifact_text: str) -> str:
    policy_text = policy.get(artifact_kind) or DEFAULT_POLICIES.get(artifact_kind, "")
    return (
        "You are an audit agent for confidential AI deployments.\n"
        "Audit the private artifact against the provided policy. Return only JSON with the "
        "single key passed, whose value must be true or false.\n\n"
        f"Artifact kind: {artifact_kind}\n"
        f"Audit policy:\n{policy_text or json.dumps(policy, indent=2, sort_keys=True)}\n\n"
        f"Artifact contents:\n{artifact_text}\n"
    )


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    artifact_kind = os.getenv("AUDIT_KIND", "serving_patch")
    artifact_path = Path(os.getenv("AUDIT_INPUT", os.getenv("SERVING_PATCH", "")))
    if not str(artifact_path):
        raise RuntimeError("AUDIT_INPUT or SERVING_PATCH is required")
    audit_policy_path = Path(os.getenv("AUDIT_POLICY", "/opt/cove_public/audit_policy.json"))
    result_path = Path(os.getenv("RESULT_PATH", "/workspace/output/audit_result.json"))
    raw_response_path = Path(
        os.getenv("RAW_MODEL_RESPONSE_PATH", "/workspace/output/audit_model_response.txt")
    )
    openai_base_url = os.getenv("OPENAI_BASE_URL", "http://audit_model_server:8000/v1")
    openai_api_key = os.getenv("OPENAI_API_KEY", "vllm")
    audit_model_id = os.getenv("AUDIT_MODEL_ID", "audit-model")
    timeout_seconds = int(os.getenv("MODEL_READY_TIMEOUT_SECONDS", "600"))
    force_pass = os.getenv("AUDIT_AGENT_FORCE_PASS", "0") == "1"

    for path in (artifact_path, audit_policy_path):
        if not path.exists():
            raise FileNotFoundError(path)

    artifact_sha = sha256_prefixed(artifact_path)
    audit_policy_sha = sha256_prefixed(audit_policy_path)
    policy = json.loads(audit_policy_path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise RuntimeError("audit policy must be a JSON object")

    wait_for_health(openai_base_url, timeout_seconds)
    client = OpenAI(base_url=openai_base_url, api_key=openai_api_key)
    prompt = build_prompt(policy, artifact_kind, read_text(artifact_path))
    completion = client.chat.completions.create(
        model=audit_model_id,
        messages=[
            {
                "role": "system",
                "content": "Return concise, valid JSON. Do not include markdown.",
            },
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=512,
    )
    raw_response = completion.choices[0].message.content or ""
    raw_response_path.parent.mkdir(parents=True, exist_ok=True)
    raw_response_path.write_text(raw_response, encoding="utf-8")

    parsed = extract_json_object(raw_response)
    parse_failed = parsed is None
    decision_parse_error = ""
    passed = False
    model_decision: bool | None = None
    if parsed is None:
        decision_parse_error = "model response did not contain a JSON object"
    else:
        passed_value = parsed.get("passed", parsed.get("pass"))
        model_decision = coerce_boolean(passed_value)
        if model_decision is None:
            decision_parse_error = "model JSON did not contain boolean 'passed' or 'pass'"
        else:
            passed = model_decision

    if force_pass:
        passed = True
        decision_parse_error = (
            "AUDIT_AGENT_FORCE_PASS=1 forced pass for pipeline testing"
        )

    result = {
        "pass": passed,
        "audit_kind": artifact_kind,
        "audited_sha256": artifact_sha,
        "audit_policy_sha256": audit_policy_sha,
        "audit_model_id": audit_model_id,
        "audit_agent_version": AUDIT_AGENT_VERSION,
        "raw_model_response_path": str(raw_response_path),
        "raw_model_response_preview": preview_text(raw_response),
        "parsed_model_response": parsed,
        "parse_failed": parse_failed,
        "model_decision": model_decision,
        "decision_parse_error": decision_parse_error,
        "force_pass": force_pass,
    }
    write_json_atomic(result_path, result)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        failure_path = Path(os.getenv("RESULT_PATH", "/workspace/output/audit_result.json"))
        failure = {
            "pass": False,
            "audit_kind": os.getenv("AUDIT_KIND", "serving_patch"),
            "audited_sha256": "sha256:" + ("0" * 64),
            "audit_policy_sha256": "sha256:" + ("0" * 64),
            "audit_model_id": os.getenv("AUDIT_MODEL_ID", "audit-model"),
            "audit_agent_version": AUDIT_AGENT_VERSION,
        }
        try:
            write_json_atomic(failure_path, failure)
        finally:
            print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
