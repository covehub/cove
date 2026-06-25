#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

TARGET_CVM_NAME="gpu-tee-45vfi"
TARGET_INSTANCE_TYPE="h200.small"
ENV_FILE="${ENV_FILE:-${DEMO_ROOT}/phala.env}"
COMPOSE_TEMPLATE="${DEMO_ROOT}/docker-compose.phala.yaml"
BUILD_DIR="${DEMO_ROOT}/build"
GENERATED_COMPOSE="${BUILD_DIR}/docker-compose.generated.phala.yaml"
DISK_SIZE="${PHALA_DISK_SIZE:-200G}"

usage() {
    cat <<EOF
Usage:
  ./scripts/deploy_existing_cvm.sh [--env-file PATH]

This script updates only ${TARGET_CVM_NAME}. It never deletes CVMs.
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --env-file)
            ENV_FILE="${2:?missing env file path}"
            shift 2
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if [[ ! -f "${ENV_FILE}" ]]; then
    echo "missing env file: ${ENV_FILE}" >&2
    echo "copy ${DEMO_ROOT}/phala.env.example to ${ENV_FILE} and set IMAGE_REF" >&2
    exit 1
fi

set -a
# shellcheck disable=SC1090
. "${ENV_FILE}"
set +a

if [[ -z "${IMAGE_REF:-}" ]]; then
    echo "IMAGE_REF must be set in ${ENV_FILE}" >&2
    exit 1
fi

case "${IMAGE_REF}" in
    *:latest)
        echo "refusing IMAGE_REF with mutable :latest tag: ${IMAGE_REF}" >&2
        exit 1
        ;;
    *"/"*":"*|*"/"*"@sha256:"*)
        ;;
    *)
        echo "IMAGE_REF must be a pullable registry image with a namespace and tag or digest: ${IMAGE_REF}" >&2
        exit 1
        ;;
esac

echo "Preflight: reading ${TARGET_CVM_NAME}"
INFO_JSON="$(npx --yes phala cvms get "${TARGET_CVM_NAME}" --json)"
export INFO_JSON

read_json_field() {
    local expression="$1"
    python3 - "$expression" <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["INFO_JSON"])
current = payload
for part in sys.argv[1].split("."):
    if not part:
        continue
    if not isinstance(current, dict):
        current = None
        break
    current = current.get(part)
print("" if current is None else current)
PY
}

SUCCESS="$(read_json_field success)"
ACTUAL_NAME="$(read_json_field name)"
ACTUAL_ID="$(read_json_field id)"
ACTUAL_APP_ID="$(read_json_field app_id)"
ACTUAL_INSTANCE_TYPE="$(read_json_field resource.instance_type)"
ACTUAL_GPUS="$(read_json_field resource.gpus)"

if [[ "${SUCCESS}" != "True" && "${SUCCESS}" != "true" ]]; then
    echo "phala cvms get did not return success=true" >&2
    exit 1
fi

echo "Target CVM: name=${ACTUAL_NAME} id=${ACTUAL_ID} app_id=${ACTUAL_APP_ID} instance_type=${ACTUAL_INSTANCE_TYPE} gpus=${ACTUAL_GPUS}"

if [[ "${ACTUAL_NAME}" != "${TARGET_CVM_NAME}" ]]; then
    echo "refusing to deploy: expected CVM name ${TARGET_CVM_NAME}, got ${ACTUAL_NAME}" >&2
    exit 1
fi

if [[ "${ACTUAL_INSTANCE_TYPE}" != "${TARGET_INSTANCE_TYPE}" ]]; then
    echo "refusing to deploy: expected ${TARGET_INSTANCE_TYPE}, got ${ACTUAL_INSTANCE_TYPE}" >&2
    exit 1
fi

if [[ "${ACTUAL_GPUS}" != "1" ]]; then
    echo "refusing to deploy: expected exactly one GPU, got ${ACTUAL_GPUS}" >&2
    exit 1
fi

mkdir -p "${BUILD_DIR}"
python3 - "${COMPOSE_TEMPLATE}" "${GENERATED_COMPOSE}" "${IMAGE_REF}" <<'PY'
from pathlib import Path
import sys

template_path = Path(sys.argv[1])
output_path = Path(sys.argv[2])
image_ref = sys.argv[3]
text = template_path.read_text(encoding="utf-8")
if "${IMAGE_REF}" not in text:
    raise SystemExit("compose template is missing ${IMAGE_REF}")
text = text.replace("${IMAGE_REF}", image_ref)
output_path.write_text(text, encoding="utf-8")
PY

echo "Generated compose: ${GENERATED_COMPOSE}"
echo "Updating existing CVM in place. No delete operation will be performed."

npx --yes phala deploy \
    --cvm-id "${TARGET_CVM_NAME}" \
    --compose "${GENERATED_COMPOSE}" \
    -e "${ENV_FILE}" \
    -e "PHALA_CVM_NAME=${ACTUAL_NAME}" \
    -e "PHALA_CVM_ID=${ACTUAL_ID}" \
    -e "PHALA_APP_ID=${ACTUAL_APP_ID}" \
    -e "PHALA_INSTANCE_TYPE=${ACTUAL_INSTANCE_TYPE}" \
    -e "BENCH_IMAGE_REF=${IMAGE_REF}" \
    --instance-type "${TARGET_INSTANCE_TYPE}" \
    --disk-size "${DISK_SIZE}" \
    --wait
