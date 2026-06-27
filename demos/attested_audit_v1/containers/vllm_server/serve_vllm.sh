#!/usr/bin/env bash
set -Eeuo pipefail

ROLE="vllm_server"

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: ${name} is required" >&2
    exit 2
  fi
}

write_failure() {
  local line="$1"
  local command="$2"
  local output_dir="${OUTPUT_DIR:-/workspace/output}"
  mkdir -p "$output_dir"
  python3 - "$output_dir/vllm_server_failure.json" "$ROLE" "$line" "$command" <<'PY'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "passed": False,
    "container_role": sys.argv[2],
    "failed_at": datetime.now(timezone.utc).isoformat(),
    "line": int(sys.argv[3]),
    "command": sys.argv[4],
}
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(path)
PY
}

trap 'write_failure "$LINENO" "$BASH_COMMAND"' ERR

VLLM_HOST="${VLLM_HOST:-0.0.0.0}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.8}"
VLLM_SERVED_MODEL_NAME="${VLLM_SERVED_MODEL_NAME:-served-model}"
VLLM_READY_TIMEOUT_SECONDS="${VLLM_READY_TIMEOUT_SECONDS:-1800}"
MODEL_PATH="${MODEL_PATH:-/tmp/model}"
MODEL_ID="${MODEL_ID:-}"
OUTPUT_DIR="${OUTPUT_DIR:-/workspace/output}"
LOG_PATH="${LOG_PATH:-${OUTPUT_DIR}/vllm_server.log}"
REQUIRE_CUDA="${REQUIRE_CUDA:-1}"
MODEL_BUNDLE="${MODEL_BUNDLE:-}"
COMPILED_RUNTIME_BUNDLE="${COMPILED_RUNTIME_BUNDLE:-}"

mkdir -p "$OUTPUT_DIR"
exec > >(tee "$LOG_PATH") 2>&1

echo "==> ${ROLE}: GPU/runtime preflight"
env | sort | grep -E '^(CUDA|NVIDIA|VLLM|LD_LIBRARY_PATH)=' || true
command -v nvidia-smi >/dev/null && nvidia-smi || true
ldconfig -p | grep -E 'libcuda|libcudart|libnvidia-ml' || true
python3 - <<'PY'
import os
from pathlib import Path

for path in ("/dev/nvidiactl", "/dev/nvidia0", "/dev/dxg"):
    print(f"{path} exists={Path(path).exists()}")
print(f"VLLM_TARGET_DEVICE={os.getenv('VLLM_TARGET_DEVICE')}")
PY
if [[ "$REQUIRE_CUDA" == "1" && ! -e /dev/nvidia0 ]]; then
  echo "ERROR: CUDA GPU device /dev/nvidia0 is not visible inside container" >&2
  exit 1
fi

if [[ -n "$COMPILED_RUNTIME_BUNDLE" ]]; then
  echo "==> ${ROLE}: installing compiled runtime bundle"
  test -f "$COMPILED_RUNTIME_BUNDLE"
  runtime_wheelhouse="$(mktemp -d)"
  tar -xzf "$COMPILED_RUNTIME_BUNDLE" -C "$runtime_wheelhouse"
  if ! compgen -G "${runtime_wheelhouse}/*.whl" >/dev/null; then
    echo "ERROR: compiled runtime bundle does not contain any wheel files" >&2
    exit 1
  fi
  python3 -m pip install --no-cache-dir --force-reinstall --no-deps "${runtime_wheelhouse}"/*.whl
  rm -rf "$runtime_wheelhouse"
fi

if [[ -n "$MODEL_BUNDLE" ]]; then
  echo "==> ${ROLE}: preparing private model bundle"
  test -f "$MODEL_BUNDLE"
  rm -rf "$MODEL_PATH"
  mkdir -p "$MODEL_PATH"
  tar -xf "$MODEL_BUNDLE" -C "$MODEL_PATH"
  SERVE_MODEL_PATH="$MODEL_PATH"
elif [[ -n "$MODEL_ID" ]]; then
  echo "==> ${ROLE}: using public model id ${MODEL_ID}"
  SERVE_MODEL_PATH="$MODEL_ID"
else
  echo "ERROR: set MODEL_BUNDLE for a private model or MODEL_ID for a public model" >&2
  exit 2
fi

echo "==> Starting vLLM on ${VLLM_HOST}:${VLLM_PORT}"
vllm serve \
  --model "$SERVE_MODEL_PATH" \
  --served-model-name "$VLLM_SERVED_MODEL_NAME" \
  --host "$VLLM_HOST" \
  --port "$VLLM_PORT" \
  --gpu-memory-utilization "$VLLM_GPU_MEMORY_UTILIZATION" &
server_pid="$!"

terminate_server() {
  kill "$server_pid" 2>/dev/null || true
  wait "$server_pid" 2>/dev/null || true
}
trap terminate_server TERM INT

deadline=$((SECONDS + VLLM_READY_TIMEOUT_SECONDS))
health_url="http://127.0.0.1:${VLLM_PORT}/health"
echo "==> Waiting for vLLM readiness at ${health_url}"
while true; do
  if curl -fsS "$health_url" >/dev/null; then
    echo "==> vLLM is ready"
    break
  fi
  if ! kill -0 "$server_pid" 2>/dev/null; then
    wait "$server_pid"
  fi
  if (( SECONDS >= deadline )); then
    echo "ERROR: vLLM did not become ready within ${VLLM_READY_TIMEOUT_SECONDS}s" >&2
    terminate_server
    exit 1
  fi
  sleep 5
done

wait "$server_pid"
