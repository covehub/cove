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

require_env SERVING_RUNTIME_BUNDLE
require_env MODEL_BUNDLE

VLLM_HOST="${VLLM_HOST:-0.0.0.0}"
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_GPU_MEMORY_UTILIZATION="${VLLM_GPU_MEMORY_UTILIZATION:-0.8}"
VLLM_SERVED_MODEL_NAME="${VLLM_SERVED_MODEL_NAME:-served-model}"
VLLM_READY_TIMEOUT_SECONDS="${VLLM_READY_TIMEOUT_SECONDS:-1800}"
MODEL_PATH="${MODEL_PATH:-/tmp/model}"
OUTPUT_DIR="${OUTPUT_DIR:-/workspace/output}"
LOG_PATH="${LOG_PATH:-${OUTPUT_DIR}/vllm_server.log}"
SERVING_RUNTIME_BUNDLE_MANIFEST="${SERVING_RUNTIME_BUNDLE_MANIFEST:-}"
SERVING_RUNTIME_BUNDLE_PARTS_GLOB="${SERVING_RUNTIME_BUNDLE_PARTS_GLOB:-}"

mkdir -p "$OUTPUT_DIR"
exec > >(tee "$LOG_PATH") 2>&1

echo "==> ${ROLE}: installing runtime bundle"
test -f "$MODEL_BUNDLE"

if [[ ! -f "$SERVING_RUNTIME_BUNDLE" ]]; then
  if [[ -z "$SERVING_RUNTIME_BUNDLE_MANIFEST" || -z "$SERVING_RUNTIME_BUNDLE_PARTS_GLOB" ]]; then
    echo "ERROR: SERVING_RUNTIME_BUNDLE is missing and split bundle inputs were not provided" >&2
    exit 1
  fi
  echo "==> ${ROLE}: reassembling split runtime bundle"
  python3 - "$SERVING_RUNTIME_BUNDLE" "$SERVING_RUNTIME_BUNDLE_MANIFEST" "$SERVING_RUNTIME_BUNDLE_PARTS_GLOB" <<'PY'
import glob
import hashlib
import json
import sys
from pathlib import Path

output_path = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
parts_glob = sys.argv[3]
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
expected_parts = manifest.get("parts")
if not isinstance(expected_parts, list) or not expected_parts:
    raise SystemExit("runtime bundle manifest has no parts")
available = {Path(path).name: Path(path) for path in glob.glob(parts_glob)}
output_path.parent.mkdir(parents=True, exist_ok=True)
full_hash = hashlib.sha256()
with output_path.open("wb") as output:
    for part in expected_parts:
        filename = part["filename"]
        path = available.get(filename)
        if path is None:
            raise SystemExit(f"missing runtime bundle part: {filename}")
        part_hash = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                part_hash.update(chunk)
                full_hash.update(chunk)
                output.write(chunk)
        observed_part_hash = "sha256:" + part_hash.hexdigest()
        if observed_part_hash != part["sha256"]:
            raise SystemExit(
                f"runtime bundle part hash mismatch for {filename}: "
                f"{observed_part_hash} != {part['sha256']}"
            )
        if path.stat().st_size != part["size_bytes"]:
            raise SystemExit(f"runtime bundle part size mismatch for {filename}")
observed_full_hash = "sha256:" + full_hash.hexdigest()
if observed_full_hash != manifest["bundle_sha256"]:
    raise SystemExit(
        f"runtime bundle hash mismatch: {observed_full_hash} != {manifest['bundle_sha256']}"
    )
print(f"reassembled {output_path} with {observed_full_hash}")
PY
fi

test -f "$SERVING_RUNTIME_BUNDLE"

rm -rf /tmp/serving-runtime-bundle
mkdir -p /tmp/serving-runtime-bundle
tar -xzf "$SERVING_RUNTIME_BUNDLE" -C /tmp/serving-runtime-bundle

rm -rf "$MODEL_PATH"
mkdir -p "$MODEL_PATH"
tar -xf "$MODEL_BUNDLE" -C "$MODEL_PATH"

shopt -s nullglob
wheels=(/tmp/serving-runtime-bundle/*.whl)
if (( ${#wheels[@]} == 0 )); then
  echo "ERROR: SERVING_RUNTIME_BUNDLE must contain at least one .whl file at its root" >&2
  exit 1
fi

uv pip install --system "${wheels[@]}" --verbose
echo "==> Starting vLLM on ${VLLM_HOST}:${VLLM_PORT}"
vllm serve \
  --model "$MODEL_PATH" \
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
