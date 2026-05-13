# End-To-End Hello World Runbook

This is the operational counterpart to [../hello_world.md](../hello_world.md):
how to bring up Covehub, two owner services, runtime sidecars, the CLI, and
the demo workflow against Phala Cloud, then verify every certificate and the
final RA-TLS service.

The detail-heavy steps live in the focused operations docs; this runbook
links to them and keeps only the orchestration order, the local environment
setup, and the hello-world-specific identifiers.

The owner-service hostnames below are CoveHub-controlled demo owner URLs.
Their hostnames are the canonical Covehub owner and publisher namespaces, so
keep them consistent with the checked-in workflow when running this runbook.

## Reference Identifiers

- Covehub API: `https://api.covehub.io`
- Covehub UI: `https://covehub.io`
- Covehub local API smoke URL: `http://127.0.0.1:3518/healthz`
- Covehub local UI smoke URL: `http://127.0.0.1:3517`
- Covehub Compose project: `/home/$USER/mats/cove`
- Covehub Docker state: `covehub-data` Compose volume
- Alice Cove home: `/home/$USER/.alice_cove`
- Bob Cove home: `/home/$USER/.bob_cove`
- Alice owner service: `https://cove-demo-hello-world-alice-provisioning.covehub.io` on local port `9000`
- Bob owner service: `https://cove-demo-hello-world-bob-provisioning.covehub.io` on local port `9001`
- Demo workflow: `/home/$USER/mats/cove/demos/hello_world/workflow/workflow.cove.yaml`

## 1. Local Prerequisites

Confirm the local toolchain:

```bash
docker --version
python3 --version
uv --version
node --version
npx --version
jq --version
curl --version
lsof -v >/dev/null
rg --version
```

Authenticate Docker locally for image builds and pushes:

```bash
docker login
```

Authenticate the Phala CLI through `npx`:

```bash
export PHALA_CLOUD_API_KEY=<alice-phala-cloud-api-key>
npx --yes phala status
npx --yes phala instance-types
```

Prepare Cloudflare Tunnel tokens for the Covehub stack and owner-service
public hostnames:

```bash
export COVEHUB_CLOUDFLARED_TOKEN=<covehub-token>
export ALICE_CLOUDFLARED_TOKEN=<alice-token>
export BOB_CLOUDFLARED_TOKEN=<bob-token>
```

Prepare a Docker Hub read-only access token for `cove init`. This is the
credential Cove ships to Phala CVMs as encrypted envs, separate from the
local `docker login` above. See
[phala_deploy.md](phala_deploy.md) §"Docker Hub authentication is required"
for why this is mandatory rather than optional.

## 2. Clean Start

This step clears the Docker-managed Covehub server state and Alice/Bob local
owner state. It does not remove unrelated containers, volumes, images, or
non-Cove services.

Stop any foreground `cove start` processes from previous runs. Then stop
the Covehub Compose stack and remove its volumes:

```bash
cd /home/$USER/mats/cove
docker compose down -v --remove-orphans
```

Remove owner-service tunnel containers from previous runs:

```bash
docker rm -f alice-cloudflared bob-cloudflared 2>/dev/null || true
```

Free the Cove demo ports:

```bash
for port in 3517 3518 9000 9001; do
  pids="$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "${pids}" ]; then
    kill ${pids}
  fi
done
```

Delete the Alice/Bob Cove homes:

```bash
rm -rf /home/$USER/.alice_cove /home/$USER/.bob_cove
```

Confirm:

```bash
test ! -e /home/$USER/.alice_cove
test ! -e /home/$USER/.bob_cove
! docker volume ls --format '{{.Name}}' | rg '^covehub_'
```

## 3. Covehub API, UI, And Tunnel

Start the Docker Compose stack that runs the Covehub API, public UI, and
Cloudflare Tunnel connector. Full procedure:
[covehub_server.md](covehub_server.md).

Create `cove/.env` with the tunnel token and local smoke-test ports:

