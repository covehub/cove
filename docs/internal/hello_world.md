# Hello World

This document describes the `hello_world` demo in full operational detail and
maps each step to the Cove primitives defined in
[architecture.md](architecture.md).

`hello_world` is the smallest workflow that exercises every core primitive:
multi-owner private artifacts, deterministic node manifests, owner approval,
dynamic artifact propagation, dependency certificates, preconditions over
upstream claims, schema-valid service results, and a long-running service with
an enclave-generated keypair.

Hub-path examples below use the current typed Covehub routes. Static artifacts
live under `v1/artifacts/...`; dynamic artifacts are materialized under
`v1/runtime/...` keyed by the workflow publisher.

## Scenario

Alice and Bob each have a secret word. Neither wants the other (or anyone else)
to see their plaintext. They want to run a joint computation that:

1. checks that each secret word is lowercase
2. transforms each word to uppercase
3. checks that the two uppercase words together are exactly 10 characters
4. serves the combined result over HTTPS using an ephemeral TLS key

The system must guarantee that no node sees a secret word unless its owner has
explicitly approved that node's generated compose, and that downstream nodes
only run after upstream nodes have certified their results.

## How This Maps To The Architecture Primitives

- **Owners**
  - Alice owns `alice_secret_word` and Alice-owned dynamic outputs.
  - Bob owns `bob_secret_word` and Bob-owned dynamic outputs.
- **Publisher and deployer**
  - Alice publishes and deploys the workflow in this demo.
- **Static artifacts**
  - Alice and Bob provision encrypted secret-word artifacts before execution.
- **Dynamic artifacts**
  - The two word-length checker nodes produce transformed secret-word
    artifacts for downstream nodes.
- **Node manifests**
  - `cove compile` emits one generated compose manifest per node.
- **Node certificates**
  - Each node emits a certificate binding its compose hash, inputs, outputs,
    results, key material, and dependency certificates.
- **Preconditions**
  - Downstream services inspect upstream certificate bodies before running.
- **Long-running service**
  - `final_server` uses a generated `session_key` and stays live after its
    node certificate is emitted.

## Participants

| Name  | Role      | Secret word | Plaintext hash                                                           | Owner URL                  |
|-------|-----------|-------------|--------------------------------------------------------------------------|----------------------------|
| Alice | Owner     | `hello`     | `sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03` | `https://cove-demo-hello-world-alice-provisioning.covehub.io` |
| Bob   | Owner     | `world`     | `sha256:e258d248fda94c63753607f7c4494ee0fcbe92f1a76bfdac795c9d84101eb317` | `https://cove-demo-hello-world-bob-provisioning.covehub.io`   |
| Alice | Publisher | -           | -                                                                        | -                          |

Alice is also the workflow publisher. Both Alice and Bob run independent owner
services that expose `/identity` and gate key release for their own artifacts.

## Artifacts

The workflow declares four artifacts. Two are static, encrypted before the run
and stored on Covehub as ciphertext. Two are dynamic, produced at runtime by
nodes and encrypted for downstream transit.

### Static artifacts

| Artifact ID          | Owner | Hub path                                   | Plaintext hash |
|----------------------|-------|--------------------------------------------|----------------|
| `alice_secret_word`  | Alice | `v1/artifacts/alice/alice_secret_word/latest`   | `sha256:5891...` |
| `bob_secret_word`    | Bob   | `v1/artifacts/bob/bob_secret_word/latest`       | `sha256:e258...` |

Each static artifact is AES-256-GCM encrypted by `cove provision` before
upload. The symmetric key lives only in the owner's local Cove home at
`<cove_home>/keys/<artifact_id>`. The reviewed `hub_path` uses the owner alias;
runtime code resolves it to the owner's domain-backed Covehub path before
fetching ciphertext. The owner's local provisioner releases the key only after
verifying attestation and allow rules.

### Dynamic artifacts

