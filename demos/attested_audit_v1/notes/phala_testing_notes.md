# Phala Testing Notes

This note captures the current state of Phala testing for
`cove/demos/attested_audit_v1`, including what has worked, what has not worked,
and the next steps.

## Objective

The goal is to test the attested audits use case on Phala/GPU TEE with the same
general shape as `demos/hello_world`:

- provision private/static artifacts through Cove/CoveHub;
- compile/push a Cove workflow;
- have owners approve key release against generated node composes;
- deploy to Phala with Cove-generated sidecars;
- produce runtime certificates that bind artifact hashes, generated compose
  hashes, preconditions, and service results.

The intended two-node workflow is:

1. `audit_serving_code`
   - Runs an audit model behind vLLM.
   - Runs an audit-agent harness against the serving patch, pristine source, and
     evaluator-owned audit policy.
   - Outputs `audit_result.json` with `pass: true/false`.
2. `compile_vllm`
   - Depends on the `audit_serving_code` certificate.
   - Applies the audited serving patch to the pristine vLLM source.
   - Builds a compiled serving runtime bundle only if audit preconditions pass.

In a full `cove deploy`, these are separate Phala nodes/CVMs. The DAG runs
`audit_serving_code` first, then `compile_vllm` after the first node certificate
and preconditions are satisfied.

## Important Distinctions

### CoveHub vs Owner Services

CoveHub stores encrypted artifacts, workflow bundles, runtime certificates, and
dynamic outputs.

Owner services release artifact keys after attestation and allow-rule checks.
They are separate from CoveHub.

For this demo:

```yaml
owners:
  eval_owner: https://cove-attested-eval-owner.erika-lee.net
  model_owner: https://cove-attested-model-owner.erika-lee.net
```

Eval owner artifacts:

- `audit_policy`
- `audit_model_weights`
- serving runtime bundle artifacts

Model owner artifacts:

- `serving_patch`
- `pristine_serving_source`

### Public CoveHub vs Local CoveHub

Earlier `cove init` runs used the public/shared CoveHub:

```text
https://api.covehub.io
```

For Phala testing, use Erika's private CoveHub instance instead:

```text
Public API: https://erika-api.covehub.io
Public UI:  https://erika-ui.covehub.io
```

The Cloudflare tunnel routes for the CoveHub Compose stack are:

```text
erika-api.covehub.io -> http://covehub-api:8000
erika-ui.covehub.io  -> http://covehub-ui:8080
```

The Cloudflare tunnel token should be stored locally as `CLOUDFLARED_TOKEN` in
`cove/.env`. Do not commit the raw token to this notes file or to the repo.

That means `covehub_server_url` should be set to:

```text
https://erika-api.covehub.io
```

This may be useful because the 4.6 GB serving runtime bundle is large and may
not fit comfortably on the shared CoveHub instance.

### Docker Registry

Use Erika's Docker Hub namespace for workload image build/push:

```text
erikaleeey
```

The Phala deployer's Cove home must also have Docker registry credentials from
`cove init`, because Phala CVMs need authenticated image pulls.

## What Has Worked

### RunPod Workload Smoke Test

The `audit_serving_code` workload logic was validated in RunPod with a
single-container harness.

Successful output included:

```json
{
  "audit_agent_version": "attested-audit-v1.simple-json.1",
  "audit_model_id": "audit-model",
  "audit_policy_sha256": "sha256:d2f5a2f66182eced1bb47b866ae2af9f2b5b760e92125f084c922b2037531981",
  "pass": true,
  "pristine_source_sha256": "sha256:98c0cbfc45975ea779c4ed3b1184097c36e60f7f4946c19c43c16bbb4df07904",
  "serving_patch_sha256": "sha256:c4932f7f3199ff57522ad2973c33f4f8dd74d7d662e40c7990b42445711a3b6c"
}
```

This validates the vLLM/audit-agent logic, not the full Cove/Phala sidecar
path.

### Owner URL Setup

The eval owner URL has successfully returned an identity document:

```bash
curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-eval-owner.erika-lee.net/identity
```

The response showed:

```text
owner_domain: cove-attested-eval-owner.erika-lee.net
owner_url:    https://cove-attested-eval-owner.erika-lee.net
version:      2
```

Cloudflare routing must use HTTPS origin with `No TLS Verify` enabled because
`cove start` serves HTTPS locally.

For Docker-based `cloudflared` on macOS, the origin may need to be:

```text
https://host.docker.internal:9000
https://host.docker.internal:9001
```

