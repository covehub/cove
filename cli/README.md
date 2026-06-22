# Cove CLI

This directory is the `cove-cli` Python package — the local CLI used for
provisioning artifacts, authoring and compiling workflows, owner review,
deploying to Phala Cloud, and running owner provisioning services.

For the architecture and trust model, see
`docs/internal/architecture.md`. For the operational runbooks (release,
deploy, end-to-end), see `docs/internal/operations/`. For known engineering
follow-ups, see `docs/internal/todos.md`.

## Requirements

Project dependencies (`cli/pyproject.toml`):

- Python 3.12+
- `cryptography`
- `jsonschema`
- `PyYAML`

External tools used by the CLI:

- `uv` — installs and runs the project from source.
- Docker CLI — image resolution and digest pinning during compile and
  publication flows.
- Docker Compose v2 — generated-compose validation and Phala deploy
  translation.

`cove deploy` additionally requires:

- A public HTTPS Covehub URL configured in `<cove_home>/config.yaml`.
- A Phala-reachable owner service URL declared per owner in the workflow YAML
  and published through Cloudflare Tunnel (or equivalent). The local owner
  service is HTTP; the public domain layer is responsible for transport
  security.
  See `docs/internal/operations/owner_services.md`.
- A Phala Cloud API key stored in `<cove_home>/config.yaml` by `cove init`.
- A Docker Hub access token (read-only is sufficient) so the deploy
  payload can ship encrypted registry credentials to Phala. See
  `docs/internal/operations/phala_deploy.md` §"Docker Hub authentication is
  required" for why anonymous pulls are not viable.

## Commands

- `cove init`
- `cove check [workflow_path]`
- `cove compile [workflow_path]`
- `cove push [workflow_path] [--overwrite]`
- `cove pull <publisher-domain>/<workflow_id> [destination]`
- `cove pull <publisher-domain>/<workflow_id>/sha256:<digest> [destination]`
- `cove hub get <hub_path> [--output PATH] [--server-url URL]`
- `cove hub inspect <hub_path> [--server-url URL]`
- `cove deploy <publisher-domain>/<workflow_id> --phala-instance-type <type>`
- `cove deploy <publisher-domain>/<workflow_id>/sha256:<digest> --phala-instance-type <type>`
- `cove provision [--overwrite] <artifact_name> <file_path>`
- `cove provision allow <artifact_id> <compose_file_path>`
- `cove provision inspect <publisher-domain>/<workflow_id>`
- `cove provision serve [port]` (compatibility alias for `cove start`)
- `cove start [port]` (defaults to `9000`)

The default `workflow_path` is `./workflow.cove.yaml`. The active Cove home
resolves from `--cove-home`, then `COVE_HOME`, then `~/.cove`.

## Cove Home Layout

`cove init` initializes a Cove home with persistent owner identity material
and CLI configuration:

```
<cove_home>/
  config.yaml                       # Covehub URL, owner URL, Phala creds
  owner-signing-private.pem         # Ed25519 owner signing key
  owner-signing-public.pem
  keys/<artifact_id>                # one stable AES key per provisioned artifact
  provision.sqlite3                 # owner-local provisioning state and allow rules
  materialized_workflows/           # default destination for `cove pull`
```

Config fields written by `cove init`:

- `covehub_server_url`
- `owner_server_url`
- `phala_cloud_api_key`
- (optional) `phala_docker_username`, `phala_docker_access_token`,
  `phala_docker_registry`

There is no Covehub registration, login, or bearer-token state. If an old
Cove home contains `username` or `access_token`, delete that home and run
`cove init` again.

## Authored Workflow Surface

- `artifacts` are either `static` or `dynamic`.
- Each static artifact must declare an owner and `plaintext_hash`.
- Each dynamic artifact must declare only an owner, and must be produced by
  exactly one service output.
- Dynamic artifact consumers must list the producer node in `dependencies`.
- `services.inputs` may reference declared static or dynamic artifacts.
- `services.outputs` must reference declared dynamic artifacts.
- `workflow_output` artifacts are rejected.
- `ephemeral_keypairs` may be declared at the workflow or service level;
  only `ed25519` is supported.
- `should_terminate` defaults to `true`. Non-terminating services do not
  produce per-service certificate results and may not declare
  `custom_certificate_field`.
- Node `dependencies` are DAG edges that drive dependency-certificate flow
  and runtime precondition gating.
- Container-to-container file handoff outside the artifact model is out of
  scope for `workflow.cove.yaml`.

## Workflow Authoring Validations

