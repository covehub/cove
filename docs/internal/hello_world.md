# Hello World Workflow

The hello-world demo is the reference Cove workflow. Alice and Bob each own a
static secret word, Carol publishes and deploys the workflow, and Phala runtime
sidecars transform and combine the words under attestation.

## Parties

| Party | Role | Public owner URL | Local demo port |
| --- | --- | --- | --- |
| Alice | Static artifact owner | `https://demo-alice.covehub.io` | `9600` |
| Bob | Static artifact owner | `https://demo-bob.covehub.io` | `9601` |
| Carol | Publisher and deployer | `https://demo-carol.covehub.io` | `9602` |

Alice and Bob initialize owner/provisioning homes only. Carol initializes with
Phala and Docker credentials because Carol publishes and deploys.

## Authored Workflow Surface

Authored `workflow.cove.yaml` does not contain artifact `hub_path` fields.
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

There is no static artifact `latest` path. Static artifacts are immutable
from the workflow's perspective and compile pins exact ciphertext paths.

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

## Workflow Shape

The workflow has four nodes:

- `alice_word_length_checker` consumes Alice's static word and publishes
  `alice_secret_word_transformed`.
- `bob_word_length_checker` consumes Bob's static word and publishes
  `bob_secret_word_transformed`.
- `character_set_checker` depends on both transformed words and checks the
  character set.
- `final_server` depends on all prior nodes, consumes both transformed words,
  and serves the combined result with an enclave-generated RA-TLS keypair.

Static input preconditions pin plaintext hashes. Dynamic input preconditions
refer to producer certificates.

## Operational Flow

Use the full runbook in
[operations/end_to_end.md](operations/end_to_end.md). The short version is:

```bash
source /home/$USER/.cove-cli-release/bin/activate

cove --cove-home /home/$USER/.alice_cove provision \
  alice_secret_word /home/$USER/cove/demos/hello_world/fixtures/alice_secret_word.txt

cove --cove-home /home/$USER/.bob_cove provision \
  bob_secret_word /home/$USER/cove/demos/hello_world/fixtures/bob_secret_word.txt

cove --cove-home /home/$USER/.carol_cove check \
  /home/$USER/cove/demos/hello_world/workflow/workflow.cove.yaml

cove --cove-home /home/$USER/.carol_cove compile \
  /home/$USER/cove/demos/hello_world/workflow/workflow.cove.yaml

cove --cove-home /home/$USER/.carol_cove push \
  /home/$USER/cove/demos/hello_world/workflow/workflow.cove.yaml

cove --cove-home /home/$USER/.alice_cove provision inspect \
  demo-carol.covehub.io/hello_world

cove --cove-home /home/$USER/.bob_cove provision inspect \
  demo-carol.covehub.io/hello_world

cove --cove-home /home/$USER/.carol_cove deploy \
  demo-carol.covehub.io/hello_world \
  --phala-instance-type tdx.medium
```
