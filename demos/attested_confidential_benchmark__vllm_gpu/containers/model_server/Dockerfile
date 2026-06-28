FROM vllm/vllm-openai@sha256:a811fd3cd85d1bece3ffd32342e2567c754efa92a08e49bcf18ae9571ac936d9

ARG VLLM_VERSION=0.17.0
WORKDIR /app

RUN python3 -m pip install --no-cache-dir \
    "cryptography>=45,<46" \
    prometheus-fastapi-instrumentator==7.1.0

COPY containers/common/cove_demo_common.py /app/cove_demo_common.py
COPY containers/model_server/main.py /app/main.py

ENV PYTHONUNBUFFERED=1
ENV REQUIRE_CUDA=1
ENV VLLM_DEVICE=cuda
ENV VLLM_DTYPE=auto
ENV VLLM_GPU_MEMORY_UTILIZATION=0.90
ENV VLLM_TENSOR_PARALLEL_SIZE=1
ENV VLLM_VERSION=${VLLM_VERSION}

ENTRYPOINT ["python3", "/app/main.py"]
