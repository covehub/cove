FROM ghcr.io/astral-sh/uv:python3.12-bookworm

WORKDIR /app

RUN apt-get update -y \
    && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      jq \
    && rm -rf /var/lib/apt/lists/*

RUN uv pip install --system "openai>=1.0.0"

COPY containers/eval_runner/run_eval.py /app/bin/run_eval.py
RUN chmod +x /app/bin/run_eval.py

CMD ["python", "/app/bin/run_eval.py"]
