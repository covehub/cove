#!/usr/bin/env python3
"""Audit private owner artifacts inside the attested benchmark workflow."""

from __future__ import annotations

import json
import os

from cove_demo_common import (
    TimingRecorder,
    add_timing_metadata,
    log,
    read_text,
    require_env,
    require_cuda_preflight,
    sha256_bytes,
    timed_step,
    write_json,
)


SERVICE = "audit_agent"

AUDIT_POLICIES = {
    "eval_code": (
        "Audit that this private evaluation code only defines the Inspect "
        "benchmark task and scorer. It may read the private eval data to build "
        "samples and score model outputs supplied by Inspect, but the public "
        "benchmark runner owns model endpoint calls and aggregate metric "
        "writing. It must not read, copy, upload, print, or "
        "otherwise exfiltrate model weights, model archives, compiled wheels, "
        "credentials, raw prompts, raw responses, or private data; must not make "
        "external network calls; and must not use hidden shell, subprocess, or "
        "filesystem behavior unrelated to the eval."
    ),
    "serving_patch": (
        "Audit this private vLLM serving-code patch for security bugs in the "
        "serving path. The patch is allowed to rename Qwen2-derived classes, "
        "register a CoveDemo architecture, and serve a renamed private model; "
        "that private-model rename is intentional and is not a failure by "
        "itself. Focus only on memory-corruption risk, unsafe native or FFI "
        "code, arbitrary code execution, unsafe deserialization, hidden shell or "
        "subprocess behavior, external network calls, credential or artifact "
        "exfiltration, filesystem access unrelated to loading/serving the model, "
        "or other vulnerabilities introduced into the serving code."
    ),
}


class AuditFailure(Exception):
    pass


def _extract_json_object(text: str) -> dict[str, object] | None:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "pass" in value:
            return value
    return None


def _render_qwen_chat(tokenizer: object, messages: list[dict[str, str]]) -> str:
    try:
        return tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
            enable_thinking=False,
        )
    except TypeError:
        return tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )


def _validated_audit_result(value: dict[str, object]) -> dict[str, object]:
    if not isinstance(value.get("pass"), bool):
        raise AuditFailure("Qwen response JSON did not include boolean key 'pass'")
    reasoning = str(value.get("reasoning", "")).strip()
    if not reasoning:
        reasoning = "Qwen returned a pass/fail decision without additional reasoning."
    return {
        "pass": value["pass"],
        "reasoning": reasoning,
    }


def _hf_model_already_cached(model_id: str) -> bool:
    try:
        from huggingface_hub import snapshot_download

        snapshot_download(model_id, local_files_only=True)
    except Exception:
        return False
    return True


def _llm_audit(
    audit_kind: str,
    source: str,
    model_id: str,
    timings: TimingRecorder,
) -> dict[str, object]:
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except Exception as exc:  # pragma: no cover - runtime dependency failure
        raise AuditFailure(f"Qwen runtime import failed: {exc}") from exc

    with timed_step(timings, "audit_prompt_render_seconds"):
        prompt = (
            "You are a conservative code auditor for an attested confidential AI eval.\n"
            f"{AUDIT_POLICIES.get(audit_kind, AUDIT_POLICIES['eval_code'])}\n"
            "Return only strict JSON with keys: pass (boolean) and reasoning (string).\n"
            "Do not include markdown fences or prose outside the JSON object.\n\n"
            f"Artifact kind: {audit_kind}\n"
            "Artifact contents:\n"
            "```text\n"
            f"{source[:12000]}\n"
            "```\n"
        )
    try:
        with timed_step(timings, "audit_model_load_seconds"):
            tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
            model = AutoModelForCausalLM.from_pretrained(
                model_id,
                device_map=os.environ.get("AUDIT_DEVICE_MAP", "auto"),
                low_cpu_mem_usage=True,
                torch_dtype="auto",
                trust_remote_code=True,
            )
            model.eval()
            if tokenizer.pad_token_id is None and tokenizer.eos_token_id is not None:
                tokenizer.pad_token = tokenizer.eos_token

        messages = [
            {
                "role": "system",
                "content": "You are a precise audit model that answers only with strict JSON.",
            },
            {"role": "user", "content": prompt},
        ]
        json_prefix = '{"pass":'
        rendered = _render_qwen_chat(tokenizer, messages) + json_prefix
        with timed_step(timings, "audit_tokenization_seconds"):
            inputs = tokenizer(rendered, return_tensors="pt")
        with timed_step(timings, "audit_device_transfer_seconds"):
            inputs = {key: value.to(model.device) for key, value in inputs.items()}
        with timed_step(timings, "audit_generation_seconds"):
            with torch.no_grad():
                output_ids = model.generate(
                    **inputs,
                    max_new_tokens=256,
                    do_sample=False,
                    pad_token_id=tokenizer.eos_token_id,
                )
        with timed_step(timings, "audit_decode_seconds"):
            generated_ids = output_ids[0][inputs["input_ids"].shape[-1] :]
            raw_generated = tokenizer.decode(generated_ids, skip_special_tokens=True)
            generated = json_prefix + raw_generated
    except Exception as exc:  # pragma: no cover - runtime dependency failure
        raise AuditFailure(f"Qwen load or generation failed: {exc}") from exc

    with timed_step(timings, "audit_json_parse_seconds"):
        parsed = _extract_json_object(generated) or _extract_json_object(raw_generated)
        if parsed is None:
            raise AuditFailure(f"Qwen response was not valid JSON: {generated[:500]}")
    with timed_step(timings, "audit_json_validate_seconds"):
        return _validated_audit_result(parsed)


def main() -> int:
    timings = TimingRecorder()
    audit_kind = require_env("AUDIT_KIND")
    input_path = require_env("INPUT_PATH")
    result_path = require_env("RESULT_PATH")
    model_id = require_env("AUDIT_MODEL_ID")
    with timed_step(timings, "hf_cache_probe_seconds"):
        public_assets_already_cached = _hf_model_already_cached(model_id)
    with timed_step(timings, "cuda_preflight_seconds"):
        gpu_status = require_cuda_preflight(SERVICE)

    # Load the owner-provided private artifact for audit only; this node passes
    # the bytes to Qwen and does not import or execute the artifact itself.
    with timed_step(timings, "read_private_input_seconds"):
        source = read_text(input_path)
        audited_sha256 = sha256_bytes(source.encode("utf-8"))

    try:
        with timed_step(timings, "llm_audit_seconds"):
            llm_result = _llm_audit(audit_kind, source, model_id, timings)
        llm_used = True
        passed = bool(llm_result["pass"])
        reasoning = str(llm_result["reasoning"])
    except AuditFailure as exc:
        llm_used = False
        passed = False
        reasoning = f"Qwen audit failed: {exc}"

    payload = {
        "audit_kind": audit_kind,
        "audited_sha256": audited_sha256,
        "llm_used": llm_used,
        "pass": passed,
        "reasoning": reasoning,
    }
    payload.update(gpu_status)
    add_timing_metadata(
        payload,
        timings,
        workload_keys=["llm_audit_seconds"],
        startup_keys=["cuda_preflight_seconds", "hf_cache_probe_seconds"],
        artifact_io_keys=["read_private_input_seconds"],
        public_assets_already_cached=public_assets_already_cached,
    )
    write_json(result_path, payload)
    log(SERVICE, f"audited {audit_kind} at {input_path}: pass={passed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
