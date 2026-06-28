FROM vllm/vllm-openai@sha256:a811fd3cd85d1bece3ffd32342e2567c754efa92a08e49bcf18ae9571ac936d9

ARG VLLM_VERSION=0.17.0
ARG INSPECT_AI_GIT_SHA=953f813c039d7b435a710ba7931d755424c8fc83
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir --upgrade \
    "openai>=2.40.0" \
    "git+https://github.com/UKGovernmentBEIS/inspect_ai.git@${INSPECT_AI_GIT_SHA}"

COPY containers/common/cove_demo_common.py /app/cove_demo_common.py
COPY containers/benchmark_runner/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV INSPECT_AI_GIT_SHA=${INSPECT_AI_GIT_SHA}
ENV REQUIRE_CUDA=1
ENV VLLM_DEVICE=cuda
ENV VLLM_DTYPE=auto
ENV VLLM_GPU_MEMORY_UTILIZATION=0.90
ENV VLLM_TENSOR_PARALLEL_SIZE=1
ENV VLLM_VERSION=${VLLM_VERSION}

ENTRYPOINT ["python3", "/app/main.py"]
