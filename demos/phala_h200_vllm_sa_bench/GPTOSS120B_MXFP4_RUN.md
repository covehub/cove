# GPT-OSS 120B MXFP4 H200 Run

Run date: 2026-06-25 UTC

## Image

```text
hpmv/phala-h200-vllm-sa-bench@sha256:3b89ca13c2d2b789ecfa6d6a5570267ce40ce0370e560b4bb07c577408fd3ac5
```

Built from `vllm/vllm-openai:v0.23.0`.

## Target

```text
CVM name: gpu-tee-45vfi
CVM id: cvm_EKL2z2jG
Instance type: h200.small
GPU: 1x NVIDIA H200, 143771 MiB
Driver: 580.95.05
CUDA: 13.0
```

The CVM was updated in place with `npx phala deploy --cvm-id gpu-tee-45vfi`.
No delete, recreate, SSH, or SCP operation was used.

## Model

```text
MODEL_ID=openai/gpt-oss-120b
dtype=auto
vLLM quantization detected at runtime: gpt_oss_mxfp4
```

This is the official MXFP4 GPT-OSS 120B path, not FP8 weights. FP8 KV cache
would be a separate serving override.

## Serve Command

The container rendered this vLLM command:

```bash
vllm serve openai/gpt-oss-120b \
  --served-model-name bench-model \
  --host 127.0.0.1 \
  --port 8000 \
  --gpu-memory-utilization 0.95 \
  --max-model-len 4096 \
  --max-num-batched-tokens 1024
```

vLLM ran with `enforce_eager=False`, V1, Inductor compile enabled, async
scheduling enabled, FlashAttention 3, and CUDA graph mode
`FULL_AND_PIECEWISE`.

## Docker Run Equivalent

This repeats the 1k/1k run outside Phala on a single H200-like host:

```bash
docker run --rm \
  --gpus all \
  --shm-size 64g \
  --ipc host \
  -p 8080:8080 \
  -v gptoss-hf-cache:/root/.cache/huggingface \
  -v gptoss-vllm-cache:/root/.cache/vllm \
  -v gptoss-results:/logs \
  -e BENCH_PROFILE=gptoss120b-mxfp4-1k1k \
  -e VLLM_DISABLE_COMPILE_CACHE=0 \
  -e VLLM_COMPILE_CACHE_SAVE_FORMAT=binary \
  -e VLLM_ENABLE_CUDA_COMPATIBILITY=1 \
  -e RESULT_DIR=/logs/gptoss120b-mxfp4-1k1k \
  hpmv/phala-h200-vllm-sa-bench@sha256:3b89ca13c2d2b789ecfa6d6a5570267ce40ce0370e560b4bb07c577408fd3ac5
```

## Profiles Run

Smoke profile:

```text
BENCH_PROFILE=gptoss120b-mxfp4-smoke
ISL=128
OSL=128
CONCURRENCIES=1x2
RANDOM_RANGE_RATIO=0.0
NUM_PROMPTS_MULT=2
NUM_WARMUP_MULT=1
```

Main profile:

```text
BENCH_PROFILE=gptoss120b-mxfp4-1k1k
ISL=1024
OSL=1024
CONCURRENCIES=1x4x8x16
RANDOM_RANGE_RATIO=0.8
NUM_PROMPTS_MULT=10
NUM_WARMUP_MULT=2
REQ_RATE=inf
```

Both profiles use `--max-num-batched-tokens 1024`.

## Startup Observations

First smoke load downloaded weights in 170.49s. Checkpoint size was 60.77 GiB.
Model loading used 64.67 GiB GPU memory and took 503.23s. CUDA graph capture
finished in 87s and used 1.23 GiB actual graph pool memory.

The later 1k/1k run reused the downloaded weights and compile cache. Cached
weight loading took 596.95s on ZFS, compile cache load took 1.213s, and CUDA
graph capture finished in 82s.

## 1k/1k Results

Result directory:

```text
/logs/gptoss120b-mxfp4-1k1k-20260625-003453Z
```

Public endpoint:

```text
https://cfa281af15905e4624d3b7fdc413334595d4c5ff-8080.dstack-pha-use1.phala.network/results/
```

| Concurrency | Completed | Output tok/s | Req/s | Mean TTFT ms | Mean TPOT ms | Mean E2E ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 10 | 172.87 | 0.19 | 62.34 | 5.72 | 5329.76 |
| 4 | 40 | 492.26 | 0.54 | 80.97 | 7.82 | 7253.92 |
| 8 | 80 | 780.41 | 0.84 | 96.47 | 9.97 | 9351.15 |
| 16 | 160 | 1202.44 | 1.31 | 122.83 | 12.92 | 11950.93 |

All saved requests completed successfully.

## 1k/1k Native CUDA Rerun

Rerun date: 2026-06-25 UTC

This repeated the same Phala 1k/1k benchmark on the same CVM, image, model,
and workload, changing only:

```text
VLLM_ENABLE_CUDA_COMPATIBILITY=0
RESULT_DIR=/logs/gptoss120b-mxfp4-1k1k-compat0-20260625-064528Z
```

Startup was healthy. Weight loading took 583.41s, model loading used 64.67 GiB
GPU memory and took 587.44s, and CUDA graph capture finished in 83s using
1.22 GiB.

| Concurrency | Completed | Output tok/s | Req/s | Mean TTFT ms | Mean TPOT ms | Mean E2E ms |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 10 | 173.12 | 0.19 | 62.56 | 5.71 | 5321.97 |
| 4 | 40 | 494.79 | 0.54 | 80.70 | 7.79 | 7217.21 |
| 8 | 80 | 781.13 | 0.84 | 99.05 | 9.96 | 9342.59 |
| 16 | 160 | 1197.25 | 1.31 | 126.78 | 12.97 | 12001.95 |

Compared with the original `VLLM_ENABLE_CUDA_COMPATIBILITY=1` run, output
throughput changed by +0.14%, +0.51%, +0.09%, and -0.43% at concurrencies
1, 4, 8, and 16 respectively.

## Smoke Results

Result directory:

```text
/logs/gptoss120b-mxfp4-smoke-20260625-001843Z
```

| Concurrency | Completed | Output tok/s | Req/s | Mean TTFT ms | Mean TPOT ms |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 2 | 169.83 | 2.00 | 27.44 | 5.63 |
| 2 | 4 | 214.63 | 5.61 | 39.57 | 6.32 |

All saved smoke requests completed successfully.
