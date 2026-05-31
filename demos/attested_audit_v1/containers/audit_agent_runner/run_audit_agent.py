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


def build_prompt(policy: dict[str, Any], patch_text: str) -> str:
    return (
        "You are an audit agent for confidential model-serving deployments.\n"
        "Audit the git diff against the provided policy. Return only JSON with the "
        "single key passed, whose value must be true or false.\n\n"
        f"Audit policy:\n{json.dumps(policy, indent=2, sort_keys=True)}\n\n"
        f"Serving patch:\n{patch_text}\n"
    )


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def main() -> int:
    serving_patch = Path(required_env("SERVING_PATCH"))
    pristine_source = Path(required_env("PRISTINE_SOURCE"))
    audit_policy_path = Path(required_env("AUDIT_POLICY"))
    result_path = Path(os.getenv("RESULT_PATH", "/workspace/output/audit_result.json"))
    raw_response_path = Path(
        os.getenv("RAW_MODEL_RESPONSE_PATH", "/workspace/output/audit_model_response.txt")
    )
    openai_base_url = os.getenv("OPENAI_BASE_URL", "http://audit_model_server:8000/v1")
    openai_api_key = os.getenv("OPENAI_API_KEY", "vllm")
    audit_model_id = os.getenv("AUDIT_MODEL_ID", "audit-model")
    timeout_seconds = int(os.getenv("MODEL_READY_TIMEOUT_SECONDS", "600"))

    for path in (serving_patch, pristine_source, audit_policy_path):
        if not path.exists():
            raise FileNotFoundError(path)

    serving_patch_sha = sha256_prefixed(serving_patch)
    pristine_source_sha = sha256_prefixed(pristine_source)
    audit_policy_sha = sha256_prefixed(audit_policy_path)
    policy = json.loads(audit_policy_path.read_text(encoding="utf-8"))
    if not isinstance(policy, dict):
        raise RuntimeError("audit policy must be a JSON object")

    wait_for_health(openai_base_url, timeout_seconds)
    client = OpenAI(base_url=openai_base_url, api_key=openai_api_key)
    prompt = build_prompt(policy, read_text(serving_patch))
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

    parsed = extract_json_object(raw_response) or {}
    passed_value = parsed.get("passed", parsed.get("pass", False))
    passed = bool(passed_value)

    result = {
        "pass": passed,
        "serving_patch_sha256": serving_patch_sha,
        "pristine_source_sha256": pristine_source_sha,
        "audit_policy_sha256": audit_policy_sha,
        "audit_model_id": audit_model_id,
        "audit_agent_version": AUDIT_AGENT_VERSION,
        "raw_model_response_path": str(raw_response_path),
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
            "serving_patch_sha256": "sha256:" + ("0" * 64),
            "pristine_source_sha256": "sha256:" + ("0" * 64),
            "audit_policy_sha256": "sha256:" + ("0" * 64),
            "audit_model_id": os.getenv("AUDIT_MODEL_ID", "audit-model"),
            "audit_agent_version": AUDIT_AGENT_VERSION,
        }
        try:
            write_json_atomic(failure_path, failure)
        finally:
            print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
