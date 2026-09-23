# Quickstart

> **User-facing quickstart.** For the full operational runbook (Phala deploy, the sidecar inventory,
> and the end-to-end hash chain), see [docs/internal/hello_world.md](internal/hello_world.md).

This walks through installing Cove and running the minimal
[`hello_world`](../demos/hello_world/) demo, which exercises every Cove primitive. For what the
pieces mean, see [concepts.md](concepts.md).

## Prerequisites

- Python 3.12+, [`uv`](https://docs.astral.sh/uv/), the Docker CLI, and Docker Compose v2.

A *real* deployment (running workflows on a TEE via Phala Cloud) additionally needs a Phala Cloud
account, a domain with valid HTTPS, and a public Docker registry. See the
[roles table](concepts.md#roles-at-a-glance).

## 1. Install

```bash
git clone https://github.com/covehub/cove.git
cd cove/cli
uv sync
uv run cove init      # creates ~/.cove with your owner identity and config
```

## 2. Validate and compile a workflow

```bash
# Syntax-check the authored workflow (offline: schema, DAG, hash anchors)
uv run cove check ../demos/hello_world/workflow/workflow.cove.yaml

# Lower it into one generated, sidecar-injected Docker Compose per node
uv run cove compile ../demos/hello_world/workflow/workflow.cove.yaml
```

`cove compile` produces the exact per-node composes that owners review and that the TEE measures.

## 3. Run the demo locally

The demo ships a scripted multi-party run. From
[`demos/hello_world/scripts`](../demos/hello_world/scripts/):

```bash
cp .env.example .env      # then fill in the Phala, Docker Hub, and Cloudflare tokens
docker compose up --build
```

This brings up the CoveHub API and UI plus the Alice, Bob, and Carol services, so you can watch a
full workflow run with every party present. See
[demos/hello_world/README.md](../demos/hello_world/README.md) for the details.

## 4. (Optional) deploy and verify

A **deployer** submits the workflow's nodes to a TEE:

```bash
uv run cove deploy <publisher>/<workflow_id> --phala-instance-type <type>
```

A **verifier** checks the running enclave's certificate chain and proxies it to a local port:

```bash
uv run cove client proxy \
  --remote <service-url> --local localhost:8080 \
  --workflow <publisher>/<workflow_id> --node <node_id> --keypair <name>
```

See the [roles table](concepts.md#roles-at-a-glance) for each role's full command sequence.

## Next steps

- [concepts.md](concepts.md) - what workflows, nodes, artifacts, and certificates are.
- [demos/](../demos/) - the confidential-benchmark workflows from the paper.
- [docs/internal/hello_world.md](internal/hello_world.md) - the full runbook for this demo.