instead of `https://127.0.0.1:9000`, because `127.0.0.1` inside the Docker
container points at the container itself.

### Artifact Packaging

The artifact packaging script is:

```text
cove/demos/attested_audit_v1/scripts/package_audit_serving_artifacts.sh
```

It stages inputs under:

```text
code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code/inputs
```

Current staged artifact hashes:

```text
audit_policy.json
sha256:d2f5a2f66182eced1bb47b866ae2af9f2b5b760e92125f084c922b2037531981

serving_patch.diff
sha256:c4932f7f3199ff57522ad2973c33f4f8dd74d7d662e40c7990b42445711a3b6c

pristine_serving_source.tar.gz
sha256:98c0cbfc45975ea779c4ed3b1184097c36e60f7f4946c19c43c16bbb4df07904

audit_model_weights.tar
sha256:f8bccd70ec9308431ee9ba2c28c8708aba01817a39a7241737d1abb1f5f5b704

audit_serving_runtime_bundle.manifest.json
sha256:abb244f07116dec88c5c75aac00ccd1a59f7178ccec1de21b357333952335ba2

audit_serving_runtime_bundle.tar.gz.partaa
sha256:e28660c88b880272a233087aa4d0744fcb389689436bc49c2cfbeec2958aca2e

audit_serving_runtime_bundle.tar.gz.partab
sha256:4bcfa54deeb6020f310eed38940aecea9eaab59b9b9fa1d042089d7c57268cae

audit_serving_runtime_bundle.tar.gz.partac
sha256:fb3388eec4ff7eff340f57cad72e54c508502016d6bc6bb3f0105f4b11175eeb

audit_serving_runtime_bundle.tar.gz.partad
sha256:526925098aaf976365718b3cf04349111a6c28443d58a12ee51cb27ed482b8b8
```

### Split Runtime Bundle Workaround

The 4.6 GB runtime bundle was too big to provision as one artifact with the old
one-shot AES-GCM path.

Current repo inspection shows CoveHub object upload has chunked transport, but
`cove provision` still reads the full plaintext into memory and encrypts with
one-shot AES-GCM before upload. Therefore, for the next Phala test, keep the
demo-level split runtime bundle path:

```text
audit_serving_runtime_bundle.manifest.json
audit_serving_runtime_bundle.tar.gz.partaa
audit_serving_runtime_bundle.tar.gz.partab
audit_serving_runtime_bundle.tar.gz.partac
audit_serving_runtime_bundle.tar.gz.partad
```

Only switch back to a single `audit_serving_runtime_bundle.tar.gz` artifact
after confirming `cove provision` supports streaming/chunked encryption, not
just chunked upload.

### Local Workflow Validation

Both workflows currently pass `cove check`:

```bash
cove/cli/.venv/bin/cove check \
  cove/demos/attested_audit_v1/workflow/workflow.cove.yaml

cove/cli/.venv/bin/cove check \
  cove/demos/attested_audit_v1/workflow/workflow.audit_serving_code_only.cove.yaml
```

## What Has Not Worked

### RunPod Docker Compose Testing

RunPod pods are themselves containers. Running Docker Compose inside the pod
became a Docker-in-Docker problem unless the pod has Docker/privileged support.

The workable RunPod path was a single-container harness, useful for workload
logic but not for Cove sidecars or true multi-container node behavior.

### Direct 4.6 GB `cove provision`

Provisioning the full runtime bundle as one artifact failed locally before
upload:

```text
OverflowError: Data or associated data too long. Max 2**31 - 1 bytes
```

This was not a Cloudflare issue. It happened in local one-shot AES-GCM
encryption before upload.


### Cloudflare 502s

Cloudflare returned 502 when the tunnel route did not correctly reach the local
owner service.

Common causes:

- route uses HTTP origin while `cove start` serves HTTPS;
- `No TLS Verify` is not enabled;
- Docker `cloudflared` uses `127.0.0.1`, which points inside the container
  rather than the Mac host;
- owner service is not running.

### Expired Certificate Errors

`cove provision` saw:

```text
certificate verify failed: certificate has expired
```

The public `curl` later returned a valid identity document, so this was likely
Cloudflare/DNS/cert routing state while the tunnel was being adjusted. Do not
provision until:

```bash
curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-eval-owner.erika-lee.net/identity
```

and the equivalent model owner URL both work without `-k`.

## Current Files Of Interest

Workflow:

```text
cove/demos/attested_audit_v1/workflow/workflow.cove.yaml
cove/demos/attested_audit_v1/workflow/workflow.audit_serving_code_only.cove.yaml
```

