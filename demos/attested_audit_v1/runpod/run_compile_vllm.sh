#!/usr/bin/env bash
set -Eeuo pipefail

RUN_ID="${RUN_ID:-$(date +%Y%m%d%H%M%S)}"
RUN_DIR="${RUN_DIR:-/workspace/runs/${RUN_ID}/compile_vllm}"

export PRISTINE_SOURCE="${PRISTINE_SOURCE:-/workspace/inputs/pristine_serving_source.tar.gz}"
export SERVING_PATCH="${SERVING_PATCH:-/workspace/inputs/serving_patch.diff}"
export COMPILED_RUNTIME_BUNDLE="${COMPILED_RUNTIME_BUNDLE:-${RUN_DIR}/compiled_serving_runtime_bundle.tar.gz}"
export RESULT_PATH="${RESULT_PATH:-${RUN_DIR}/compile_result.json}"
export BUILD_DIR="${BUILD_DIR:-${RUN_DIR}/build}"
export WHEELHOUSE="${WHEELHOUSE:-${RUN_DIR}/wheelhouse}"
export LOG_PATH="${LOG_PATH:-${RUN_DIR}/compile_vllm.log}"

mkdir -p "$RUN_DIR"

echo "==> Run ID: ${RUN_ID}"
echo "==> Run dir: ${RUN_DIR}"
echo "==> Running vLLM compiler"
/app/bin/compile_vllm.sh

echo "==> Compile result:"
cat "$RESULT_PATH"
