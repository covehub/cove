# Vendored SA-Bench Files

The files under `vendor/sa-bench` and `vendor/lib` are copied from
`NVIDIA/srt-slurm` and retain their original Apache-2.0 license headers.

Upstream revision captured: `53fffaa6a98547c03602b1bd0157489329a2350e`
from `refs/heads/main`.

Source paths:

- `src/srtctl/benchmarks/scripts/sa-bench/*`
- `src/srtctl/benchmarks/scripts/lib/profiling.sh`

This demo calls `benchmark_serving.py` directly with `--backend vllm` so the
single-container workload targets a local vLLM OpenAI-compatible server rather
than the Dynamo frontend used by SRT-Slurm cluster recipes.
