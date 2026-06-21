# Cove Architecture

Cove is a framework for compositional, multi-party confidential workflows. It
lets mutually distrusting parties run workflows over private artifacts inside
trusted execution environments (TEEs), while producing public certificates that
bind runtime claims to the measured nodes that produced them.

This document is the canonical description of the architecture and security
model. For the operational view of the same system, see the runbooks under
[operations/](operations/). For the demo that exercises every primitive
end-to-end, see [hello_world.md](hello_world.md). For known gaps and follow-up
engineering work, see [todos.md](todos.md).

## Core Idea

Cove lifts TEE attestation from "this code identity ran" to "this measured
workflow stage ran over these authenticated inputs, after these verified
preconditions, and emitted these schema-valid results."

A Cove workflow is a directed acyclic graph of nodes. Each node runs in its own
TEE, may consume private artifacts, may depend on upstream node certificates,
may run one or more user workload services, may produce encrypted dynamic
artifacts, and emits a node certificate.

That certificate binds:

- the node's measured runtime manifest
- admitted input artifact metadata
- verified upstream dependency certificates
- encrypted output artifact metadata
- schema-validated service results
- enclave-generated ephemeral public keys
- a TEE attestation over the certificate body

Downstream nodes can require certificate claims from upstream nodes as
preconditions. External verifiers can recursively verify a terminal certificate
and the predecessor certificates it references.

## Roles

Any party may hold one or more roles. No role is trusted merely because of its
identity.

- **Publisher**
  - Compiles the workflow and publishes the canonical workflow object.
  - Publication is hash-addressed so the workflow bytes can be fetched and
    checked by any party.
- **Owner**
  - Controls one or more private artifacts.
  - Holds an application-level owner signing key that anchors artifact
    authority, provisioning endpoint bindings, and owner approvals.
  - Runs a local provisioning service that releases artifact keys only to
    attested nodes the owner has approved.
- **Deployer**
  - Submits node manifests to the TEE platform for execution.
  - The deployer is not trusted for integrity; manifests and certificates are
    independently checkable.
- **Verifier**
  - Starts from a terminal certificate and checks its validity plus every
    upstream certificate it references.
  - Anyone with the workflow bytes and the TEE vendor attestation roots can be
    a verifier.

## Components

Cove consists of three open-source components.

- **Cove CLI**
  - Local software for provisioning static artifacts, authoring workflows,
    compiling node manifests, owner review, deployer operations, and verifier
    operations.
- **Cove runtime**
  - A fixed catalog of canonical sidecars that run inside each TEE beside
    user-authored workload containers.
  - Sidecars implement security-sensitive workflow primitives so workloads can
    stay ordinary containers.
- **Covehub**
  - A convenience storage and transport layer for workflow objects, node
    manifests, encrypted artifact envelopes, and certificates.
  - Covehub is not trusted. Stored objects must be hash-checkable, encrypted,
    or attested so tampering is detectable.
  - Covehub also publishes a read-only browser UI at `covehub.io` for
    discovery; see [operations/ui.md](operations/ui.md) and
    [operations/covehub_server.md](operations/covehub_server.md). The UI
    is a discovery convenience, not part of the trust root.

## Object Model

### Workflows

A workflow names:

- participating owners, owner public keys, and provisioning endpoint policy
- static and dynamic artifacts
- nodes and dependency edges
- services inside each node
- input and output bindings from artifacts to service mount paths
- service preconditions
- result schemas
- enclave-generated ephemeral keypairs

The workflow has a canonical serialization. The workflow hash names the
workflow bundle. Compilation from workflow bytes to per-node manifests is
deterministic, so every reviewer and verifier can recompute the same node
manifests and node hashes locally.

### Artifacts

An artifact is a unit of private data with exactly one owner.

- **Static artifacts**
  - Known at workflow declaration time.
  - Encrypted by the owner before execution.
  - Uploaded as ciphertext, while the owner keeps the key locally.
- **Dynamic artifacts**
  - Produced during service execution.
  - Encrypted inside the producing TEE under owner-managed key material.
  - Consumed by downstream nodes only after dependency certificates and owner
    release policy allow it.

Every artifact has a canonical identifier used by workflows, allow rules,
key-release requests, artifact envelopes, and certificate references.

### Owner URL And Domain Identity

An owner's service is identified by its public owner URL. Local `cove start`
serves plain HTTP; production deployments normally place that HTTP service
behind Cloudflare Tunnel or an equivalent domain-secured layer. Transport keys
are not owner identity keys and must not be reused for Cove application
signatures.