- Compose YAML loading and service-name validation.
- DAG validation, including cycle detection.
- Required `plaintext_hash` on every static artifact declaration.
- Errors when a static input is not hash-anchored through an exact literal
  precondition: `inputs.<artifact>.plaintext_hash == "sha256:..."`.
- Warnings on nodes with static inputs but no declared `preconditions`.
- Owner URLs must be origins. Their hostnames become the canonical Covehub
  namespaces and must be lowercase DNS hostnames with at least one dot, for
  example `alice.example.test`.
- `cove check` is syntax-only. Network identity and object discovery
  happen at compile, provision, push, pull, deploy, or runtime.

## Covehub Typed Routes

The CLI talks to Covehub through these typed object routes:

```
v1/artifacts/<owner-domain>/<artifact_name>/sha256:<digest>
v1/workflows/<publisher-domain>/<workflow_id>/sha256:<digest>
v1/workflows/<publisher-domain>/<workflow_id>/latest
v1/runtime/<publisher-domain>/<workflow_id>/certificates/<node_id>/sha256:<digest>
v1/runtime/<publisher-domain>/<workflow_id>/certificates/<node_id>/latest
v1/runtime/<publisher-domain>/<workflow_id>/artifacts/<artifact_name>/sha256:<digest>
v1/runtime/<publisher-domain>/<workflow_id>/artifacts/<artifact_name>/latest
```

Integrity comes from exact path naming and payload-hash verification at the
server. Authored static artifacts carry the expected `plaintext_hash`;
`cove compile` asks the owner service to resolve that value to the exact
generated ciphertext path and writes that review metadata into
`workflow.normalized.cove.yaml`. `cove check` enforces literal precondition
anchors against the plaintext hash. Static artifact and workflow mutations
are signed with the local owner key; the server verifies the current
`<owner_url>/identity` document before it accepts the write.

The default public Covehub target is `https://api.covehub.io`.

## Provisioning And Encryption

`cove provision` encrypts a static artifact locally with a stable
per-artifact AES-256 key, uploads it to the exact ciphertext hash path,
prints both `plaintext_hash` and `ciphertext_hash`, and stores the key at
`<cove_home>/keys/<artifact_id>`.

- `plaintext_hash` is the trust-critical workflow value enforced by
  `cove check` preconditions.
- `ciphertext_hash` is the immutable Covehub object address under
  `v1/artifacts/<owner-domain>/<artifact_id>/<ciphertext_hash>`.

The local provisioner returns the AES key as `key_b64`. Key release is
gated by attested Phala/dstack quotes and the owner's allow rules; see
`docs/internal/security_model.md` for the full release model.

## Compilation And Generated Composes

`cove compile` lowers the authored workflow into one generated compose per
node under:

```
workflow/build/workflow.normalized.cove.yaml
workflow/build/nodes/<node>/compose.generated.yaml
workflow/build/nodes/<node>/compose.generated.sha256
```

The generated compose mounts only the staged inputs, ephemeral keypair
files, and direct dependency certificates each workload service declares.
Sidecars write staged inputs, key material, dependency certificates,
service results, and the node certificate under `/cove`.

Sidecar runtime config is embedded directly into each generated sidecar
service:

- `environment.COVE_CONFIG_JSON`
- `environment.COVE_SERVICE_NAME`
- `environment.COVE_COMPOSE_HASH`

`cove compile` resolves each owner identity and each static artifact's exact
owner-signed ciphertext path by `artifact_id + plaintext_hash`. Static
artifacts must already be provisioned before compile so the generated
normalized workflow and sidecar configs can pin exact paths. Dynamic artifact
paths are generated as
`v1/runtime/<publisher-domain>/<workflow_id>/artifacts/<artifact_id>/latest`.

`cove compile` fails closed if any image cannot be resolved to an immutable
digest-pinned reference. Generated sidecars use the canonical pinned refs
copied from `containers/canonical_container_digests.json` into the CLI
release; tagged workload images must have a real local `RepoDigest` or
compilation fails. The sidecar image set is:

- `cove-base`
- `cove-artifact-provisioner`
- `cove-precondition-checker`
- `cove-dependency-certificate-fetcher`
- `cove-service-certificate-writer`
- `cove-key-manager`
- `cove-node-certificate-writer`

The role images are intentionally thin. Their Dockerfiles do `FROM
cove-base`, copy one checked-in `main.py`, and run `python /app/main.py`;
shared helper code lives in the internal `cove_container_runtime` library
that `cove-base` installs.

## Publication Model (`cove push`)

`cove push` does not publish authored workflow source directly. It:

- runs `cove compile`,
- uploads the generated node composes unchanged as the reviewed runtime
  artifact,
- uploads the workflow bundle to
  `v1/workflows/<publisher-domain>/<workflow_id>/sha256:<digest>`,
