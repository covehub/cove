FROM python:3.12-slim

ARG VLLM_VERSION=0.17.0
ARG INSPECT_AI_GIT_SHA=953f813c039d7b435a710ba7931d755424c8fc83
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl g++ git libnuma1 libtcmalloc-minimal4 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    openai \
    prometheus-fastapi-instrumentator==7.1.0 \
    "git+https://github.com/UKGovernmentBEIS/inspect_ai.git@${INSPECT_AI_GIT_SHA}" \
    "https://github.com/vllm-project/vllm/releases/download/v${VLLM_VERSION}/vllm-${VLLM_VERSION}+cpu-cp38-abi3-manylinux_2_35_x86_64.whl"

COPY containers/common/cove_demo_common.py /app/cove_demo_common.py
COPY containers/benchmark_runner/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV INSPECT_AI_GIT_SHA=${INSPECT_AI_GIT_SHA}
ENV VLLM_VERSION=${VLLM_VERSION}

ENTRYPOINT ["python", "/app/main.py"]
