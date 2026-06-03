# `attested_audit_v1`

`attested_audit_v1` is a three-node Cove demo that runs end to end on Phala:

1. audit proposed vLLM serving-code changes;
2. compile the approved patched vLLM runtime;
3. run an operational eval smoke test through the compiled runtime.

## Actors

The workflow has two owners:

- **Eval owner** - owns the audit policy, audit model weights, and eval
  benchmark fixture.
- **Model owner** - owns the proposed serving patch, pristine serving source,
  and compiled serving runtime output.

The checked-in Phala testing configuration currently uses:

```text
Eval owner:  https://cove-attested-eval-owner.erika-lee.net
Model owner: https://cove-attested-model-owner.erika-lee.net
```

## Artifacts

Static eval-owner artifacts:

- `audit_policy`
- `audit_model_weights`
- `eval_benchmark`

Static model-owner artifacts:

- `serving_patch`
- `pristine_serving_source`

Dynamic model-owner artifact:

- `compiled_serving_runtime_bundle`

Dynamic eval-owner artifact:

- `eval_responses`

The old private audit-serving runtime bundle parts are no longer on the main
Phala path. The audit and eval model servers now use a `vllm/vllm-openai`-based
image and provision only the model weights. The compile node still produces a
patched vLLM wheel bundle, which the eval node installs before serving.

## Nodes

### `audit_serving_code`

Starts a vLLM audit model server and runs the audit-agent runner.

Inputs:

- eval owner's audit model weights;
- eval owner's audit policy;
- model owner's serving patch;
- model owner's pristine serving source.

Result:

- `audit_result.json`, validated by
  `workflow/schemas/audit_result.v1.json`.

Current testing note: the runner preserves raw model response diagnostics, but
`AUDIT_AGENT_FORCE_PASS=1` forces `pass=true` so the downstream Phala pipeline
can be exercised while the audit model/prompt is improved.

### `compile_vllm`

Depends on the `audit_serving_code` certificate.

It only runs if:

- `audit_serving_code.results.audit_agent_runner.pass == true`;
- the serving patch hash matches the audited patch hash;
- the pristine source hash matches the audited source hash.

Output:

- `compiled_serving_runtime_bundle`, a dynamic Cove artifact containing built
  vLLM wheel(s).

Result:

- `compile_result.json`, validated by
  `workflow/schemas/compile_result.v1.json`.

Current testing note: on compiler failure, the script writes `pass=false`,
captures a log preview, and creates a tiny placeholder bundle so CoveHub can
publish a diagnostic certificate. Downstream consumers must require
`vllm_compiler.pass == true`.

### `run_eval`

Depends on the `compile_vllm` certificate.

It only runs if:

- `compile_vllm.results.vllm_compiler.pass == true`.

Inputs:

- eval owner's audit model weights;
- eval owner's `eval_benchmark` fixture;
- model owner's dynamic `compiled_serving_runtime_bundle`.

Flow:

1. install the compiled vLLM wheel bundle into the vLLM server image;
2. unpack model weights;
3. start OpenAI-compatible `vllm serve`;
4. run the eval runner against `/v1/chat/completions`;
5. write `eval_result.json`;
6. publish `eval_responses.jsonl`.

Result:

- `eval_result.json`, validated by `workflow/schemas/eval_result.v1.json`.

Output:

- `eval_responses`, a dynamic artifact containing per-prompt response records.

Current testing note: this node uses a 10-prompt XSTest smoke subset. `pass`
currently means `operational_pass`, while `quality_pass` is `null`.

## Key Files

- `workflow/workflow.cove.yaml` - full three-node workflow.
- `workflow/workflow.audit_serving_code_only.cove.yaml` - first-node-only
  workflow kept for cheaper audit-node smoke testing.
- `workflow/nodes/audit_serving_code.compose.yaml` - audit node workload
  compose.
- `workflow/nodes/compile_vllm.compose.yaml` - compiler node workload compose.
- `workflow/nodes/run_eval.compose.yaml` - eval node workload compose.
- `workflow/schemas/audit_result.v1.json` - audit result schema.
- `workflow/schemas/compile_result.v1.json` - compile result schema.
- `workflow/schemas/eval_result.v1.json` - eval result schema.
- `containers/vllm_server` - serves model weights with vLLM and can optionally
  install a compiled runtime bundle.
