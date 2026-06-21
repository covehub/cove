FROM python:3.12-slim

ARG VLLM_VERSION=0.17.0
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir wheel

RUN mkdir -p /opt/wheels \
    && curl -L "https://github.com/vllm-project/vllm/releases/download/v${VLLM_VERSION}/vllm-${VLLM_VERSION}+cpu-cp38-abi3-manylinux_2_35_x86_64.whl" -o "/opt/wheels/vllm-${VLLM_VERSION}+cpu-cp38-abi3-manylinux_2_35_x86_64.whl" \
    && git clone --depth 1 --branch "v${VLLM_VERSION}" https://github.com/vllm-project/vllm /opt/vllm-src

COPY containers/common/attested_confidential_eval_common.py /app/attested_confidential_eval_common.py
COPY containers/compile_serving_wheel/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV VLLM_SOURCE_ROOT=/opt/vllm-src
ENV VLLM_VERSION=${VLLM_VERSION}
ENV VLLM_WHEEL_PATH=/opt/wheels/vllm-${VLLM_VERSION}+cpu-cp38-abi3-manylinux_2_35_x86_64.whl

ENTRYPOINT ["python", "/app/main.py"]
