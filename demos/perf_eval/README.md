# H200 Performance Smoke Eval

This is a standalone Docker Compose performance perf eval that we want to run on H200s to test
raw vLLM serving performance.

## Run

From this directory:

```bash
docker compose -f docker-compose.h200.yaml up --build --abort-on-container-exit --exit-code-from perf_runner
```

Results are written to:

```text
results/perf_summary.json
results/perf_requests.jsonl
```

## Common Settings

```bash
export MODEL_ID=Qwen/Qwen2.5-7B-Instruct
export SERVED_MODEL_NAME=perf-model
export REQUESTS=128
export CONCURRENCY=16
export MAX_TOKENS=256
export GPU_MEMORY_UTILIZATION=0.90
export MAX_MODEL_LEN=4096
```

For gated Hugging Face models:

```bash
export HUGGING_FACE_HUB_TOKEN=...
```

To reuse a local Hugging Face cache:

```bash
export HF_CACHE_DIR=/path/to/hf-cache
```

## Notes

- The runner uses non-streaming `/v1/chat/completions`, so it reports request
  latency and aggregate token throughput, not time-to-first-token.
- The default prompt is intentionally simple. Override it with:

```bash
export PROMPT_TEMPLATE='Summarize this request in three bullet points. Request id: {i}'
```

- Stop the stack with:

```bash
docker compose -f docker-compose.h200.yaml down
```
