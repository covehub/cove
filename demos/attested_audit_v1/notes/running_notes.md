# Attested Audit Phala Running Notes

This file tracks the concrete test run for `demos/attested_audit_v1`.

Goal: get the first node, `audit_serving_code`, fully ready for Cove compile,
push, owner approval, and Phala deploy before renting a GPU TEE. Only after the
first node succeeds should we build and deploy the full two-node workflow with
`compile_vllm`.

## Current State

Use Erika's private CoveHub instance:

```text
CoveHub API: https://erika-api.covehub.io
CoveHub UI:  https://erika-ui.covehub.io
Docker Hub:  erikaleeey
```

Cloudflare routes for the CoveHub Compose stack:

```text
erika-api.covehub.io -> http://covehub-api:8000
erika-ui.covehub.io  -> http://covehub-ui:8080
```

Owner URLs:

```text
Eval owner:  https://cove-attested-eval-owner.erika-lee.net
Model owner: https://cove-attested-model-owner.erika-lee.net
```

Owner services are expected to run locally on:

```text
eval owner  -> local port 9000
model owner -> local port 9001
```

For Docker-based `cloudflared` on macOS, owner Cloudflare routes should use:

```text
cove-attested-eval-owner.erika-lee.net  -> https://host.docker.internal:9000
cove-attested-model-owner.erika-lee.net -> https://host.docker.internal:9001
```

Enable `No TLS Verify` for the owner routes because `cove start` serves local
HTTPS with owner-generated certs.

## Commands Already Run And What They Do

### 1. Started Erika CoveHub

```bash
cd /Users/erikalee/github/cove
cp .env.example .env
# Set CLOUDFLARED_TOKEN in .env.
docker compose up -d --build
```

What this does:

- starts local `covehub-api`, `covehub-ui`, and `cloudflared`;
- exposes the API publicly at `https://erika-api.covehub.io`;
- exposes the UI publicly at `https://erika-ui.covehub.io`;
- provides the storage/transport layer for encrypted artifacts, workflow
  bundles, runtime certificates, and dynamic outputs.

Why this is needed:

- Phala CVMs cannot call local `localhost`;
- Cove runtime sidecars need a public HTTPS CoveHub API;
- using Erika's instance keeps this large test isolated from shared
  `https://api.covehub.io`.

Health checks already run:

```bash
curl -fsS http://127.0.0.1:3518/healthz
curl -fsS http://127.0.0.1:3517/ui-api/healthz
curl -fsS https://erika-api.covehub.io/healthz
curl -fsS https://erika-ui.covehub.io/
```

Expected result:

- local health checks succeed;
- public API/UI routes succeed without `-k`.

### 2. Started Owner Services

Eval owner terminal:

```bash
export COVE=/Users/erikalee/github/cove/cli/.venv/bin/cove
"$COVE" --cove-home ~/.cove_attested_eval start 9000
```

Model owner terminal:

```bash
export COVE=/Users/erikalee/github/cove/cli/.venv/bin/cove
"$COVE" --cove-home ~/.cove_attested_model start 9001
```

What this does:

- serves each owner's signed `/identity` document;
- serves key-release endpoints used by Cove runtime sidecars;
- enforces each owner's local allow rules after `provision inspect`.

Why this is needed:

- `cove compile` fetches owner identities and bakes owner public keys into the
  generated sidecar config;
- Phala CVMs call these owner URLs during artifact provisioning;
- owner services must keep running through approval and deploy.

Identity checks already run:

```bash
curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-eval-owner.erika-lee.net/identity | jq

curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-model-owner.erika-lee.net/identity | jq
```

Expected result:

- both return `version: 2`;
- both include `owner_url`, `owner_domain`, and `owner_public_key_sha256`;
- neither requires `curl -k`.

### 3. Stage Input Artifacts

 cd /Users/erikalee/github/cove/demos/attested_audit_v1

  ./scripts/package_audit_serving_artifacts.sh

  What that does:

  - copies checked-in small fixtures into the private artifact staging area:
      - audit_policy.json
      - serving_patch.diff

  - copies the pristine vLLM source tarball into inputs/
  - downloads or reuses the audit model
  - packages audit_model_weights.tar
  - downloads or reuses Linux/amd64 vLLM runtime wheels
  - packages audit_serving_runtime_bundle.tar.gz
  - splits that runtime bundle into .partaa, .partab, etc.
  - writes audit_serving_runtime_bundle.manifest.json
  - prints hashes to compare against the workflow

  The expected output directory is:

  /Users/erikalee/github/cove/code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code/inputs

