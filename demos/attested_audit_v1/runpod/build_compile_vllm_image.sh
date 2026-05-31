#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_NAME="cove-demo-attested-audit-v1-vllm-compiler"
DOCKER_NAMESPACE="${DOCKER_NAMESPACE:-}"
DOCKER_TAG="${DOCKER_TAG:-runpod-compile}"
DOCKER_PLATFORM="${DOCKER_PLATFORM:-linux/amd64}"
PUSH_IMAGE=0

usage() {
    cat <<'EOF'
Usage:
  ./runpod/build_compile_vllm_image.sh --docker-namespace NAMESPACE [--tag TAG] [--platform PLATFORM] [--push]

Environment alternatives:
  DOCKER_NAMESPACE=NAMESPACE DOCKER_TAG=TAG DOCKER_PLATFORM=linux/amd64 ./runpod/build_compile_vllm_image.sh --push
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --docker-namespace)
            DOCKER_NAMESPACE="${2:?missing namespace value}"
            shift 2
            ;;
        --tag)
            DOCKER_TAG="${2:?missing tag value}"
            shift 2
            ;;
        --platform)
            DOCKER_PLATFORM="${2:?missing platform value}"
            shift 2
            ;;
        --push)
            PUSH_IMAGE=1
            shift
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

if [[ -z "${DOCKER_NAMESPACE}" ]]; then
    echo "--docker-namespace or DOCKER_NAMESPACE is required" >&2
    exit 1
fi

remote_image="${DOCKER_NAMESPACE}/${IMAGE_NAME}:${DOCKER_TAG}"

echo "==> Building ${remote_image}"
if [[ "${PUSH_IMAGE}" == "1" ]]; then
    docker buildx build \
        --platform "${DOCKER_PLATFORM}" \
        -t "${remote_image}" \
        -f "${DEMO_ROOT}/containers/vllm_compiler/Dockerfile" \
        --push \
        "${DEMO_ROOT}"
else
    docker build \
        --platform "${DOCKER_PLATFORM}" \
        -t "${remote_image}" \
        -f "${DEMO_ROOT}/containers/vllm_compiler/Dockerfile" \
        "${DEMO_ROOT}"
fi

echo
echo "RunPod compiler image:"
echo "${remote_image}"
echo "Platform: ${DOCKER_PLATFORM}"
