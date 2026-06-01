# `attested_audit_v1`

`attested_audit_v1` is a two-node Cove demo for auditing serving code before a
serving runtime is built.

The demo answers one question first: did the proposed serving patch pass the
eval owner's audit policy? If yes, the second node can build a patched vLLM
runtime bundle from the audited inputs.

## Actors

The workflow has two owners:

- **Eval owner** - owns the audit policy, audit model weights, and audit serving
  runtime bundle.
- **Model owner** - owns the proposed serving patch and pristine serving source.

The checked-in Phala testing configuration currently uses:

```text
Eval owner:  https://cove-attested-eval-owner.erika-lee.net
Model owner: https://cove-attested-model-owner.erika-lee.net
```

## Artifacts

Eval owner artifacts:

- `audit_policy`
- `audit_model_weights`
- `audit_serving_runtime_bundle_manifest`
- `audit_serving_runtime_bundle_part_aa`
- `audit_serving_runtime_bundle_part_ab`
- `audit_serving_runtime_bundle_part_ac`
- `audit_serving_runtime_bundle_part_ad`

Model owner artifacts:

- `serving_patch`
- `pristine_serving_source`

The runtime bundle is split into parts because current `cove provision`
encrypts each artifact in one shot. The full runtime bundle is larger than the
one-shot AES-GCM input limit, so Phala testing should provision the manifest and
parts, not the full `audit_serving_runtime_bundle.tar.gz`.

## Nodes

### `audit_serving_code`

This node starts a vLLM audit model server and runs the audit-agent runner.

Inputs:

- eval owner's audit policy;
- eval owner's audit model weights;
- eval owner's split runtime bundle;
- model owner's serving patch;
- model owner's pristine serving source.

Output:

- `audit_result.json`, validated by `workflow/schemas/audit_result.v1.json`.

The certificate result includes `pass: true` or `pass: false` plus the hashes of
the audited inputs.

### `compile_vllm`

This node depends on the `audit_serving_code` certificate.

It only runs if:

- `audit_serving_code` passed;
- the serving patch hash matches the audited patch hash;
- the pristine source hash matches the audited source hash.

Output:

- `compiled_serving_runtime_bundle`, a dynamic Cove artifact.

## Key Files

- `workflow/workflow.cove.yaml` - full two-node workflow.
- `workflow/workflow.audit_serving_code_only.cove.yaml` - first-node-only
  workflow for cheaper Phala smoke testing.
- `workflow/nodes/audit_serving_code.compose.yaml` - audit node workload
  compose.
- `workflow/nodes/compile_vllm.compose.yaml` - compiler node workload compose.
- `workflow/schemas/audit_result.v1.json` - audit result schema.
- `workflow/schemas/compile_result.v1.json` - compile result schema.
- `containers/vllm_server` - installs the supplied runtime and serves the audit
  model with vLLM.
- `containers/audit_agent_runner` - runs the audit against the model server.
- `containers/vllm_compiler` - applies the audited patch and packages patched
  runtime wheels.
- `scripts/package_audit_serving_artifacts.sh` - stages private inputs.
- `scripts/build_all_containers.sh` - builds and pushes workload images.
- `running_notes.md` - current Phala run notes and troubleshooting.
- `phala_testing_notes.md` - historical Phala testing notes.

## End-To-End Phala Demo

Use the first-node-only workflow first. Build and deploy the full two-node
workflow only after `audit_serving_code` succeeds on Phala.

### 1. Start CoveHub

Run CoveHub from the repo root:

```bash
cd /Users/erikalee/github/cove
cp .env.example .env
# Set CLOUDFLARED_TOKEN in .env.
docker compose up -d --build
```

For Erika's private CoveHub instance:

```text
API: https://erika-api.covehub.io
UI:  https://erika-ui.covehub.io
```

Cloudflare tunnel routes:

```text
erika-api.covehub.io -> http://covehub-api:8000
erika-ui.covehub.io  -> http://covehub-ui:8080
```

Verify:

```bash
curl -fsS http://127.0.0.1:3518/healthz
curl -fsS https://erika-api.covehub.io/healthz
```

### 2. Start Owner Services

Initialize two Cove homes if they do not already exist:

```bash
export COVE=/Users/erikalee/github/cove/cli/.venv/bin/cove

"$COVE" --cove-home ~/.cove_attested_eval init
"$COVE" --cove-home ~/.cove_attested_model init
```

Both homes should use:

```text
covehub_server_url: https://erika-api.covehub.io
```

Start the owner services in separate terminals:

```bash
"$COVE" --cove-home ~/.cove_attested_eval start 9000
```

```bash
"$COVE" --cove-home ~/.cove_attested_model start 9001
```

Verify:

```bash
curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-eval-owner.erika-lee.net/identity

curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-model-owner.erika-lee.net/identity
```

### 3. Stage Input Artifacts

For a first-time run:

```bash
cd /Users/erikalee/github/cove/demos/attested_audit_v1
./scripts/package_audit_serving_artifacts.sh
```

Skip this if the input directory already exists and the hashes match the
workflow.

Set:

```bash
export INPUTS=/Users/erikalee/github/cove/code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code/inputs
```

### 4. Build And Push First-Node Images

Build only the `audit_serving_code` images first:

```bash
cd /Users/erikalee/github/cove/demos/attested_audit_v1
export DOCKER_DEFAULT_PLATFORM=linux/amd64

./scripts/build_all_containers.sh \
  --docker-namespace erikaleeey \
  --tag phala-two-node-v1 \
  --push \
  --audit-serving-code-only
```