The owner URL is the identity root. Its normalized hostname is the canonical
Covehub namespace for static artifacts, published workflows, runtime objects,
and owner identity documents. Authored workflows use aliases for readability:

```yaml
owners:
  alice: https://alice.example.test
```

The alias remains the review-facing owner handle in normalized workflows,
published bundle metadata, and generated sidecar config. Runtime code resolves
the alias through the owner URL when it needs the domain-backed Covehub
namespace `alice.example.test`.

Each owner service publishes `/identity`, which contains `owner_url`,
`owner_domain`, the owner public key, public-key hash, validity window, and
owner signature. Clients and runtime sidecars verify that signed identity
before trusting the endpoint or sending key-release requests. Covehub write
mutations for artifacts and workflows are signed with the same owner key and
accepted only when the current identity document served from the owner URL
matches the route domain.

### Nodes And Manifests

A node is the unit of TEE execution. It contains one or more services plus the
compiler-injected sidecars needed to run those services safely.

The node manifest captures:

- user workload images
- sidecar images and inline sidecar configuration
- shared volumes and declared mounts
- dependency ordering
- digest-pinned image references
- platform-specific runtime configuration

In the reference architecture, the manifest is a canonical Docker Compose file
run inside Intel TDX through Phala dstack. The framework does not depend on
Docker Compose specifically; it needs a deterministic composition format that
can be canonically serialized and bound into TEE attestation.

The node manifest hash is the node identity:

- owners approve it
- the TEE attests it
- downstream nodes pin it as an expected dependency
- verifiers recompute it from the workflow object

### Services

A service is a user-authored containerized workload inside a node.

Each service may declare:

- input artifacts mounted as plaintext at declared paths
- output artifacts written as plaintext at declared paths
- ephemeral keypairs generated inside the TEE
- a precondition over admitted input metadata and upstream certificates
- a result schema and result path
- whether it is terminating or long-running

Terminating services write schema-valid result files that are included in the
node certificate. Long-running services can expose endpoints whose live TLS
keys are bound back to the node certificate through RA-TLS.

### Certificates

Each node emits one node certificate. A certificate body records the node's
workflow identity, node identity, manifest hash, admitted inputs, verified
dependencies, outputs, service results, and ephemeral public keys.

The certificate wrapper commits to the body hash and carries a TEE attestation
whose report data commits to that body. A valid certificate therefore cannot be
mutated, reassigned to a different measured node, or detached from its measured
runtime without breaking verification.

## Workflow Lifecycle

### 1. Provision Static Artifacts

Each owner provisions every static artifact they control:

1. Generate or reuse local symmetric key material.
2. Encrypt the plaintext locally.
3. Upload the ciphertext envelope to the exact Covehub path
   `v1/artifacts/<owner-domain>/<artifact-id>/sha256:<ciphertext-digest>`.
4. Keep the decryption key in the owner's local key store.
5. Publish or refresh the owner-signed provisioning endpoint binding for the
   key-release service that controls that artifact.

Plaintext does not leave the owner's machine during provisioning.

### 2. Author, Compile, And Publish

The parties jointly author the workflow. The publisher then compiles it into a
canonical workflow manifest plus one canonical node manifest per node.

Compilation:

- injects canonical first-party sidecars
- resolves all images to immutable digest-pinned references
- materializes sidecar configuration
- fixes shared mounts and service ordering
- emits deterministic node manifests

The workflow object is published under a path keyed by its hash. Anyone can
fetch the bytes, recompute the workflow hash, rerun compilation, and recover
the same node manifests.

### 3. Review And Approve

Each owner reviews the manifests that touch artifacts they own. Review focuses
on:

- workload images
- declared input and output mounts
- dependency edges
- preconditions
- result schemas
- canonical sidecar images
- the manifest hash being approved

For each approved artifact-node pair, the owner records a local allow rule
binding the artifact identifier to the approved node manifest hash. Later
key-release requests must present fresh attestation for that exact node.

### 4. Execute The DAG

The deployer submits node manifests to the TEE platform in topological order.
Inside each node, the canonical runtime sequence is:

1. Fetch and verify dependency certificates.
2. Admit input artifact metadata and verify artifact authority.
3. Verify each owner-signed provisioning endpoint binding before contacting
   owner key-release services.
4. Evaluate service preconditions over admitted metadata and dependency bodies.
5. Request keys from artifact owners using fresh attestation.
6. Decrypt input plaintext inside the TEE and stage it for workloads.
7. Run user workload services.
8. Encrypt and publish declared dynamic outputs.
9. Assemble and attest the node certificate.

