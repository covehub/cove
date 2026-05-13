# Cove Security Model

This document describes the security properties Cove provides for compiled
workflows, first-party sidecars, dependency certificates, and artifact-release
gating. It also calls out what Cove explicitly does not trust and which
guarantees only hold in hardware-backed attestation mode.

For the architectural model these properties derive from, see
[architecture.md](architecture.md). For known gaps and follow-up work, see
[todos.md](todos.md).

## High-Level Model

Cove treats each generated per-node Docker Compose file as the reviewable unit
of execution.

Each node compose:

- contains compiler-injected first-party sidecars,
- is hashed after generation,
- and is expected to run exactly as reviewed by the parties who approve it.

When a node depends on earlier nodes, Cove compiles nodes in stable
topological order and bakes the expected upstream node compose hashes into the
downstream dependency-fetch configuration. Later nodes therefore validate not
just "some certificate from node X", but a certificate from the exact expected
workflow id, node id, and generated compose hash for node X.

Dependency-free nodes do not include `cove_dependency_certificate_fetcher`
because there are no upstream runtime certificates to wait on. Nodes with
dependencies gate on that fetcher before their preconditions and workloads
run.

## Security Guarantees

### 1. Downstream nodes bind to exact upstream composes

For every node with `dependencies:`, Cove compiles upstream nodes first and
stores their generated compose hashes. The downstream compose embeds:

- `expected_workflow_id`
- `expected_node_id`
- `expected_generated_node_compose_hash`

The dependency fetcher refuses to write a dependency certificate unless all of
those values match and the dependency certificate's attestation verifies.

### 2. Node certificates bind to the reviewed node compose

`cove_node_certificate_writer` produces a node certificate whose
`certificate_body` includes the node's generated compose hash.

The sidecar then creates attestation report data over:

- the canonical hash of the certificate body,
- and the generated node compose hash.

The uploaded certificate also carries redundant explicit fields in the
attestation bundle so verifiers can confirm that:

- the attested certificate body hash matches the actual certificate body,
- the attested compose hash matches the certificate body compose hash,
- and the attested node id matches the certificate body node id.

### 3. Artifact release is bound to node identity and provisioner identity

`cove_artifact_provisioner` requests key release with attestation report data
that commits to:

- workflow publisher,
- workflow id,
- node id,
- node compose hash,
- canonical artifact-provisioner digest.

The provisioning server verifies that attestation before it checks the local
allow rule. A valid allow rule alone is not enough if the attestation identity
does not match.

### 4. User workloads cannot claim compiler-owned sidecar surfaces

The compiler/checker rejects authored workload compose that tries to reuse
compiler-owned surfaces, including:

- service names starting with `cove_`,
- `COVE_CONFIG_JSON`, `COVE_SERVICE_NAME`, or `COVE_COMPOSE_HASH`,
- canonical first-party sidecar images.

This prevents user-authored services from impersonating compiler sidecars.

### 5. Dangerous authored Docker settings are blocked

Authored workload compose fails validation if it uses:

- `privileged`
- `devices`
- `device_cgroup_rules`
- `cap_add`
- `security_opt`
- `pid`
- `ipc`
- `network_mode`

Authored workload services are also blocked from binding into compiler-managed
control paths such as `/cove`, `./runtime`, and `./assets`.

### 6. Quote sockets are not mounted into workloads

When attestation mode is `phala_dstack` on `platform.provider=phala` and
`platform.runtime=dstack`, Cove mounts `/var/run/dstack.sock` only into the
first-party sidecars that need to generate quotes. The compiler does not mount
that socket into user-authored workload services.

### 7. Synthetic attestation is excluded from production paths

Synthetic and mock attestation helpers exist in dedicated test-support code
for automated coverage. They are not reachable from the runtime, compiler,
provisioner, or server code paths used in production. Production claims
require the hardware-backed `phala_dstack` attestation mode.

### 8. Public owner routing stays outside the trust root

