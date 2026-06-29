# Phala Cloud Deployment

This runbook describes how `cove deploy` interacts with Phala Cloud, the
non-obvious constraints that shape the deploy payload, and how to monitor
and verify the resulting CVMs.

## Deploy Model

Phala CVM creation is a two-phase API flow:

1. `provision_cvm` reserves resources and returns an `app_id` plus a Phala
   `compose_hash`.
2. `commit_cvm_provision` commits that `app_id` and `compose_hash` to create
   the CVM.

`cove deploy` keeps that model intact for the default create-new-CVM path.
It pulls the reviewed workflow bundle, translates each per-node compose file
into the shape Phala accepts, calls `provision_cvm` for every selected node,
then calls `commit_cvm_provision`.

For manual one-at-a-time sequencing, `cove deploy --workflow-node <node>
--phala-reuse-cvm-id <cvm_id>` updates one existing CVM instead of reserving
a new one. That path uses Phala's two-phase compose-file update flow:
`provision_cvm_compose_file_update` followed by
`commit_cvm_compose_file_update`. It still uses Cove's normal translated
node compose and staged dependency-certificate wait. Reuse preserves the
existing CVM encrypted environment; create a fresh CVM when Docker registry
credentials need to be installed or rotated.

## Deploy-Time Prerequisites And Non-Obvious Constraints

These are the design facts that shape every Cove-on-Phala deployment.
Skipping any of them produces failures that look like ordinary CVM boot
problems but are actually configuration errors at the deploy boundary.

### 1. Docker Hub authentication is required

Phala worker IPs share an anonymous Docker Hub pull-rate budget. A single
non-trivial workflow exhausts it during one deploy and the CVM fails to boot
with:

```text
toomanyrequests: You have reached your unauthenticated pull rate limit
```

Cove ships registry credentials to Phala as encrypted envs after
`provision_cvm` returns `app_env_encrypt_pubkey`. The names are
`DSTACK_DOCKER_USERNAME`, `DSTACK_DOCKER_PASSWORD`, and (for non-Docker-Hub
registries) `DSTACK_DOCKER_REGISTRY`. Provide these at `cove init` for the
deploying Cove home, or override per-deploy with `--phala-docker-username`,
`--phala-docker-access-token`, and `--phala-docker-registry`. A read-only
Docker Hub access token is sufficient and recommended.

### 2. Encrypted env names must be allowlisted

dstack decrypts the env blob at CVM start, but it drops any variable that
isn't listed in `compose_file.allowed_envs` *before* the pre-launch Docker
login step runs. Cove's deploy translator sends both fields together; anyone
modifying the translator must keep them in sync. The serial-log signature of
this bug is:

```text
Skipping unauthorized environment variable: DSTACK_DOCKER_USERNAME
Skipping unauthorized environment variable: DSTACK_DOCKER_PASSWORD
```

followed by anonymous pulls and a `toomanyrequests` failure. The fix is in
the deploy payload, not in the workflow — recompiling, repushing, or
reapproving will not change the outcome.

### 3. `--phala-instance-type` is required for new CVMs

Phala's provision API rejects requests that omit both `instance_type` and the
older `vcpu`/`memory` pair. Cove has no built-in default; the flag is
mandatory when creating a new CVM and chosen explicitly per deploy. Use
`tdx.medium` for small hello-world-class workflows; use a GPU instance type
(e.g. `h200.small`) when the workload images include GPU software. The
workload image itself decides whether the GPU is used; switching instance
types does not change Cove workflow semantics.

When reusing an existing CVM with `--phala-reuse-cvm-id`, `--phala-instance-type`
is not required because the existing CVM already fixes the hardware shape.
The existing CVM must already have any Docker registry credentials needed for
image pulls; Cove does not rotate encrypted env vars during compose-file
updates.

### 4. Resource changes do not invalidate review

Instance type, region, OS image, node, disk size, listed/visibility flags,
public-logs, and public-sysinfo are all deploy-time and live **outside** the
reviewed compose hash. Changing any of them does not require rebuilding
workload containers, rebuilding first-party runtime sidecars, recompiling
the workflow, repushing it to Covehub, repulling it, or repeating owner
approval. Failed CVMs caused by registry auth or resource selection can be
deleted and the workflow redeployed with adjusted flags.

### 5. Reviewed compose hash ≠ Phala-translated compose hash