Node compose:

```text
cove/demos/attested_audit_v1/workflow/nodes/audit_serving_code.compose.yaml
cove/demos/attested_audit_v1/workflow/nodes/compile_vllm.compose.yaml
```

Containers:

```text
cove/demos/attested_audit_v1/containers/vllm_server
cove/demos/attested_audit_v1/containers/audit_agent_runner
cove/demos/attested_audit_v1/containers/vllm_compiler
```

Scripts:

```text
cove/demos/attested_audit_v1/scripts/package_audit_serving_artifacts.sh
cove/demos/attested_audit_v1/scripts/build_all_containers.sh
```

## Phala Test Plan

### Strategy

Run the Phala test in two passes:

1. First-node-only workflow: validate CoveHub, owner identities, owner approval,
   static artifact release, digest-pinned images, GPU CVM startup, vLLM health,
   audit-agent execution, and `audit_serving_code` certificate publication.
2. Full two-node workflow: validate dependency certificate gating,
   `compile_vllm` preconditions, audited patch/source hash continuity, dynamic
   compiled runtime bundle publication, and final runtime certificate fields.

Do not start with the full two-node workflow. The compiler node is expensive
and should only be tested after `audit_serving_code` is proven on Phala.

### Estimated Timeline

Expected elapsed time for the first-node Phala run is 3-6 hours. Expected
elapsed time for the full two-node run is 5-10+ hours, depending on image push
time, artifact upload time, GPU scheduling, vLLM startup, and compiler runtime.

```text
Step                                      Estimate     Parallelizable
Confirm CLI/repo state                    15-30 min    yes
Start private CoveHub + Cloudflare         30-60 min    yes
Initialize owner homes + owner tunnels     30-60 min    yes
Build/push/pin workload images             45-120 min   yes
Package/hash artifacts                     30-90 min    yes
Provision artifacts                        30-120 min   partly
Patch workflow hashes/hub paths            15-30 min    after provision
cove check/compile/push                    15-30 min    no
Owner inspect/approval                     15-30 min    eval/model parallel
First-node Phala deploy                    30-120 min   no
Full two-node Phala deploy                 1-4+ hr      no
```

Parallelize the CoveHub/owner setup, Docker image build/push, and artifact
packaging. Serialization begins at provisioning, because artifact declarations,
workflow compilation, approval, and deploy all depend on the exact provisioned
objects and generated compose hashes.

### 0. Shared Variables

```bash
export COVE=/Users/erikalee/github/cove/cli/.venv/bin/cove
export REPO=/Users/erikalee/github/cove
export DEMO="$REPO/demos/attested_audit_v1"
export WORKFLOW="$DEMO/workflow/workflow.cove.yaml"
export WORKFLOW_FIRST="$DEMO/workflow/workflow.audit_serving_code_only.cove.yaml"
export COVEHUB_API_URL=https://erika-api.covehub.io
export COVEHUB_UI_URL=https://erika-ui.covehub.io
export DOCKER_NAMESPACE=erikaleeey
export IMAGE_TAG=phala-two-node-v1
```

### 1. Confirm Local Tooling

```bash
docker --version
python3 --version
uv --version
node --version
npx --version
jq --version
curl --version
rg --version
docker login
```

Confirm the CLI is synced:

```bash
cd "$REPO/cli"
uv sync
"$COVE" --help
```

### 2. Start Erika CoveHub

Create `cove/.env` with the Cloudflare token. Keep the raw token out of this
file and out of git history.

```bash
cd "$REPO"
umask 077
cp .env.example .env
$EDITOR .env
```

Required `.env` values:

```text
CLOUDFLARED_TOKEN=<erika-cloudflare-token>
COVEHUB_API_PORT=3518
COVEHUB_UI_PORT=3517
COVE_UI_CACHE_TTL_SECONDS=5
COVE_UI_PREVIEW_BYTES=4096
```

Cloudflare tunnel routes:

```text
erika-api.covehub.io -> http://covehub-api:8000
erika-ui.covehub.io  -> http://covehub-ui:8080
```

Start and verify:

```bash
cd "$REPO"
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:3518/healthz
curl -fsS http://127.0.0.1:3517/ui-api/healthz
curl -fsS "$COVEHUB_API_URL/healthz"
curl -fsS "$COVEHUB_UI_URL/"
```

### 3. Initialize Owner Homes

Run `cove init` for both owner homes. Use `https://erika-api.covehub.io` as
the CoveHub server URL.