```bash
cd /home/$USER/mats/cove
umask 077
{
  printf 'CLOUDFLARED_TOKEN=%s\n' "${COVEHUB_CLOUDFLARED_TOKEN}"
  printf 'COVEHUB_API_PORT=3518\n'
  printf 'COVEHUB_UI_PORT=3517\n'
  printf 'COVE_UI_CACHE_TTL_SECONDS=5\n'
  printf 'COVE_UI_PREVIEW_BYTES=4096\n'
} > .env
```

In Cloudflare Zero Trust, configure the Covehub tunnel public hostname routes
to point at the Docker service names on the Compose network:

```text
covehub.io      -> http://covehub-ui:8080
api.covehub.io  -> http://covehub-api:8000
```

Do not use `localhost:3517` or `localhost:3518` for these routes when
`cloudflared` is running inside Compose; inside that container, `localhost`
means the tunnel container itself and produces public 502s.

Start the stack:

```bash
docker compose up -d --build
```

Verify local API and UI smoke endpoints:

```bash
docker compose ps
curl -fsS http://127.0.0.1:3518/healthz
curl -fsS http://127.0.0.1:3517/ui-api/healthz
curl -fsS http://127.0.0.1:3517/ui-api/summary | jq
```

Verify public API and UI reachability under normal WebPKI:

```bash
curl -A 'cove-runtime/0.0.1' -fsS https://api.covehub.io/healthz
curl -fsS https://covehub.io/
```

On a fresh Docker volume, the UI summary should show zero workflows until
Alice and Bob provision artifacts and Alice publishes the workflow later in
this runbook.

## 4. Runtime Sidecars

Build, push, and pin the canonical first-party sidecars. Full procedure:
[runtime_release.md](runtime_release.md).

```bash
docker login
cd /home/$USER/mats/cove/containers
./scripts/build_all_containers.sh --docker-namespace covehub --tag <release-tag> --push
```

`--push` rewrites `containers/canonical_container_digests.json` in place.

## 5. CLI Build

Carry the refreshed digest policy into the CLI and build the wheel. Full
procedure: [cli_release.md](cli_release.md).

```bash
cd /home/$USER/mats/cove
cp containers/canonical_container_digests.json cli/canonical_container_digests.json
cp containers/canonical_container_digests.json cli/src/cove_cli/canonical_container_digests.json

rm -rf /tmp/cove-cli-wheelhouse
mkdir -p /tmp/cove-cli-wheelhouse
cd /home/$USER/mats/cove/cli
uv build --out-dir /tmp/cove-cli-wheelhouse

rm -rf /home/$USER/.cove-cli-release
python3 -m venv /home/$USER/.cove-cli-release
/home/$USER/.cove-cli-release/bin/python -m pip install --upgrade pip
/home/$USER/.cove-cli-release/bin/python -m pip install /tmp/cove-cli-wheelhouse/cove_cli-*.whl
/home/$USER/.cove-cli-release/bin/cove --help
```

All Alice and Bob commands below use this installed `cove` executable.

## 6. Demo Workload Containers

Build and push the hello-world workload images:

```bash
cd /home/$USER/mats/cove/demos/hello_world
./scripts/build_all_containers.sh --docker-namespace covehub --tag <release-tag> --push
```

`--push` refreshes the demo workload digest policy and rewrites the
workflow node compose files to point at the pushed digests:

```text
/home/$USER/mats/cove/demos/hello_world/canonical_container_digests.json
/home/$USER/mats/cove/demos/hello_world/workflow/nodes/*.compose.yaml
```

Confirm the pinned refs:

```bash
cd /home/$USER/mats/cove/demos/hello_world
rg -n 'image: "covehub/cove-demo-hello-world-.*@sha256:' workflow/nodes/*.compose.yaml
cat canonical_container_digests.json
```

## 7. Owner Services

Configure two Cloudflare Tunnel routes (one per owner) and start each
owner's local service. Full procedure: [owner_services.md](owner_services.md).

Cloudflare routes:

```text
cove-demo-hello-world-alice-provisioning.covehub.io -> https://127.0.0.1:9000
cove-demo-hello-world-bob-provisioning.covehub.io   -> https://127.0.0.1:9001
```

In Cloudflare Zero Trust, configure each owner tunnel under **Networks >
Tunnels > <tunnel> > Public Hostnames**.

Alice:

```text
Public hostname: cove-demo-hello-world-alice-provisioning.covehub.io
Service type:    HTTPS
Service URL:     127.0.0.1:9000
```

Bob:

```text
Public hostname: cove-demo-hello-world-bob-provisioning.covehub.io
Service type:    HTTPS
Service URL:     127.0.0.1:9001
```

For both public hostnames, open **Additional application settings → TLS** and
enable **No TLS Verify**. The owner services intentionally serve local HTTPS
with owner-generated certificates; if this setting is omitted, Cloudflare will
return `502` with an origin error like `x509: certificate signed by unknown
authority`.

Initialize Alice and Bob:

```bash
/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.alice_cove init
/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.bob_cove init
```

Use these prompt values:

```text
Covehub server URL:                            https://api.covehub.io
Alice owner server URL:                        https://cove-demo-hello-world-alice-provisioning.covehub.io
Alice Phala Cloud API key:                     <alice-phala-cloud-api-key>
Alice Phala Docker registry username:          <dockerhub-username>
Alice Phala Docker registry access token:      <dockerhub-read-only-token>
Alice Phala Docker registry:                   leave blank for Docker Hub
Bob owner server URL:                          https://cove-demo-hello-world-bob-provisioning.covehub.io
Bob Phala Cloud API key:                       leave blank unless Bob will deploy
Bob Phala Docker registry auth:                leave blank unless Bob will deploy
```

There is no Covehub registration or login prompt. If an old Cove home still
contains `username` or `access_token`, remove that Cove home and run
`cove init` again.

Run the two owner services in separate terminals:

```bash
/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.alice_cove start 9000
```

```bash
/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.bob_cove start 9001
```

Run the two owner Cloudflare Tunnel containers in separate terminals:

```bash
docker run --rm --network host --name alice-cloudflared \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run \
  --token "${ALICE_CLOUDFLARED_TOKEN}"
```

```bash
docker run --rm --network host --name bob-cloudflared \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run \
  --token "${BOB_CLOUDFLARED_TOKEN}"
```

Verify Covehub health and both owner identities under normal WebPKI:

```bash
curl -A 'cove-runtime/0.0.1' -fsS https://api.covehub.io/healthz
curl -A 'cove-runtime/0.0.1' -fsS https://cove-demo-hello-world-alice-provisioning.covehub.io/identity
curl -A 'cove-runtime/0.0.1' -fsS https://cove-demo-hello-world-bob-provisioning.covehub.io/identity
```

The `/identity` documents must include `version: 2`, `owner_url`,
`owner_domain`, `owner_public_key_pem`, and `owner_public_key_sha256`, and
must not include owner TLS certificate fields.

## 8. Provision, Compile, Push

This step repopulates the fresh Docker-backed Covehub. Re-run it whenever you
start from empty Docker volumes, because the server has no artifacts,
workflow bundle, runtime certificates, or runtime artifacts yet.

Each owner provisions their static artifact:

```bash
/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.alice_cove provision \
  alice_secret_word /home/$USER/mats/cove/demos/hello_world/fixtures/alice_secret_word.txt

/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.bob_cove provision \
  bob_secret_word /home/$USER/mats/cove/demos/hello_world/fixtures/bob_secret_word.txt
```

The demo workflow references:

```text
v1/artifacts/alice/alice_secret_word/latest
v1/artifacts/bob/bob_secret_word/latest
```

Alice (the publisher) checks, compiles, and uploads the workflow:

```bash
/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.alice_cove check \
  /home/$USER/mats/cove/demos/hello_world/workflow/workflow.cove.yaml

/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.alice_cove compile \
  /home/$USER/mats/cove/demos/hello_world/workflow/workflow.cove.yaml

/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.alice_cove push \
  /home/$USER/mats/cove/demos/hello_world/workflow/workflow.cove.yaml
```

