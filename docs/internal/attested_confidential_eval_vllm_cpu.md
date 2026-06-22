# Attested Confidential Eval vLLM CPU Runbook

This document is the end-to-end runbook for
`demos/attested_confidential_eval_vllm_cpu`. The workflow demonstrates the
attested confidential eval design on Phala TDX using CPU-only vLLM.

The workflow ref is:

```text
demo-carol.covehub.io/attested_confidential_eval_vllm_cpu
```

The workflow file is:

```text
/home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/workflow/workflow.cove.yaml
```

## What This Demo Proves

The demo has five nodes:

- `audit_serving_code` audits Alice's private vLLM patch.
- `compile_serving_code` applies Alice's patch to pinned public vLLM and emits
  the compiled CPU wheel as a dynamic artifact.
- `audit_eval_code` audits Bob's private one-file eval runner.
- `model_benchmark` installs the compiled wheel, serves Alice's private model,
  runs Bob's private HarmBench eval data, and emits aggregate metrics only.
- `model_deployment` serves the same model and compiled wheel over RA-TLS after
  benchmark success.

Alice owns:

- `alice_private_model`
- `alice_private_serving_patch`
- `compiled_serving_wheel`

Bob owns:

- `bob_private_eval_code`
- `bob_private_eval_data`

Pinned public bases:

- vLLM `v0.17.0` at `b31e9326a7d9394aab8c767f8ebe225c65594b60`
- Inspect AI at `953f813c039d7b435a710ba7931d755424c8fc83`
- HarmBench at `8e1604d1171fe8a48d8febecd22f600e462bdcdd`
- audit model default `Qwen/Qwen3.5-9B`

The private model archive uses `Qwen/Qwen2.5-0.5B-Instruct` for this CPU demo.
The originally proposed Qwen3.5 hybrid path requires kernels that are not
available in the CPU vLLM wheel on Phala.

## 1. Start Covehub, Tunnels, CLI, And Owners

Use the shared hello-world setup steps in [hello_world.md](hello_world.md):

- start the root Covehub API/UI Compose stack,
- start the parties Cloudflare tunnel,
- build and install the release CLI wheel,
- initialize Alice, Bob, and Carol Cove homes,
- start Alice, Bob, and Carol owner services on ports `9600`, `9601`, and
  `9602`.

Carol needs a Phala Cloud API key and Docker Hub credentials. Alice and Bob do
not need deploy credentials.

## 2. Prepare Private Inputs

Generate Alice and Bob's private inputs:

```bash
cd /home/$USER/cove

uv run --with huggingface_hub --with pyyaml \
  python demos/attested_confidential_eval_vllm_cpu/scripts/prepare_demo_inputs.py
```

This creates:

```text
demos/attested_confidential_eval_vllm_cpu/runtime_inputs/alice_private_model.tar
demos/attested_confidential_eval_vllm_cpu/runtime_inputs/alice_private_serving_patch.diff
demos/attested_confidential_eval_vllm_cpu/runtime_inputs/bob_private_eval_code.py
demos/attested_confidential_eval_vllm_cpu/runtime_inputs/bob_private_eval_data.jsonl
```

The preparation script also refreshes the static `plaintext_hash` pins in
`workflow/workflow.cove.yaml`.

Bob's private eval data is converted from the official HarmBench file:

```text
data/behavior_datasets/harmbench_behaviors_text_test.csv
```

The generated JSONL has 320 HarmBench text behaviors. Bob's eval code is one
standalone Python file that imports Inspect AI and defines/runs
`CoveDemoHarmBenchEval`; it does not depend on `inspect_evals`.

Verify the workflow after generation:

```bash
uv run --directory cli cove check \
  /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/workflow/workflow.cove.yaml
```

## 3. Build Workload Images

The checked-in node compose files are digest-pinned to known working CPU
workload images. Rebuild and push only when changing container code:

```bash
cd /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu
./scripts/build_all_containers.sh \
  --docker-namespace covehub \
  --tag <release-tag> \
  --push
```

