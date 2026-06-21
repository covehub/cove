# Hello World End-To-End Runbook

This document is the operational walkthrough for the `hello_world` demo. It
brings up production Covehub (`api.covehub.io` and `covehub.io`), the three
public demo owner services, and the hello-world workflow on Phala. It assumes
the repo is checked out at `/home/$USER/cove`.

## What This Demo Proves

The hello-world workflow is the smallest checked-in workflow that exercises the
core Cove primitives end to end:

- Alice and Bob each own one static secret word.
- Carol publishes and deploys the workflow.
- Runtime sidecars decrypt static inputs only inside attested Phala CVMs.
- Intermediate dynamic artifacts are published through runtime channels.
- Downstream nodes verify dependency certificates and JsonLogic preconditions.
- The final node serves an HTTPS endpoint with an enclave-generated RA-TLS
  keypair.

The workflow has four nodes:

- `alice_word_length_checker`
- `bob_word_length_checker`
- `character_set_checker`
- `final_server`

## Reference Identifiers

- Covehub API: `https://api.covehub.io`
- Covehub UI: `https://covehub.io`
- Alice owner URL: `https://demo-alice.covehub.io`
- Bob owner URL: `https://demo-bob.covehub.io`
- Carol owner/publisher URL: `https://demo-carol.covehub.io`
- Alice local owner port: `9600`
- Bob local owner port: `9601`
- Carol local owner port: `9602`
- Workflow ref: `demo-carol.covehub.io/hello_world`
- Workflow file: `/home/$USER/cove/demos/hello_world/workflow/workflow.cove.yaml`

The Alice/Bob/Carol parties tunnel and the Covehub API/UI tunnel are separate
Cloudflare connectors. Keep their credentials separate and use the connector
command or token provided by the operator for the environment you are running.

## Authored Workflow Surface

The authored `workflow.cove.yaml` does not contain artifact `hub_path` fields.
Static artifacts are declared by owner plus plaintext hash; dynamic artifacts
are declared by owner only:

```yaml
owners:
  alice: https://demo-alice.covehub.io
  bob: https://demo-bob.covehub.io
  carol: https://demo-carol.covehub.io

artifacts:
  alice_secret_word:
    type: static
    owner: alice
    plaintext_hash: sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03
  bob_secret_word:
    type: static
    owner: bob
    plaintext_hash: sha256:e258d248fda94c63753607f7c4494ee0fcbe92f1a76bfdac795c9d84101eb317
  alice_secret_word_transformed:
    type: dynamic
    owner: alice
  bob_secret_word_transformed:
    type: dynamic
    owner: bob
```

There is no static artifact `latest` path. Static artifacts are immutable from
the workflow's perspective and compile pins exact ciphertext paths.

## Generated Paths

`cove provision` uploads static artifacts to:

```text
v1/artifacts/<owner-domain>/<artifact-id>/sha256:<ciphertext-digest>
```

`cove compile` asks each owner service to resolve
`artifact_id + plaintext_hash` to that exact ciphertext path. The generated
`workflow.normalized.cove.yaml` includes review-facing `hub_path` metadata,
for example:

```text
v1/artifacts/demo-alice.covehub.io/alice_secret_word/sha256:<ciphertext-digest>
v1/runtime/demo-carol.covehub.io/hello_world/artifacts/alice_secret_word_transformed/latest
```

Dynamic artifacts use Carol's publisher domain because their channel belongs
to the published workflow runtime namespace.

## Runtime Verification

Compile output is review material, not a trust root. Runtime sidecars still
verify:

- baked owner identity and owner URL,
- owner domain and generated path owner segment,
- signed owner key-release response,
- ciphertext hash from the exact static path,
- decrypted plaintext hash against the authored `plaintext_hash`,
- dynamic producer certificates and channel paths.

Owner aliases like `alice` and `bob` are authoring conveniences only. Covehub
paths use domains, never `/alice/...` or `/bob/...`.

## 1. Start Covehub API/UI

Start the root Compose stack in a long-running shell or process supervisor:

```bash
cd /home/$USER/cove
docker compose up --build
```

The root Compose stack runs:

- `covehub-api` on local `127.0.0.1:3518`
- `covehub-ui` on local `127.0.0.1:3517`
- the Covehub API/UI `cloudflared` connector using the configured tunnel token

For a fresh production reset, stop the stack and remove only the production
Covehub volume:

```bash
cd /home/$USER/cove
docker compose down -v --remove-orphans
docker compose up --build
```

