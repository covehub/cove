# `containers`

This directory owns Cove's first-party runtime sidecar images. `cove
compile` injects these images, pinned by digest, into every generated node
compose. Owner key-release also enforces the canonical
`cove-artifact-provisioner` digest from this set as part of the allow-rule
identity.

For the full runtime release flow (build → push → digest copy into the CLI
tree → CLI wheel rebuild), see
`docs/internal/operations/runtime_release.md` and
`docs/internal/operations/cli_release.md`. For the role each sidecar plays
in the workflow lifecycle, see `docs/internal/architecture.md`
§"Runtime Sidecars".

## Image Inventory

- `cove-base` — shared base image that installs `cove_container_runtime`.
- `cove-artifact-provisioner` — downloads ciphertext, requests keys with
  attestation, verifies hashes, decrypts inside the TEE, and stages
  inputs under `/cove/inputs/`. Also runs in `dynamic_output` mode to
  encrypt and upload declared dynamic outputs.
- `cove-precondition-checker` — evaluates JsonLogic preconditions over
  staged input metadata and dependency certificates.
- `cove-dependency-certificate-fetcher` — polls Covehub for upstream node
  certificates and writes them under
  `/cove/certificates/<dependency>/certificate.json`.
- `cove-service-certificate-writer` — validates each terminating service's
  result against its schema and stages the per-service result JSON.
- `cove-key-manager` — generates the workflow-declared ephemeral keypairs
  inside the enclave.
- `cove-node-certificate-writer` — assembles the node certificate, attests
  it, and uploads it to Covehub.

## Packaging Model

Dockerfiles are intentionally thin. The split is:

- `containers/base/Dockerfile` installs `cove_container_runtime` into
  `cove-base`.
- Each role Dockerfile starts from `cove-base`, copies one checked-in
  `main.py`, and runs `python /app/main.py`.

Shared logic — attestation, certificate construction, JsonLogic
evaluation, IO helpers — lives in `cove_container_runtime/`. Role images
own only their entrypoint and role-specific config handling.

## Requirements

Build dependencies:

- Docker with a Buildx-compatible build environment.
- `python3` on `PATH` (the helper scripts read and rewrite the canonical
  digest JSON).
- A working `cove_container_runtime/` checkout — `cove-base` installs it.

Publishing dependencies:

- `docker login` to the target registry namespace before pushing.
  Anonymous pushes are rejected; anonymous *pulls* on Phala worker IPs
  later hit shared rate limits, which is why CVMs receive registry
  credentials as encrypted envs at deploy time.

Docker Compose is not required to build images; it is used by the CLI to
validate and run generated node composes.

## Building Locally

To build the local image tags without publishing:

```bash
cd containers
./scripts/build_all_containers.sh
```

This builds `cove-base` and all six role images. It does not modify
`canonical_container_digests.json`. Use this for local validation before
publishing a new canonical sidecar release.

## Publishing And Refreshing Canonical Digests

To publish a new canonical sidecar release:

```bash
cd containers
./scripts/build_all_containers.sh --docker-namespace <namespace> --tag <tag> --push
```

With `--push`, the script also tags and pushes each image, reads the
returned `RepoDigests`, and rewrites
`canonical_container_digests.json` with the new pinned `canonical_ref`
values.

That JSON is the container-side release artifact. Publishing the images
alone is not enough — the refreshed JSON must be copied into both
`cli/canonical_container_digests.json` and
`cli/src/cove_cli/canonical_container_digests.json` before the next CLI
wheel is built. Operators with installed CLIs would otherwise pin the
previous canonical digest set. Full procedure:
`docs/internal/operations/runtime_release.md`.

## How The CLI Uses The Copied Digest Policy

Once the digest policy is copied into the CLI tree and a new CLI wheel is
built, the installed CLI uses it for two things:

- **Compile and publish** — generated sidecar services pin those exact
  refs.
- **Owner review and key release** — the local provision server checks
  that the requesting `cove-artifact-provisioner`'s image digest matches
  the canonical pinned digest, in addition to checking the allow rule's
  workflow id, node id, and reviewed compose hash.

## Relationship To `hello_world`

The ordinary `hello_world` demo flow does not rebuild first-party
sidecars. It pulls the pinned set with:

- `containers/scripts/pull_canonical_containers.sh` — first-party
  sidecars from `cli/canonical_container_digests.json`.
- `demos/hello_world/scripts/pull_canonical_containers.sh` — demo
  workload images from
  `demos/hello_world/canonical_container_digests.json`.

Demo workload publishing (a separate flow from this directory) lives at
`demos/hello_world/scripts/build_all_containers.sh` and refreshes the
demo-local workload policy file, not this sidecar release artifact.