This updates `canonical_container_digests.json` and rewrites image refs in
`workflow/nodes/*.compose.yaml`.

## 4. Provision Static Artifacts

Activate the release CLI venv:

```bash
source /home/$USER/.cove-cli-release/bin/activate
```

Alice provisions her model archive and serving patch:

```bash
cove --cove-home /home/$USER/.alice_cove provision \
  alice_private_model \
  /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/runtime_inputs/alice_private_model.tar

cove --cove-home /home/$USER/.alice_cove provision \
  alice_private_serving_patch \
  /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/runtime_inputs/alice_private_serving_patch.diff
```

Bob provisions his eval code and eval data:

```bash
cove --cove-home /home/$USER/.bob_cove provision \
  bob_private_eval_code \
  /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/runtime_inputs/bob_private_eval_code.py

cove --cove-home /home/$USER/.bob_cove provision \
  bob_private_eval_data \
  /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/runtime_inputs/bob_private_eval_data.jsonl
```

## 5. Carol Compiles And Publishes

Carol checks, compiles, and publishes:

```bash
cove --cove-home /home/$USER/.carol_cove check \
  /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/workflow/workflow.cove.yaml

cove --cove-home /home/$USER/.carol_cove compile \
  /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/workflow/workflow.cove.yaml

cove --cove-home /home/$USER/.carol_cove push \
  /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/workflow/workflow.cove.yaml
```

The published ref is:

```text
demo-carol.covehub.io/attested_confidential_eval_vllm_cpu
```

## 6. Alice And Bob Approve

Alice approves access to her private model, private patch, and the dynamic
compiled wheel channel:

```bash
cove --cove-home /home/$USER/.alice_cove provision inspect \
  demo-carol.covehub.io/attested_confidential_eval_vllm_cpu
```

Bob approves access to his private eval code and eval data:

```bash
cove --cove-home /home/$USER/.bob_cove provision inspect \
  demo-carol.covehub.io/attested_confidential_eval_vllm_cpu
```

Keep all owner services running while Phala executes. Runtime sidecars call the
owner URLs for key release.

## 7. Deploy To Phala

If Carol's Phala account has enough quota, deploy the full DAG:

```bash
cove --cove-home /home/$USER/.carol_cove deploy \
  demo-carol.covehub.io/attested_confidential_eval_vllm_cpu \
  --phala-instance-type tdx.4xlarge \
  --phala-disk-size-gb 120 \
  --phala-public-logs \
  --phala-public-sysinfo \
  --no-phala-listed \
  --dependency-timeout-seconds 7200
```

If quota only allows one `tdx.4xlarge` at a time, deploy sequentially and delete
completed CVMs before starting the next node:

```bash
COMMON_DEPLOY_FLAGS="\
  --phala-instance-type tdx.4xlarge \
  --phala-disk-size-gb 120 \
  --phala-public-logs \
  --phala-public-sysinfo \
  --no-phala-listed \
  --dependency-timeout-seconds 7200"

cove --cove-home /home/$USER/.carol_cove deploy \
  demo-carol.covehub.io/attested_confidential_eval_vllm_cpu \
  --workflow-node audit_serving_code \
  $COMMON_DEPLOY_FLAGS

cove --cove-home /home/$USER/.carol_cove deploy \
  demo-carol.covehub.io/attested_confidential_eval_vllm_cpu \
  --workflow-node audit_eval_code \
  $COMMON_DEPLOY_FLAGS

cove --cove-home /home/$USER/.carol_cove deploy \
  demo-carol.covehub.io/attested_confidential_eval_vllm_cpu \
  --workflow-node compile_serving_code \
  $COMMON_DEPLOY_FLAGS

cove --cove-home /home/$USER/.carol_cove deploy \
  demo-carol.covehub.io/attested_confidential_eval_vllm_cpu \
  --workflow-node model_benchmark \
  $COMMON_DEPLOY_FLAGS

cove --cove-home /home/$USER/.carol_cove deploy \
  demo-carol.covehub.io/attested_confidential_eval_vllm_cpu \
  --workflow-node model_deployment \
  $COMMON_DEPLOY_FLAGS
```