| Artifact ID                      | Owner | Hub path                                                         |
|----------------------------------|-------|------------------------------------------------------------------|
| `alice_secret_word_transformed`  | Alice | `runtime/hello_world/artifacts/alice_secret_word_transformed/latest`  |
| `bob_secret_word_transformed`    | Bob   | `runtime/hello_world/artifacts/bob_secret_word_transformed/latest`    |

Dynamic artifacts are produced by `alice_word_length_checker` and
`bob_word_length_checker` respectively. The producing node encrypts the output
with a fresh key obtained from the owner's provisioner and uploads the
ciphertext to Covehub. Downstream nodes consume these as `dynamic_input`,
fetching the ciphertext from Covehub and the decryption key from the owner's
provisioner (gated by the producer's node certificate).

## Ephemeral keypairs

| Name          | Algorithm | Used by        |
|---------------|-----------|----------------|
| `session_key` | ed25519   | `final_server` |

`cove-key-manager` generates a fresh ed25519 private key, public key, and
self-signed X.509 certificate (CN `hello_world.final_server.session_key`,
7-day validity). The `final_server` workload uses the certificate and private
key as its TLS identity.

## DAG topology

```
alice_word_length_checker ──┬──> character_set_checker ──> final_server
                            │                              ▲
bob_word_length_checker   ──┘                              │
        │                                                  │
        └──────────────────────────────────────────────────┘
```

Both word-length checkers are roots (no dependencies). `character_set_checker`
depends on both. `final_server` depends on all three.

`cove deploy cove-demo-hello-world-alice-provisioning.covehub.io/hello_world --phala-instance-type tdx.medium` submits nodes
in stable topological order:

1. `alice_word_length_checker`
2. `bob_word_length_checker`
3. `character_set_checker`
4. `final_server`

## Authored workflow definition

File: `demos/hello_world/workflow/workflow.cove.yaml`

```yaml
cove_version: 1

workflow:
  id: hello_world

platform:
  provider: phala
  runtime: dstack

owners:
  alice: https://cove-demo-hello-world-alice-provisioning.covehub.io
  bob: https://cove-demo-hello-world-bob-provisioning.covehub.io

ephemeral_keypairs:
  session_key:
    algorithm: ed25519
```

Each node references an authored compose file under `workflow/nodes/` and
declares its services, inputs, outputs, preconditions, and dependencies.

## Node 1: `alice_word_length_checker`

### Purpose

Receives Alice's encrypted secret word, decrypts it, checks that it is
lowercase, writes the uppercase-transformed output as a dynamic artifact, and
certifies the check result.

### Authored compose

File: `workflow/nodes/alice_word_length_checker.compose.yaml`

```yaml
services:
  word_length_checker:
    image: covehub/cove-demo-hello-world-word-length-checker@sha256:<digest>
    environment:
      INPUT_PATH: /workspace/input/alice_secret_word.txt
      RESULT_PATH: /workspace/output/alice_word_length_result.json
      OUTPUT_PATH: /workspace/output/alice_secret_word_transformed.txt
```

### Workload logic

Source: `demos/hello_world/containers/word_length_checker/main.py`

```python
secret_word = read_secret_word(input_path)    # reads and strips the file
passed = secret_word == secret_word.lower()   # lowercase check
write_json(result_path, {"pass": passed})     # e.g. {"pass": true}
write_text(output_path, secret_word.upper() + "\n")  # "HELLO\n"
```

The workload itself has no network access and no awareness of Cove. It reads a
file, writes two files, and exits.

### Precondition

```yaml
preconditions:
  "==":
    - { "var": "inputs.alice_secret_word.plaintext_hash" }
    - "sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"
```

This JsonLogic rule verifies at runtime that the decrypted artifact's hash
matches the hash declared in the workflow. The `cove-precondition-checker`
sidecar evaluates this against input metadata written by the artifact
provisioner.

### Generated compose services (6 total)

After `cove compile`, the generated compose for this node contains:

#### 1. `cove_provision_alice_secret_word`

Image: `covehub/cove-artifact-provisioner@sha256:<digest>`

Mode: `static_input`

Behavior:
1. Resolves owner alias `alice` to Alice's owner URL, then downloads ciphertext
   from Covehub at
   `v1/artifacts/cove-demo-hello-world-alice-provisioning.covehub.io/alice_secret_word/latest`
2. Builds an attestation bundle (report data =
   `cove_key_release_v1 || sha256(canonical_json({workflow_publisher_domain, workflow_id, node_id, compose_hash, artifact_provisioner_digest}))`)
3. POSTs to Alice's owner service at `https://cove-demo-hello-world-alice-provisioning.covehub.io/v1/artifacts/key-release`
   with headers: `X-TDX-Quote`, `X-Cove-Node-Id`, `X-Cove-Compose-Hash`,
   `X-Cove-Attestation-Format`, `X-Cove-Report-Data`
4. Receives the AES-256-GCM key in the response
5. Decrypts ciphertext (nonce is prepended to the ciphertext blob)
6. Verifies `sha256(plaintext) == expected_plaintext_hash`
7. Writes the decrypted plaintext to
   `/cove/inputs/alice_secret_word/alice_secret_word`
8. Writes input metadata to
   `/cove/inputs/alice_secret_word/metadata.json`

Config (via `COVE_CONFIG_JSON`):
```json
{
  "artifact_name": "alice_secret_word",
  "mode": "static_input",
  "hub_path": "v1/artifacts/alice/alice_secret_word/latest",
  "expected_plaintext_hash": "sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
  "owner": "alice",
  "owners": {
    "alice": "https://cove-demo-hello-world-alice-provisioning.covehub.io"
  },
  "owner_identity": {
    "version": 2,
    "owner_url": "https://cove-demo-hello-world-alice-provisioning.covehub.io",
    "owner_domain": "cove-demo-hello-world-alice-provisioning.covehub.io",
    "owner_public_key_pem": "...",
    "owner_public_key_sha256": "sha256:...",
    "signature": "..."
  },
  "staged_plaintext_path": "/cove/inputs/alice_secret_word/alice_secret_word",
  "metadata_path": "/cove/inputs/alice_secret_word/metadata.json",
  "covehub_server_url": "http://127.0.0.1:8000",
  "workflow_publisher_domain": "cove-demo-hello-world-alice-provisioning.covehub.io",
  "workflow_id": "hello_world",
  "node_id": "alice_word_length_checker"
}
```

Network: `host` (needs to reach Covehub and Alice's provisioner)

#### 2. `cove_preconditions_word_length_checker`

Image: `covehub/cove-precondition-checker@sha256:<digest>`

Behavior:
1. Waits for `/cove/inputs/alice_secret_word/metadata.json` to exist (file
   polling, 30s timeout, 0.2s interval)
2. Loads the metadata JSON and builds a context object:
   `{"inputs": {"alice_secret_word": <metadata>}, "certificates": {}}`
3. Evaluates the JsonLogic precondition against this context
4. Exits 0 if the precondition passes, exits non-zero otherwise (fail closed)

Depends on: `cove_provision_alice_secret_word` (service_completed_successfully)

#### 3. `word_length_checker` (the workload)

Image: `covehub/cove-demo-hello-world-word-length-checker@sha256:<digest>`

Depends on: `cove_preconditions_word_length_checker`
(service_completed_successfully)

Volume mounts:
- `cove-input-*` named volume -> `/workspace/input` (read-only after the compiler-generated `cove_copy_*` helper copies `/cove/inputs/alice_secret_word/alice_secret_word` into it)
- `cove-bind-*` named volume -> `/workspace/output` (read-write)

The workload reads its decrypted input, produces `alice_word_length_result.json`
and `alice_secret_word_transformed.txt`, and exits.

#### 4. `cove_service_certificate_writer_word_length_checker`

Image: `covehub/cove-service-certificate-writer@sha256:<digest>`

Behavior:
1. Reads the workload's result from
   `/workspace/output/alice_word_length_result.json`
2. Validates the result against the inline JSON schema in `COVE_CONFIG_JSON`
3. Writes the validated result to
   `/cove/certificates/alice_word_length_checker/word_length_checker/result.json`

Depends on: `word_length_checker` (service_completed_successfully)

Shares the `/workspace/output` volume with the workload (via YAML anchor).

#### 5. `cove_publish_alice_secret_word_transformed`

Image: `covehub/cove-artifact-provisioner@sha256:<digest>`

Mode: `dynamic_output`

Behavior:
1. Reads the workload's output from
   `/workspace/output/alice_secret_word_transformed.txt`
2. Obtains a fresh encryption key from Alice's provisioner via attestation
3. Encrypts the output with AES-256-GCM
4. Uploads ciphertext to Covehub at
   `v1/runtime/cove-demo-hello-world-alice-provisioning.covehub.io/hello_world/artifacts/alice_secret_word_transformed/latest`
5. Writes output metadata to
   `/cove/outputs/alice_secret_word_transformed/metadata.json`

Depends on: `word_length_checker` (service_completed_successfully)

Network: `host`

#### 6. `cove_node_certificate_writer`

Image: `covehub/cove-node-certificate-writer@sha256:<digest>`

Behavior:
1. Loads input metadata from
   `/cove/inputs/alice_secret_word/metadata.json`
2. Loads output metadata from
   `/cove/outputs/alice_secret_word_transformed/metadata.json`
3. Loads the service result from
   `/cove/certificates/alice_word_length_checker/word_length_checker/result.json`
4. Builds the canonical certificate body:
   ```json
   {
     "workflow_id": "hello_world",
     "node_id": "alice_word_length_checker",
     "generated_node_compose_hash": "<sha256 of compose.generated.yaml>",
     "inputs": {"alice_secret_word": {...}},
     "ephemeral_keypairs": {},
     "results": {"word_length_checker": {"pass": true}},
     "outputs": {"alice_secret_word_transformed": {...}}
   }
   ```
5. Computes `certificate_body_hash = sha256(canonical_json(certificate_body))`
6. Builds attestation report data:
   `cove_node_certificate_v1 || sha256(canonical_json({certificate_body_hash, generated_node_compose_hash}))`
7. Collects the attestation bundle
8. Writes the full certificate to
   `/cove/certificates/alice_word_length_checker/certificate.json`
9. Uploads the certificate to Covehub at
   `v1/runtime/cove-demo-hello-world-alice-provisioning.covehub.io/hello_world/certificates/alice_word_length_checker/latest`

Depends on: all of `cove_provision_alice_secret_word`,
`cove_publish_alice_secret_word_transformed`, and
`cove_service_certificate_writer_word_length_checker`
(all service_completed_successfully)

Network: `host`

### Intra-node execution order

```
cove_provision_alice_secret_word
        │
        v
cove_preconditions_word_length_checker
        │
        v
word_length_checker (workload)
        │
        ├──> cove_service_certificate_writer_word_length_checker
        │
        └──> cove_publish_alice_secret_word_transformed
                │
                v
cove_node_certificate_writer  (waits for all three above)
```

## Node 2: `bob_word_length_checker`

Structurally identical to `alice_word_length_checker`, but operating on Bob's
secret word.

### Key differences

| Field                      | Alice node                        | Bob node                          |
|----------------------------|-----------------------------------|-----------------------------------|
| Artifact                   | `alice_secret_word`               | `bob_secret_word`                 |
| Owner service              | `https://cove-demo-hello-world-alice-provisioning.covehub.io`  | `https://cove-demo-hello-world-bob-provisioning.covehub.io`    |
| Expected plaintext hash    | `sha256:5891...`                  | `sha256:e258...`                  |
| Output artifact            | `alice_secret_word_transformed`   | `bob_secret_word_transformed`     |
| Generated compose hash     | `sha256:fa19f3cd...`              | `sha256:b3918f98...`              |

All six sidecars are present with the same image refs and the same execution
order. The only differences are the artifact names, hub paths, owner name,
owner identity, and expected hashes.

## Node 3: `character_set_checker`

### Purpose

Receives both transformed (uppercase) words as dynamic inputs, checks that
their combined length is exactly 10, and certifies the result.

### Dependencies

- `alice_word_length_checker`
- `bob_word_length_checker`

### Workload logic

Source: `demos/hello_world/containers/character_set_checker/main.py`

```python
combined = read_secret_word(alice_input_path) + read_secret_word(bob_input_path)
passed = len(combined) == 10    # "HELLO" + "WORLD" = 10 chars
write_json(result_path, {"pass": passed})
```

### Preconditions

```yaml
preconditions:
  and:
    - "==":
        - { "var": "certificates.alice_word_length_checker.certificate_body.results.word_length_checker.pass" }
        - true
    - "==":
        - { "var": "certificates.bob_word_length_checker.certificate_body.results.word_length_checker.pass" }
        - true
```

This node will not run unless both upstream nodes certified that their
respective word passed the lowercase check.

### Generated compose services (7 total)

#### 1. `cove_dependency_certificate_fetcher`

Image: `covehub/cove-dependency-certificate-fetcher@sha256:<digest>`

Behavior:
1. Polls Covehub for each dependency's runtime certificate:
   - `v1/runtime/cove-demo-hello-world-alice-provisioning.covehub.io/hello_world/certificates/alice_word_length_checker/latest`
   - `v1/runtime/cove-demo-hello-world-alice-provisioning.covehub.io/hello_world/certificates/bob_word_length_checker/latest`
2. For each downloaded certificate, verifies:
   - `certificate_body_hash` matches canonical body
   - `node_id` matches expected
   - `workflow_id` matches expected
   - `generated_node_compose_hash` matches expected (pinned at compile time)
   - attestation bundle is valid
3. Writes verified certificates to:
   - `/cove/certificates/alice_word_length_checker/certificate.json`
   - `/cove/certificates/bob_word_length_checker/certificate.json`

Config:
```json
{
  "dependencies": [
    {
      "node_name": "alice_word_length_checker",
      "certificate_path": "/cove/certificates/alice_word_length_checker/certificate.json",
      "expected_workflow_id": "hello_world",
      "expected_node_id": "alice_word_length_checker",
      "expected_generated_node_compose_hash": "sha256:fa19f3cd..."
    },
    {
      "node_name": "bob_word_length_checker",
      "certificate_path": "/cove/certificates/bob_word_length_checker/certificate.json",
      "expected_workflow_id": "hello_world",
      "expected_node_id": "bob_word_length_checker",
      "expected_generated_node_compose_hash": "sha256:b3918f98..."
    }
  ],
  "timeout_seconds": 90.0,
  "poll_interval_seconds": 0.5
}
```

No dependencies within this node (runs immediately on startup). Network: `host`.

#### 2. `cove_provision_alice_secret_word_transformed`

Mode: `dynamic_input`

Behavior:
1. Waits for `cove_dependency_certificate_fetcher` to complete
2. Reads the producer certificate from
   `/cove/certificates/alice_word_length_checker/certificate.json`
3. Downloads ciphertext from Covehub
4. Obtains decryption key from Alice's provisioner via attestation
5. Decrypts and stages plaintext at
   `/cove/inputs/alice_secret_word_transformed/alice_secret_word_transformed`

#### 3. `cove_provision_bob_secret_word_transformed`

Mode: `dynamic_input`

Same as above but for Bob's transformed word, using Bob's owner service at
`https://cove-demo-hello-world-bob-provisioning.covehub.io`.

#### 4. `cove_preconditions_character_set_checker`

Depends on: both provision sidecars and the dependency certificate fetcher.

Loads both upstream certificates and both input metadata files. Evaluates the
`and` precondition checking that both upstream `word_length_checker.pass ==
true`.

#### 5. `character_set_checker` (the workload)

Depends on: `cove_preconditions_character_set_checker` and
`cove_dependency_certificate_fetcher`.

Volume mounts include both decrypted transformed words as read-only inputs and
both upstream certificates as read-only certificate mounts.

#### 6. `cove_service_certificate_writer_character_set_checker`

Reads the workload result, validates against schema, writes to
`/cove/certificates/character_set_checker/character_set_checker/result.json`.

#### 7. `cove_node_certificate_writer`

Builds and uploads the node certificate for `character_set_checker`, including
the `{"pass": true}` result in `results.character_set_checker`.

### Intra-node execution order

```
cove_dependency_certificate_fetcher
        │
        ├──> cove_provision_alice_secret_word_transformed
        │
        └──> cove_provision_bob_secret_word_transformed
                │
                v
cove_preconditions_character_set_checker  (waits for both provisions + fetcher)
        │
        v
character_set_checker (workload)
        │
        v
cove_service_certificate_writer_character_set_checker
        │
        v
cove_node_certificate_writer  (waits for provisions, fetcher, and cert writer)
```

## Node 4: `final_server`

### Purpose

Receives both transformed words, generates an ephemeral TLS keypair, and serves
the combined secret message over HTTPS. This is a long-running (non-terminating)
service.

### Dependencies

- `alice_word_length_checker`
- `bob_word_length_checker`
- `character_set_checker`

### Workload logic

Source: `demos/hello_world/containers/final_server/main.py`

```python
alice_word = read_secret_word(alice_input_path)  # "HELLO"
bob_word = read_secret_word(bob_input_path)      # "WORLD"
message = f"alice's secret word is: {alice_word} and bob's secret word is: {bob_word}"

# Serves HTTPS on 0.0.0.0:8443 using the ephemeral session_key TLS cert
server = HTTPServer((host, port), Handler)
ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ssl_context.load_cert_chain(certfile=tls_cert_path, keyfile=tls_key_path)
server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
```

Endpoints:
- `GET /health` -> `{"status": "ok"}`
- `GET /message` -> `alice's secret word is: HELLO and bob's secret word is: WORLD`

Port mapping: host `18443` -> container `8443`.

### Precondition

```yaml
preconditions:
  and:
    - "==":
        - { "var": "certificates.character_set_checker.certificate_body.results.character_set_checker.pass" }
        - true
```

The server only starts if the character set check passed. This transitively
guarantees that both word-length checks also passed.

### `should_terminate: False`

The `final_server` service is declared as non-terminating. This affects:
- The node certificate writer waits on `service_healthy` (not
  `service_completed_successfully`)
- The compose includes a healthcheck polling `GET /health` every 2s

### Generated compose services (8 total)

#### 1. `cove_key_manager`

Image: `covehub/cove-key-manager@sha256:<digest>`

Behavior:
1. Generates a fresh ed25519 keypair
2. Creates a self-signed X.509 certificate with
   CN=`hello_world.final_server.session_key`, valid for 7 days
3. Writes to:
   - `/cove/ephemeral_keypairs/session_key/private.pem`
   - `/cove/ephemeral_keypairs/session_key/public.pem`
   - `/cove/ephemeral_keypairs/session_key/certificate.pem`
   - `/cove/ephemeral_keypairs/session_key/metadata.json`

No dependencies (runs immediately).

#### 2. `cove_dependency_certificate_fetcher`

Fetches certificates for all three upstream nodes:
- `alice_word_length_checker` (expected compose hash `sha256:fa19...`)
- `bob_word_length_checker` (expected compose hash `sha256:b391...`)
- `character_set_checker` (expected compose hash `sha256:200e...`)

#### 3-4. `cove_provision_{alice,bob}_secret_word_transformed`

Same as in `character_set_checker`. Mode `dynamic_input`, depends on the
dependency certificate fetcher.

#### 5. `cove_preconditions_final_server`

Loads all three upstream certificates and both input metadata files. Evaluates
the precondition checking `character_set_checker.pass == true`.

#### 6. `final_server` (the workload)

Depends on: `cove_preconditions_final_server`, `cove_key_manager`, and
`cove_dependency_certificate_fetcher`.

Volume mounts (all read-only):
- Both decrypted transformed words
- The `session_key` private key, public key, certificate, and metadata
- All three upstream node certificates

Healthcheck:
```yaml
healthcheck:
  test: [CMD, python, -c, "...urllib.request to https://127.0.0.1:8443/health..."]
  interval: 2s
  timeout: 2s
  retries: 15
  start_period: 1s
```

#### 7. `cove_node_certificate_writer`

Depends on: both provision sidecars, dependency fetcher, key manager, and
`final_server` (condition: `service_healthy`, not `service_completed_successfully`).

The certificate body includes:
- `inputs`: both transformed word metadata
- `ephemeral_keypairs`: `session_key` metadata (including `public_key_hash`)
- `results`: empty (no terminating services)
- `outputs`: empty

### Intra-node execution order

```
cove_key_manager                cove_dependency_certificate_fetcher
      │                                    │
      │                         ┌──────────┼──────────┐
      │                         v          v          │
      │              cove_provision_   cove_provision_ │
      │              alice_...        bob_...          │
      │                         │          │          │
      │                         v          v          │
      │              cove_preconditions_final_server   │
      │                         │                     │
      └─────────────────────────┤                     │
                                v                     │
                          final_server (long-running)  │
                                │ (healthy)           │
                                v                     │
                    cove_node_certificate_writer <─────┘
```

## Cross-node data flow

### Alice's secret word through the full DAG

```
Alice's local Cove home
  keys/alice_secret_word (AES-256-GCM key)
        │
        v [cove provision]
Covehub: v1/artifacts/cove-demo-hello-world-alice-provisioning.covehub.io/alice_secret_word/latest (ciphertext)
        │
        v [cove_provision_alice_secret_word in alice_word_length_checker]
/cove/inputs/alice_secret_word/alice_secret_word (plaintext: "hello")
        │
        v [word_length_checker workload]
/workspace/output/alice_secret_word_transformed.txt ("HELLO\n")
/workspace/output/alice_word_length_result.json ({"pass": true})
        │
        ├──> [cove_publish_alice_secret_word_transformed]
        │    Encrypts "HELLO\n" and uploads to Covehub
        │
        └──> [cove_service_certificate_writer]
             Copies result to /cove/certificates/.../result.json
        │
        v [cove_node_certificate_writer]
Covehub: v1/runtime/cove-demo-hello-world-alice-provisioning.covehub.io/hello_world/certificates/alice_word_length_checker/latest
        │
        v [cove_dependency_certificate_fetcher in character_set_checker]
/cove/certificates/alice_word_length_checker/certificate.json
        │
        v [cove_provision_alice_secret_word_transformed in character_set_checker]
/cove/inputs/alice_secret_word_transformed/alice_secret_word_transformed ("HELLO")
        │
        v ... flows through character_set_checker to final_server
```

### Certificate chain

```
alice_word_length_checker.certificate.json
  ├── certificate_body.results.word_length_checker.pass = true
  └── certificate_body.outputs.alice_secret_word_transformed = {...}

bob_word_length_checker.certificate.json
  ├── certificate_body.results.word_length_checker.pass = true
  └── certificate_body.outputs.bob_secret_word_transformed = {...}

character_set_checker.certificate.json
  ├── reads alice_word_length_checker.certificate (precondition)
  ├── reads bob_word_length_checker.certificate (precondition)
  └── certificate_body.results.character_set_checker.pass = true

final_server.certificate.json
  ├── reads character_set_checker.certificate (precondition)
  ├── certificate_body.ephemeral_keypairs.session_key.public_key_hash = ...
  └── certificate_body.inputs = {alice_secret_word_transformed, bob_secret_word_transformed}
```

## Owner approval model

### What Alice approves

Alice runs `cove provision inspect cove-demo-hello-world-alice-provisioning.covehub.io/hello_world` and approves:

| Artifact              | Node                          | Compose hash     |
|-----------------------|-------------------------------|------------------|
| `alice_secret_word`   | `alice_word_length_checker`   | `sha256:fa19...` |

She also approves the dynamic artifacts for downstream nodes that consume her
transformed word:

| Artifact                         | Node                    | Compose hash     |
|----------------------------------|-------------------------|------------------|
| `alice_secret_word_transformed`  | `character_set_checker` | `sha256:200e...` |
| `alice_secret_word_transformed`  | `final_server`          | (its compose hash) |

Alice does NOT approve `alice_secret_word` for `bob_word_length_checker`,
`character_set_checker`, or `final_server`. The provisioner will refuse key
release for any unapproved combination.

### What Bob approves

Symmetrically, Bob approves `bob_secret_word` for `bob_word_length_checker`
only, and `bob_secret_word_transformed` for `character_set_checker` and
`final_server`.

### Allow rule binding

Each allow rule binds all five fields:

```
artifact_id + hub_path + publisher/workflow_id + node_id + compose_hash + artifact_provisioner_digest
```

If any field doesn't match (wrong node, different compose hash after
recompilation, different artifact provisioner image), the provisioner returns an
error and key release fails.

## Hash verification chain

The integrity model chains hashes through the full workflow:

1. **Authored plaintext hash** in `workflow.cove.yaml`:
   `sha256:5891...` for `alice_secret_word`

2. **Owner-local ciphertext hash** in `provision.sqlite3`:
   `sha256(encrypt(plaintext))`, recorded at provision time

3. **Compose hash** in `compose.generated.sha256`:
   reviewed generated compose hash, computed at compile time with
   `COVE_COMPOSE_HASH` normalized out of the hash input

4. **Canonical sidecar digest** in `canonical_container_digests.json`:
   `covehub/cove-artifact-provisioner@sha256:<digest>`

5. **Allow rule** binds compose hash + sidecar digest to artifact + node

6. **Runtime plaintext verification** in `cove-artifact-provisioner`:
   `sha256(decrypted) == expected_plaintext_hash`

7. **Node certificate body hash**:
   `sha256(canonical_json(certificate_body))`

8. **Dependency certificate verification** in downstream nodes:
   checks `expected_generated_node_compose_hash` against the upstream
   certificate's `generated_node_compose_hash`

## Filesystem layout at runtime

Inside any node's container environment, the `/cove` tree is:

```
/cove/
  inputs/
    <artifact>/
      <artifact>          # decrypted plaintext bytes
      metadata.json       # {plaintext_hash, ciphertext_hash, ...}
  outputs/
    <artifact>/
      metadata.json       # written by publish sidecar
  ephemeral_keypairs/
    <keypair>/
      private.pem
      public.pem
      certificate.pem
      metadata.json       # {name, algorithm, public_key_hash, ...}
  certificates/
    <dependency_node>/
      certificate.json    # full node certificate from upstream
    <this_node>/
      <service>/
        result.json       # service result (from cert writer)
      certificate.json    # this node's own certificate
```

The generated compose is self-contained at runtime:

```
environment:
  COVE_CONFIG_JSON: ...   # sidecar role config, including schemas
  COVE_SERVICE_NAME: ...
  COVE_COMPOSE_HASH: ...  # reviewed generated node compose hash
```

## What The Demo Proves

1. **Encrypted static artifact provisioning**: secret words are AES-256-GCM
   encrypted, stored on Covehub as ciphertext, and decrypted only inside
   owner-approved nodes.

2. **Node-scoped owner approval**: Alice and Bob approve their artifacts for
   specific nodes with specific compose hashes; the provisioner enforces those
   allow rules.

3. **Dynamic artifact transit**: outputs are encrypted and re-provisioned to
   downstream nodes, never stored in plaintext on Covehub.

4. **Digest-pinned compose review**: owners inspect the exact generated compose
   before approving, and the compose hash is bound into allow rules.

5. **Dependency-certificate gating**: downstream nodes wait for upstream
   certificates and verify their workflow ID, node ID, and compose hash before
   depending on upstream claims.

6. **JsonLogic preconditions**: nodes enforce runtime invariants by evaluating
   JsonLogic over input metadata and upstream certificate bodies.

7. **Ephemeral keypair management**: the `session_key` is generated fresh per
   deployment and its public key hash is attested in the node certificate.

8. **Certificate chain shape**: each node's certificate records its inputs,
   outputs, results, and keypairs, forming the demo's certificate path from
   secret-word ingestion to final HTTPS service.
