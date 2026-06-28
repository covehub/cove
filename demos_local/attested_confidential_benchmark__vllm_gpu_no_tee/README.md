# Local No-TEE GPU Benchmark

This demo runs the GPU attested confidential benchmark workload without Phala
TEE provisioning. It is intended for one RunPod GPU pod with a shared network
volume, running either one Docker container at a time or, on RunPod templates
without Docker-in-Docker, one local process at a time.

The local run is not attested. Each node writes `record.json` with
`attestation.type: none` and `tee: false`.

## Measurement Boundary

Primary comparisons use each workload result's
`benchmark_profile.workload_seconds`. Docker pulls, SSH latency, Phala update
time, local artifact encryption/decryption, and wrapper overhead are reported
separately.

The report includes one row per workflow node:

- `audit_serving_code`
- `compile_serving_code`
- `audit_eval_code`
- `model_benchmark`
- `model_deployment`

The runner executes them serially, but the workflow shape remains the true
five-node DAG: the two audits and compile are independent roots.

## RunPod Process Setup

Start one GPU pod with SSH enabled and a network volume mounted at `/logs` or
`/workspace`. Place this repo on the pod, usually at `/logs/cove`, and use one
shared state root:

```bash
export COVE_LOCAL_STATE_ROOT=/logs/cove-no-tee
export RUN_ID=run-$(date -u +%Y%m%dT%H%M%SZ)
```

Prepare the pod process environment once. This installs Python packages used by
the wrapper, aligns Torch with the pinned vLLM wheel, and ensures the compile
node has the pinned vLLM source checkout and base CUDA wheel:

```bash
cd /logs/cove
bash demos_local/attested_confidential_benchmark__vllm_gpu_no_tee/scripts/bootstrap_process_env.sh
```

Prepare encrypted local state on the pod:

```bash
python3 demos_local/attested_confidential_benchmark__vllm_gpu_no_tee/scripts/prepare_state.py \
  --state-root "$COVE_LOCAL_STATE_ROOT"
```

Run the serial benchmark directly as local processes:

```bash
python3 demos_local/attested_confidential_benchmark__vllm_gpu_no_tee/scripts/run_sequence.py \
  --executor process \
  --state-root "$COVE_LOCAL_STATE_ROOT" \
  --run-id "$RUN_ID" \
  --workload-root /logs/cove/demos/attested_confidential_benchmark__vllm_gpu/containers \
  --detach-deployment
```

Collect reports:

```bash
python3 demos_local/attested_confidential_benchmark__vllm_gpu_no_tee/scripts/collect_report.py \
  --state-root "$COVE_LOCAL_STATE_ROOT" \
  --run-id "$RUN_ID"
```

## RunPod Docker Setup

For environments with Docker available inside the pod, pull the pinned no-TEE
wrapper images:

```bash
./demos_local/attested_confidential_benchmark__vllm_gpu_no_tee/scripts/pull_images.sh \
  --image-prefix "$RUNPOD_DOCKER_IMAGE_PREFIX" \
  --tag no-tee-gpu
```

Run the serial benchmark on the pod:

```bash
python demos_local/attested_confidential_benchmark__vllm_gpu_no_tee/scripts/run_sequence.py \
  --executor docker \
  --state-root "$COVE_LOCAL_STATE_ROOT" \
  --run-id "$RUN_ID" \
  --image-prefix "$RUNPOD_DOCKER_IMAGE_PREFIX" \
  --tag no-tee-gpu
```

Reports are written under:

```text
$COVE_LOCAL_STATE_ROOT/results/$RUN_ID/raw/*.json
$COVE_LOCAL_STATE_ROOT/results/$RUN_ID/summary.json
$COVE_LOCAL_STATE_ROOT/results/$RUN_ID/summary.csv
```

## SSH Invocation

From your workstation, the same runner can invoke the pod over SSH when the repo
is already present on the pod:

```bash
export COVE_RUNPOD_SSH_TARGET=<user>@<host>
export COVE_RUNPOD_DEMO_ROOT=/logs/cove/demos_local/attested_confidential_benchmark__vllm_gpu_no_tee

python demos_local/attested_confidential_benchmark__vllm_gpu_no_tee/scripts/run_sequence.py \
  --executor ssh \
  --ssh-target "$COVE_RUNPOD_SSH_TARGET" \
  --remote-demo-root "$COVE_RUNPOD_DEMO_ROOT" \
  --remote-executor process \
  --remote-state-root /logs/cove-no-tee \
  --remote-workload-root /logs/cove/demos/attested_confidential_benchmark__vllm_gpu/containers \
  --state-root /logs/cove-no-tee \
  --run-id "$RUN_ID" \
  --detach-deployment
```

## Files

- `containers/` has thin Docker wrappers for the four workload image types.
- `runner/local_node_wrapper.py` runs one node inside a no-TEE container.
- `runner/benchmark.py` runs serial Docker/SSH execution and report aggregation.
- `scripts/` contains the user-facing entrypoints.