`cove compile` fetches each `<owner_url>/identity`, verifies the owner
signature under normal TLS, and bakes the owner public key into the
generated sidecar config under
`/home/$USER/mats/cove/demos/hello_world/workflow/build/`.

## 9. Owner Approval

Each owner pulls, inspects, and approves the published workflow:

```bash
/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.alice_cove provision inspect cove-demo-hello-world-alice-provisioning.covehub.io/hello_world
/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.bob_cove   provision inspect cove-demo-hello-world-alice-provisioning.covehub.io/hello_world
```

Answer `y` only for nodes whose generated compose and artifact access are
expected (see [../hello_world.md](../hello_world.md) §"Owner approval
model"). The owner services must stay running after approval — Phala
runtime sidecars call the public owner URLs for key release during the
deploy.

Starting from a fresh Covehub requires this approval pass even if the local
owner homes previously approved an older server's workflow. The pulled bundle,
compose hashes, and allow rules must line up with what Alice just republished.

## 10. Phala Deploy

Full deploy semantics, design constraints, and flag reference:
[phala_deploy.md](phala_deploy.md).

If a previous run left CVMs from this workflow, delete them in Phala first;
Phala does not allow two CVMs with the same name. Then:

```bash
/home/$USER/.cove-cli-release/bin/cove --cove-home /home/$USER/.alice_cove deploy \
  cove-demo-hello-world-alice-provisioning.covehub.io/hello_world \
  --phala-instance-type tdx.medium \
  --phala-disk-size-gb 40 \
  --phala-public-logs \
  --phala-public-sysinfo
```

Record the per-node `cvm_id`, `app_id`, and Phala-side compose hash from
the deploy summary. The `cvm_id` is what the Phala CLI uses for status,
serial logs, and container inspection. The Phala compose hash is **not**
the hash Cove runtime certificates attest — see
[phala_deploy.md](phala_deploy.md) §"Reviewed compose hash ≠
Phala-translated compose hash".

## 11. Monitor And Verify

Export Alice's Phala API key from her Cove home:

```bash
export PHALA_CLOUD_API_KEY="$(
  /home/$USER/.cove-cli-release/bin/python - <<'PY'
import os
from cove_cli.config import ensure_local_config
print(ensure_local_config(os.path.expanduser("~/.alice_cove")).phala_cloud_api_key)
PY
)"
```

Poll all four CVMs until they report services or stop:

```bash
for id in <alice-cvm-id> <bob-cvm-id> <character-set-cvm-id> <final-cvm-id>; do
  npx --yes phala cvms get "$id" --json \
    | jq -r '"\(.id)\t\(.name)\t\(.status)\tservices=\(.services|length)\tboot_error=\(.boot_error // "")"'
done
```

For serial logs (CVM still booting, or `phala ps` empty):

```bash
npx --yes phala logs --cvm-id <cvm-id> --serial -n 200
```

For container logs (services visible):

```bash
npx --yes phala ps <cvm-id>
npx --yes phala logs --cvm-id <cvm-id> <service-name> -n 200 --stderr
```

For the bundle-driven runtime certificate verification script (which reads
expected compose hashes from the pulled bundle so it stays correct as the
workflow grows or shrinks), see [phala_deploy.md](phala_deploy.md)
§"Runtime Certificate Verification".

For the RA-TLS check on `final_server`, including the Phala TLS-passthrough
URL transform, see [phala_deploy.md](phala_deploy.md) §"Final Service
RA-TLS Verification".

## 12. Optional Server State Inspection

```bash
cd /home/$USER/mats/cove

docker compose exec -T covehub-api find /var/lib/covehub/data/artifacts -type f | sort
docker compose exec -T covehub-api find /var/lib/covehub/data/workflows -type f | sort
docker compose exec -T covehub-api find /var/lib/covehub/data/runtime   -type f | sort
```
