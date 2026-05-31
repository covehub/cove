#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEMO_ROOT}/../../.." && pwd)"

MODEL_ID="${MODEL_ID:-Qwen/Qwen3-0.6B}"
VLLM_REQUIREMENT="${VLLM_REQUIREMENT:-vllm==0.10.2}"
ARTIFACT_ROOT="${ARTIFACT_ROOT:-${REPO_ROOT}/code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code}"
MODEL_DIR="${MODEL_DIR:-${ARTIFACT_ROOT}/audit_model}"
WHEEL_DIR="${WHEEL_DIR:-${ARTIFACT_ROOT}/serving_runtime_wheels_linux_amd64_py311}"
INPUT_DIR="${INPUT_DIR:-${ARTIFACT_ROOT}/inputs}"
PRISTINE_SOURCE="${PRISTINE_SOURCE:-${REPO_ROOT}/code_audit_bench/data/public_artifacts/vllm/vllm-72506c9.tar.gz}"
DOWNLOAD_RUNTIME_WITH_DOCKER="${DOWNLOAD_RUNTIME_WITH_DOCKER:-1}"
RUNTIME_DOWNLOAD_IMAGE="${RUNTIME_DOWNLOAD_IMAGE:-python:3.11-slim}"
RUNTIME_PART_PREFIX="${RUNTIME_PART_PREFIX:-audit_serving_runtime_bundle.tar.gz.part}"
RUNTIME_PART_SIZE="${RUNTIME_PART_SIZE:-1536m}"

log() {
    echo "==> $*"
}

require_file() {
    local path="$1"
    if [[ ! -f "${path}" ]]; then
        echo "ERROR: missing required file: ${path}" >&2
        exit 1
    fi
}

ensure_hf_cli() {
    if command -v hf >/dev/null 2>&1; then
        return
    fi
    log "Installing Hugging Face CLI"
    python3 -m pip install --user -U huggingface_hub
    export PATH="${HOME}/Library/Python/3.9/bin:${HOME}/.local/bin:${PATH}"
    if ! command -v hf >/dev/null 2>&1; then
        echo "ERROR: hf was not found after installing huggingface_hub; add your user bin directory to PATH" >&2
        exit 1
    fi
}

sha256_literal() {
    local path="$1"
    local digest
    digest="$(shasum -a 256 "${path}" | awk '{print $1}')"
    printf 'sha256:%s' "${digest}"
}