```bash
"$COVE" --cove-home ~/.cove_attested_eval init
"$COVE" --cove-home ~/.cove_attested_model init
```

Eval owner values:

```text
CoveHub server URL:                       https://erika-api.covehub.io
Owner server URL:                         https://cove-attested-eval-owner.erika-lee.net
Phala Cloud API key:                      <eval-owner-phala-key>
Phala Docker registry username:           erikaleeey
Phala Docker registry access token:       <dockerhub-read-token>
Phala Docker registry:                    leave blank for Docker Hub
```

Model owner values:

```text
CoveHub server URL:                       https://erika-api.covehub.io
Owner server URL:                         https://cove-attested-model-owner.erika-lee.net
Phala Cloud API key:                      leave blank unless model owner deploys
Phala Docker registry credentials:        leave blank unless model owner deploys
```

Start owner services in separate terminals:

```bash
"$COVE" --cove-home ~/.cove_attested_eval start 9000
```

```bash
"$COVE" --cove-home ~/.cove_attested_model start 9001
```

Owner Cloudflare routes must use HTTPS origin and `No TLS Verify`:

```text
cove-attested-eval-owner.erika-lee.net  -> https://host.docker.internal:9000
cove-attested-model-owner.erika-lee.net -> https://host.docker.internal:9001
```

Use `https://127.0.0.1:<port>` only if the tunnel connector is not running in a
Docker container.

Verify owner identities:

```bash
curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-eval-owner.erika-lee.net/identity

curl -A 'cove-runtime/0.0.1' -fsS \
  https://cove-attested-model-owner.erika-lee.net/identity
```

### 4. Build, Push, And Pin Workload Images

Run this in parallel with artifact packaging.

```bash
cd "$DEMO"
export DOCKER_DEFAULT_PLATFORM=linux/amd64

./scripts/build_all_containers.sh \
  --docker-namespace "$DOCKER_NAMESPACE" \
  --tag "$IMAGE_TAG" \
  --push
```

This rewrites:

```text
demos/attested_audit_v1/canonical_container_digests.json
demos/attested_audit_v1/workflow/nodes/*.compose.yaml
```

Verify no placeholder digests remain:

```bash
! rg -n '111111|222222|333333' "$DEMO/workflow/nodes"
rg -n 'image: ".+@sha256:' "$DEMO/workflow/nodes"
cat "$DEMO/canonical_container_digests.json"
```

### 5. Package Artifacts

Run this in parallel with image build/push.

```bash
cd "$DEMO"
./scripts/package_audit_serving_artifacts.sh
```

Then set `INPUTS` to the generated input directory. The historical location was:

```bash
export INPUTS=/Users/erikalee/github/cove/code_audit_bench/data/private_artifacts/attested_audit_v1/audit_serving_code/inputs
```

If the package script prints a different artifact root, use that path instead.

### 6. Provision Static Artifacts

Use the split runtime bundle artifacts for this Phala round.

Eval owner:

```bash
"$COVE" --cove-home ~/.cove_attested_eval provision audit_policy "$INPUTS/audit_policy.json"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_model_weights "$INPUTS/audit_model_weights.tar"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_manifest "$INPUTS/audit_serving_runtime_bundle.manifest.json"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_aa "$INPUTS/audit_serving_runtime_bundle.tar.gz.partaa"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_ab "$INPUTS/audit_serving_runtime_bundle.tar.gz.partab"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_ac "$INPUTS/audit_serving_runtime_bundle.tar.gz.partac"
"$COVE" --cove-home ~/.cove_attested_eval provision audit_serving_runtime_bundle_part_ad "$INPUTS/audit_serving_runtime_bundle.tar.gz.partad"
```

Model owner:

```bash
"$COVE" --cove-home ~/.cove_attested_model provision serving_patch "$INPUTS/serving_patch.diff"
"$COVE" --cove-home ~/.cove_attested_model provision pristine_serving_source "$INPUTS/pristine_serving_source.tar.gz"
```

Save all printed `hub_path`, `plaintext_hash`, and `ciphertext_hash` values.
Update the workflow if any hash or artifact name differs from the checked-in
declarations.

Expected static artifact path shape:

```text
v1/artifacts/cove-attested-eval-owner.erika-lee.net/<artifact>/latest
v1/artifacts/cove-attested-model-owner.erika-lee.net/<artifact>/latest
```

The authored workflow may use owner aliases such as `eval_owner` and
`model_owner`; Cove materializes these to owner domains during publish and
approval. The important part is that artifact names and plaintext hashes match.

