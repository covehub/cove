# Phala H200 vLLM SA-Bench

Single-container vLLM benchmark for the existing Phala H200 TEE CVM.

This demo intentionally does not use `demos/perf_eval` and does not reuse its
runner. The container starts a local vLLM server, runs the NVIDIA SRT-Slurm
SA-Bench client against `/v1/completions`, writes raw and rollup results, then
keeps serving `/results` over HTTP.

## Hard Safety Rule

Do not delete the H200 CVM.

The deploy helper only updates the existing `gpu-tee-45vfi` CVM in place. It
preflights `phala cvms get gpu-tee-45vfi --json` and refuses to deploy unless
the target is exactly `gpu-tee-45vfi`, `h200.small`, and one GPU.

The helper never calls `phala cvms delete`.

## Files

- `Dockerfile` builds the single self-evaluating benchmark image.
- `docker-compose.phala.yaml` has one service, `h200-bench`.
- `phala.env.example` is the template for deploy-time env.
- `scripts/run_benchmark.py` starts vLLM, runs SA-Bench, rolls up results, and
  serves `/results`.
- `scripts/deploy_existing_cvm.sh` safely updates the existing CVM in place.
- `vendor/sa-bench` and `vendor/lib` are vendored from NVIDIA SRT-Slurm with
  their original license headers.

## Build And Push

Choose a registry image that Phala can pull. Do not use `:latest`.

```bash
cd demos/phala_h200_vllm_sa_bench

./scripts/build_image.sh \
  --image <registry>/<namespace>/phala-h200-vllm-sa-bench:<tag> \
  --push
```

## Configure

```bash
cd demos/phala_h200_vllm_sa_bench
cp phala.env.example phala.env
```

Set at least:

```text
IMAGE_REF=<registry>/<namespace>/phala-h200-vllm-sa-bench:<tag>
BENCH_PROFILE=smoke
```

The Qwen and GPT-OSS profiles are public, so `HF_TOKEN` is optional unless you
override the model to a gated repository.

## Local Dry Run

This validates profile parsing and command rendering without starting vLLM:

```bash
DRY_RUN=1 BENCH_PROFILE=smoke python3 scripts/run_benchmark.py --dry-run
```

## Update The Existing CVM

This reboots/updates `gpu-tee-45vfi` in place with the one-container compose:

```bash
cd demos/phala_h200_vllm_sa_bench
./scripts/deploy_existing_cvm.sh
```

Equivalent Phala shape after preflight and compose rendering:

```bash
npx --yes phala deploy \
  --cvm-id gpu-tee-45vfi \
  --compose demos/phala_h200_vllm_sa_bench/build/docker-compose.generated.phala.yaml \
  -e demos/phala_h200_vllm_sa_bench/phala.env \
  --instance-type h200.small \
  --disk-size 200G \
  --wait
```

## Profiles

- `smoke`: `Qwen/Qwen3-0.6B`, 128 input / 128 output, concurrencies `1x4x8`.
- `qwen32b-bf16-1k1k`: `Qwen/Qwen3-32B`, BF16, 1024 input / 1024 output,
  concurrencies `1x4x16x32x64`.
- `qwen32b-bf16-8k1k`: `Qwen/Qwen3-32B`, BF16, 8192 input / 1024 output,
  concurrencies `1x2x4x8x16`.
- `qwen32b-fp8-1k1k`: `Qwen/Qwen3-32B-FP8`, 1024 input / 1024 output,
  concurrencies `1x4x16x32x64`.
- `gptoss120b-mxfp4-smoke`: `openai/gpt-oss-120b`, MXFP4 weights via vLLM
  `auto` dtype, 128 input / 128 output, concurrencies `1x2`,
  `--max-num-batched-tokens 1024`.
- `gptoss120b-mxfp4-1k1k`: `openai/gpt-oss-120b`, MXFP4 weights via vLLM
  `auto` dtype, 1024 input / 1024 output, concurrencies `1x4x8x16`,
  `--max-num-batched-tokens 1024`.

Run smoke first. For GPT-OSS, establish the MXFP4 baseline before trying FP8
KV-cache overrides such as `VLLM_EXTRA_ARGS="--max-num-batched-tokens 1024
--kv-cache-dtype fp8"`.

## Results

The container writes:

- `/logs/sa-bench_*/results_concurrency_*.json`
- `/logs/benchmark-rollup.json`
- `/logs/benchmark-rollup.csv`
- `/logs/environment.json`
- `/logs/status.json` or `/logs/failure.json`

Fetch recent logs and endpoint URLs:

```bash
./scripts/show_results.sh
```

The Phala endpoint exposes:

```text
https://<app-or-instance>-8080.<phala-domain>/results/benchmark-rollup.json
https://<app-or-instance>-8080.<phala-domain>/results/benchmark-rollup.csv
```

## Useful Overrides

All profile defaults can be overridden in `phala.env`:

```text
MODEL_ID=
SERVED_MODEL_NAME=
DTYPE=
MAX_MODEL_LEN=
GPU_MEMORY_UTILIZATION=
CONCURRENCIES=
ISL=
OSL=
NUM_PROMPTS_MULT=
NUM_WARMUP_MULT=
REQ_RATE=
RANDOM_RANGE_RATIO=
RANDOM_NUM_WORKERS=
VLLM_EXTRA_ARGS=
CUDA_LAUNCH_BLOCKING=
VLLM_DISABLE_COMPILE_CACHE=
VLLM_COMPILE_CACHE_SAVE_FORMAT=
SA_BENCH_EXTRA_ARGS=
```

`VLLM_EXTRA_ARGS` is appended to `vllm serve`. `SA_BENCH_EXTRA_ARGS` is
appended to every `benchmark_serving.py` invocation.