Do not wipe or target staging hosts (`staging.covehub.io` or
`api-staging.covehub.io`) during this run.

Verify:

```bash
curl -fsS http://127.0.0.1:3518/healthz
curl -fsS http://127.0.0.1:3517/ui-api/healthz
curl -A 'cove-runtime/0.0.1' -fsS https://api.covehub.io/healthz
curl -fsS https://covehub.io/
```

The `covehub` Cloudflare tunnel should have exactly one active replica for
this run. If another replica is attached, Cloudflare can send `api.covehub.io`
and `covehub.io` traffic to different origins, which makes the UI and API
appear out of sync. Stop old replicas or rotate the tunnel token before
publishing.

## 2. Start Parties Tunnel

Start the parties Cloudflare connector command supplied for this environment
in its own long-running shell or process supervisor.

The parties tunnel must route:

```text
demo-alice.covehub.io -> http://127.0.0.1:9600
demo-bob.covehub.io   -> http://127.0.0.1:9601
demo-carol.covehub.io -> http://127.0.0.1:9602
```

Do not add `api.covehub.io` or `covehub.io` to this parties tunnel; those use
the separate Covehub API/UI tunnel from the Compose stack.

## 3. Build And Install CLI

After the release venv is active, use `cove`, `python`, and `pip` directly.
Use a dedicated release venv for this run instead of `cli/.venv`; that verifies
the rebuilt wheel exactly as an operator would install it, while `cli/.venv` is
for local development.

```bash
cd /home/$USER/cove
rm -rf /tmp/cove-cli-wheelhouse /home/$USER/.cove-cli-release
mkdir -p /tmp/cove-cli-wheelhouse

cd /home/$USER/cove/cli
uv build --out-dir /tmp/cove-cli-wheelhouse

python3 -m venv /home/$USER/.cove-cli-release
source /home/$USER/.cove-cli-release/bin/activate
python -m pip install --upgrade pip
pip install /tmp/cove-cli-wheelhouse/cove_cli-*.whl
cove --help
```

If the runtime sidecar images were rebuilt, copy
`containers/canonical_container_digests.json` into
`cli/src/cove_cli/canonical_container_digests.json` before building the wheel.

## 4. Initialize Parties

Alice and Bob are data owners only for this demo. Leave Phala and Docker
credential prompts blank for them.

```bash
rm -rf /home/$USER/.alice_cove /home/$USER/.bob_cove /home/$USER/.carol_cove

cove --cove-home /home/$USER/.alice_cove init
cove --cove-home /home/$USER/.bob_cove init
cove --cove-home /home/$USER/.carol_cove init
```

Use these values:

```text
Common Covehub server URL: https://api.covehub.io

Alice owner server URL: https://demo-alice.covehub.io
Alice Phala Cloud API key: leave blank
Alice Docker credentials: leave blank

Bob owner server URL: https://demo-bob.covehub.io
Bob Phala Cloud API key: leave blank
Bob Docker credentials: leave blank

Carol owner server URL: https://demo-carol.covehub.io
Carol Phala Cloud API key: provide Carol's Phala Cloud API key
Carol Docker registry username: covehub
Carol Docker registry access token: provide the covehub Docker Hub access token
Carol Docker registry: leave blank for Docker Hub
```

## 5. Start Owner Services

Start each owner service in a separate long-running shell or process
supervisor. Activate the release venv in each shell, then run one service:

```bash
source /home/$USER/.cove-cli-release/bin/activate
cove --cove-home /home/$USER/.alice_cove start 9600
```

```bash
source /home/$USER/.cove-cli-release/bin/activate
cove --cove-home /home/$USER/.bob_cove start 9601
```

```bash
source /home/$USER/.cove-cli-release/bin/activate
cove --cove-home /home/$USER/.carol_cove start 9602
```

Verify identity documents through the public routes:

```bash
curl -A 'cove-runtime/0.0.1' -fsS https://demo-alice.covehub.io/identity
curl -A 'cove-runtime/0.0.1' -fsS https://demo-bob.covehub.io/identity
curl -A 'cove-runtime/0.0.1' -fsS https://demo-carol.covehub.io/identity
```

The local owner services are plain HTTP. The public URLs are HTTPS because
Cloudflare secures the domain layer.

## 6. Build Runtime And Workload Images

Rebuild and push first-party sidecars when their code changed:

