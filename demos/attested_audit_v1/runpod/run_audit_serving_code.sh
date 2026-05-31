#!/usr/bin/env bash
set -Eeuo pipefail

RUN_ID="${RUN_ID:-$(date +%Y%m%d%H%M%S)}"
RUN_DIR="${RUN_DIR:-/workspace/runs/${RUN_ID}/audit_serving_code}"

export SERVING_RUNTIME_BUNDLE="${SERVING_RUNTIME_BUNDLE:-/workspace/inputs/audit_serving_runtime_bundle.tar.gz}"
export MODEL_BUNDLE="${MODEL_BUNDLE:-/workspace/inputs/audit_model_weights.tar}"
export MODEL_PATH="${MODEL_PATH:-/tmp/audit_model}"
export VLLM_HOST="${VLLM_HOST:-0.0.0.0}"
export VLLM_PORT="${VLLM_PORT:-8000}"
export VLLM_SERVED_MODEL_NAME="${VLLM_SERVED_MODEL_NAME:-audit-model}"
export OUTPUT_DIR="${OUTPUT_DIR:-${RUN_DIR}}"
export LOG_PATH="${LOG_PATH:-${RUN_DIR}/vllm_server.log}"

export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://127.0.0.1:${VLLM_PORT}/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-vllm}"
export AUDIT_MODEL_ID="${AUDIT_MODEL_ID:-${VLLM_SERVED_MODEL_NAME}}"
export SERVING_PATCH="${SERVING_PATCH:-/workspace/inputs/serving_patch.diff}"
export PRISTINE_SOURCE="${PRISTINE_SOURCE:-/workspace/inputs/pristine_serving_source.tar.gz}"
export AUDIT_POLICY="${AUDIT_POLICY:-/workspace/inputs/audit_policy.json}"
export RESULT_PATH="${RESULT_PATH:-${RUN_DIR}/audit_result.json}"
export RAW_MODEL_RESPONSE_PATH="${RAW_MODEL_RESPONSE_PATH:-${RUN_DIR}/audit_model_response.txt}"

mkdir -p "$RUN_DIR"

echo "==> Run ID: ${RUN_ID}"
echo "==> Run dir: ${RUN_DIR}"
echo "==> Starting vLLM server"
/app/bin/serve_vllm.sh &
server_pid="$!"

cleanup() {
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

echo "==> Running audit agent"
python /app/bin/run_audit_agent.py

echo "==> Audit result:"
cat "$RESULT_PATH"