- `containers/audit_agent_runner` - calls the audit model server and writes
  audit diagnostics.
- `containers/vllm_compiler` - applies the audited patch and packages patched
  runtime wheels.
- `containers/eval_runner` - runs the XSTest smoke fixture and writes eval
  results.
- `fixtures/xstest_smoke_v1.jsonl` - 10-prompt XSTest smoke subset.
- `notes/understanding.md` - conceptual notes on image vs. wheel, eval grading,
  and GPU/TEE quote questions.
- `notes/phala_testing_notes.md` - current Phala testing notes and temporary
  overrides.
- `notes/patch_backlog.md` - known cleanup work.

## Prerequisites

Before running the Phala demo, set up:

- **CoveHub** reachable from Phala. Current testing uses
  `https://erika-api.covehub.io`.
- **Owner services** reachable from Phala. Current testing uses:
  - `https://cove-attested-eval-owner.erika-lee.net`
  - `https://cove-attested-model-owner.erika-lee.net`
- **Phala API key** for the deployer.
- **Docker Hub credentials** for pushing and pulling digest-pinned images.
- **Local tools**: Docker, Node/`npx`, `curl`, `jq`, `rg`, and the Cove CLI
  virtualenv.

Set common environment variables:

```bash
cd /Users/erikalee/github/cove

export COVE=/Users/erikalee/github/cove/cli/.venv/bin/cove
export WORKFLOW_FULL=/Users/erikalee/github/cove/demos/attested_audit_v1/workflow/workflow.cove.yaml
export WORKFLOW_REF=cove-attested-eval-owner.erika-lee.net/attested_audit_v1
export CERTIFI_CA=$(/Users/erikalee/github/cove/cli/.venv/bin/python -c 'import certifi; print(certifi.where())')
export SSL_CERT_FILE="$CERTIFI_CA"
export REQUESTS_CA_BUNDLE="$CERTIFI_CA"
```

## Start Local Services

Start CoveHub from the repo root:

```bash
cd /Users/erikalee/github/cove
docker compose up -d --build
```

Start owner services in separate terminals:

```bash
"$COVE" --cove-home ~/.cove_attested_eval start 9000
```

```bash
"$COVE" --cove-home ~/.cove_attested_model start 9001
```

## Provision Static Artifacts

The fixture paths currently used in the workflow are:

```bash
export INPUTS=/Users/erikalee/github/cove/code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code/inputs
export DEMO=/Users/erikalee/github/cove/demos/attested_audit_v1
```

Eval owner:

```bash
"$COVE" --cove-home ~/.cove_attested_eval provision audit_policy "$INPUTS/audit_policy.json"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_model_weights "$INPUTS/audit_model_weights.tar"
"$COVE" --cove-home ~/.cove_attested_eval provision eval_benchmark "$DEMO/fixtures/xstest_smoke_v1.jsonl"
```

Model owner:

```bash
"$COVE" --cove-home ~/.cove_attested_model provision serving_patch "$DEMO/fixtures/serving_patch.diff"
"$COVE" --cove-home ~/.cove_attested_model provision pristine_serving_source "$INPUTS/pristine_serving_source.tar.gz"
```

## Build And Pin Workload Images

The workflow uses digest-pinned workload images. Build/push images from the demo
root, then update:

- `workflow/nodes/*.compose.yaml`;
- `canonical_container_digests.json`.

Current workload images:

- `cove-demo-attested-audit-v1-vllm-server`
- `cove-demo-attested-audit-v1-audit-agent-runner`
- `cove-demo-attested-audit-v1-vllm-compiler`
- `cove-demo-attested-audit-v1-eval-runner`

The current Phala run uses manually pinned image digests in the compose files.
For Docker Buildx images, pin the pullable `linux/amd64` manifest digest rather
than an OCI image index digest if Phala cannot pull the index digest.

## Compile, Push, And Approve

Compile and push the full workflow:

```bash
"$COVE" --cove-home ~/.cove_attested_eval check "$WORKFLOW_FULL"
"$COVE" --cove-home ~/.cove_attested_eval compile "$WORKFLOW_FULL"
"$COVE" --cove-home ~/.cove_attested_eval push "$WORKFLOW_FULL" --overwrite
```

Inspect pending provisioning approvals:

