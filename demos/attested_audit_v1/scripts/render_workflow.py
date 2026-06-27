#!/usr/bin/env python3
"""Refresh static artifact hashes in the attested audit workflow."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

import yaml


STATIC_ARTIFACT_FILES = {
    "private_model": "private_model.tar",
    "private_serving_patch": "private_serving_patch.diff",
    "private_eval_code": "private_eval_code.py",
    "private_eval_data": "private_eval_data.jsonl",
}

PRECONDITION_PATHS = {
    "private_model": [
        ("nodes", "audit_serving_code", "services", "audit_model_server", "preconditions", "==", 1),
        ("nodes", "audit_eval_code", "services", "audit_model_server", "preconditions", "==", 1),
        ("nodes", "run_eval", "services", "eval_model_server", "preconditions", "and", 1, "==", 1),
        ("nodes", "model_deployment", "services", "serve", "preconditions", "and", 2, "==", 1),
    ],
    "private_serving_patch": [
        ("nodes", "audit_serving_code", "services", "audit_agent_runner", "preconditions", "==", 1),
        ("nodes", "compile_vllm", "services", "vllm_compiler", "preconditions", "and", 1, "==", 1),
    ],
    "private_eval_code": [
        ("nodes", "audit_eval_code", "services", "audit_agent_runner", "preconditions", "==", 1),
        ("nodes", "run_eval", "services", "eval_runner", "preconditions", "and", 2, "==", 1),
    ],
    "private_eval_data": [
        ("nodes", "run_eval", "services", "eval_runner", "preconditions", "and", 3, "==", 1),
    ],
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--workflow-path",
        type=Path,
        default=Path("demos/attested_audit_v1/workflow/workflow.cove.yaml"),
    )
    parser.add_argument(
        "--inputs-root",
        type=Path,
        default=Path("demos/attested_audit_v1/runtime_inputs"),
    )
    return parser.parse_args()


def _set_path(node: object, path: tuple[object, ...], value: str) -> None:
    current = node
    for key in path[:-1]:
        current = current[key]
    current[path[-1]] = value


def main() -> int:
    args = parse_args()
    workflow = yaml.safe_load(args.workflow_path.read_text(encoding="utf-8"))
    artifacts = workflow["artifacts"]

    observed_hashes = {
        artifact_name: sha256_file(args.inputs_root / filename)
        for artifact_name, filename in STATIC_ARTIFACT_FILES.items()
    }

    for artifact_name, digest in observed_hashes.items():
        artifacts[artifact_name]["plaintext_hash"] = digest
        for path in PRECONDITION_PATHS[artifact_name]:
            _set_path(workflow, path, digest)

    args.workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False),
        encoding="utf-8",
    )
    print(f"updated {args.workflow_path}")
    for artifact_name, digest in observed_hashes.items():
        print(f"{artifact_name}: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