write_runtime_manifest() {
    local bundle_path="$1"
    local manifest_path="$2"
    python3 - "$bundle_path" "$manifest_path" "$RUNTIME_PART_PREFIX" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

bundle_path = Path(sys.argv[1])
manifest_path = Path(sys.argv[2])
part_prefix = sys.argv[3]
part_paths = sorted(bundle_path.parent.glob(f"{part_prefix}*"))
if not part_paths:
    raise SystemExit(f"no runtime bundle parts found for prefix {part_prefix}")

def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()

payload = {
    "schema_version": 1,
    "bundle_filename": bundle_path.name,
    "bundle_sha256": sha256(bundle_path),
    "parts": [
        {
            "filename": path.name,
            "sha256": sha256(path),
            "size_bytes": path.stat().st_size,
        }
        for path in part_paths
    ],
}
manifest_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

mkdir -p "${MODEL_DIR}" "${WHEEL_DIR}" "${INPUT_DIR}"

require_file "${DEMO_ROOT}/fixtures/audit_policy.json"
require_file "${DEMO_ROOT}/fixtures/serving_patch.diff"
require_file "${PRISTINE_SOURCE}"

log "Staging small checked-in artifacts"
cp "${DEMO_ROOT}/fixtures/audit_policy.json" "${INPUT_DIR}/audit_policy.json"
cp "${DEMO_ROOT}/fixtures/serving_patch.diff" "${INPUT_DIR}/serving_patch.diff"
cp "${PRISTINE_SOURCE}" "${INPUT_DIR}/pristine_serving_source.tar.gz"

ensure_hf_cli

if [[ ! -f "${MODEL_DIR}/config.json" ]]; then
    log "Downloading audit model ${MODEL_ID}"
    hf download "${MODEL_ID}" --local-dir "${MODEL_DIR}"
else
    log "Using existing audit model at ${MODEL_DIR}"
fi

log "Packaging audit model bundle"
tar -cf "${INPUT_DIR}/audit_model_weights.tar" -C "${MODEL_DIR}" .

if ! compgen -G "${WHEEL_DIR}/*.whl" >/dev/null; then
    log "Downloading serving runtime wheels for ${VLLM_REQUIREMENT}"
    if [[ "${DOWNLOAD_RUNTIME_WITH_DOCKER}" == "1" ]]; then
        if ! command -v docker >/dev/null 2>&1; then
            echo "ERROR: docker is required for linux/amd64 runtime wheel packaging" >&2
            exit 1
        fi
        docker run --rm --platform linux/amd64 \
            -v "${WHEEL_DIR}:/wheelhouse" \
            "${RUNTIME_DOWNLOAD_IMAGE}" \
            python -m pip download --only-binary=:all: -d /wheelhouse "${VLLM_REQUIREMENT}"
    else
        python3 -m pip download -d "${WHEEL_DIR}" "${VLLM_REQUIREMENT}"
    fi
else
    log "Using existing serving runtime wheels at ${WHEEL_DIR}"
fi

log "Packaging serving runtime bundle"
tar -czf "${INPUT_DIR}/audit_serving_runtime_bundle.tar.gz" -C "${WHEEL_DIR}" .
rm -f "${INPUT_DIR}/${RUNTIME_PART_PREFIX}"*
split -b "${RUNTIME_PART_SIZE}" \
    "${INPUT_DIR}/audit_serving_runtime_bundle.tar.gz" \
    "${INPUT_DIR}/${RUNTIME_PART_PREFIX}"
write_runtime_manifest \
    "${INPUT_DIR}/audit_serving_runtime_bundle.tar.gz" \
    "${INPUT_DIR}/audit_serving_runtime_bundle.manifest.json"

MANIFEST="${INPUT_DIR}/artifact_manifest.txt"
{
    echo "audit_serving_code artifact package"
    echo
    echo "artifact_root=${ARTIFACT_ROOT}"
    echo "model_id=${MODEL_ID}"
    echo "vllm_requirement=${VLLM_REQUIREMENT}"
    echo "runtime_wheel_dir=${WHEEL_DIR}"
    echo "runtime_download_image=${RUNTIME_DOWNLOAD_IMAGE}"
    echo
    artifacts=(
        audit_policy.json
        serving_patch.diff
        pristine_serving_source.tar.gz
        audit_model_weights.tar
        audit_serving_runtime_bundle.tar.gz
        audit_serving_runtime_bundle.manifest.json
    )
    for part in "${INPUT_DIR}/${RUNTIME_PART_PREFIX}"*; do
        artifacts+=("$(basename "${part}")")
    done
    for artifact in "${artifacts[@]}"; do
        path="${INPUT_DIR}/${artifact}"
        printf '%s  %s  %s\n' "$(sha256_literal "${path}")" "$(wc -c < "${path}" | tr -d ' ')" "${path}"
    done
} > "${MANIFEST}"

log "Packaged artifacts"
ls -lh "${INPUT_DIR}"
echo
cat "${MANIFEST}"
echo
echo "Workflow values to update before cove compile:"
echo "  audit_policy.plaintext_hash: $(sha256_literal "${INPUT_DIR}/audit_policy.json")"
echo "  serving_patch.plaintext_hash: $(sha256_literal "${INPUT_DIR}/serving_patch.diff")"
echo "  pristine_serving_source.plaintext_hash: $(sha256_literal "${INPUT_DIR}/pristine_serving_source.tar.gz")"
echo "  audit_model_weights.plaintext_hash: $(sha256_literal "${INPUT_DIR}/audit_model_weights.tar")"
echo "  audit_serving_runtime_bundle.plaintext_hash: $(sha256_literal "${INPUT_DIR}/audit_serving_runtime_bundle.tar.gz")"
echo "  audit_serving_runtime_bundle_manifest.plaintext_hash: $(sha256_literal "${INPUT_DIR}/audit_serving_runtime_bundle.manifest.json")"
for part in "${INPUT_DIR}/${RUNTIME_PART_PREFIX}"*; do
    echo "  $(basename "${part}").plaintext_hash: $(sha256_literal "${part}")"
done
echo
echo "TODO: replace this demo-level split with first-class streaming/chunked artifact encryption in Cove."