Do not delete the final `model_deployment` CVM after it is healthy; it is the
serving endpoint.

## 8. Verify Certificates

Fetch the latest certificates:

```bash
for node in audit_serving_code audit_eval_code compile_serving_code model_benchmark model_deployment; do
  curl -A 'cove-runtime/0.0.1' -fsS \
    "https://api.covehub.io/v1/runtime/demo-carol.covehub.io/attested_confidential_eval_vllm_cpu/certificates/${node}/latest" \
    > "/tmp/${node}.json"
done
```

Check the important result fields:

```bash
python - <<'PY'
import json
from pathlib import Path

for node in ["audit_serving_code", "audit_eval_code", "compile_serving_code", "model_benchmark"]:
    cert = json.loads(Path(f"/tmp/{node}.json").read_text())
    print(node)
    print(json.dumps(cert["certificate_body"].get("results", {}), indent=2))
PY
```

Expected high-level outcomes:

- serving audit `pass: true`
- eval audit `pass: true`
- compile result `pass: true`
- benchmark `passes_threshold: true`
- benchmark `benchmark_name: CoveDemoHarmBenchEval`
- benchmark `attack_success_rate` is the HarmBench-style DirectRequest ASR
- benchmark `score` is robust refusal rate, equal to `1 - attack_success_rate`
- benchmark `serving_wheel_sha256` equals compile `compiled_wheel_sha256`
- deployment inputs for `alice_private_model` and `compiled_serving_wheel`
  equal the benchmark-attested model and wheel hashes

## 9. Verify The RA-TLS Serving Endpoint

Get the deployment endpoint from Phala:

```bash
export PHALA_CLOUD_API_KEY="$(
  python - <<'PY'
import os
from cove_cli.config import ensure_local_config
print(ensure_local_config(os.path.expanduser("~/.carol_cove")).phala_cloud_api_key)
PY
)"

npx --yes phala cvms list --json
npx --yes phala cvms get <model-deployment-app-id> --json
```

Use the TLS-passthrough endpoint. If Phala reports:

```text
https://<app-id>-18443.dstack-pha-prod5.phala.network
```

use:

```text
https://<app-id>-18443s.dstack-pha-prod5.phala.network
```

Then verify:

```bash
SERVE_URL="https://<app-id>-18443s.dstack-pha-prod5.phala.network"

curl -kfsS "${SERVE_URL}/health"
curl -kfsS "${SERVE_URL}/v1/models"
curl -kfsS "${SERVE_URL}/v1/chat/completions" \
  -H 'Content-Type: application/json' \
  -d '{"model":"CoveDemoModel","messages":[{"role":"user","content":"Reply with exactly: ok"}],"max_tokens":4,"temperature":0}'
```

The TLS certificate common name should be:

```text
attested_confidential_eval_vllm_cpu.model_deployment.ratls_key
```

## 10. Run The Client UI

The client lives next to the workflow:

```bash
cd /home/$USER/cove/demos/attested_confidential_eval_vllm_cpu/client

PORT=5177 \
COVE_DEMO_ENDPOINT="$SERVE_URL" \
COVE_WORKFLOW_ID=attested_confidential_eval_vllm_cpu \
node server.mjs
```

Open:

```text
http://127.0.0.1:5177
```

The main panel sends non-streaming OpenAI-compatible chat requests to
`CoveDemoModel`. The sidebar fetches the Cove certificates from Covehub,
inspects the RA-TLS certificate, and verifies:

- all expected node certificates are present,
- all certificates bind to the selected workflow and node ids,
- serving patch and eval-code audit hashes match the consumed artifacts,
- compile output hash matches the dynamic wheel artifact,
- benchmark consumed the audited eval code, audited patch, model archive, eval
  data, and compiled wheel,
- deployment uses the same model and wheel attested by benchmark,
- the HarmBench DirectRequest ASR/refusal-rate aggregate is displayed without
  raw prompts or model generations,
- the endpoint healthcheck passes,
- the endpoint serves `CoveDemoModel`.
