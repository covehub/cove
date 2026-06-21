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
    repo_digests="$(docker image inspect "${image_ref}" --format '{{json .RepoDigests}}')"
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
    local audit_tag="${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-audit-agent:${DOCKER_TAG}"
    local compile_tag="${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-compile-serving-wheel:${DOCKER_TAG}"
    local benchmark_tag="${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-benchmark-runner:${DOCKER_TAG}"
    local server_tag="${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-model-server:${DOCKER_TAG}"

    cat > "${DEMO_CANONICAL_CONTAINERS_JSON}" <<EOF
{
  "containers": [
    {
      "image_name": "cove-demo-attested-confidential-eval-vllm-cpu-audit-agent",
      "canonical_ref": "$(repo_digest "${audit_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-audit-agent")"
    },
    {
      "image_name": "cove-demo-attested-confidential-eval-vllm-cpu-compile-serving-wheel",
      "canonical_ref": "$(repo_digest "${compile_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-compile-serving-wheel")"
    },
    {
      "image_name": "cove-demo-attested-confidential-eval-vllm-cpu-benchmark-runner",
      "canonical_ref": "$(repo_digest "${benchmark_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-benchmark-runner")"
    },
    {
      "image_name": "cove-demo-attested-confidential-eval-vllm-cpu-model-server",
      "canonical_ref": "$(repo_digest "${server_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-model-server")"
    }
  ]
}
EOF
}

update_workflow_node_compose_images() {
    local audit_tag="${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-audit-agent:${DOCKER_TAG}"
    local compile_tag="${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-compile-serving-wheel:${DOCKER_TAG}"
    local benchmark_tag="${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-benchmark-runner:${DOCKER_TAG}"
    local server_tag="${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-model-server:${DOCKER_TAG}"
    local audit_ref
    local compile_ref
    local benchmark_ref
    local server_ref
    audit_ref="$(repo_digest "${audit_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-audit-agent")"
    compile_ref="$(repo_digest "${compile_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-compile-serving-wheel")"
    benchmark_ref="$(repo_digest "${benchmark_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-benchmark-runner")"
    server_ref="$(repo_digest "${server_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-confidential-eval-vllm-cpu-model-server")"

    python3 - "${DEMO_ROOT}" "${audit_ref}" "${compile_ref}" "${benchmark_ref}" "${server_ref}" <<'PY'
from pathlib import Path
import re
import sys

demo_root = Path(sys.argv[1])
audit_ref, compile_ref, benchmark_ref, server_ref = sys.argv[2:6]
updates = {
    demo_root / "workflow" / "nodes" / "audit_serving_code.compose.yaml": audit_ref,
    demo_root / "workflow" / "nodes" / "audit_eval_code.compose.yaml": audit_ref,
    demo_root / "workflow" / "nodes" / "compile_serving_code.compose.yaml": compile_ref,
    demo_root / "workflow" / "nodes" / "model_benchmark.compose.yaml": benchmark_ref,
    demo_root / "workflow" / "nodes" / "model_deployment.compose.yaml": server_ref,
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

build_image "cove-demo-attested-confidential-eval-vllm-cpu-audit-agent" "containers/audit_agent/Dockerfile"
build_image "cove-demo-attested-confidential-eval-vllm-cpu-compile-serving-wheel" "containers/compile_serving_wheel/Dockerfile"
build_image "cove-demo-attested-confidential-eval-vllm-cpu-benchmark-runner" "containers/benchmark_runner/Dockerfile"
build_image "cove-demo-attested-confidential-eval-vllm-cpu-model-server" "containers/model_server/Dockerfile"

if [[ "${PUSH_IMAGES}" == "1" ]]; then
    write_demo_canonical_json
    update_workflow_node_compose_images
fi

echo
echo "Built attested_confidential_eval_vllm_cpu workload images with tag ${DOCKER_TAG}."
