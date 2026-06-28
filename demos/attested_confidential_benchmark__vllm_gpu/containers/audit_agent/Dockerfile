FROM vllm/vllm-openai@sha256:14ea8b431aaaf75eb873c46c8ebfbad2b4b0790d30c66126d789d8cb9bd0aab9

ARG AUDIT_MODEL_ID=Qwen/Qwen3.5-9B

WORKDIR /app

RUN python3 -m pip install --no-cache-dir --upgrade \
    accelerate \
    pillow \
    transformers \
    huggingface_hub

COPY containers/common/cove_demo_common.py /app/cove_demo_common.py
COPY containers/audit_agent/main.py /app/main.py

ENV AUDIT_MODEL_ID=${AUDIT_MODEL_ID}
ENV AUDIT_DEVICE_MAP=auto
ENV PYTHONUNBUFFERED=1
ENV REQUIRE_CUDA=1

ENTRYPOINT ["python3", "/app/main.py"]
