FROM python:3.12-slim

ARG AUDIT_MODEL_ID=Qwen/Qwen3.5-9B

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    accelerate \
    pillow \
    torch \
    transformers \
    huggingface_hub

COPY containers/common/cove_demo_common.py /app/cove_demo_common.py
COPY containers/audit_agent/main.py /app/main.py

ENV AUDIT_MODEL_ID=${AUDIT_MODEL_ID}
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/main.py"]
