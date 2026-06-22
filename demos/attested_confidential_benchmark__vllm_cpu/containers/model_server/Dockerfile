FROM python:3.12-slim

ARG VLLM_VERSION=0.17.0
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl g++ libnuma1 libtcmalloc-minimal4 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    prometheus-fastapi-instrumentator==7.1.0 \
    "https://github.com/vllm-project/vllm/releases/download/v${VLLM_VERSION}/vllm-${VLLM_VERSION}+cpu-cp38-abi3-manylinux_2_35_x86_64.whl"

COPY containers/common/cove_demo_common.py /app/cove_demo_common.py
COPY containers/model_server/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV VLLM_VERSION=${VLLM_VERSION}

ENTRYPOINT ["python", "/app/main.py"]
