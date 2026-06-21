# Cove

Cove is a framework for compositional, multi-party confidential workflows.
It lets mutually distrusting parties run workflows over private artifacts
inside trusted execution environments (TEEs), while producing public
certificates that bind runtime claims to the measured nodes that produced
them.

This repository is the reference implementation: the CLI, the runtime
sidecars, the Covehub storage server, the public browser UI, the demo
workflow, and all internal documentation.

## Repository Layout

Each directory has its own README. Start there for package-level details.

| Path | What it is |
| --- | --- |
| [cli/](cli/) | The `cove` CLI: provision artifacts, author and compile workflows, owner review, deploy to Phala Cloud, run owner services. |
| [server/](server/) | Covehub: the FastAPI typed object server (`api.covehub.io`). Stores hash-addressed bytes; not part of the Cove trust root. |
| [ui/](ui/) | The public read-only Covehub browser (`covehub.io`). React frontend + small FastAPI indexer. Convenience discovery, not an integrity oracle. |
| [containers/](containers/) | First-party runtime sidecar images (`cove-base`, `cove-artifact-provisioner`, `cove-precondition-checker`, …) and the canonical digest set the CLI ships. |
| [cove_container_runtime/](cove_container_runtime/) | Shared Python library used by the sidecar images for attestation, certificates, and the JsonLogic precondition subset. |
| [demos/hello_world/](demos/hello_world/) | The reference workflow that exercises every primitive end-to-end: Alice and Bob as data owners, Carol as publisher/deployer, static and dynamic artifacts, dependency-certificate gating, JsonLogic preconditions, and a long-running RA-TLS service. |
| [docs/internal/](docs/internal/) | Internal-developer documentation: architecture, security model, operations runbooks, and the engineering TODO list. |
| [scripts/](scripts/) | Top-level helper scripts. |

## Top-Level Files

- [compose.yaml](compose.yaml) — the canonical Covehub deployment: API, public UI, and Cloudflare Tunnel as a single Compose stack.
- [.env.example](.env.example) — template for the private Compose environment. Copy it locally, set the Cloudflare tunnel token, and optionally tune local smoke ports. The real env file is gitignored.

## Quick Start

If you want to run the hello-world demo against Phala Cloud, the
authoritative walkthrough is [docs/internal/operations/end_to_end.md](docs/internal/operations/end_to_end.md).

If you only want to bring up Covehub itself (API + UI + tunnel):

```bash
cd /path/to/cove
cp .env.example .env
$EDITOR .env  # set the Cloudflare tunnel token
docker compose up -d --build
```

Verify locally and publicly:

```bash
curl -fsS http://127.0.0.1:3518/healthz
curl -fsS http://127.0.0.1:3517/ui-api/healthz
curl -A 'cove-runtime/0.0.1' -fsS https://api.covehub.io/healthz
curl -fsS https://covehub.io/
```

Full deployment runbook: [docs/internal/operations/covehub_server.md](docs/internal/operations/covehub_server.md).

## Documentation

The conceptual and operational documentation lives under
[docs/internal/](docs/internal/). The index there is the right entry point;
in reading order:

1. [docs/internal/architecture.md](docs/internal/architecture.md) — what Cove is, the object model, the workflow lifecycle, attestation.
2. [docs/internal/security_model.md](docs/internal/security_model.md) — trust boundary, what Cove guarantees, residual risks.
3. [docs/internal/hello_world.md](docs/internal/hello_world.md) — the demo walkthrough mapped to architecture primitives.
4. [docs/internal/operations/](docs/internal/operations/) — focused runbooks (Covehub server, UI, owner services, runtime release, CLI release, Phala deploy, end-to-end).
5. [docs/internal/todos.md](docs/internal/todos.md) — known engineering follow-ups.
