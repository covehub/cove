# `attested_audit_v1`

GPU attested-audit demo for CoveHub on Phala dstack.

The workflow has 5 nodes:

1. `audit_serving_code` audits the private serving patch.
2. `compile_vllm` builds the patched vLLM runtime bundle.
3. `audit_eval_code` audits the private eval harness.
4. `run_eval` serves the private model and runs a HarmBench smoke eval.
5. `model_deployment` serves the approved private model/runtime.

The demo uses Phala H200 CVMs. The last tested CVM shape was `h200.small`
with 1 H200 GPU and 200 GB disk.

## Models

- Private eval/serving model: `Qwen/Qwen2.5-0.5B-Instruct`
- Public audit model: `Qwen/Qwen3.5-9B`

The audit model is loaded from Hugging Face by the audit nodes and is not a
private Cove artifact. The private model is packaged as `private_model.tar` and
is mounted only by `run_eval` and `model_deployment`.

## Artifacts

Private static artifacts:

- `private_model`
- `private_serving_patch`
- `private_eval_code`
- `private_eval_data`

Dynamic artifacts:

- `compiled_serving_runtime_bundle`
- `eval_responses`

## Current Routes

```text
CoveHub API:  https://api.covehub.io
Eval owner:   https://cove-attested-eval-owner.erika-lee.net
Model owner:  https://cove-attested-model-owner.erika-lee.net
Workflow:     cove-attested-eval-owner.erika-lee.net/attested_audit_v1
```

## Setup

```bash
cd /Users/erikalee/github/cove

export COVE=/Users/erikalee/github/cove/cli/.venv/bin/cove
export DEMO=/Users/erikalee/github/cove/demos/attested_audit_v1
export WORKFLOW=$DEMO/workflow/workflow.cove.yaml
export WORKFLOW_REF=cove-attested-eval-owner.erika-lee.net/attested_audit_v1
export CVM_ID=<phala-cvm-id>
```

Start the two owner servers:

```bash
"$COVE" --cove-home ~/.cove_attested_eval start 9000
"$COVE" --cove-home ~/.cove_attested_model start 9001
```

Prepare the private inputs. This downloads the private Qwen 2.5 0.5B model,
writes the eval harness, writes the HarmBench subset, and updates workflow
hash preconditions.

```bash
python3 "$DEMO/scripts/prepare_demo_inputs.py"
```

Provision the four private artifacts:

```bash
"$COVE" --cove-home ~/.cove_attested_model provision private_model "$DEMO/runtime_inputs/private_model.tar"
"$COVE" --cove-home ~/.cove_attested_model provision private_serving_patch "$DEMO/runtime_inputs/private_serving_patch.diff"

"$COVE" --cove-home ~/.cove_attested_eval provision private_eval_code "$DEMO/runtime_inputs/private_eval_code.py"
"$COVE" --cove-home ~/.cove_attested_eval provision private_eval_data "$DEMO/runtime_inputs/private_eval_data.jsonl"
```

## Publish Workflow

```bash
"$COVE" --cove-home ~/.cove_attested_eval check "$WORKFLOW"
"$COVE" --cove-home ~/.cove_attested_eval compile "$WORKFLOW"
"$COVE" --cove-home ~/.cove_attested_eval push "$WORKFLOW" --overwrite
```

Approve provisioning requests for both owners:

```bash
"$COVE" --cove-home ~/.cove_attested_eval provision inspect "$WORKFLOW_REF"
"$COVE" --cove-home ~/.cove_attested_model provision inspect "$WORKFLOW_REF"
```

## Deploy On Phala

Deploy nodes in dependency order. Do not run dependent nodes before their
upstream certificates are published.

```bash
npx --yes phala deploy --cvm-id "$CVM_ID" --compose "$DEMO/workflow/build/nodes/audit_serving_code/compose.generated.yaml" --wait
npx --yes phala deploy --cvm-id "$CVM_ID" --compose "$DEMO/workflow/build/nodes/compile_vllm/compose.generated.yaml" --wait
npx --yes phala deploy --cvm-id "$CVM_ID" --compose "$DEMO/workflow/build/nodes/audit_eval_code/compose.generated.yaml" --wait
npx --yes phala deploy --cvm-id "$CVM_ID" --compose "$DEMO/workflow/build/nodes/run_eval/compose.generated.yaml" --wait
npx --yes phala deploy --cvm-id "$CVM_ID" --compose "$DEMO/workflow/build/nodes/model_deployment/compose.generated.yaml" --wait
```

`phala deploy --wait` can time out while containers keep running. Check state
with:

```bash
npx --yes phala ps "$CVM_ID"
```

## Verify

Certificates are published under the workflow publisher runtime path:

```text
https://api.covehub.io/v1/runtime/cove-attested-eval-owner.erika-lee.net/attested_audit_v1/certificates/<node>/latest
```

For the final serving certificate:

```bash
curl -fsS "https://api.covehub.io/v1/runtime/cove-attested-eval-owner.erika-lee.net/attested_audit_v1/certificates/model_deployment/latest"
```

The expected shape is:

- 5 nodes in the workflow.
- 4 private static artifacts.
- Audit nodes use public `Qwen/Qwen3.5-9B`.
- Eval and final serving nodes use private `private_model.tar`.
- `run_eval.results.eval_runner.pass == true`.
- `model_deployment` inputs are `compiled_serving_runtime_bundle` and
  `private_model`.
