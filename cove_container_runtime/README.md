# Cove Container Runtime

This directory is the `cove-container-runtime` distribution (import package
`cove_container_runtime`) — the shared Python library used by Cove's
first-party sidecar containers. It is not the user-facing `cove` CLI; that
lives under `cli/`.

For the role each sidecar plays in the workflow lifecycle, see
`docs/internal/architecture.md` §"Runtime Sidecars". For the sidecar
release flow, see `docs/internal/operations/runtime_release.md`.

## What It Is For

Every first-party sidecar image uses the same runtime support code for:

- loading inline sidecar config and reviewed compose identity from
  environment,
- common file and HTTP helpers,
- attestation bundle collection and verification,
- node certificate generation and verification,
- the JsonLogic subset used by preconditions.

`containers/base/Dockerfile` installs this package into `cove-base`, and
each role image inherits it from there.

## Main Modules

### `common.py`

This is the shared runtime utility module.

Important pieces:

- `SidecarContext`
  - parsed runtime context for one sidecar instance
- `load_inline_sidecar_context()`
  - parses `COVE_CONFIG_JSON` and reads `COVE_COMPOSE_HASH`
- file helpers
  - JSON/text/bytes read and write helpers
- HTTP helpers
  - `http_get_json`
  - `http_post_json`
  - `http_put_bytes`
- crypto helpers for artifact decryption
  - `decode_key_b64`
  - `decrypt_ciphertext_bytes`

### `attestation.py`

This module owns the runtime attestation helpers.

Important pieces:

- `AttestationSettings`
  - normalized attestation backend settings from generated sidecar config
- `collect_attestation_bundle()`
  - gathers a Phala/dstack attestation bundle from the runtime environment
- `verify_attestation_bundle()`
  - verifies attestation bundle contents against expected report data

### `certificates.py`

This module owns the node certificate model built on top of the attestation
helpers.

Important pieces:

- `generate_ed25519_keypair_material()`
  - used by the key manager sidecar
- `build_node_certificate()`
  - used by the node certificate writer
- `verify_node_certificate()`
  - used by dependency-certificate fetchers
- `test_support.py`
  - contains synthetic/mock certificate helpers for automated tests only

### `jsonlogic.py`

The JsonLogic subset used by precondition expressions:

- `var`
- `==`
- `and`

The precondition checker sidecar evaluates workflow preconditions against
two contexts:

- `inputs` — admitted input artifact metadata,
- `certificates` — verified upstream node certificate bodies.

## Inline Sidecar Contract

Every generated sidecar service receives these environment variables:

- `COVE_CONFIG_JSON`
- `COVE_SERVICE_NAME`
- `COVE_COMPOSE_HASH`

The contract is:

- `COVE_CONFIG_JSON`
  - role-specific JSON config emitted by `cove compile`
- `COVE_SERVICE_NAME`
  - the service name inside the generated compose
- `COVE_COMPOSE_HASH`
  - the reviewed generated node compose hash emitted by `cove compile`

`load_inline_sidecar_context()` reads those values directly from environment.
Runtime-side code uses `COVE_COMPOSE_HASH` as part of node identity; containers
do not read the generated compose file at runtime.

## Who Uses It

Every first-party sidecar under `containers/` depends on this package:

- artifact provisioner
- precondition checker
- dependency-certificate fetcher
- service certificate writer
- key manager
- node certificate writer

The pattern in each role's `main.py`:

1. Call `load_inline_sidecar_context()` to parse `COVE_CONFIG_JSON` and
   `COVE_COMPOSE_HASH`.
2. Role-specific code reads `context.config`.
3. Shared helpers from this package handle IO, hashing, attestation,
   certificate construction, and verification.

## Local Development

Run the tests with:

```bash
cd cove_container_runtime
uv sync --extra dev
uv run --extra dev pytest tests
```

The tests cover:

- inline sidecar config loading
- attestation bundle creation and verification
- node certificate creation and verification
- JsonLogic evaluation
- shared helper behavior
