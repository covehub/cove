# Cove Internal Documentation

Internal-developer documentation for Cove. User-facing docs live elsewhere;
everything under this tree assumes the reader is working on Cove itself or
operating it on behalf of the project.

## Reading Order

If you just want to run the smallest demo, jump straight to
[hello_world.md](hello_world.md). For the attested confidential benchmark vLLM CPU
demo, use [attested_confidential_benchmark__vllm_cpu.md](attested_confidential_benchmark__vllm_cpu.md).
The ordered list below is the conceptual deep-dive.

For someone new to the project:

1. [architecture.md](architecture.md) — what Cove is, the object model, the
   workflow lifecycle, and how attestation binds runtime claims to reviewed
   compose hashes.
2. [security_model.md](security_model.md) — the security guarantees, what
   Cove trusts, what it explicitly does not, and the residual risks.
3. [hello_world.md](hello_world.md) — the smallest workflow that exercises
   every primitive, with the full end-to-end runbook from local setup through
   Phala deployment and RA-TLS verification.
4. [attested_confidential_benchmark__vllm_cpu.md](attested_confidential_benchmark__vllm_cpu.md)
   — the confidential benchmark vLLM CPU workflow, including private artifact prep,
   audit/compile/benchmark/deploy, and the client UI.
5. [todos.md](todos.md) — known engineering follow-ups.

## Contents

| Document | Purpose |
| --- | --- |
| [architecture.md](architecture.md) | Canonical architecture and object model. |
| [security_model.md](security_model.md) | Trust boundary, security guarantees, residual risks. |
| [hello_world.md](hello_world.md) | Hello-world end-to-end runbook and primitive walkthrough. |
| [attested_confidential_benchmark__vllm_cpu.md](attested_confidential_benchmark__vllm_cpu.md) | Attested confidential benchmark vLLM CPU end-to-end runbook and client verification flow. |
| [todos.md](todos.md) | Engineering follow-ups not tracked in code. |
| [operations/README.md](operations/README.md) | Map of the operations runbooks: which one to read for which task. |
| [operations/covehub_server.md](operations/covehub_server.md) | Run the Docker Compose Covehub API and public UI stack behind Cloudflare Tunnel. |
| [operations/ui.md](operations/ui.md) | Public CoveHub browser UI: indexer surface, frontend views, env vars, tests. |
| [operations/owner_services.md](operations/owner_services.md) | Run owner services and publish signed identities. |
| [operations/runtime_release.md](operations/runtime_release.md) | Build and publish first-party runtime sidecars. |
| [operations/cli_release.md](operations/cli_release.md) | Build, install, and ship the Cove CLI wheel. |
| [operations/phala_deploy.md](operations/phala_deploy.md) | Deploy on Phala Cloud, including the non-obvious deploy-time constraints. |

## Conventions

- **Hostnames.** Hello-world docs use concrete CoveHub-controlled demo
  hostnames. Generic owner-service runbooks use `<your domain>` only where
  the operator must supply their own public hostname.
- **Personas.** Alice and Bob appear in concrete demo runbooks such as
  [hello_world.md](hello_world.md) and
  [attested_confidential_benchmark__vllm_cpu.md](attested_confidential_benchmark__vllm_cpu.md).
  Other documents use generic roles (operator, owner, publisher).
- **Image digests.** Documentation does not bake specific
  `@sha256:<digest>` values into prose; the canonical digest set lives in
  `containers/canonical_container_digests.json` and is what the CLI ships.
- **Covehub deployment.** The canonical Covehub run path is the Docker Compose
  API + UI + tunnel stack from `cove/`; see
  [operations/covehub_server.md](operations/covehub_server.md).