```bash
"$COVE" --cove-home ~/.cove_attested_eval provision inspect "$WORKFLOW_REF"
"$COVE" --cove-home ~/.cove_attested_model provision inspect "$WORKFLOW_REF"
```

Approve both owners where needed.

## Deploy On Phala

Current operational path is manual/generated-compose deployment with the Phala
CLI. Deploy generated node composes in dependency order:

```bash
npx --yes phala deploy \
  --cvm-id c5222836489e0b2b48236de67f3974039d0163ac \
  --compose demos/attested_audit_v1/workflow/build/nodes/audit_serving_code/compose.generated.yaml \
  --wait
```

```bash
npx --yes phala deploy \
  --cvm-id c5222836489e0b2b48236de67f3974039d0163ac \
  --compose demos/attested_audit_v1/workflow/build/nodes/compile_vllm/compose.generated.yaml \
  --wait
```

```bash
npx --yes phala deploy \
  --cvm-id c5222836489e0b2b48236de67f3974039d0163ac \
  --compose demos/attested_audit_v1/workflow/build/nodes/run_eval/compose.generated.yaml \
  --wait
```

Because dependency certificates are hash-strict, changing an upstream node's
generated compose hash may require redeploying downstream dependencies in order.
For example, if `run_eval` expects a newer `compile_vllm` compose hash than the
latest published compile certificate contains, redeploy `compile_vllm`. If that
compile deploy expects a newer `audit_serving_code` hash, redeploy
`audit_serving_code` first.

## Verify Certificates

Audit node:

```bash
curl -fsS "https://erika-api.covehub.io/v1/runtime/cove-attested-eval-owner.erika-lee.net/attested_audit_v1/certificates/audit_serving_code/latest" \
| python3 -c 'import json,sys; b=json.load(sys.stdin)["certificate_body"]; print(b["generated_node_compose_hash"]); print(b["results"]["audit_agent_runner"]["pass"])'
```

Compile node:

```bash
curl -fsS "https://erika-api.covehub.io/v1/runtime/cove-attested-eval-owner.erika-lee.net/attested_audit_v1/certificates/compile_vllm/latest" \
| python3 -c 'import json,sys; b=json.load(sys.stdin)["certificate_body"]; r=b["results"]["vllm_compiler"]; print(b["generated_node_compose_hash"]); print(r["pass"]); print(r.get("wheel_names"))'
```

Eval node:

```bash
curl -fsS "https://erika-api.covehub.io/v1/runtime/cove-attested-eval-owner.erika-lee.net/attested_audit_v1/certificates/run_eval/latest" \
| python3 -c 'import json,sys; b=json.load(sys.stdin)["certificate_body"]; r=b["results"]["eval_runner"]; print(b["generated_node_compose_hash"]); print(r["pass"]); print(r["operational_pass"]); print(r["quality_pass"]); print(r["requests_succeeded"], "/", r["requests_attempted"])'
```

## Current Eval Semantics

The eval node uses `fixtures/xstest_smoke_v1.jsonl`, a 10-prompt subset of the
XSTest safety test suite. This is a benchmark-style fixture for smoke testing.

Current result meaning:

```text
pass == operational_pass
quality_pass == null
```

This proves the node can provision inputs, start the model server, call the
OpenAI-compatible endpoint, capture non-empty responses, publish
`eval_responses`, and write a certificate result.

It does not prove the model is safe or that it passed XSTest. Real grading still
needs a defined eval-owner policy, likely using deterministic checks, a
classifier, a judge model, or a hybrid scheme.

## Known Cleanup

See `notes/patch_backlog.md` for the current patch list. High-priority items:

- remove `AUDIT_AGENT_FORCE_PASS=1` after the audit model/prompt is fixed;
- replace the marker-file serving patch with a real benign vLLM source patch;
- align the compiler environment with the runtime image so the compiled wheel
  does not create dependency skew;
- remove smoke-test overrides such as `FLASHINFER_DISABLE_VERSION_CHECK=1`;
- define real eval grading semantics for XSTest or another benchmark.

## RunPod Testing

RunPod testing is useful for workload logic only. It can verify container-level
behavior, but it does not exercise CoveHub, owner approval, sidecars, Phala
attestation, runtime certificates, or the full three-node DAG.

RunPod files live under:

```text
runpod/
```
