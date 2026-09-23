#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEMO_CANONICAL_CONTAINERS_JSON="${DEMO_ROOT}/canonical_container_digests.json"

DOCKER_NAMESPACE=""
DOCKER_TAG="dev"
PUSH_IMAGES=0

usage() {
    cat <<'EOF'
Usage:
  ./scripts/build_all_containers.sh [--docker-namespace NAMESPACE] [--tag TAG] [--push]

Examples:
  ./scripts/build_all_containers.sh
  ./scripts/build_all_containers.sh --docker-namespace covehub --tag v1
  ./scripts/build_all_containers.sh --docker-namespace covehub --tag v1 --push
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
    local local_image="${image_name}:${DOCKER_TAG}"

    echo "==> Building ${local_image}"
    docker build \
        -t "${local_image}" \
        -f "${DEMO_ROOT}/${dockerfile_path}" \
        "${DEMO_ROOT}"

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

repo_digest() {
    local image_ref="$1"
    local expected_prefix="$2"
    local repo_digests
    repo_digests="$(docker image inspect \
        "${image_ref}" \
        --format '{{json .RepoDigests}}')"
    python3 - "${expected_prefix}" "${repo_digests}" <<'PY'
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

write_demo_canonical_json() {
    local word_tag="${DOCKER_NAMESPACE}/cove-demo-hello-world-word-length-checker:${DOCKER_TAG}"
    local char_tag="${DOCKER_NAMESPACE}/cove-demo-hello-world-character-set-checker:${DOCKER_TAG}"
    local final_tag="${DOCKER_NAMESPACE}/cove-demo-hello-world-final-server:${DOCKER_TAG}"

    cat > "${DEMO_CANONICAL_CONTAINERS_JSON}" <<EOF
{
  "containers": [
    {
      "image_name": "cove-demo-hello-world-word-length-checker",
      "canonical_ref": "$(repo_digest "${word_tag}" "${DOCKER_NAMESPACE}/cove-demo-hello-world-word-length-checker")"
    },
    {
      "image_name": "cove-demo-hello-world-character-set-checker",
      "canonical_ref": "$(repo_digest "${char_tag}" "${DOCKER_NAMESPACE}/cove-demo-hello-world-character-set-checker")"
    },
    {
      "image_name": "cove-demo-hello-world-final-server",
      "canonical_ref": "$(repo_digest "${final_tag}" "${DOCKER_NAMESPACE}/cove-demo-hello-world-final-server")"
    }
  ]
}
EOF
}

update_workflow_node_compose_images() {
    local word_tag="${DOCKER_NAMESPACE}/cove-demo-hello-world-word-length-checker:${DOCKER_TAG}"
    local char_tag="${DOCKER_NAMESPACE}/cove-demo-hello-world-character-set-checker:${DOCKER_TAG}"
    local final_tag="${DOCKER_NAMESPACE}/cove-demo-hello-world-final-server:${DOCKER_TAG}"
    local word_ref
    local char_ref
    local final_ref
    word_ref="$(repo_digest "${word_tag}" "${DOCKER_NAMESPACE}/cove-demo-hello-world-word-length-checker")"
    char_ref="$(repo_digest "${char_tag}" "${DOCKER_NAMESPACE}/cove-demo-hello-world-character-set-checker")"
    final_ref="$(repo_digest "${final_tag}" "${DOCKER_NAMESPACE}/cove-demo-hello-world-final-server")"

    python3 - "${DEMO_ROOT}" "${word_ref}" "${char_ref}" "${final_ref}" <<'PY'
from pathlib import Path
import re
import sys

demo_root = Path(sys.argv[1])
word_ref, char_ref, final_ref = sys.argv[2:5]
updates = {
    demo_root / "workflow" / "nodes" / "alice_character_set_checker.compose.yaml": char_ref,
    demo_root / "workflow" / "nodes" / "bob_character_set_checker.compose.yaml": char_ref,
    demo_root / "workflow" / "nodes" / "word_length_checker.compose.yaml": word_ref,
    demo_root / "workflow" / "nodes" / "final_server.compose.yaml": final_ref,
}
pattern = re.compile(r'(^\s*image:\s*")([^"]+)(")', re.MULTILINE)

for path, image_ref in updates.items():
    text = path.read_text(encoding="utf-8")
    updated_text, replacements = pattern.subn(rf'\1{image_ref}\3', text, count=1)
    if replacements != 1:
        raise SystemExit(f"expected exactly one image line in {path}")
    path.write_text(updated_text, encoding="utf-8")
PY
}

build_image "cove-demo-hello-world-word-length-checker" "containers/word_length_checker/Dockerfile"
build_image "cove-demo-hello-world-character-set-checker" "containers/character_set_checker/Dockerfile"
build_image "cove-demo-hello-world-final-server" "containers/final_server/Dockerfile"
if [[ "${PUSH_IMAGES}" == "1" ]]; then
    write_demo_canonical_json
    update_workflow_node_compose_images
fi

echo
echo "Built hello_world workload images with tag ${DOCKER_TAG}."
if [[ -n "${DOCKER_NAMESPACE}" ]]; then
    echo "Registry namespace: ${DOCKER_NAMESPACE}"
fi
if [[ "${PUSH_IMAGES}" == "1" ]]; then
    echo "Updated demos/hello_world/canonical_container_digests.json with pinned workload refs."
    echo "Updated workflow/nodes/*.compose.yaml with pinned workload refs."
    echo "This is the demo workload refresh step."
else
    echo "Pinned workload refs were not refreshed because --push was not used."
    echo "This local build is useful for validating workload container changes"
    echo "before you publish a new pinned demo workload release."
    echo "For ordinary local demo runs, use scripts/pull_canonical_containers.sh"
    echo "from demos/hello_world/ instead of rebuilding workload images."
fi
if [[ "${PUSH_IMAGES}" == "1" ]]; then
    echo
    echo "Next release checklist:"
    echo "- Commit demos/hello_world/canonical_container_digests.json."
    echo "- Commit demos/hello_world/workflow/nodes/*.compose.yaml."
    echo "- Re-run cove compile for demos/hello_world/workflow/workflow.cove.yaml."
    echo "- Re-push the published workflow bundle if this demo is consumed via cove pull."
    echo "- Re-pull and re-allow reviewed nodes because compose hashes can change."
fi
