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

## RunPod Testing

RunPod does not support Docker-in-Docker in the environment so we can only test the
same workload logic by running vLLM directly in the pod and copying
`perf_runner.py` into the pod's network volume.

In one RunPod shell, start vLLM:

```bash
mkdir -p /workspace/hf-cache /workspace/tmp /workspace/vllm-cache

export HF_HOME=/workspace/hf-cache
export HUGGINGFACE_HUB_CACHE=/workspace/hf-cache/hub
export TRANSFORMERS_CACHE=/workspace/hf-cache/transformers
export XDG_CACHE_HOME=/workspace/.cache
export TMPDIR=/workspace/tmp
export VLLM_CACHE_ROOT=/workspace/vllm-cache

vllm serve Qwen/Qwen2.5-7B-Instruct \
  --served-model-name perf-model \
  --host 0.0.0.0 \
  --port 8000 \
  --gpu-memory-utilization 0.90 \
  --max-model-len 4096 \
  --download-dir /workspace/hf-cache
```

Copy this folder into the pod using `runpodctl`:

```bash
cd /path/to/cove/demos
COPYFILE_DISABLE=1 tar --no-xattrs -czf /tmp/perf_eval.tar.gz perf_eval
runpodctl send /tmp/perf_eval.tar.gz
```

In the pod:

```bash
cd /workspace
runpodctl receive <transfer-code>
mkdir -p /workspace/cove/demos
tar --no-same-owner --no-xattrs -xzf perf_eval.tar.gz -C /workspace/cove/demos
cd /workspace/cove/demos/perf_eval
```

Then run the benchmark against the local vLLM server:

```bash
export OPENAI_BASE_URL=http://127.0.0.1:8000/v1
export OPENAI_API_KEY=vllm
export MODEL_NAME=perf-model
export REQUESTS=128
export CONCURRENCY=16
export MAX_TOKENS=256
export OUTPUT_DIR=./results

python3 perf_runner.py
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
