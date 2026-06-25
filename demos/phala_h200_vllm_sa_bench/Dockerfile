FROM vllm/vllm-openai:v0.23.0

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl tini \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir \
    aiohttp \
    datasets \
    huggingface_hub \
    numpy \
    pandas \
    Pillow \
    tqdm \
    transformers

WORKDIR /opt/phala-h200-bench

COPY vendor/sa-bench /opt/sa-bench
COPY vendor/lib /opt/lib
COPY scripts/run_benchmark.py /opt/phala-h200-bench/run_benchmark.py

RUN chmod +x /opt/sa-bench/bench.sh /opt/phala-h200-bench/run_benchmark.py

ENV HF_HOME=/root/.cache/huggingface
ENV HUGGINGFACE_HUB_CACHE=/root/.cache/huggingface/hub
ENV TRANSFORMERS_CACHE=/root/.cache/huggingface/transformers
ENV VLLM_CACHE_ROOT=/root/.cache/vllm
ENV VLLM_ENABLE_CUDA_COMPATIBILITY=1
ENV PYTHONUNBUFFERED=1
ENV RESULT_DIR=/logs

EXPOSE 8080

ENTRYPOINT ["tini", "--", "python3", "/opt/phala-h200-bench/run_benchmark.py"]
