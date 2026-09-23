# Concepts

> **User-facing concepts.** For the full object model, trust model, and verification procedure, see
> [docs/internal/architecture.md](internal/architecture.md) and
> [docs/internal/security_model.md](internal/security_model.md).

## Core concepts

- **Workflow** - a directed acyclic graph (DAG) of nodes, together with the artifacts, owners, and dependencies that connect them. A workflow has a canonical, hash-addressed serialization, so any party can recompute its identity from its bytes.
- **Node** - the unit of TEE execution. Each node runs in its own enclave, may consume artifacts, may depend on upstream nodes' certificates, runs one or more services, and emits a node certificate.
- **Service** - an ordinary container workload inside a node. Terminating services produce a certificate result; long-running services (e.g. an inference API) serve indefinitely.
- **Artifact** - a unit of private data owned by exactly one party. **Static** artifacts exist up front and are encrypted and uploaded before execution; **dynamic** artifacts are produced inside an enclave during execution and encrypted under the owner's key.
- **Precondition** - a JsonLogic predicate that gates a service on its input hashes and on fields from upstream nodes' certificates. This is where a workflow encodes rules like "only run if the audit passed."
- **Node certificate** - binds a node's measured runtime manifest, admitted inputs, verified dependencies, outputs, and schema-valid results to a TEE attestation. Downstream nodes and external verifiers rely on it.
- **Ephemeral keypair** - a keypair generated fresh inside the enclave (Ed25519) that a service can use as a TLS identity, giving clients an attested channel to the enclave.
- **CoveHub** - an untrusted storage and transport layer for workflows, encrypted artifacts, and certificates. Everything on it is hash-checkable, encrypted, or signed, so tampering is detectable. A public instance runs at [covehub.io](https://covehub.io).
- **TEE / attestation** - the Trusted Execution Environment that runs each node and emits a hardware-signed quote over what it ran. The reference implementation uses Intel TDX via Phala Cloud's dstack.

## Roles at a glance

A Cove workflow involves four roles. Any party may hold any combination of them, and no role requires
trusting any other.

| Role | What they do | Key commands |
|---|---|---|
| **Publisher** | Compile the workflow and publish the canonical workflow object. | `cove init` → `cove check` → `cove compile` → `cove push` |
| **Owner** | Control private artifacts; run a provisioning service that releases artifact keys only to attested, approved nodes. | `cove init` → `cove start <port>` → `cove provision [--overwrite] <name> <file>` → `cove provision inspect <pub>/<wf>` *(review + approve)* or `cove provision allow <artifact_id> <compose>` |
| **Deployer** | Submit node manifests to the TEE platform for execution. | `cove deploy <pub>/<wf> --phala-instance-type <type>` |
| **Verifier** | Read a terminal certificate and check it and its full upstream chain. Anyone with the workflow bytes and the TEE vendor's roots can verify. | `cove client proxy --remote <url> --local localhost:8080 --workflow <pub>/<wf> --node <node_id> --keypair <name>`, or `cove pull` / `cove hub inspect` |

Operating a CoveHub instance is *not* a trust role: Covehub is untrusted, so any party (or a third party)
may run one with the root [`compose.yaml`](../compose.yaml) and a `.env` (from
[`.env.example`](../.env.example)). To exercise every role locally as all parties, use the scripted
`hello_world` demo (see [quickstart.md](quickstart.md)).

> **Before you approve a workflow (Owners, read this).** Approving releases your private artifacts to
> the enclaves a workflow runs, and Cove cannot protect you from approving a bad one. `cove provision
> inspect` prints every node compose that touches your artifacts and asks you to confirm. Check three
> things: **intent** (does the workflow do exactly what you agreed to, with no extra service that
> exfiltrates your data?), **containers** (have you audited each image at its pinned digest?), and
> **preconditions** (do they actually bind the guarantees you care about, e.g. that serving code was
> audited before it touched your weights?).

For the full trust model and the step-by-step verification procedure, see
[docs/internal/architecture.md](internal/architecture.md) and
[docs/internal/security_model.md](internal/security_model.md).
