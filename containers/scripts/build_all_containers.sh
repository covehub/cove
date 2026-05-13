#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONTAINERS_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${CONTAINERS_ROOT}/.." && pwd)"
CANONICAL_DIGESTS_FILE="${CONTAINERS_ROOT}/canonical_container_digests.json"

DOCKER_NAMESPACE=""
DOCKER_TAG="dev"
PUSH_IMAGES=0

usage() {
    cat <<'EOF'
Usage:
  ./containers/scripts/build_all_containers.sh [--docker-namespace NAMESPACE] [--tag TAG] [--push]

Examples:
  ./containers/scripts/build_all_containers.sh
  ./containers/scripts/build_all_containers.sh --docker-namespace yourname --tag v1
  ./containers/scripts/build_all_containers.sh --docker-namespace yourname --tag v1 --push
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
        --push)
            PUSH_IMAGES=1
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

if [[ "${PUSH_IMAGES}" == "1" && -z "${DOCKER_NAMESPACE}" ]]; then
    echo "--push requires --docker-namespace" >&2
    exit 1
fi

build_image() {
    local image_name="$1"
    local dockerfile_path="$2"
    shift 2
    local local_image="${image_name}:${DOCKER_TAG}"

    echo "==> Building ${local_image}"
    docker build \
        -t "${local_image}" \
        -f "${CONTAINERS_ROOT}/${dockerfile_path}" \
        "$@" \
        "${CONTAINERS_ROOT}/.."

    if [[ -n "${DOCKER_NAMESPACE}" ]]; then
        local remote_image="${DOCKER_NAMESPACE}/${image_name}:${DOCKER_TAG}"
        echo "==> Tagging ${remote_image}"
        docker tag "${local_image}" "${remote_image}"
        if [[ "${PUSH_IMAGES}" == "1" ]]; then
            echo "==> Pushing ${remote_image}"
            docker push "${remote_image}"
        fi
    fi
}

canonical_ref() {
    local image_name="$1"
    local repo_digests
    repo_digests="$(docker image inspect \
        "${DOCKER_NAMESPACE}/${image_name}:${DOCKER_TAG}" \
        --format '{{json .RepoDigests}}')"
    python3 - "${DOCKER_NAMESPACE}/${image_name}" "${repo_digests}" <<'PY'
import json
import sys

expected_prefix = sys.argv[1]
payload = sys.argv[2].strip()
digests = json.loads(payload)
if not isinstance(digests, list):
    raise SystemExit("docker image inspect returned a non-list RepoDigests payload")
for entry in digests:
    if isinstance(entry, str) and entry.startswith(expected_prefix + "@sha256:"):
        print(entry)
        raise SystemExit(0)
raise SystemExit(f"missing RepoDigest for {expected_prefix}")
PY
}

build_image "cove-base" "base/Dockerfile"

build_image \
    "cove-artifact-provisioner" \
    "artifact_provisioner/Dockerfile" \
    --build-arg "COVE_BASE_IMAGE=cove-base:${DOCKER_TAG}"
build_image \
    "cove-precondition-checker" \
    "precondition_checker/Dockerfile" \
    --build-arg "COVE_BASE_IMAGE=cove-base:${DOCKER_TAG}"
build_image \
    "cove-dependency-certificate-fetcher" \
    "dependency_certificate_fetcher/Dockerfile" \
    --build-arg "COVE_BASE_IMAGE=cove-base:${DOCKER_TAG}"
build_image \
    "cove-service-certificate-writer" \
    "service_certificate_writer/Dockerfile" \
    --build-arg "COVE_BASE_IMAGE=cove-base:${DOCKER_TAG}"
build_image \
    "cove-key-manager" \
    "key_manager/Dockerfile" \
    --build-arg "COVE_BASE_IMAGE=cove-base:${DOCKER_TAG}"
build_image \
    "cove-node-certificate-writer" \
    "node_certificate_writer/Dockerfile" \
    --build-arg "COVE_BASE_IMAGE=cove-base:${DOCKER_TAG}"

echo
echo "Built Cove base image and first-party sidecar images with tag ${DOCKER_TAG}."
if [[ "${PUSH_IMAGES}" == "1" ]]; then
    cat > "${CANONICAL_DIGESTS_FILE}" <<EOF
{
  "containers": [
    {
      "image_name": "cove-base",
      "canonical_ref": "$(canonical_ref "cove-base")"
    },
    {
      "image_name": "cove-artifact-provisioner",
      "canonical_ref": "$(canonical_ref "cove-artifact-provisioner")"
    },
    {
      "image_name": "cove-precondition-checker",
      "canonical_ref": "$(canonical_ref "cove-precondition-checker")"
    },
    {
      "image_name": "cove-dependency-certificate-fetcher",
      "canonical_ref": "$(canonical_ref "cove-dependency-certificate-fetcher")"
    },
    {
      "image_name": "cove-service-certificate-writer",
      "canonical_ref": "$(canonical_ref "cove-service-certificate-writer")"
    },
    {
      "image_name": "cove-key-manager",
      "canonical_ref": "$(canonical_ref "cove-key-manager")"
    },
    {
      "image_name": "cove-node-certificate-writer",
      "canonical_ref": "$(canonical_ref "cove-node-certificate-writer")"
    }
  ]
}
EOF
    echo "Updated containers/canonical_container_digests.json with pinned sidecar refs."
else
    echo "Pinned canonical sidecar refs were not refreshed because --push was not used."
    echo "For ordinary local demo runs, use scripts/pull_canonical_containers.sh"
    echo "from the repo root instead of rebuilding first-party sidecars."
fi
if [[ -n "${DOCKER_NAMESPACE}" ]]; then
    echo "Registry namespace: ${DOCKER_NAMESPACE}"
fi
if [[ "${PUSH_IMAGES}" == "1" ]]; then
    echo
    echo "Next release checklist:"
    echo "- Commit containers/canonical_container_digests.json."
    echo "- Copy containers/canonical_container_digests.json into cli/canonical_container_digests.json and cli/src/cove_cli/canonical_container_digests.json."
    echo "- Rebuild and ship a new CLI package because the CLI enforces the canonical sidecar digest policy."
    echo "- Update downstream consumers only if this sidecar release also changed their demo or runtime workflow behavior."
fi
