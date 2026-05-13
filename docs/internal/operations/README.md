# Cove Operations Runbooks

The operations layer is the "how to actually run this" side of the Cove
internal docs. Each runbook here covers one focused operational task; the
[end-to-end runbook](end_to_end.md) is the orchestrating walkthrough that
exercises all of them in sequence for the hello-world demo.

For the conceptual layer (architecture, security model, the demo
walkthrough mapped to primitives), start at the
[internal docs index](../README.md).

## Which Runbook Do I Read?

| If you want to … | Read |
| --- | --- |
| Stand up the full hello-world deploy from scratch | [end_to_end.md](end_to_end.md) |
| Run the Docker Compose Covehub API + UI stack and route it through Cloudflare | [covehub_server.md](covehub_server.md) |
| Understand the public Covehub browser UI: API surface, screens, env vars, tests | [ui.md](ui.md) |
| Run an owner service and publish a signed `/identity` | [owner_services.md](owner_services.md) |
| Build, push, and pin canonical first-party runtime sidecars | [runtime_release.md](runtime_release.md) |
| Build, install, and ship the Cove CLI wheel | [cli_release.md](cli_release.md) |
| Deploy on Phala Cloud and survive the deploy-time constraints | [phala_deploy.md](phala_deploy.md) |

## Conventions

These match the conventions in the [internal docs index](../README.md):

- Hello-world docs use concrete CoveHub-controlled demo hostnames. Generic
  owner-service runbooks use `<your domain>` only where the operator must
  supply their own public hostname.
- Personas (Alice, Bob) appear only in the hello-world walkthrough
  ([../hello_world.md](../hello_world.md)) and the
  [end-to-end runbook](end_to_end.md). Other runbooks use generic roles
  (operator, owner, publisher).
- The canonical container digests live in
  `cove/containers/canonical_container_digests.json` and travel with the
  CLI wheel; runbooks here reference the file rather than baking specific
  `@sha256:…` values into prose.
- Covehub's canonical full-stack run path is Docker Compose from `cove/`.
  Cloudflare routes for that stack target Compose service names
  (`covehub-api:8000`, `covehub-ui:8080`), not host loopback ports.
