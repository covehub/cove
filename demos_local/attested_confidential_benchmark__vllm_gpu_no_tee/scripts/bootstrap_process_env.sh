#!/usr/bin/env bash
set -euo pipefail

VLLM_VERSION="${VLLM_VERSION:-0.17.0}"
VLLM_GIT_SHA="${VLLM_GIT_SHA:-b31e9326a7d9394aab8c767f8ebe225c65594b60}"
VLLM_WHEEL_FLAVOR="${VLLM_WHEEL_FLAVOR:-cu130}"
VLLM_SOURCE_ROOT="${VLLM_SOURCE_ROOT:-/opt/vllm-src}"
VLLM_WHEEL_DIR="${VLLM_WHEEL_DIR:-/opt/wheels}"
VLLM_WHEEL_PATH="${VLLM_WHEEL_PATH:-${VLLM_WHEEL_DIR}/vllm-${VLLM_VERSION}+${VLLM_WHEEL_FLAVOR}-cp38-abi3-manylinux_2_35_x86_64.whl}"

python3 -m pip install --no-cache-dir --force-reinstall \
    torch==2.10.0 \
    torchvision==0.25.0 \
    torchaudio==2.10.0

python3 -m pip install --no-cache-dir --upgrade \
    "cryptography>=45,<46" \
    wheel \
    accelerate \
    pillow \
    transformers \
    huggingface_hub \
    "openai>=2.40.0" \
    prometheus-fastapi-instrumentator==7.1.0 \
    "git+https://github.com/UKGovernmentBEIS/inspect_ai.git@953f813c039d7b435a710ba7931d755424c8fc83"

if [[ ! -d "${VLLM_SOURCE_ROOT}/.git" ]]; then
    rm -rf "${VLLM_SOURCE_ROOT}"
    git clone --depth 1 --branch "v${VLLM_VERSION}" https://github.com/vllm-project/vllm "${VLLM_SOURCE_ROOT}"
fi

observed_sha="$(git -C "${VLLM_SOURCE_ROOT}" rev-parse HEAD)"
if [[ "${observed_sha}" != "${VLLM_GIT_SHA}" ]]; then
    echo "unexpected vLLM source checkout: ${observed_sha} != ${VLLM_GIT_SHA}" >&2
    exit 1
fi

if [[ ! -f "${VLLM_WHEEL_PATH}" ]]; then
    mkdir -p "${VLLM_WHEEL_DIR}"
    curl -fL \
    "https://github.com/vllm-project/vllm/releases/download/v${VLLM_VERSION}/vllm-${VLLM_VERSION}%2B${VLLM_WHEEL_FLAVOR}-cp38-abi3-manylinux_2_35_x86_64.whl" \
        -o "${VLLM_WHEEL_PATH}"
fi

python3 -m pip install --no-cache-dir --force-reinstall --no-deps "${VLLM_WHEEL_PATH}"

python3 - <<'PY'
import importlib.util
import sys

missing = [
    name
    for name in ("cryptography", "huggingface_hub", "inspect_ai", "openai", "transformers", "wheel")
    if importlib.util.find_spec(name) is None
]
if missing:
    raise SystemExit(f"missing required Python packages after bootstrap: {missing}")
import torch
import vllm
import vllm._C  # noqa: F401

print(
    "process environment ready for "
    f"Python {sys.version.split()[0]}, torch {torch.__version__}, vllm {vllm.__version__}"
)
PY