Phala's `provision_cvm` returns its own `compose_hash` over the deploy
payload (which includes `allowed_envs`, the runner type, etc.). Cove runtime
certificates **do not** attest that hash. They attest the
`compose.generated.sha256` recorded in the pulled bundle — the hash owners
saw and approved.

When verifying certificates, derive expected compose hashes from the bundle
manifest and the per-node `compose.generated.sha256`, not from the
`compose deploy` summary. The Phala-side `compose_hash` is useful only for
matching CVMs in Phala's UI and for the `commit_cvm_provision` payload
itself.

### 6. RA-TLS requires Phala's TLS-passthrough URL form

Phala's normal app endpoint terminates TLS at the gateway and serves a
WebPKI certificate from the gateway, *not* the in-enclave RA-TLS
certificate. For an RA-TLS client to receive the session certificate that's
bound to the node certificate in Covehub, the published port segment in the
URL must end with `s` — for example `-18443s.<phala-host>` instead of
`-18443.<phala-host>`. Without this, a `curl -k` against the normal endpoint
appears to succeed but the served certificate will not match the certificate
recorded in the node certificate, and the RA-TLS check will reject it.

### 7. Registry credentials never persist

`DSTACK_DOCKER_USERNAME`, `DSTACK_DOCKER_PASSWORD`, and
`DSTACK_DOCKER_REGISTRY` flow only through Phala's encrypted-env channel.
They are never written into generated compose, the published workflow
bundle, Covehub state, runtime certificates, deploy summaries, or logs.
Image pulls happen before containers start, so registry credentials cannot
live in compose env stanzas — the values would be visible in the reviewed
workflow material if they did.

## Local Prerequisites

The operator machine needs Docker (for local image builds and pushes) and
the Phala CLI invoked through `npx`:

```bash
docker --version
docker login            # required to push images during release; see runtime_release.md
export PHALA_CLOUD_API_KEY=<phala-cloud-api-key>
npx --yes phala status
npx --yes phala instance-types
```