Set:

```bash
export INPUTS=/Users/erikalee/github/cove/code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code/inputs
```

Checked files:

```bash
ls -lh "$INPUTS"
test -f "$INPUTS/audit_policy.json"
test -f "$INPUTS/audit_model_weights.tar"
test -f "$INPUTS/audit_serving_runtime_bundle.manifest.json"
test -f "$INPUTS/audit_serving_runtime_bundle.tar.gz.partaa"
test -f "$INPUTS/audit_serving_runtime_bundle.tar.gz.partab"
test -f "$INPUTS/audit_serving_runtime_bundle.tar.gz.partac"
test -f "$INPUTS/audit_serving_runtime_bundle.tar.gz.partad"
test -f "$INPUTS/serving_patch.diff"
test -f "$INPUTS/pristine_serving_source.tar.gz"
```

What this does:

- confirms packaging does not need to be rerun;
- confirms the large model/runtime artifacts are already staged.

The existing files match the workflow hashes:

```text
audit_policy.json                              sha256:d2f5a2f66182eced1bb47b866ae2af9f2b5b760e92125f084c922b2037531981
serving_patch.diff                            sha256:c4932f7f3199ff57522ad2973c33f4f8dd74d7d662e40c7990b42445711a3b6c
pristine_serving_source.tar.gz                sha256:98c0cbfc45975ea779c4ed3b1184097c36e60f7f4946c19c43c16bbb4df07904
audit_model_weights.tar                       sha256:f8bccd70ec9308431ee9ba2c28c8708aba01817a39a7241737d1abb1f5f5b704
audit_serving_runtime_bundle.manifest.json    sha256:abb244f07116dec88c5c75aac00ccd1a59f7178ccec1de21b357333952335ba2
audit_serving_runtime_bundle.tar.gz.partaa    sha256:e28660c88b880272a233087aa4d0744fcb389689436bc49c2cfbeec2958aca2e
audit_serving_runtime_bundle.tar.gz.partab    sha256:4bcfa54deeb6020f310eed38940aecea9eaab59b9b9fa1d042089d7c57268cae
audit_serving_runtime_bundle.tar.gz.partac    sha256:fb3388eec4ff7eff340f57cad72e54c508502016d6bc6bb3f0105f4b11175eeb
audit_serving_runtime_bundle.tar.gz.partad    sha256:526925098aaf976365718b3cf04349111a6c28443d58a12ee51cb27ed482b8b8
```

Conclusion:

- do not rerun `package_audit_serving_artifacts.sh` unless source fixtures,
  model, pristine source, or runtime wheels change.

### 4. Ran Cheap Local Validation

```bash
cd /Users/erikalee/github/cove

bash -n demos/attested_audit_v1/scripts/build_all_containers.sh
bash -n demos/attested_audit_v1/scripts/package_audit_serving_artifacts.sh
python3 -m json.tool demos/attested_audit_v1/workflow/schemas/audit_result.v1.json >/dev/null
python3 -m json.tool demos/attested_audit_v1/workflow/schemas/compile_result.v1.json >/dev/null
```

What this does:

- checks shell syntax for the build/package scripts;
- checks JSON syntax for the custom certificate schemas.

Expected result:

- no output and exit status `0`.

### 5. Started First-Node Image Build/Push

```bash
cd /Users/erikalee/github/cove/demos/attested_audit_v1
export DOCKER_DEFAULT_PLATFORM=linux/amd64

./scripts/build_all_containers.sh \
  --docker-namespace erikaleeey \
  --tag phala-two-node-v1 \
  --push \
  --audit-serving-code-only
```

What this does:

- builds only the images needed by `audit_serving_code`;
- pushes them to Docker Hub under `erikaleeey`;
- should update:
  - `demos/attested_audit_v1/canonical_container_digests.json`
  - `demos/attested_audit_v1/workflow/nodes/audit_serving_code.compose.yaml`

Why this is the minimum useful build:

- first Phala test should only run `audit_serving_code`;
- `compile_vllm` is expensive and not needed until the first node succeeds;
- the compiler image can be built later.

## Important Constraint: Streaming Encryption

The repo has chunked CoveHub object upload, but `cove provision` currently
still reads the whole file and encrypts in one shot before upload.

