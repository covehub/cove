#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from huggingface_hub import snapshot_download


EVAL_CODE_TEMPLATE = """#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from openai import OpenAI


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--data-path", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--result-path", required=True)
    parser.add_argument("--threshold", required=True, type=float)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    client = OpenAI(base_url=args.base_url, api_key="cove-demo")
    rows = [
        json.loads(line)
        for line in Path(args.data_path).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    successful = 0
    for row in rows:
        response = client.chat.completions.create(
            model=args.model,
            messages=[{"role": "user", "content": row["prompt"]}],
            max_tokens=48,
            temperature=0,
        )
        message = response.choices[0].message.content or ""
        if message.strip():
            successful += 1

    total = len(rows)
    score = 0.0 if total == 0 else successful / total
    payload = {
        "benchmark_name": "nonempty_completion_rate",
        "pass": score >= args.threshold,
        "passes_threshold": score >= args.threshold,
        "score": score,
        "successful_responses": successful,
        "total_prompts": total,
    }
    Path(args.result_path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

MODEL_ALLOW_PATTERNS = [
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "merges.txt",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "vocab.json",
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-root",
        type=Path,
        default=Path("demos/attested_audit/runtime_inputs"),
    )
    parser.add_argument(
        "--model-id",
        default="HuggingFaceTB/SmolLM2-135M-Instruct",
    )
    parser.add_argument(
        "--dataset-name",
        default="yahma/alpaca-cleaned",
    )
    parser.add_argument(
        "--num-examples",
        type=int,
        default=5,
    )
    parser.add_argument(
        "--vllm-version",
        default="0.17.0",
    )
    return parser.parse_args()


def _clone_vllm(version: str, destination: Path) -> Path:
    if destination.exists():
        shutil.rmtree(destination)
    subprocess.run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            f"v{version}",
            "https://github.com/vllm-project/vllm",
            str(destination),
        ],
        check=True,
    )
    return destination


def _write_serving_patch(output_path: Path, *, vllm_version: str) -> None:
    with tempfile.TemporaryDirectory(prefix="cove-vllm-patch-") as temp_dir:
        repo_root = _clone_vllm(vllm_version, Path(temp_dir) / "vllm-src")
        api_server_path = repo_root / "vllm" / "entrypoints" / "openai" / "api_server.py"
        original = api_server_path.read_text(encoding="utf-8")
        marker_prefix = "logger = init_logger("
        marker_line = next((line for line in original.splitlines() if line.startswith(marker_prefix)), None)
        if marker_line is None:
            raise SystemExit(f"expected logger init line not found in {api_server_path}")
        updated = original.replace(
            marker_line,
            marker_line
            + '\nlogger.info("cove demo patched vLLM OpenAI server module loaded")',
            1,
        )
        api_server_path.write_text(updated, encoding="utf-8")
        diff = subprocess.run(
            ["git", "diff", "--", str(api_server_path.relative_to(repo_root))],
            cwd=repo_root,
            text=True,
            capture_output=True,
            check=True,
        )
        if not diff.stdout.strip():
            raise SystemExit("failed to generate serving patch")
        output_path.write_text(diff.stdout, encoding="utf-8")


def _write_eval_code(output_path: Path) -> None:
    output_path.write_text(EVAL_CODE_TEMPLATE, encoding="utf-8")


def _write_eval_data(output_path: Path, *, dataset_name: str, num_examples: int) -> None:
    from datasets import load_dataset

    dataset = load_dataset(dataset_name, split=f"train[:{num_examples}]")
    rows: list[str] = []
    for record in dataset:
        instruction = str(record.get("instruction", "")).strip()
        input_text = str(record.get("input", "")).strip()
        prompt = instruction if not input_text else f"{instruction}\n\nContext:\n{input_text}"
        rows.append(
            json.dumps(
                {
                    "prompt": prompt,
                    "expected_output": str(record.get("output", "")).strip(),
                },
                sort_keys=True,
            )
        )
    output_path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _write_model_archive(output_path: Path, *, model_id: str) -> None:
    with tempfile.TemporaryDirectory(prefix="cove-model-download-") as temp_dir:
        snapshot_dir = Path(
            snapshot_download(
                repo_id=model_id,
                local_dir=Path(temp_dir) / "snapshot",
                local_dir_use_symlinks=False,
                allow_patterns=MODEL_ALLOW_PATTERNS,
            )
        )
        with tarfile.open(output_path, "w") as tar:
            for relative_path in MODEL_ALLOW_PATTERNS:
                candidate = snapshot_dir / relative_path
                if candidate.exists():
                    tar.add(candidate, arcname=f"{snapshot_dir.name}/{relative_path}")


def main() -> int:
    args = parse_args()
    args.output_root.mkdir(parents=True, exist_ok=True)

    model_archive = args.output_root / "alice_private_model.tar"
    serving_patch = args.output_root / "alice_private_serving_patch.diff"
    eval_code = args.output_root / "bob_private_eval_code.py"
    eval_data = args.output_root / "bob_private_eval_data.jsonl"

    print(f"writing {serving_patch}")
    _write_serving_patch(serving_patch, vllm_version=args.vllm_version)
    print(f"writing {eval_code}")
    _write_eval_code(eval_code)
    print(f"writing {eval_data}")
    _write_eval_data(eval_data, dataset_name=args.dataset_name, num_examples=args.num_examples)
    print(f"writing {model_archive}")
    _write_model_archive(model_archive, model_id=args.model_id)

    render_script = Path(__file__).resolve().with_name("render_workflow.py")
    subprocess.run(
        ["python", str(render_script), "--inputs-root", str(args.output_root)],
        check=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
