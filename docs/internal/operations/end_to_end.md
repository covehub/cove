# End-To-End Hello World Runbook

This runbook brings up production Covehub (`api.covehub.io` and
`covehub.io`), the three public demo owner services, and the hello-world
workflow on Phala. It assumes the repo is checked out at `/home/$USER/cove`.

## Reference Identifiers

- Covehub API: `https://api.covehub.io`
- Covehub UI: `https://covehub.io`
- Alice owner URL: `https://alice.cove-demo-parties.covehub.io`
- Bob owner URL: `https://bob.cove-demo-parties.covehub.io`
- Carol owner/publisher URL: `https://carol.cove-demo-parties.covehub.io`
- Alice local owner port: `9600`
- Bob local owner port: `9601`
- Carol local owner port: `9602`
- Workflow ref: `carol.cove-demo-parties.covehub.io/hello_world`
- Workflow file: `/home/$USER/cove/demos/hello_world/workflow/workflow.cove.yaml`

`~/.cloudflare_parties_token` contains the full `cloudflared tunnel run ...`
command for the Alice/Bob/Carol parties tunnel. The Covehub API/UI tunnel is
separate and is configured through `CLOUDFLARED_TOKEN` in `/home/$USER/cove/.env`.

## 1. Start Covehub API/UI

Use the first window in the `cove` tmux session:

```bash
tmux new-window -t cove -n covehub 'cd /home/$USER/cove && docker compose up --build'
```

The root Compose stack runs:

- `covehub-api` on local `127.0.0.1:3518`
- `covehub-ui` on local `127.0.0.1:3517`
- the Covehub API/UI `cloudflared` connector using `.env`

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

## 2. Start Parties Tunnel

Use the second window in the `cove` tmux session:

```bash
tmux new-window -t cove -n parties 'cd /home/$USER/cove && bash -lc "$(cat ~/.cloudflare_parties_token)"'
```

The parties tunnel must route:

```text
alice.cove-demo-parties.covehub.io -> http://127.0.0.1:9600
bob.cove-demo-parties.covehub.io   -> http://127.0.0.1:9601
carol.cove-demo-parties.covehub.io -> http://127.0.0.1:9602
```

Do not add `api.covehub.io` or `covehub.io` to this parties tunnel; those use
the separate Covehub API/UI tunnel from the Compose stack.

## 3. Build And Install CLI

After the release venv is active, use `cove`, `python`, and `pip` directly.

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

Alice owner server URL: https://alice.cove-demo-parties.covehub.io
Alice Phala Cloud API key: leave blank
Alice Docker credentials: leave blank

Bob owner server URL: https://bob.cove-demo-parties.covehub.io
Bob Phala Cloud API key: leave blank
Bob Docker credentials: leave blank

Carol owner server URL: https://carol.cove-demo-parties.covehub.io
Carol Phala Cloud API key: <phala-api-key>
Carol Docker registry username: <dockerhub-username>
Carol Docker registry access token: <dockerhub-read-token>
Carol Docker registry: leave blank for Docker Hub
```

## 5. Start Owner Services

Use separate tmux windows, not panes:

```bash
tmux new-window -t cove -n alice-owner 'source /home/$USER/.cove-cli-release/bin/activate && cove --cove-home /home/$USER/.alice_cove start 9600'
tmux new-window -t cove -n bob-owner 'source /home/$USER/.cove-cli-release/bin/activate && cove --cove-home /home/$USER/.bob_cove start 9601'
tmux new-window -t cove -n carol-owner 'source /home/$USER/.cove-cli-release/bin/activate && cove --cove-home /home/$USER/.carol_cove start 9602'
```

Verify identity documents through the public routes:

```bash
curl -A 'cove-runtime/0.0.1' -fsS https://alice.cove-demo-parties.covehub.io/identity
curl -A 'cove-runtime/0.0.1' -fsS https://bob.cove-demo-parties.covehub.io/identity
curl -A 'cove-runtime/0.0.1' -fsS https://carol.cove-demo-parties.covehub.io/identity
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
v1/artifacts/alice.cove-demo-parties.covehub.io/alice_secret_word/sha256:<ciphertext-digest>
v1/runtime/carol.cove-demo-parties.covehub.io/hello_world/artifacts/alice_secret_word_transformed/latest
```

The published workflow ref is:

```text
carol.cove-demo-parties.covehub.io/hello_world
```

## 9. Alice And Bob Approve

Alice and Bob inspect Carol's published workflow and approve only their
expected accesses:

```bash
cove --cove-home /home/$USER/.alice_cove provision inspect \
  carol.cove-demo-parties.covehub.io/hello_world

cove --cove-home /home/$USER/.bob_cove provision inspect \
  carol.cove-demo-parties.covehub.io/hello_world
```

Owner services must keep running. Phala sidecars call the public owner URLs
at runtime for key release and verify signed owner responses against the
baked owner identities.

## 10. Carol Deploys To Phala

Delete any old Phala CVMs for this workflow if name collisions exist, then:

```bash
cove --cove-home /home/$USER/.carol_cove deploy \
  carol.cove-demo-parties.covehub.io/hello_world \
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
  v1/runtime/carol.cove-demo-parties.covehub.io/hello_world/certificates/final_server/latest \
  --server-url https://api.covehub.io
```

For dependency-certificate verification scripts and final RA-TLS verification,
see [phala_deploy.md](phala_deploy.md).
