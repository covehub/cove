FROM python:3.12-slim

ARG VLLM_VERSION=0.17.0
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl g++ libnuma1 libtcmalloc-minimal4 \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    openai \
    "https://github.com/vllm-project/vllm/releases/download/v${VLLM_VERSION}/vllm-${VLLM_VERSION}+cpu-cp38-abi3-manylinux_2_35_x86_64.whl"

COPY containers/common/attested_confidential_eval_common.py /app/attested_confidential_eval_common.py
COPY containers/benchmark_runner/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV VLLM_VERSION=${VLLM_VERSION}

ENTRYPOINT ["python", "/app/main.py"]
