#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

IMAGE_REF="${IMAGE_REF:-}"
PUSH_IMAGE=0

usage() {
    cat <<'EOF'
Usage:
  ./scripts/build_image.sh --image IMAGE_REF [--push]

Examples:
  ./scripts/build_image.sh --image hpmv/phala-h200-vllm-sa-bench:smoke --push
  IMAGE_REF=ghcr.io/owner/phala-h200-vllm-sa-bench:smoke ./scripts/build_image.sh --push
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --image)
            IMAGE_REF="${2:?missing image ref}"
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

if [[ -z "${IMAGE_REF}" ]]; then
    echo "IMAGE_REF is required" >&2
    usage >&2
    exit 1
fi

docker build -t "${IMAGE_REF}" -f "${DEMO_ROOT}/Dockerfile" "${DEMO_ROOT}"

if [[ "${PUSH_IMAGE}" == "1" ]]; then
    docker push "${IMAGE_REF}"
fi

echo "Built ${IMAGE_REF}"
