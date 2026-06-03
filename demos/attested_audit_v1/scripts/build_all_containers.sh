#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEMO_CANONICAL_CONTAINERS_JSON="${DEMO_ROOT}/canonical_container_digests.json"

DOCKER_NAMESPACE=""
DOCKER_TAG="dev"
PUSH_IMAGES=0
AUDIT_SERVING_CODE_ONLY=0

usage() {
    cat <<'EOF'
Usage:
  ./scripts/build_all_containers.sh [--docker-namespace NAMESPACE] [--tag TAG] [--push] [--audit-serving-code-only]

Examples:
  ./scripts/build_all_containers.sh
  ./scripts/build_all_containers.sh --docker-namespace yourname --tag v1
  ./scripts/build_all_containers.sh --docker-namespace yourname --tag v1 --push
  ./scripts/build_all_containers.sh --docker-namespace yourname --tag v1 --push --audit-serving-code-only
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
        --audit-serving-code-only)
            AUDIT_SERVING_CODE_ONLY=1
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
    local vllm_tag="${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-vllm-server:${DOCKER_TAG}"
    local runner_tag="${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-audit-agent-runner:${DOCKER_TAG}"
    local compiler_tag="${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-vllm-compiler:${DOCKER_TAG}"
    local eval_runner_tag="${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-eval-runner:${DOCKER_TAG}"
    local vllm_ref
    local runner_ref
    local compiler_ref
    local eval_runner_ref
    vllm_ref="$(repo_digest "${vllm_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-vllm-server")"
    runner_ref="$(repo_digest "${runner_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-audit-agent-runner")"

    if [[ "${AUDIT_SERVING_CODE_ONLY}" == "1" ]]; then
        python3 - "${DEMO_CANONICAL_CONTAINERS_JSON}" "${vllm_ref}" "${runner_ref}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
vllm_ref, runner_ref = sys.argv[2:4]
payload = {
    "containers": [
        {
            "image_name": "cove-demo-attested-audit-v1-vllm-server",
            "canonical_ref": vllm_ref,
        },
        {
            "image_name": "cove-demo-attested-audit-v1-audit-agent-runner",
            "canonical_ref": runner_ref,
        },
    ]
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
        return
    fi

    compiler_ref="$(repo_digest "${compiler_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-vllm-compiler")"
    eval_runner_ref="$(repo_digest "${eval_runner_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-eval-runner")"

    python3 - "${DEMO_CANONICAL_CONTAINERS_JSON}" \
      "${vllm_ref}" \
      "${runner_ref}" \
      "${compiler_ref}" \
      "${eval_runner_ref}" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
vllm_ref, runner_ref, compiler_ref, eval_runner_ref = sys.argv[2:6]
payload = {
    "containers": [
        {
            "image_name": "cove-demo-attested-audit-v1-vllm-server",
            "canonical_ref": vllm_ref,
        },
        {
            "image_name": "cove-demo-attested-audit-v1-audit-agent-runner",
            "canonical_ref": runner_ref,
        },
        {
            "image_name": "cove-demo-attested-audit-v1-vllm-compiler",
            "canonical_ref": compiler_ref,
        },
        {
            "image_name": "cove-demo-attested-audit-v1-eval-runner",
            "canonical_ref": eval_runner_ref,
        },
    ]
}
path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
}

update_workflow_node_compose_images() {
    local vllm_tag="${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-vllm-server:${DOCKER_TAG}"
    local runner_tag="${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-audit-agent-runner:${DOCKER_TAG}"
    local compiler_tag="${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-vllm-compiler:${DOCKER_TAG}"
    local eval_runner_tag="${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-eval-runner:${DOCKER_TAG}"
    local vllm_ref
    local runner_ref
    local compiler_ref
    local eval_runner_ref
    vllm_ref="$(repo_digest "${vllm_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-vllm-server")"
    runner_ref="$(repo_digest "${runner_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-audit-agent-runner")"

    if [[ "${AUDIT_SERVING_CODE_ONLY}" == "1" ]]; then
        python3 - "${DEMO_ROOT}" "${vllm_ref}" "${runner_ref}" <<'PY'
from pathlib import Path
import re
import sys

demo_root = Path(sys.argv[1])
vllm_ref, runner_ref = sys.argv[2:4]
path = demo_root / "workflow" / "nodes" / "audit_serving_code.compose.yaml"
updates = {
    "audit_model_server": vllm_ref,
    "audit_agent_runner": runner_ref,
}
text = path.read_text(encoding="utf-8")
for service_name, image_ref in updates.items():
    pattern = re.compile(
        rf"(^  {re.escape(service_name)}:\n(?:    .*\n)*?    image:\s*\")([^\"]+)(\")",
        re.MULTILINE,
    )
    text, replacements = pattern.subn(rf"\1{image_ref}\3", text, count=1)
    if replacements != 1:
        raise SystemExit(f"expected exactly one image line for {service_name} in {path}")
path.write_text(text, encoding="utf-8")
PY
        return
    fi

    compiler_ref="$(repo_digest "${compiler_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-vllm-compiler")"
    eval_runner_ref="$(repo_digest "${eval_runner_tag}" "${DOCKER_NAMESPACE}/cove-demo-attested-audit-v1-eval-runner")"

    python3 - "${DEMO_ROOT}" "${vllm_ref}" "${runner_ref}" "${compiler_ref}" "${eval_runner_ref}" <<'PY'
from pathlib import Path
import re
import sys

demo_root = Path(sys.argv[1])
vllm_ref, runner_ref, compiler_ref, eval_runner_ref = sys.argv[2:6]
updates_by_path = {
    demo_root / "workflow" / "nodes" / "audit_serving_code.compose.yaml": {
        "audit_model_server": vllm_ref,
        "audit_agent_runner": runner_ref,
    },
    demo_root / "workflow" / "nodes" / "compile_vllm.compose.yaml": {
        "vllm_compiler": compiler_ref,
    },
    demo_root / "workflow" / "nodes" / "run_eval.compose.yaml": {
        "eval_model_server": vllm_ref,
        "eval_runner": eval_runner_ref,
    },
}
for path, updates in updates_by_path.items():
    text = path.read_text(encoding="utf-8")
    for service_name, image_ref in updates.items():
        pattern = re.compile(
            rf"(^  {re.escape(service_name)}:\n(?:    .*\n)*?    image:\s*\")([^\"]+)(\")",
            re.MULTILINE,
        )
        text, replacements = pattern.subn(rf"\1{image_ref}\3", text, count=1)
        if replacements != 1:
            raise SystemExit(f"expected exactly one image line for {service_name} in {path}")
    path.write_text(text, encoding="utf-8")
PY
}

build_image "cove-demo-attested-audit-v1-vllm-server" "containers/vllm_server/Dockerfile"
build_image "cove-demo-attested-audit-v1-audit-agent-runner" "containers/audit_agent_runner/Dockerfile"
if [[ "${AUDIT_SERVING_CODE_ONLY}" != "1" ]]; then
    build_image "cove-demo-attested-audit-v1-vllm-compiler" "containers/vllm_compiler/Dockerfile"
    build_image "cove-demo-attested-audit-v1-eval-runner" "containers/eval_runner/Dockerfile"
fi

if [[ "${PUSH_IMAGES}" == "1" ]]; then
    write_demo_canonical_json
    update_workflow_node_compose_images
fi

echo
echo "Built attested_audit_v1 workload images with tag ${DOCKER_TAG}."
if [[ "${AUDIT_SERVING_CODE_ONLY}" == "1" ]]; then
    echo "Mode: audit_serving_code only."
fi
if [[ -n "${DOCKER_NAMESPACE}" ]]; then
    echo "Registry namespace: ${DOCKER_NAMESPACE}"
fi
if [[ "${PUSH_IMAGES}" == "1" ]]; then
    echo "Updated demos/attested_audit_v1/canonical_container_digests.json with pinned workload refs."
    echo "Updated workflow/nodes/*.compose.yaml with pinned workload refs."
    echo
    echo "Next release checklist:"
    echo "- Commit demos/attested_audit_v1/canonical_container_digests.json."
    echo "- Commit demos/attested_audit_v1/workflow/nodes/*.compose.yaml."
    echo "- Replace placeholder static artifact hashes for large external artifacts."
    echo "- Re-run cove check and cove compile."
else
    echo "Pinned workload refs were not refreshed because --push was not used."
fi