Failures abort the node before it emits a certificate for the claimed work.

### 5. Verify A Terminal Certificate

A verifier starts from a terminal certificate, the workflow bytes, and the TEE
vendor attestation roots.

For the terminal certificate and recursively for each dependency certificate,
the verifier checks:

- the certificate body hash matches the wrapper
- the TEE quote verifies against the vendor roots
- report data commits to the certificate body hash under the Cove certificate
  label
- the attested node manifest hash equals the hash rederived from local
  deterministic compilation
- the workflow hash in the certificate body matches the workflow bytes being
  verified
- referenced dependency certificates and artifact envelopes match the
  committed fields in the certificate body
- service results conform to their schemas

Because workflows are DAGs, recursive verification terminates. Accepting a
terminal certificate commits the verifier to the accepted predecessor closure
of that node.

## Runtime Sidecars

The canonical sidecar roles are:

- **dependency fetcher**
  - Fetches and verifies upstream node certificates before the node consumes
    dependent claims.
- **input provisioner**
  - Admits input artifacts, requests owner keys with attestation, decrypts
    plaintext inside the TEE, and stages declared inputs.
- **precondition checker**
  - Evaluates service preconditions over admitted input metadata and verified
    dependency certificate bodies.
- **key manager**
  - Generates ephemeral keypairs inside the enclave and stages public metadata
    for the node certificate.
- **output provisioner**
  - Encrypts declared dynamic outputs inside the enclave and uploads the
    resulting encrypted artifact envelopes.
- **node certificate writer**
  - Collects authenticated inputs, dependencies, outputs, results, and
    ephemeral public keys, then emits the node certificate.

Sidecar image digests and configuration are covered by the node manifest hash.
Workload containers receive only their declared input, output, dependency, and
key paths. They do not receive the TEE attestation surface.

## Attestation And Cryptographic Conventions

The reference design uses:

- SHA-256 for hashing
- deterministic canonical serialization for structured records
- AES-256-GCM for authenticated encryption
- Ed25519 for signatures and keypairs where applicable
- TEE attestation with a 64-byte report-data field

Transport private keys are purpose-limited to connection security at the
public domain layer. Owner approvals, artifact-envelope signatures, owner
identity documents, Covehub domain write proofs, and other Cove protocol
signatures use owner signing keys over domain-separated canonical payloads.
This separation prevents the transport handshake from being treated as an
application-signing oracle.

Each Cove quote lays report data out as two concatenated SHA-256 hashes:

1. the hash of a fixed label string
2. the hash of a canonical payload

The labels are:

- `cove.key_release.v1`
- `cove.artifact.v1`
- `cove.node_certificate.v1`

The measured node manifest hash identifies which enclave produced the quote.
The report data binds application-level behavior or release context.

## RA-TLS

Long-running nodes may expose live network services. RA-TLS binds a live TLS
session to the attested node certificate:

1. The key manager generates an ephemeral keypair inside the TEE.
2. The service uses that private key as its TLS key.
3. The node certificate records the corresponding public key metadata.
4. A client verifies the node certificate and checks that the live TLS public
   key matches the public key recorded in the certificate.

This transfers the certificate's guarantees to the live session without
trusting the network path or service operator. The Cove RA-TLS check
intentionally bypasses WebPKI: the served certificate is self-signed by the
in-enclave key manager, and trust is rooted in the attested node certificate
on Covehub. Operators reaching the service through an HTTPS-terminating
gateway must use a passthrough URL form so the in-enclave certificate, not the
gateway's WebPKI cert, is presented at the TLS handshake; see
[operations/phala_deploy.md](operations/phala_deploy.md) for the
Phala-specific form.

## Security Model

For the full security model, including trusted assumptions, non-trusted
components, security goals, and residual risks, see
[security_model.md](security_model.md).

## Reference Example

The checked-in example is `hello_world`: Alice and Bob own two static
secret-word artifacts, Carol publishes and deploys the workflow, the runtime
produces two dynamic transformed-word artifacts, and the final service exposes
a long-running RA-TLS endpoint. It exercises the same core primitives
end-to-end:

- owner-controlled private artifacts
- deterministic node compilation
- reviewed per-node manifests
- node-scoped artifact approval
- dynamic artifact propagation
- dependency certificate gating
- schema-validated results
- an enclave-generated service keypair

See [hello_world.md](hello_world.md) for the detailed walkthrough.
