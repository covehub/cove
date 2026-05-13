# Cove Internal Documentation

Internal-developer documentation for Cove. User-facing docs live elsewhere;
everything under this tree assumes the reader is working on Cove itself or
operating it on behalf of the project.

## Reading Order

If you just want to run the demo, jump straight to
[operations/end_to_end.md](operations/end_to_end.md) and treat
[operations/README.md](operations/README.md) as the runbook map. The
ordered list below is the conceptual deep-dive.

For someone new to the project:

1. [architecture.md](architecture.md) — what Cove is, the object model, the
   workflow lifecycle, and how attestation binds runtime claims to reviewed
   compose hashes.
2. [security_model.md](security_model.md) — the security guarantees, what
   Cove trusts, what it explicitly does not, and the residual risks.
3. [hello_world.md](hello_world.md) — the smallest workflow that exercises
   every primitive, with full per-node sidecar inventory and the hash chain
   from secret-word ingestion to RA-TLS.
4. [operations/end_to_end.md](operations/end_to_end.md) — the operational
   walkthrough of the hello-world deploy, linking out to the focused
   operations docs.
5. [todos.md](todos.md) — known engineering follow-ups.

## Contents

| Document | Purpose |
| --- | --- |
| [architecture.md](architecture.md) | Canonical architecture and object model. |
| [security_model.md](security_model.md) | Trust boundary, security guarantees, residual risks. |
| [hello_world.md](hello_world.md) | Demo walkthrough mapped to the architecture primitives. |
| [todos.md](todos.md) | Engineering follow-ups not tracked in code. |
| [operations/README.md](operations/README.md) | Map of the operations runbooks: which one to read for which task. |
| [operations/covehub_server.md](operations/covehub_server.md) | Run the Docker Compose Covehub API and public UI stack behind Cloudflare Tunnel. |
| [operations/ui.md](operations/ui.md) | Public CoveHub browser UI: indexer surface, frontend views, env vars, tests. |
| [operations/owner_services.md](operations/owner_services.md) | Run owner services and publish signed identities. |
| [operations/runtime_release.md](operations/runtime_release.md) | Build and publish first-party runtime sidecars. |
| [operations/cli_release.md](operations/cli_release.md) | Build, install, and ship the Cove CLI wheel. |
| [operations/phala_deploy.md](operations/phala_deploy.md) | Deploy on Phala Cloud, including the non-obvious deploy-time constraints. |
| [operations/end_to_end.md](operations/end_to_end.md) | Hello-world end-to-end runbook that links the rest together. |

## Conventions

- **Hostnames.** Hello-world docs use concrete CoveHub-controlled demo
  hostnames. Generic owner-service runbooks use `<your domain>` only where
  the operator must supply their own public hostname.
- **Personas.** Alice and Bob appear only in
  [hello_world.md](hello_world.md) and the
  [end-to-end runbook](operations/end_to_end.md) that runs that demo. Other
  documents use generic roles (operator, owner, publisher).
- **Image digests.** Documentation does not bake specific
  `@sha256:<digest>` values into prose; the canonical digest set lives in
  `containers/canonical_container_digests.json` and is what the CLI ships.
- **Covehub deployment.** The canonical Covehub run path is the Docker Compose
  API + UI + tunnel stack from `cove/`; see
  [operations/covehub_server.md](operations/covehub_server.md).