```bash
cd /home/$USER/cove/containers
./scripts/build_all_containers.sh --docker-namespace covehub --tag <release-tag> --push
cp canonical_container_digests.json /home/$USER/cove/cli/src/cove_cli/canonical_container_digests.json
```

Then rebuild the CLI wheel as shown above.

Build and push hello-world workload images when workload code changed:

```bash
cd /home/$USER/cove/demos/hello_world
./scripts/build_all_containers.sh --docker-namespace covehub --tag <release-tag> --push
```

This rewrites the hello-world canonical digest file and node compose image
refs.

## 7. Provision Static Artifacts

Alice and Bob provision only their static inputs:

```bash
source /home/$USER/.cove-cli-release/bin/activate

cove --cove-home /home/$USER/.alice_cove provision \
  alice_secret_word /home/$USER/cove/demos/hello_world/fixtures/alice_secret_word.txt

cove --cove-home /home/$USER/.bob_cove provision \
  bob_secret_word /home/$USER/cove/demos/hello_world/fixtures/bob_secret_word.txt
```

Static artifacts upload to exact ciphertext paths:

```text
v1/artifacts/<owner-domain>/<artifact-id>/sha256:<ciphertext-digest>
```

There is no static `latest` path and no authored `hub_path` in
`workflow.cove.yaml`.

## 8. Carol Compiles And Publishes

Carol is the publisher/deployer:

```bash
source /home/$USER/.cove-cli-release/bin/activate

cove --cove-home /home/$USER/.carol_cove check \
  /home/$USER/cove/demos/hello_world/workflow/workflow.cove.yaml

cove --cove-home /home/$USER/.carol_cove compile \
  /home/$USER/cove/demos/hello_world/workflow/workflow.cove.yaml

cove --cove-home /home/$USER/.carol_cove push \
  /home/$USER/cove/demos/hello_world/workflow/workflow.cove.yaml
```

Compile asks Alice and Bob's owner services to resolve
`artifact_id + plaintext_hash` to exact generated static paths. The normalized
workflow contains review-facing `hub_path` metadata such as:

```text
v1/artifacts/demo-alice.covehub.io/alice_secret_word/sha256:<ciphertext-digest>
v1/runtime/demo-carol.covehub.io/hello_world/artifacts/alice_secret_word_transformed/latest
```

The published workflow ref is:

```text
demo-carol.covehub.io/hello_world
```

## 9. Alice And Bob Approve

Alice and Bob inspect Carol's published workflow and approve only their
expected accesses:

```bash
cove --cove-home /home/$USER/.alice_cove provision inspect \
  demo-carol.covehub.io/hello_world

cove --cove-home /home/$USER/.bob_cove provision inspect \
  demo-carol.covehub.io/hello_world
```

Owner services must keep running. Phala sidecars call the public owner URLs at
runtime for key release and verify signed owner responses against the baked
owner identities.

## 10. Carol Deploys To Phala

Delete any old Phala CVMs for this workflow if name collisions exist, then:

```bash
cove --cove-home /home/$USER/.carol_cove deploy \
  demo-carol.covehub.io/hello_world \
  --phala-instance-type tdx.medium \
  --phala-disk-size-gb 40 \
  --phala-public-logs \
  --phala-public-sysinfo
```

Record the `cvm_id` values from the summary.

## 11. Verify Runtime

Use Carol's configured Phala API key:

```bash
export PHALA_CLOUD_API_KEY="$(
  python - <<'PY'
import os
from cove_cli.config import ensure_local_config
print(ensure_local_config(os.path.expanduser("~/.carol_cove")).phala_cloud_api_key)
PY
)"
```

Check CVM status:

```bash
for id in <alice-cvm-id> <bob-cvm-id> <character-set-cvm-id> <final-cvm-id>; do
  npx --yes phala cvms get "$id" --json \
    | jq -r '"\(.id)\t\(.name)\t\(.status)\tservices=\(.services|length)\tboot_error=\(.boot_error // "")"'
done
```

Check runtime certificates:

```bash
cove hub inspect \
  v1/runtime/demo-carol.covehub.io/hello_world/certificates/final_server/latest \
  --server-url https://api.covehub.io
```

Get the final service endpoint from the final CVM status output and use the
TLS-passthrough form by appending `s` to the `-18443` port segment:

```bash
FINAL_URL="https://<final-app-id>-18443.dstack-pha-prod5.phala.network"
FINAL_URL="${FINAL_URL/-18443./-18443s.}"

curl -kfsS "${FINAL_URL}/health"
curl -kfsS "${FINAL_URL}/message"
```
