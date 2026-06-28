FROM python:3.12-slim

ARG VLLM_VERSION=0.17.0
ARG VLLM_GIT_SHA=b31e9326a7d9394aab8c767f8ebe225c65594b60
ARG VLLM_WHEEL_FLAVOR=cu130
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir wheel

RUN mkdir -p /opt/wheels \
    && curl -fL "https://github.com/vllm-project/vllm/releases/download/v${VLLM_VERSION}/vllm-${VLLM_VERSION}%2B${VLLM_WHEEL_FLAVOR}-cp38-abi3-manylinux_2_35_x86_64.whl" -o "/opt/wheels/vllm-${VLLM_VERSION}+${VLLM_WHEEL_FLAVOR}-cp38-abi3-manylinux_2_35_x86_64.whl" \
    && git clone --depth 1 --branch "v${VLLM_VERSION}" https://github.com/vllm-project/vllm /opt/vllm-src \
    && test "$(git -C /opt/vllm-src rev-parse HEAD)" = "${VLLM_GIT_SHA}"

COPY containers/common/cove_demo_common.py /app/cove_demo_common.py
COPY containers/compile_serving_wheel/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV VLLM_SOURCE_ROOT=/opt/vllm-src
ENV VLLM_GIT_SHA=${VLLM_GIT_SHA}
ENV VLLM_VERSION=${VLLM_VERSION}
ENV VLLM_WHEEL_FLAVOR=${VLLM_WHEEL_FLAVOR}
ENV VLLM_WHEEL_PATH=/opt/wheels/vllm-${VLLM_VERSION}+${VLLM_WHEEL_FLAVOR}-cp38-abi3-manylinux_2_35_x86_64.whl

ENTRYPOINT ["python3", "/app/main.py"]