Local `docker login` is for *pushing* images from the operator machine. It
is unrelated to the Docker Hub auth shipped to CVMs at deploy time
(prerequisite #1).

## Running A Deploy

Required form for new CVMs:

```bash
cove deploy <publisher>/<workflow_id> --phala-instance-type <type>
```

Typical CPU example:

```bash
cove --cove-home <home> deploy <publisher>/<workflow_id> \
  --phala-instance-type tdx.medium \
  --phala-disk-size-gb 40 \
  --phala-public-logs \
  --phala-public-sysinfo
```

GPU example:

```bash
cove deploy <publisher>/<workflow_id> --phala-instance-type h200.small
```

Create just one new CVM for a selected node:

```bash
cove deploy <publisher>/<workflow_id> \
  --workflow-node <node_id> \
  --phala-instance-type <type>
```

Reuse one existing CVM for a selected node:

```bash
cove deploy <publisher>/<workflow_id> \
  --workflow-node <node_id> \
  --phala-reuse-cvm-id <cvm_id>
```

Override registry credentials for one deploy:

```bash
cove deploy <publisher>/<workflow_id> \
  --phala-instance-type tdx.medium \
  --phala-docker-username <user> \
  --phala-docker-access-token <token>
```

For non-Docker-Hub registries, also pass `--phala-docker-registry <host>`.

### Deploy Flag Reference

| Cove flag | Effect / Phala payload field |
| --- | --- |
| `--workflow-node <node_id>` | selects one Cove workflow node before any Phala request |
| `--phala-reuse-cvm-id <cvm_id>` | switches from `provision_cvm`/`commit_cvm_provision` to `provision_cvm_compose_file_update`/`commit_cvm_compose_file_update` |
| `--phala-instance-type <type>` | root `instance_type` |
| `--phala-region <region>` | root `region` |
| `--phala-os-image <image>` | root `image` |
| `--phala-node-id <id>` | root `node_id` |
| `--phala-disk-size-gb <gb>` | root `disk_size` |
| `--phala-listed` / `--no-phala-listed` | root `listed` |
| `--phala-public-logs` / `--no-phala-public-logs` | `compose_file.public_logs` |
| `--phala-public-sysinfo` / `--no-phala-public-sysinfo` | `compose_file.public_sysinfo` |
| `--phala-docker-username <name>` | `compose_file.allowed_envs` plus encrypted `DSTACK_DOCKER_USERNAME` |
| `--phala-docker-access-token <token>` | `compose_file.allowed_envs` plus encrypted `DSTACK_DOCKER_PASSWORD` |
| `--phala-docker-registry <registry>` | `compose_file.allowed_envs` plus encrypted `DSTACK_DOCKER_REGISTRY` |

### Provision Payload Shape

```json
{
  "name": "<cvm-name>",
  "instance_type": "tdx.medium",
  "disk_size": 40,
  "compose_file": {
    "runner": "docker-compose",
    "name": "<cvm-name>",
    "public_logs": true,
    "public_sysinfo": true,
    "allowed_envs": [
      "DSTACK_DOCKER_USERNAME",
      "DSTACK_DOCKER_PASSWORD"
    ],
    "docker_compose_file": "services:\n  ..."
  }
}
```

### Commit Payload Shape

```json
{
  "app_id": "<app-id-from-provision>",
  "compose_hash": "<compose-hash-from-provision>",
  "env_keys": [
    "DSTACK_DOCKER_USERNAME",
    "DSTACK_DOCKER_PASSWORD"
  ],
  "encrypted_env": "<client-side-encrypted-env-blob>"
}
```

### Redeploy Notes

- Phala does not allow two CVMs with the same name. Delete a previous
  failed CVM before redeploying the same workflow.
- Changing only registry auth or resource flags requires deleting failed
  CVMs and rerunning `cove deploy`. It does not require recompile, repush,
  repull, or reapproval.

## Post-Deploy Monitoring

`cove deploy` prints a deployment summary per node: deployment name,
`cvm_id`, `app_id`, status, and Phala compose hash. Record at least the
`cvm_id` and `app_id`; the Phala CLI uses `cvm_id` for status, serial logs,
container lists, and container logs.

Export the Phala API key from the Cove home used to deploy:

```bash
export PHALA_CLOUD_API_KEY="$(
  /home/$USER/.cove-cli-release/bin/python - <<'PY'
import os
from cove_cli.config import ensure_local_config
print(ensure_local_config(os.path.expanduser("<cove-home>")).phala_cloud_api_key)
PY
)"
```

Poll CVM status:

```bash
for id in <cvm-id-1> <cvm-id-2> ...; do
  npx --yes phala cvms get "$id" --json \
    | jq -r '"\(.id)\t\(.name)\t\(.status)\tservices=\((.services // [])|length)\tallowed_envs=\((.compose_file.allowed_envs // [])|join(","))\tboot_error=\(.boot_error // "")"'
done
```

The Phala CLI returns CVM fields at the top level of the JSON object, not
under `.data`. Common transient states:

- `status: processing`, no services — VM is booting or pulling images.
- `status: stopped`, `services: 0`, no `boot_error` — the compose
  pre-launch or initial `docker compose` startup failed; serial logs are
  the source of truth.

Use serial logs while the CVM is booting or when `phala ps` shows no
containers:

```bash
npx --yes phala logs --cvm-id <cvm-id> --serial -n 200
```

Use container inspection once services are visible:

```bash
npx --yes phala ps <cvm-id>
npx --yes phala logs --cvm-id <cvm-id> <service-name> -n 200 --stderr
```

### Reading Serial Logs For Auth Failures

A successful registry-auth path produces:

```text
Docker credentials found
Docker login successful: docker.io
```

If serial logs instead show:

```text
Skipping unauthorized environment variable: DSTACK_DOCKER_USERNAME
Skipping unauthorized environment variable: DSTACK_DOCKER_PASSWORD
```

the deploy payload is missing the `compose_file.allowed_envs` entries.
This is a deploy translator bug or a stale CLI install; fix at the deploy
side, delete failed CVMs, redeploy.

If serial logs show `Docker login failed`, the env names were authorized
but the credentials are wrong. Rerun `cove init` or pass `--phala-docker-*`
overrides on the next deploy.

## Runtime Certificate Verification

Every node publishes its runtime certificate to Covehub at:

```text
<covehub-url>/v1/runtime/<publisher>/<workflow_id>/certificates/<node_id>/latest
```

HTTP `404` means the node has not published its certificate yet. HTTP `200`
means Covehub accepted the upload and verified the attestation headers,
certificate body hash, quoted certificate body hash, node ID, workflow ID,
and compose hash.

Independently re-check certificate bodies and Phala/dstack attestations from
the repo checkout. The script reads expected compose hashes from the pulled
bundle (`bundle.manifest.json` plus per-node `compose.generated.sha256`) so
it stays correct as the workflow gains or loses nodes:

```bash
export COVEHUB_URL="<covehub-url>"
export PUBLISHER="<publisher>"
export WORKFLOW_ID="<workflow_id>"
export BUNDLE_ROOT="<cove-home>/materialized_workflows/${PUBLISHER}/${WORKFLOW_ID}"

PYTHONPATH=/home/$USER/cove/cove_container_runtime/src \
/home/$USER/.cove-cli-release/bin/python - <<'PY'
import json
import os
import urllib.request
from pathlib import Path

from cove_container_runtime.certificates import verify_node_certificate

server = os.environ["COVEHUB_URL"].rstrip("/")
publisher = os.environ["PUBLISHER"]
workflow_id = os.environ["WORKFLOW_ID"]
bundle_root = Path(os.environ["BUNDLE_ROOT"])
manifest = json.loads((bundle_root / "bundle.manifest.json").read_text())

for node in manifest["nodes"]:
    node_id = node["node_id"]
    compose_hash = (bundle_root / "nodes" / node_id / "compose.generated.sha256").read_text().strip()
    url = f"{server}/v1/runtime/{publisher}/{workflow_id}/certificates/{node_id}/latest"
    request = urllib.request.Request(url, headers={"User-Agent": "cove-runtime/0.0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        certificate = json.load(response)
    verify_node_certificate(
        certificate,
        expected_workflow_id=workflow_id,
        expected_node_name=node_id,
        expected_generated_node_compose_hash=compose_hash,
    )
    body = certificate["certificate_body"]
    print(
        node_id,
        certificate["certificate_body_hash"],
        certificate["attestation_bundle"]["format"],
        body["generated_node_compose_hash"],
    )
PY
```

For nodes with dynamic outputs, Covehub should also have the runtime
artifacts:

```bash
for artifact in <artifact-name-1> <artifact-name-2>; do
  /usr/bin/curl -sS -o /dev/null -w "%{http_code} ${artifact}\n" \
    "<covehub-url>/v1/runtime/<publisher>/<workflow_id>/artifacts/${artifact}/latest"
done
```

## Final Service RA-TLS Verification

The final service listens on HTTPS inside the CVM. Get the app endpoint
from the final CVM, then transform it to Phala's TLS-passthrough form by
appending `s` to the published port segment (see prerequisite #6):

```bash
FINAL_URL="$(
  npx --yes phala cvms get <final-cvm-id> --json \
    | jq -r '.app_url // .endpoints[0].app'
)"
FINAL_URL="$(
  FINAL_URL="${FINAL_URL}" /home/$USER/.cove-cli-release/bin/python - <<'PY'
import os
import re

url = os.environ["FINAL_URL"]
port = "184" + "43"
print(re.sub(fr"-({port})([.])", r"-\1s\2", url, count=1))
PY
)"
```

Reach the service. `-k` is correct here because trust comes from the final
node certificate plus the TEE quote, not from WebPKI:

```bash
/usr/bin/curl -kfsS "${FINAL_URL}/health"
/usr/bin/curl -kfsS "${FINAL_URL}/message"
```

Then check that the live TLS certificate matches the
`certificate_body.ephemeral_keypairs.<keypair_name>.certificate_pem` recorded
in the final node certificate. The workflow's authored `ephemeral_keypairs`
section names this keypair (e.g. `session_key`); pass it via
`FINAL_KEYPAIR_NAME` if it differs:

```bash
PYTHONPATH=/home/$USER/cove/cove_container_runtime/src \
FINAL_URL="${FINAL_URL}" \
/home/$USER/.cove-cli-release/bin/python - <<'PY'
import hashlib
import json
import os
import socket
import ssl
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from cove_container_runtime.certificates import verify_node_certificate

server = os.environ.get("COVEHUB_URL", "https://api.covehub.io").rstrip("/")
publisher = os.environ.get("PUBLISHER", "<publisher>")
workflow_id = os.environ.get("WORKFLOW_ID", "<workflow_id>")
node_id = os.environ.get("FINAL_NODE_ID", "<final-node-id>")
keypair_name = os.environ.get("FINAL_KEYPAIR_NAME", "session_key")
bundle_root = Path(
    os.environ.get(
        "BUNDLE_ROOT",
        f"/home/{os.environ['USER']}/<cove-home>/materialized_workflows/{publisher}/{workflow_id}",
    )
)
expected_compose_hash = (
    bundle_root / "nodes" / node_id / "compose.generated.sha256"
).read_text().strip()
certificate_url = f"{server}/v1/runtime/{publisher}/{workflow_id}/certificates/{node_id}/latest"

request = urllib.request.Request(
    certificate_url,
    headers={"User-Agent": "cove-runtime/0.0.1"},
)
with urllib.request.urlopen(request, timeout=30) as response:
    certificate = json.load(response)

verify_node_certificate(
    certificate,
    expected_workflow_id=workflow_id,
    expected_node_name=node_id,
    expected_generated_node_compose_hash=expected_compose_hash,
)

session_key = certificate["certificate_body"]["ephemeral_keypairs"][keypair_name]
expected_pem = session_key["certificate_pem"].encode("utf-8")
expected_hash = session_key["certificate_hash"]

parsed = urlparse(os.environ["FINAL_URL"])
hostname = parsed.hostname
port = parsed.port or 443
if hostname is None:
    raise SystemExit("FINAL_URL must include a hostname")

context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE
with socket.create_connection((hostname, port), timeout=20) as sock:
    with context.wrap_socket(sock, server_hostname=hostname) as tls:
        served_pem = ssl.DER_cert_to_PEM_cert(tls.getpeercert(binary_form=True)).encode("utf-8")

served_hash = "sha256:" + hashlib.sha256(served_pem).hexdigest()
if served_pem != expected_pem or served_hash != expected_hash:
    raise SystemExit(
        f"served TLS certificate does not match Covehub final certificate: {served_hash} != {expected_hash}"
    )

print(f"final RA-TLS certificate matches Covehub: {served_hash}")
PY
```

That check ties together the live serving endpoint, the final node runtime
certificate on Covehub, the reviewed Cove compose hash from the pulled
bundle, and the Phala/dstack quote embedded in the certificate.

## Appendix: Phala API Surface Beyond `cove deploy`

Phala's public docs and the `phala-cloud` Python SDK expose more than Cove
currently wraps. Cove's deploy translator covers the workflow pull, compose
translation, `provision_cvm`, `commit_cvm_provision`, and deployment
summary path with the flags listed above. Everything else listed here is
available through the Phala CLI or SDK directly:

- **Instance discovery** — list instance type families and concrete CPU/GPU
  instance types.
- **OS image discovery and updates** — list OS images, inspect available
  OS images for a CVM, update a CVM OS image.
- **Node discovery** — list available nodes and workspace nodes.
- **Workspace/account APIs** — current user, workspaces, workspace
  details, workspace quotas.
- **CVM deployment** — provision and commit new CVMs.
- **CVM lifecycle** — start, stop, shutdown, restart, delete, watch state,
  fetch state, fetch batched status, refresh instance IDs.
- **CVM inspection** — fetch CVM info, compose file, Docker Compose YAML,
  user config, network data, CVM stats, container stats.
- **Compose and runtime updates** — provision/commit compose-file updates,
  patch a CVM, confirm a patch, update Docker Compose YAML, update
  pre-launch scripts, update encrypted envs, update resources, update
  visibility.
- **Visibility controls** — public logs, public sysinfo, public TCB info,
  public listing.
- **App queries** — list apps, get app details, list app CVMs, get app
  revisions, get revision details, get app filter options, get app
  attestation data, get app metered usage.
- **Attestation and allow checks** — CVM attestation, app attestation,
  device allowlist status, CVM allow checks, app allow checks, batched
  app/CVM allow checks.
- **KMS and on-chain helpers** — list KMS instances, fetch KMS info, fetch
  app env encryption public keys, allocate next app IDs, fetch on-chain
  KMS detail, add compose hashes, deploy app auth metadata.
- **SSH keys** — list, create, delete, import GitHub profile keys, sync
  GitHub keys.
- **Replicas** — create CVM replicas under an existing app.

## References

- Phala CLI deploy docs: https://docs.phala.com/phala-cloud/phala-cloud-cli/deploy
- Phala API deployment guide: https://docs.phala.com/phala-cloud/references/api-deployment-guide
- Phala instance type docs: https://docs.phala.com/phala-cloud/phala-cloud-cli/instance-types
- Phala OS image docs: https://docs.phala.com/phala-cloud/phala-cloud-cli/os-images
- Phala API overview: https://docs.phala.com/phala-cloud/phala-cloud-api/overview