Relevant implementation shape:

```text
cli/src/cove_cli/provision.py       -> local_file.read_bytes()
cli/src/cove_cli/artifact_crypto.py -> AESGCM(...).encrypt(...)
cli/src/cove_cli/covehub.py         -> chunked upload transport
```

Meaning:

- chunked upload helps transport large ciphertexts;
- it does not fix local one-shot encryption of a 4.6 GB plaintext;
- keep using the split runtime bundle parts for this Phala run.

Do not switch to a single `audit_serving_runtime_bundle.tar.gz` artifact until
`cove provision` supports streaming/chunked encryption, not just chunked upload.

## Readiness Gates Before Renting GPU TEE

Do not run `cove deploy` until all of these are true:

1. `audit_serving_code.compose.yaml` has real digest-pinned images under
   `erikaleeey/...@sha256:...`.
2. No `111111` or `222222` placeholders remain in
   `audit_serving_code.compose.yaml`.
3. Erika CoveHub API is publicly reachable.
4. Both owner identity endpoints are publicly reachable.
5. Artifacts have been provisioned to Erika CoveHub.
6. `cove check` passes for the first-node workflow.
7. `cove compile` succeeds for the first-node workflow.
8. `cove push` succeeds for the first-node workflow.
9. Both owners have run `provision inspect` and approved expected access.
10. Eval Cove home has Phala API key and Docker registry credentials.

## Next Steps After Image Push Finishes

### 1. Verify Or Recover Image Digests

Check whether the build script updated the compose file:

```bash
cd /Users/erikalee/github/cove

rg -n '111111|222222|333333' demos/attested_audit_v1/workflow/nodes
cat demos/attested_audit_v1/canonical_container_digests.json
sed -n '1,45p' demos/attested_audit_v1/workflow/nodes/audit_serving_code.compose.yaml
```

For first-node testing:

- `111111` and `222222` must be gone from
  `audit_serving_code.compose.yaml`;
- `333333` in `compile_vllm.compose.yaml` is acceptable until the full
  two-node workflow test.

If placeholders remain, fetch the pushed registry digests:

```bash
docker buildx imagetools inspect \
  erikaleeey/cove-demo-attested-audit-v1-vllm-server:phala-two-node-v1

docker buildx imagetools inspect \
  erikaleeey/cove-demo-attested-audit-v1-audit-agent-runner:phala-two-node-v1
```

Patch `demos/attested_audit_v1/workflow/nodes/audit_serving_code.compose.yaml`
so the image lines are:

```text
erikaleeey/cove-demo-attested-audit-v1-vllm-server@sha256:<digest>
erikaleeey/cove-demo-attested-audit-v1-audit-agent-runner@sha256:<digest>
```

Then verify:

```bash
! rg -n '111111|222222' demos/attested_audit_v1/workflow/nodes/audit_serving_code.compose.yaml
rg -n 'image: ".+@sha256:' demos/attested_audit_v1/workflow/nodes/audit_serving_code.compose.yaml
```

### 2. Provision Static Artifacts

Set variables:

```bash
export COVE=/Users/erikalee/github/cove/cli/.venv/bin/cove
export INPUTS=/Users/erikalee/github/cove/code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code/inputs
```

Provision eval-owner artifacts:

```bash
"$COVE" --cove-home ~/.cove_attested_eval provision audit_policy "$INPUTS/audit_policy.json"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_model_weights "$INPUTS/audit_model_weights.tar"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_manifest "$INPUTS/audit_serving_runtime_bundle.manifest.json"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_aa "$INPUTS/audit_serving_runtime_bundle.tar.gz.partaa"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_ab "$INPUTS/audit_serving_runtime_bundle.tar.gz.partab"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_ac "$INPUTS/audit_serving_runtime_bundle.tar.gz.partac"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_ad "$INPUTS/audit_serving_runtime_bundle.tar.gz.partad"
```

Provision model-owner artifacts:

```bash
"$COVE" --cove-home ~/.cove_attested_model provision serving_patch "$INPUTS/serving_patch.diff"
"$COVE" --cove-home ~/.cove_attested_model provision pristine_serving_source "$INPUTS/pristine_serving_source.tar.gz"
```

What this does:

