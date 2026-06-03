#!/usr/bin/env bash
set -Eeuo pipefail

ROLE="vllm_compiler"
COMPILER_VERSION="attested-audit-v1.vllm-compiler.1"

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: ${name} is required" >&2
    exit 2
  fi
}

write_result() {
  local result_path="$1"
  local passed="$2"
  local message="${3:-}"
  python3 - "$result_path" "$passed" "$COMPILER_VERSION" "$message" <<'PY'
import json
import os
import sys
from pathlib import Path

result_path = Path(sys.argv[1])
passed = sys.argv[2].lower() == "true"
compiler_version = sys.argv[3]
message = sys.argv[4]
wheelhouse = Path(os.environ.get("WHEELHOUSE", ""))
wheel_names = sorted(p.name for p in wheelhouse.glob("*.whl")) if wheelhouse.exists() else []
payload = {
    "pass": passed,
    "compiler_version": compiler_version,
    "wheel_names": wheel_names,
}
if message:
    payload["message"] = message
result_path.parent.mkdir(parents=True, exist_ok=True)
tmp = result_path.with_suffix(result_path.suffix + ".tmp")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(result_path)
PY
}

write_failure() {
  local line="$1"
  local command="$2"
  local result_path="${RESULT_PATH:-/workspace/output/compile_result.json}"
  write_result "$result_path" false "failed at line ${line}: ${command}"
}

trap 'write_failure "$LINENO" "$BASH_COMMAND"' ERR

RUN_ID="${RUN_ID:-$(date +%Y%m%d%H%M%S)}"
RUN_DIR="${RUN_DIR:-/workspace/runs/${RUN_ID}/compile_vllm}"
PRISTINE_SOURCE="${PRISTINE_SOURCE:-/workspace/inputs/pristine_serving_source.tar.gz}"
SERVING_PATCH="${SERVING_PATCH:-/workspace/inputs/serving_patch.diff}"
COMPILED_RUNTIME_BUNDLE="${COMPILED_RUNTIME_BUNDLE:-${RUN_DIR}/compiled_serving_runtime_bundle.tar.gz}"
RESULT_PATH="${RESULT_PATH:-${RUN_DIR}/compile_result.json}"
BUILD_DIR="${BUILD_DIR:-/workspace/output/build}"
SOURCE_DIR="${SOURCE_DIR:-${BUILD_DIR}/source}"
WHEELHOUSE="${WHEELHOUSE:-/workspace/output/wheelhouse}"
LOG_PATH="${LOG_PATH:-${RUN_DIR}/compile_vllm.log}"
CUDA_VERSION="${CUDA_VERSION:-12.8.1}"
export WHEELHOUSE

mkdir -p "$(dirname "$LOG_PATH")" "$BUILD_DIR" "$WHEELHOUSE" "$(dirname "$COMPILED_RUNTIME_BUNDLE")"
exec > >(tee "$LOG_PATH") 2>&1

echo "==> ${ROLE}: starting"
test -f "$PRISTINE_SOURCE"
test -f "$SERVING_PATCH"

rm -rf "$SOURCE_DIR" "$WHEELHOUSE"
mkdir -p "$SOURCE_DIR" "$WHEELHOUSE"

echo "==> Extracting pristine vLLM source"
tar -xzf "$PRISTINE_SOURCE" -C "$SOURCE_DIR" --strip-components=1

cd "$SOURCE_DIR"
git init
git config user.email "cove@example.invalid"
git config user.name "cove"
git add .
git commit -m pristine

echo "==> Applying serving patch"
git apply --check "$SERVING_PATCH"
git apply "$SERVING_PATCH"

CUDA_MINOR="$(echo "$CUDA_VERSION" | cut -d. -f1,2 | tr -d '.')"
PYTORCH_INDEX="${PYTORCH_CUDA_INDEX_BASE_URL:-https://download.pytorch.org/whl}/cu${CUDA_MINOR}"

echo "==> Installing vLLM build dependencies"
uv pip install --system -r requirements/cuda.txt --extra-index-url "$PYTORCH_INDEX"
uv pip install --system -r requirements/build.txt --extra-index-url "$PYTORCH_INDEX"

echo "==> Building patched vLLM wheel"
python3 setup.py bdist_wheel --dist-dir="$WHEELHOUSE" --py-limited-api=cp38

echo "==> Creating compiled serving runtime bundle"
tar -czf "$COMPILED_RUNTIME_BUNDLE" -C "$WHEELHOUSE" .

write_result "$RESULT_PATH" true
echo "==> Compile complete: ${COMPILED_RUNTIME_BUNDLE}"