### 7. Local Validation

```bash
cd "$REPO"

"$COVE" --cove-home ~/.cove_attested_eval check "$WORKFLOW_FIRST"
"$COVE" --cove-home ~/.cove_attested_eval check "$WORKFLOW"

python3 -m json.tool "$DEMO/workflow/schemas/audit_result.v1.json" >/dev/null
python3 -m json.tool "$DEMO/workflow/schemas/compile_result.v1.json" >/dev/null
bash -n "$DEMO/scripts/build_all_containers.sh"
bash -n "$DEMO/scripts/package_audit_serving_artifacts.sh"
```

### 8. First-Node-Only Compile, Push, Approve

```bash
"$COVE" --cove-home ~/.cove_attested_eval compile "$WORKFLOW_FIRST"
"$COVE" --cove-home ~/.cove_attested_eval push "$WORKFLOW_FIRST"
```

Publisher domain is the eval owner domain:

```bash
export WORKFLOW_REF=cove-attested-eval-owner.erika-lee.net/attested_audit_v1
```

Each owner inspects and approves the pushed workflow:

```bash
"$COVE" --cove-home ~/.cove_attested_eval provision inspect "$WORKFLOW_REF"
"$COVE" --cove-home ~/.cove_attested_model provision inspect "$WORKFLOW_REF"
```

During approval, inspect the generated composes and confirm:

- workload image refs are digest-pinned under `erikaleeey/...@sha256:...`;
- `audit_serving_code` has exactly the expected eval/model owner inputs;
- split runtime bundle parts match the workflow declarations;
- custom certificate field points at `/workspace/output/audit_result.json`;
- no unexpected artifact access appears.

Keep both owner services running after approval.

### 9. First-Node-Only Phala Deploy

Delete stale Phala CVMs with the same workflow/node names before redeploying.

```bash
"$COVE" --cove-home ~/.cove_attested_eval deploy "$WORKFLOW_REF" \
  --phala-instance-type h200.small \
  --phala-disk-size-gb 120 \
  --phala-public-logs \
  --phala-public-sysinfo
```

Record the deploy output:

```text
audit_serving_code cvm_id
audit_serving_code app_id
Phala compose hash
Cove reviewed compose hash
```

Monitor:

```bash
export PHALA_CLOUD_API_KEY=<eval-owner-phala-key>

npx --yes phala cvms get <audit-cvm-id> --json | jq
npx --yes phala ps <audit-cvm-id>
npx --yes phala logs --cvm-id <audit-cvm-id> audit_model_server -n 200 --stderr
npx --yes phala logs --cvm-id <audit-cvm-id> audit_agent_runner -n 200 --stderr
```

First-node success criteria:

- static artifacts are provisioned without owner key-release failures;
- vLLM reaches health;
- `audit_agent_runner` writes schema-valid `audit_result.json`;
- runtime certificate appears in Erika CoveHub;
- certificate `results.audit_agent_runner.pass` is `true`;
- certificate input plaintext hashes match the workflow.

### 10. Full Two-Node Compile, Push, Approve, Deploy

Only do this after the first-node-only run succeeds.

```bash
"$COVE" --cove-home ~/.cove_attested_eval compile "$WORKFLOW"
"$COVE" --cove-home ~/.cove_attested_eval push "$WORKFLOW"

"$COVE" --cove-home ~/.cove_attested_eval provision inspect "$WORKFLOW_REF"
"$COVE" --cove-home ~/.cove_attested_model provision inspect "$WORKFLOW_REF"

"$COVE" --cove-home ~/.cove_attested_eval deploy "$WORKFLOW_REF" \
  --phala-instance-type h200.small \
  --phala-disk-size-gb 120 \
  --phala-public-logs \
  --phala-public-sysinfo
```

Full workflow success criteria:

- `audit_serving_code` publishes a passing certificate;
- `compile_vllm` starts only after the audit certificate is available;
- `compile_vllm` preconditions confirm the audited patch/source hashes equal
  its own input hashes;
- `vllm_compiler` writes schema-valid `compile_result.json`;
- `compiled_serving_runtime_bundle` is published as a dynamic output;
- both node certificates are visible in Erika CoveHub.

### 11. CoveHub State Inspection

```bash
cd "$REPO"

docker compose exec -T covehub-api find /var/lib/covehub/data/artifacts -type f | sort
docker compose exec -T covehub-api find /var/lib/covehub/data/workflows -type f | sort
docker compose exec -T covehub-api find /var/lib/covehub/data/runtime -type f | sort
```
