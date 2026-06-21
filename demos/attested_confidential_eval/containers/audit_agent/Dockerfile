FROM python:3.12-slim

ARG AUDIT_MODEL_ID=HuggingFaceTB/SmolLM2-135M-Instruct

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    torch \
    transformers \
    huggingface_hub

COPY containers/common/attested_confidential_eval_common.py /app/attested_confidential_eval_common.py
COPY containers/audit_agent/main.py /app/main.py

ENV AUDIT_MODEL_ID=${AUDIT_MODEL_ID}
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/app/main.py"]