Verify that `workflow/nodes/audit_serving_code.compose.yaml` has real
digest-pinned image refs and no placeholders:

```bash
cd /Users/erikalee/github/cove
! rg -n '111111|222222' demos/attested_audit_v1/workflow/nodes/audit_serving_code.compose.yaml
rg -n 'image: ".+@sha256:' demos/attested_audit_v1/workflow/nodes/audit_serving_code.compose.yaml
```

### 5. Provision Artifacts

Use the same shell for the provisioning commands:

```bash
export COVE=/Users/erikalee/github/cove/cli/.venv/bin/cove
export INPUTS=/Users/erikalee/github/cove/code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code/inputs
export CERTIFI_CA=$(/Users/erikalee/github/cove/cli/.venv/bin/python -c 'import certifi; print(certifi.where())')
export SSL_CERT_FILE="$CERTIFI_CA"
export COVEHUB_REQUEST_TIMEOUT_SECONDS=600
```

Eval owner:

```bash
"$COVE" --cove-home ~/.cove_attested_eval provision audit_policy "$INPUTS/audit_policy.json"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_manifest "$INPUTS/audit_serving_runtime_bundle.manifest.json"
/usr/bin/time -l "$COVE" --cove-home ~/.cove_attested_eval provision audit_model_weights "$INPUTS/audit_model_weights.tar"
/usr/bin/time -l "$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_aa "$INPUTS/audit_serving_runtime_bundle.tar.gz.partaa"
/usr/bin/time -l "$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_ab "$INPUTS/audit_serving_runtime_bundle.tar.gz.partab"
/usr/bin/time -l "$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_ac "$INPUTS/audit_serving_runtime_bundle.tar.gz.partac"
/usr/bin/time -l "$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_ad "$INPUTS/audit_serving_runtime_bundle.tar.gz.partad"
```

Model owner:

```bash
"$COVE" --cove-home ~/.cove_attested_model provision serving_patch "$INPUTS/serving_patch.diff"
"$COVE" --cove-home ~/.cove_attested_model provision pristine_serving_source "$INPUTS/pristine_serving_source.tar.gz"
```

Do not provision the full `audit_serving_runtime_bundle.tar.gz`.

### 6. Check, Compile, And Push First-Node Workflow

```bash
export WORKFLOW_FIRST=/Users/erikalee/github/cove/demos/attested_audit_v1/workflow/workflow.audit_serving_code_only.cove.yaml

"$COVE" --cove-home ~/.cove_attested_eval check "$WORKFLOW_FIRST"
"$COVE" --cove-home ~/.cove_attested_eval compile "$WORKFLOW_FIRST"
"$COVE" --cove-home ~/.cove_attested_eval push "$WORKFLOW_FIRST"
```

### 7. Owner Approval

```bash
export WORKFLOW_REF=cove-attested-eval-owner.erika-lee.net/attested_audit_v1

"$COVE" --cove-home ~/.cove_attested_eval provision inspect "$WORKFLOW_REF"
"$COVE" --cove-home ~/.cove_attested_model provision inspect "$WORKFLOW_REF"
```

Each owner should approve only the expected artifacts for
`audit_serving_code`. Keep both owner services running after approval.

### 8. Deploy First Node To Phala

Run this only after the workflow is checked, compiled, pushed, and approved:

```bash
"$COVE" --cove-home ~/.cove_attested_eval deploy "$WORKFLOW_REF" \
  --phala-instance-type h200.small \
  --phala-disk-size-gb 120 \
  --phala-public-logs \
  --phala-public-sysinfo
```

Monitor with the Phala CLI:

```bash
export PHALA_CLOUD_API_KEY=<eval-owner-phala-key>
npx --yes phala cvms get <audit-cvm-id> --json
npx --yes phala ps <audit-cvm-id>
npx --yes phala logs --cvm-id <audit-cvm-id> audit_model_server -n 200 --stderr
npx --yes phala logs --cvm-id <audit-cvm-id> audit_agent_runner -n 200 --stderr
```

Success means `audit_serving_code` publishes a certificate whose
`audit_agent_runner.pass` result is `true`.

## Full Two-Node Demo

After the first node succeeds, build and push the compiler image, replace the
placeholder digest in `workflow/nodes/compile_vllm.compose.yaml`, and run the
full workflow:

```bash
export WORKFLOW=/Users/erikalee/github/cove/demos/attested_audit_v1/workflow/workflow.cove.yaml

"$COVE" --cove-home ~/.cove_attested_eval check "$WORKFLOW"
"$COVE" --cove-home ~/.cove_attested_eval compile "$WORKFLOW"
"$COVE" --cove-home ~/.cove_attested_eval push "$WORKFLOW"
"$COVE" --cove-home ~/.cove_attested_eval provision inspect "$WORKFLOW_REF"
"$COVE" --cove-home ~/.cove_attested_model provision inspect "$WORKFLOW_REF"
```

Then deploy the same `WORKFLOW_REF`. The `compile_vllm` node should run only
after the audit certificate passes and its patch/source input hashes match the
audited hashes.

## RunPod Testing

RunPod testing is useful for workload logic only. It can verify that the vLLM
server and audit-agent runner work with staged inputs, but it does not exercise
CoveHub, owner approval, sidecars, Phala attestation, runtime certificates, or
the full two-node DAG.

RunPod files live under:

```text
runpod/
```

Use them for quick container-level debugging before repeating a Phala run.
