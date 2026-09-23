# RunPod `attested_audit_v1`

This directory has ways to run early attested-audit nodes without Cove sidecars.

- `audit-serving-code.compose.yaml` runs the real two-container shape, but
  requires Docker inside the RunPod pod.
- `Dockerfile.audit_serving_code` builds a single RunPod runtime image that
  starts vLLM and the audit runner in one container. Use this when the pod
  cannot run Docker inside itself. It intentionally does not include CUDA
  compiler/dev packages; the later compile node should use a separate compiler
  image.
- `compile-vllm.compose.yaml` runs the compiler container against staged
  `/workspace/inputs` and writes the compiled bundle under
  `/workspace/runs/<run_id>/compile_vllm`.
- `build_compile_vllm_image.sh` builds the compiler image for direct use as a
  RunPod pod image when Docker is unavailable inside the pod.

## Stage Inputs

Attach a RunPod network volume at `/workspace` and stage:

```text
/workspace/inputs/audit_serving_runtime_bundle.tar.gz
/workspace/inputs/audit_model_weights.tar
/workspace/inputs/pristine_serving_source.tar.gz
/workspace/inputs/serving_patch.diff
/workspace/inputs/audit_policy.json
```

`serving_patch.diff` and `audit_policy.json` can come from this demo's
`fixtures/` directory. The repo already has a vLLM source tarball that can be
used as the pristine source input:

```text
code_audit_bench/data/public_artifacts/vllm/vllm-72506c9.tar.gz
```

The model bundle should be a tar archive whose contents are the Hugging Face
model directory. The runtime bundle should be a tar.gz archive containing one or
more installable vLLM wheel files at the archive root.

## Run

### Compile Node

If the RunPod pod cannot run Docker inside itself, build and push the compiler
image from a local machine:

```bash
cd cove/demos/attested_audit_v1
./runpod/build_compile_vllm_image.sh \
  --docker-namespace <registry-or-user> \
  --tag runpod-compile-v1 \
  --push
```

Deploy a RunPod pod directly from:

```text
<registry-or-user>/cove-demo-attested-audit-v1-vllm-compiler:runpod-compile-v1
```

Attach the `/workspace` network volume and make sure these files exist:

```text
/workspace/inputs/pristine_serving_source.tar.gz
/workspace/inputs/serving_patch.diff
```

Set optional environment variables:

```text
RUN_ID=<optional-run-id>
PRISTINE_SOURCE=/workspace/inputs/pristine_serving_source.tar.gz
SERVING_PATCH=/workspace/inputs/serving_patch.diff
```

The pod command defaults to `/app/bin/compile_vllm.sh`. Results are written to:

```text
/workspace/runs/<run_id>/compile_vllm/
```

If the RunPod machine can run Docker, the same image can be tested through
Compose:

```bash
export RUN_ID="$(date +%Y%m%d%H%M%S)"
export VLLM_COMPILER_IMAGE=<registry-or-user>/cove-demo-attested-audit-v1-vllm-compiler:runpod-compile-v1

mkdir -p "/workspace/runs/${RUN_ID}/compile_vllm"
docker compose -f cove/demos/attested_audit_v1/runpod/compile-vllm.compose.yaml up \
  --abort-on-container-exit \
  --exit-code-from vllm-compiler
```

Verify:

```bash
cat "/workspace/runs/${RUN_ID}/compile_vllm/compile_result.json"
ls -lh "/workspace/runs/${RUN_ID}/compile_vllm/compiled_serving_runtime_bundle.tar.gz"
```

### Single-Container Pod Path

Build and push this image from a local machine with Docker:

```bash
cd cove/demos/attested_audit_v1
./runpod/build_audit_serving_code_image.sh \
  --docker-namespace <registry-or-user> \
  --tag runpod-audit-v1 \
  --push
```

Deploy a RunPod pod using:

```text
<registry-or-user>/cove-demo-attested-audit-v1-runpod-audit-serving-code:runpod-audit-v1
```

Set or keep these environment defaults:

```text
RUN_ID=<optional-run-id>
SERVING_RUNTIME_BUNDLE=/workspace/inputs/audit_serving_runtime_bundle.tar.gz
MODEL_BUNDLE=/workspace/inputs/audit_model_weights.tar
SERVING_PATCH=/workspace/inputs/serving_patch.diff
PRISTINE_SOURCE=/workspace/inputs/pristine_serving_source.tar.gz
AUDIT_POLICY=/workspace/inputs/audit_policy.json
AUDIT_MODEL_ID=audit-model
```

After the pod starts, results are under:

```text
/workspace/runs/<run_id>/audit_serving_code/
```

The image includes `serving_patch.diff`, `audit_policy.json`, and the test
script. It does not include the large pristine vLLM source tarball. Upload or
copy that tarball to:

```text
/workspace/inputs/pristine_serving_source.tar.gz
```

Then the full single-container test can be run with:

```bash
/app/bin/stage_inputs_and_run.sh
```

That script copies the small fixtures from the image, downloads the tiny audit
model, downloads vLLM wheels, stages `/workspace/inputs`, and runs
`/app/bin/run_audit_serving_code.sh`.

### Docker Compose Path

```bash
export RUN_ID="$(date +%Y%m%d%H%M%S)"
export VLLM_SERVER_IMAGE=<registry>/cove-demo-attested-audit-v1-vllm-server:<tag>
export AUDIT_AGENT_RUNNER_IMAGE=<registry>/cove-demo-attested-audit-v1-audit-agent-runner:<tag>
export AUDIT_MODEL_ID=audit-model

mkdir -p "/workspace/runs/${RUN_ID}/audit_serving_code"
docker compose -f cove/demos/attested_audit_v1/runpod/audit-serving-code.compose.yaml up \
  --abort-on-container-exit \
  --exit-code-from audit-agent-runner
```

## Verify

```bash
cat "/workspace/runs/${RUN_ID}/audit_serving_code/audit_result.json"
cat "/workspace/runs/${RUN_ID}/audit_serving_code/audit_model_response.txt"
```

The result should be valid against
`workflow/schemas/audit_result.v1.json`, and the three hash fields should match
the staged patch, pristine source, and policy artifacts.