- advances `v1/workflows/<publisher-domain>/<workflow_id>/latest`,
- supports exact pulls and deploys via
  `<publisher-domain>/<workflow_id>/sha256:<digest>`.

The published bundle contains:

- `workflow.normalized.cove.yaml`
- `nodes/<node>/compose.generated.yaml`
- `nodes/<node>/compose.generated.sha256`
- `nodes/<node>/assets/`
- `bundle.manifest.json`

The generated compose is the review artifact. `cove pull` materializes the
bundle into:

```
<cove_home>/materialized_workflows/<publisher-domain>/<workflow_id>/
```

Verification scripts in `docs/internal/operations/phala_deploy.md` derive
expected per-node compose hashes from `bundle.manifest.json` plus each
`compose.generated.sha256`. The pulled bundle is the source of truth for
hashes that runtime certificates attest, **not** the Phala-translated
deploy-payload `compose_hash`.

## Owner Approval And Allow Rules

Owners approve access against the pulled generated node compose, not the
authored workflow.

- `cove provision inspect <publisher-domain>/<workflow_id>` pulls the bundle,
  prints each node compose that references one of the local owner domain's
  artifacts, and prompts `y/n`.
- `cove provision allow <artifact_id> <compose_file_path>` records one
  allow rule directly for one pulled node compose.

Each allow rule is keyed by:

- local artifact id and hub path,
- publisher and workflow id,
- node id,
- pulled compose hash,
- the canonical `cove-artifact-provisioner` image digest from the packaged
  CLI digest policy.

The local provision server refuses key release unless the requesting
artifact provisioner presents a matching reviewed node identity *and* its
image digest is present in the packaged CLI digest policy. That digest
policy is shipped with the CLI wheel; refreshing the canonical sidecar
images requires copying the new `containers/canonical_container_digests.json`
into the CLI tree and rebuilding the wheel
(`docs/internal/operations/runtime_release.md` and
`docs/internal/operations/cli_release.md`).

## Owner Service

`cove init` creates the persistent owner identity material. `cove start
[port]` runs the local HTTP owner service in the foreground; a public
hostname can be exposed through Cloudflare Tunnel
(`docs/internal/operations/owner_services.md`).

The runtime trust path is the signed `/identity` document and signed owner
responses, not TLS on the local owner-service process. Public owner URLs may
still be HTTPS through Cloudflare or another secured domain layer.

Workflow YAML must declare each owner's URL explicitly:

```yaml
owners:
  alice: https://alice.example.test
```

`cove check` validates the shape offline and rejects authored
`artifacts.*.hub_path`; paths are generated. `cove compile` fetches
`<owner_url>/identity`, verifies the signed identity document, and bakes
the resolved owner URL, owner domain, public key, public-key hash, and
signature into generated sidecar config. The alias (`alice`) is
authoring-only; normalized workflows, generated Covehub paths, and
published refs use the URL hostname.

## Deploy

`cove deploy` reads the Phala API key from `<cove_home>/config.yaml`. It
does not load `PHALA_CLOUD_API_KEY` from the shell. Resource selection is
explicit per deploy:

```bash
cove --cove-home <home> deploy <publisher-domain>/<workflow_id> \
  --phala-instance-type tdx.medium
```

The compiled workflow must already reference public HTTPS Covehub and
owner URLs that Phala can reach. For the full deploy model, supported
flags, and the non-obvious deploy-time constraints (Docker Hub auth,
`allowed_envs`, RA-TLS passthrough URL form), see
`docs/internal/operations/phala_deploy.md`.

## Local Development

Run the CLI tests:

```bash
cd cli
uv sync --extra dev
uv run --extra dev pytest tests
```

Run `cove check` against the demo workflow:

```bash
cd cli
uv sync
uv run cove check ../demos/hello_world/workflow/workflow.cove.yaml
```

Compile the demo workflow into generated node composes:

```bash
uv run cove --cove-home <home> compile \
  ../demos/hello_world/workflow/workflow.cove.yaml
```

Inspect one generated compose:

```bash
cat ../demos/hello_world/workflow/build/nodes/final_server/compose.generated.yaml
docker compose -f ../demos/hello_world/workflow/build/nodes/final_server/compose.generated.yaml config
```

For the full hello-world walkthrough — provisioning, compile, push, pull,
review, deploy, and certificate verification — see
`docs/internal/hello_world.md`.

## Releasing The CLI

For the wheel-build flow and the release rule
(`runtime sidecars → canonical_container_digests.json → CLI package`), see
`docs/internal/operations/cli_release.md`.

## Not Implemented

- workflow-level server URL configuration
- workflow-output artifact channels