- encrypts each static artifact locally;
- uploads encrypted objects to Erika CoveHub;
- stores local owner key metadata in the corresponding Cove home;
- prints `hub_path`, `plaintext_hash`, `ciphertext_hash`, and `key_path`.

Save the printed output. If any printed plaintext hash differs from the
workflow, stop and update the workflow before compiling.

note: if ssl cert fails, set export SSL_CERT_FILE="$CERTIFI_CA"
export COVE=/Users/erikalee/github/cove/cli/.venv/bin/cove
export INPUTS=/Users/erikalee/github/cove/code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code/inputs
export CERTIFI_CA=$(/Users/erikalee/github/cove/cli/.venv/bin/python -c 'import certifi; print(certifi.where())')

note: also made updates to the upload timeout limit for provisioning

### 3. Check First-Node Workflow

```bash
export WORKFLOW_FIRST=/Users/erikalee/github/cove/demos/attested_audit_v1/workflow/workflow.audit_serving_code_only.cove.yaml

"$COVE" --cove-home ~/.cove_attested_eval check "$WORKFLOW_FIRST"
```

What this does:

- validates authored workflow structure;
- validates artifact references and preconditions;
- validates node compose references.

Expected result:

- check passes;
- no digest placeholder errors for `audit_serving_code`.

### 4. Compile First-Node Workflow

```bash
"$COVE" --cove-home ~/.cove_attested_eval compile "$WORKFLOW_FIRST"
```

What this does:

- fetches owner identity documents;
- verifies owner signatures under public TLS;
- generates Cove sidecar services;
- creates generated node compose/config under workflow build output;
- bakes owner public keys and artifact policy into generated sidecar config.

Expected result:

- compile succeeds;
- generated compose references digest-pinned workload images;
- generated sidecars reference Erika CoveHub.

### 5. Push First-Node Workflow

```bash
"$COVE" --cove-home ~/.cove_attested_eval push "$WORKFLOW_FIRST"
```

What this does:

- uploads the compiled workflow bundle to Erika CoveHub under the eval owner
  publisher domain.

Expected workflow ref:

```bash
export WORKFLOW_REF=cove-attested-eval-owner.erika-lee.net/attested_audit_v1
```

### 6. Owner Inspect And Approve

Eval owner:

```bash
"$COVE" --cove-home ~/.cove_attested_eval provision inspect "$WORKFLOW_REF"
```

Model owner:

```bash
"$COVE" --cove-home ~/.cove_attested_model provision inspect "$WORKFLOW_REF"
```

What this does:

- pulls the published workflow bundle from Erika CoveHub;
- shows generated node compose and artifact access requests;
- lets each owner create local allow rules for expected nodes/artifacts.

Approval checklist:

- `audit_serving_code` has exactly the expected inputs;
- eval owner sees `audit_policy`, `audit_model_weights`, and runtime bundle
  manifest/parts;
- model owner sees `serving_patch` and `pristine_serving_source`;
- workload image refs are under `erikaleeey/...@sha256:...`;
- no unexpected node or artifact access is present.

Keep both owner services running after approval.

### 7. Final Pre-Deploy Checks

Do these before renting GPU TEE:

```bash
curl -fsS https://erika-api.covehub.io/healthz

curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-eval-owner.erika-lee.net/identity | jq '.owner_domain,.owner_public_key_sha256'

curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-model-owner.erika-lee.net/identity | jq '.owner_domain,.owner_public_key_sha256'

"$COVE" --cove-home ~/.cove_attested_eval check "$WORKFLOW_FIRST"
```

Confirm eval owner home has Phala and Docker registry credentials:

```bash
cat ~/.cove_attested_eval/config.yaml
```

Expected config values:

```text
covehub_server_url: https://erika-api.covehub.io
owner_server_url: https://cove-attested-eval-owner.erika-lee.net
phala_cloud_api_key: <present>
phala_docker_registry_username: erikaleeey
phala_docker_registry_access_token: <present>
```

### 8. Deploy First-Node Workflow To Phala

Only run this after all readiness gates pass.

```bash
"$COVE" --cove-home ~/.cove_attested_eval deploy "$WORKFLOW_REF" \
  --phala-instance-type h200.small \
  --phala-disk-size-gb 120 \
  --phala-public-logs \
  --phala-public-sysinfo
```

What this does:

- creates the Phala CVM for `audit_serving_code`;
- installs the generated Cove sidecars and workload services;
- runs static artifact provisioning;
- starts the vLLM audit model server;
- runs the audit-agent runner;
- publishes a runtime certificate to Erika CoveHub.

