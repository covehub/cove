#!/usr/bin/env bash
set -Eeuo pipefail

REPO_ROOT="${REPO_ROOT:-/workspace/code_audit_bench}"
DEMO_ROOT="${DEMO_ROOT:-${REPO_ROOT}/cove/demos/attested_audit_v1}"
FIXTURE_DIR="${FIXTURE_DIR:-/app/fixtures}"
INPUT_DIR="${INPUT_DIR:-/workspace/inputs}"
MODEL_ID="${MODEL_ID:-Qwen/Qwen3-0.6B}"
AUDIT_MODEL_DIR="${AUDIT_MODEL_DIR:-/workspace/models/audit_model}"
RUNTIME_WHEELS_DIR="${RUNTIME_WHEELS_DIR:-/workspace/runtime_wheels}"
RUN_ID="${RUN_ID:-$(date +%Y%m%d%H%M%S)}"
PRISTINE_SOURCE_INPUT="${PRISTINE_SOURCE_INPUT:-/workspace/inputs/pristine_serving_source.tar.gz}"

log() {
  echo "==> $*"
}

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "ERROR: missing required file: $path" >&2
    exit 1
  fi
}

log "Creating input directories"
mkdir -p "$INPUT_DIR" "$AUDIT_MODEL_DIR" "$RUNTIME_WHEELS_DIR" /workspace/runs

log "Copying fixture inputs"
if [[ -f "${FIXTURE_DIR}/serving_patch.diff" ]]; then
  cp "${FIXTURE_DIR}/serving_patch.diff" "${INPUT_DIR}/serving_patch.diff"
else
  cp "${DEMO_ROOT}/fixtures/serving_patch.diff" "${INPUT_DIR}/serving_patch.diff"
fi
if [[ -f "${FIXTURE_DIR}/audit_policy.json" ]]; then
  cp "${FIXTURE_DIR}/audit_policy.json" "${INPUT_DIR}/audit_policy.json"
else
  cp "${DEMO_ROOT}/fixtures/audit_policy.json" "${INPUT_DIR}/audit_policy.json"
fi

if [[ -f "${INPUT_DIR}/pristine_serving_source.tar.gz" ]]; then
  log "Using existing pristine source at ${INPUT_DIR}/pristine_serving_source.tar.gz"
elif [[ -f "$PRISTINE_SOURCE_INPUT" ]]; then
  cp "$PRISTINE_SOURCE_INPUT" "${INPUT_DIR}/pristine_serving_source.tar.gz"
elif [[ -f "${REPO_ROOT}/code_audit_bench/data/public_artifacts/vllm/vllm-72506c9.tar.gz" ]]; then
  cp "${REPO_ROOT}/code_audit_bench/data/public_artifacts/vllm/vllm-72506c9.tar.gz" \
    "${INPUT_DIR}/pristine_serving_source.tar.gz"
else
  cat >&2 <<'EOF'
ERROR: missing /workspace/inputs/pristine_serving_source.tar.gz.
Upload or copy the vLLM source tarball to that path before running this script.
EOF
  exit 1
fi

log "Installing Hugging Face downloader if needed"
python3 -m pip install -U huggingface_hub

if [[ ! -f "${AUDIT_MODEL_DIR}/config.json" ]]; then
  log "Downloading audit model ${MODEL_ID}"
  hf download "${MODEL_ID}" \
    --local-dir "${AUDIT_MODEL_DIR}"
else
  log "Using existing audit model at ${AUDIT_MODEL_DIR}"
fi

log "Packaging audit model bundle"
tar -cf "${INPUT_DIR}/audit_model_weights.tar" -C "${AUDIT_MODEL_DIR}" .

if ! compgen -G "${RUNTIME_WHEELS_DIR}/*.whl" >/dev/null; then
  log "Downloading vLLM runtime wheels"
  python3 -m pip download -d "${RUNTIME_WHEELS_DIR}" vllm
else
  log "Using existing wheels in ${RUNTIME_WHEELS_DIR}"
fi

log "Packaging serving runtime bundle"
tar -czf "${INPUT_DIR}/audit_serving_runtime_bundle.tar.gz" -C "${RUNTIME_WHEELS_DIR}" .

log "Input inventory"
ls -lh "$INPUT_DIR"

log "Running audit_serving_code"
export RUN_ID
export MODEL_BUNDLE="${INPUT_DIR}/audit_model_weights.tar"
export SERVING_RUNTIME_BUNDLE="${INPUT_DIR}/audit_serving_runtime_bundle.tar.gz"
export SERVING_PATCH="${INPUT_DIR}/serving_patch.diff"
export PRISTINE_SOURCE="${INPUT_DIR}/pristine_serving_source.tar.gz"
export AUDIT_POLICY="${INPUT_DIR}/audit_policy.json"
export AUDIT_MODEL_ID="${AUDIT_MODEL_ID:-audit-model}"
export VLLM_SERVED_MODEL_NAME="${VLLM_SERVED_MODEL_NAME:-${AUDIT_MODEL_ID}}"

/app/bin/run_audit_serving_code.sh

log "Result"
cat "/workspace/runs/${RUN_ID}/audit_serving_code/audit_result.json"

log "Expected hashes"
sha256sum "${INPUT_DIR}/serving_patch.diff"
sha256sum "${INPUT_DIR}/pristine_serving_source.tar.gz"
sha256sum "${INPUT_DIR}/audit_policy.json"