Owner services are reached at public HTTPS hostnames fronted by Cloudflare
Tunnel. That routing layer provides reachability only:

- compile resolves and verifies the signed `/identity` document,
- Covehub accepts owner/publisher mutations only after verifying the public key
  currently served at `<owner_url>/identity` matches the request identity and
  validates the matching domain write proof,
- runtime verifies signed key-release responses against the baked owner public
  key,
- artifact release trust still lives with the owner's local service, keys, and
  allow rules.

Public reachability does not turn the routing layer into a new approval or
integrity authority.

## Trust Assumptions

### Trusted

- Intel TDX and the relevant CPU/hardware attestation roots.
- The TEE runtime/provider path used for quote generation and verification.
- Docker Engine and Docker Compose correctly enforcing the generated compose.
- The canonical first-party sidecar images and their pinned digests.
- The public Cove source code corresponding to the reviewed compose and
  sidecars.
- The humans approving artifact allow rules and reviewing generated node
  composes.

### Explicitly not trusted

- The Covehub server.
- Cloudflare, DNS, or the `cloudflared` public ingress tunnel.
- The orchestration layer that launches Docker Compose files.
- Owner-service metadata or hosting environment.
- Network transport and ordinary server-side metadata.

This separation is intentional. Cove is designed so those components can sit
outside a TEE and still be acceptable, because runtime release and dependency
checks rely on reviewed composes, pinned first-party sidecars, and verified
attestation rather than on the services themselves.

## Operational Assumptions

Cove assumes:

- operators inspect each generated node compose before allowing artifact access,
- owners inspect the provisioning-server code they run,
- provisioning decisions are trusted only when they originate from the parties
  themselves,
- and all relevant source code is public and inspectable.

If those review steps do not happen, the remaining technical controls are still
useful, but they no longer provide the full review-and-attest workflow Cove is
designed around.

## Residual Risks

### Cove does not prove a workload can never generate a quote

Cove avoids mounting known quote sockets or devices into workloads, and it
warns on obvious references such as `vsock` or `/var/run/dstack.sock`.
However, Docker policy alone cannot prove that arbitrary guest code cannot
generate a quote if the platform exposes quote instructions or quote channels
globally inside the guest. The narrower guarantee is:

- Cove will not mount the dstack quote socket into user workloads.

### Publisher bundles are not signed

Generated workflow bundles are hashed and reviewable, but the published bundle
format is not signed by an application-level publisher key. Review and
compose-hash binding reduce risk here, but a publisher-signing layer is still
missing. Tracked under [todos.md](todos.md).

### Some first-party sidecars use host networking

Several compiler-injected sidecars run with `network_mode: host`. This is a
deliberate tradeoff for the current protocol shape — they need direct access
to the loopback Covehub origin and to owner services — but it broadens their
ambient network reach and is a real residual risk. Narrowing this is tracked
under [todos.md](todos.md).

### Local raw-key development path

The local development provisioning flow includes a direct raw-key release path
for demos and offline workflows. It is convenient but is not equivalent to
production attested key release and should not be treated as one.

### Docker and host compromise are out of scope

If the Docker daemon, host kernel, or trusted TEE stack is compromised, Cove's
guarantees no longer hold. Cove narrows the amount of trust placed in higher
level services, but it depends on the container runtime and the underlying
trusted hardware path.

## Practical Summary

The intended production story is:

1. Parties review the generated per-node composes.
2. Owners approve artifact release for specific reviewed node composes.
3. Nodes run compiler-injected first-party sidecars with pinned digests.
4. Artifact release and dependency acceptance are both conditioned on verified
   attestation tied to those reviewed compose hashes.

The intended non-story is just as important:

- Cove does not ask anyone to trust the orchestration layer, the Covehub
  server, or provisioning-server metadata as integrity roots.
- It does ask anyone to trust the hardware attestation path, Docker, the public
  reviewed code, and the human review/approval workflow around generated
  composes.