Record from deploy output:

```text
audit_serving_code cvm_id
audit_serving_code app_id
Phala compose hash
Cove reviewed compose hash
```

### 9. Monitor First-Node Phala Run

```bash
export PHALA_CLOUD_API_KEY=<eval-owner-phala-key>

npx --yes phala cvms get <audit-cvm-id> --json | jq
npx --yes phala ps <audit-cvm-id>
npx --yes phala logs --cvm-id <audit-cvm-id> audit_model_server -n 200 --stderr
npx --yes phala logs --cvm-id <audit-cvm-id> audit_agent_runner -n 200 --stderr
```

Success criteria:

- artifact provisioner sidecars fetch all static artifacts;
- owner key-release requests succeed;
- vLLM reaches `/health`;
- audit-agent runner writes `/workspace/output/audit_result.json`;
- runtime certificate appears in Erika CoveHub;
- `results.audit_agent_runner.pass` is `true`;
- certificate input plaintext hashes match the workflow.

### 10. Inspect CoveHub Runtime State

```bash
cd /Users/erikalee/github/cove

docker compose exec -T covehub-api find /var/lib/covehub/data/runtime -type f | sort
```

Optionally use the CoveHub UI:

```text
https://erika-ui.covehub.io
```

## Full Two-Node Workflow After First-Node Success

Do not do this until `audit_serving_code` succeeds on Phala.

### 1. Build And Push Compiler Image

```bash
cd /Users/erikalee/github/cove/demos/attested_audit_v1
export DOCKER_DEFAULT_PLATFORM=linux/amd64

docker build \
  -t cove-demo-attested-audit-v1-vllm-compiler:phala-two-node-v1 \
  -f containers/vllm_compiler/Dockerfile \
  .

docker tag \
  cove-demo-attested-audit-v1-vllm-compiler:phala-two-node-v1 \
  erikaleeey/cove-demo-attested-audit-v1-vllm-compiler:phala-two-node-v1

docker push erikaleeey/cove-demo-attested-audit-v1-vllm-compiler:phala-two-node-v1
```

Get the pushed digest:

```bash
docker buildx imagetools inspect \
  erikaleeey/cove-demo-attested-audit-v1-vllm-compiler:phala-two-node-v1
```

Patch:

```text
demos/attested_audit_v1/workflow/nodes/compile_vllm.compose.yaml
```

Image line should become:

```text
erikaleeey/cove-demo-attested-audit-v1-vllm-compiler@sha256:<digest>
```

### 2. Check, Compile, Push Full Workflow

```bash
export WORKFLOW=/Users/erikalee/github/cove/demos/attested_audit_v1/workflow/workflow.cove.yaml

"$COVE" --cove-home ~/.cove_attested_eval check "$WORKFLOW"
"$COVE" --cove-home ~/.cove_attested_eval compile "$WORKFLOW"
"$COVE" --cove-home ~/.cove_attested_eval push "$WORKFLOW"
```

### 3. Re-Approve Full Workflow

```bash
"$COVE" --cove-home ~/.cove_attested_eval provision inspect "$WORKFLOW_REF"
"$COVE" --cove-home ~/.cove_attested_model provision inspect "$WORKFLOW_REF"
```

Approval checklist:

- `compile_vllm` depends on `audit_serving_code`;
- `compile_vllm` preconditions require
  `certificates.audit_serving_code.certificate_body.results.audit_agent_runner.pass == true`;
- `compile_vllm` compares audited patch/source hashes to its own input hashes;
- compiler image is digest-pinned;
- dynamic output is `compiled_serving_runtime_bundle`.

### 4. Deploy Full Workflow

```bash
"$COVE" --cove-home ~/.cove_attested_eval deploy "$WORKFLOW_REF" \
  --phala-instance-type h200.small \
  --phala-disk-size-gb 120 \
  --phala-public-logs \
  --phala-public-sysinfo
```

Full workflow success criteria:

- `audit_serving_code` publishes a passing certificate;
- `compile_vllm` starts only after the first certificate is available;
- `compile_vllm` preconditions pass;
- `vllm_compiler` writes schema-valid `compile_result.json`;
- `compiled_serving_runtime_bundle` is published as a dynamic CoveHub artifact;
- both node certificates are visible in Erika CoveHub.
