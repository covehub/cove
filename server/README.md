# Covehub Server

This directory is the Covehub server project: a FastAPI application that
acts as Cove's public storage and transport layer. Covehub stores typed,
hash-addressed bytes on a local filesystem data root. It has no user
database, no registration flow, and no bearer-token login surface.

Covehub is **not** part of Cove's integrity root. Every object it stores
is hash-checkable, encrypted, or attested so tampering is independently
detectable; see `docs/internal/security_model.md` for the formal trust
boundary.

For the architecture and object model, see
`docs/internal/architecture.md`. For the operational runbook (loopback
binding, Cloudflare Tunnel, state inspection), see
`docs/internal/operations/covehub_server.md`. For known engineering
follow-ups, see `docs/internal/todos.md`.

## Requirements

Project dependencies from `server/pyproject.toml`:

- Python 3.12+
- `fastapi`
- `uvicorn`
- `cove-container-runtime`

Development/test dependencies:

- `httpx`
- `pytest`

External runtime requirements:

- `uv`
- local filesystem access

Docker is not required to run the server itself. The deployment runbook
runs the FastAPI process directly under `uv` and fronts it with a
`cloudflared` Docker container; see
`docs/internal/operations/covehub_server.md`.

For Phala deployments, the hub must be reachable at a public HTTPS URL.
The server host does not need a Phala Cloud API key; that deploy
credential lives in the operator's `<cove_home>/config.yaml` and is used
only by `cove deploy` on the operator machine.

## API Summary

### Health

- `GET /healthz`

Response shape:

```json
{
  "ok": true,
  "app": "ok",
  "data_root": "ok"
}
```

### Domain Write Proofs

Static artifacts, workflow bundles, and runtime resets are authorized by
the owner URL domain in the route. Covehub fetches and verifies the current
`<owner_url>/identity` document over ordinary HTTPS, confirms the request
identity has the same owner public key and matching route domain, then checks an
Ed25519 write signature with the served public key. Covehub does not pin or
compare owner-service TLS certificates, and it does not require exact
byte-for-byte equality between the request identity document and the current
served document.

Required headers:

- `X-Cove-Owner-Identity`: base64url canonical JSON owner identity
- `X-Cove-Write-Timestamp`: fresh UTC ISO timestamp
- `X-Cove-Write-Signature-Algorithm: ed25519`
- `X-Cove-Write-Signature`: base64 Ed25519 signature over method, path,
  payload hash, owner domain, identity hash, timestamp, and purpose
  `covehub_domain_write_v1`

### Typed Objects

Covehub stores typed, named objects. Exact object routes end in
`sha256:<digest>` and are immutable. A successful exact upload also advances
that namespace's mutable `latest` pointer.

- Static artifact exact:
  `PUT`/`GET`/`HEAD /v1/artifacts/{owner}/{artifact_name}/sha256:{digest}`
- Static artifact latest:
  `GET`/`HEAD /v1/artifacts/{owner}/{artifact_name}/latest`
- Workflow exact:
  `PUT`/`GET`/`HEAD /v1/workflows/{publisher}/{workflow_id}/sha256:{digest}`
- Workflow latest:
  `GET`/`HEAD /v1/workflows/{publisher}/{workflow_id}/latest`

Rules:

- `sha256:<digest>` uses lowercase 64-character SHA-256 hex.
- `{owner}` and `{publisher}` are lowercase DNS hostnames with dots, for
  example `alice.example.test`; schemes, ports, slashes, and uppercase
  letters are rejected.
- `PUT` for static artifacts and workflows requires a signed domain write
  proof for the owner or publisher domain.
- `PUT` verifies that `sha256(payload)` matches the exact path segment.
- Exact objects are immutable; identical repeat uploads return `200`.
- `latest` is a convenience pointer, not a reproducibility guarantee.

### Runtime Objects

- Runtime node certificate exact:
  `PUT`/`GET`/`HEAD /v1/runtime/{publisher}/{workflow_id}/certificates/{node_id}/sha256:{digest}`
- Runtime node certificate latest:
  `GET`/`HEAD /v1/runtime/{publisher}/{workflow_id}/certificates/{node_id}/latest`
- Runtime artifact exact:
  `PUT`/`GET`/`HEAD /v1/runtime/{publisher}/{workflow_id}/artifacts/{artifact_name}/sha256:{digest}`
- Runtime artifact latest:
  `GET`/`HEAD /v1/runtime/{publisher}/{workflow_id}/artifacts/{artifact_name}/latest`
- Runtime reset:
  `DELETE /v1/runtime/{publisher}/{workflow_id}`

Runtime artifact and certificate writes remain unauthenticated by user
accounts. They are gated by Phala/dstack attestation evidence. Runtime reset
uses the signed domain write proof for `{publisher}`.

Runtime node certificate uploads:

- require payload digest validation against `{digest}`
- require a JSON object payload
- verify `certificate_body_hash`
- verify certificate body workflow id, node id, and generated compose hash
- verify the certificate attestation bundle against the uploaded headers
- store the certificate as an immutable object

Required node-certificate headers:

- `X-TDX-Quote`
- `X-Cove-Node-Id`
- `X-Cove-Compose-Hash`
- optional `X-TDX-Event-Log`

Runtime artifact envelope uploads:

- require payload digest validation against `{digest}`
- verify report data for the workflow id, artifact name, node id, and compose
  hash carried in headers
- store the opaque encrypted envelope as an immutable object

Required runtime artifact headers:

- `X-Cove-Workflow-Id`
- `X-Cove-Artifact-Name`
- `X-Cove-Node-Id`
- `X-Cove-Compose-Hash`
- `X-Cove-Attestation-Format`
- `X-Cove-Report-Data`
- `X-TDX-Quote`
- optional `X-TDX-Event-Log`

After upload, runtime objects can be fetched through their exact route or the
matching `latest` route.

## Storage Layout

Published bytes are written under typed route-like namespaces:

```text
data/
  artifacts/<owner>/<artifact_name>/sha256:<digest>
  artifacts/<owner>/<artifact_name>/latest
  workflows/<publisher>/<workflow_id>/sha256:<digest>
  workflows/<publisher>/<workflow_id>/latest
  runtime/<publisher>/<workflow_id>/certificates/<node_id>/sha256:<digest>
  runtime/<publisher>/<workflow_id>/certificates/<node_id>/latest
  runtime/<publisher>/<workflow_id>/artifacts/<artifact_name>/sha256:<digest>
  runtime/<publisher>/<workflow_id>/artifacts/<artifact_name>/latest
```

Covehub does not store artifact keys. Artifact encryption keys remain
owner-local in Cove homes.

## Configuration

Environment variables:

- `COVE_SERVER_DATA_ROOT`
- `COVE_SERVER_HOST`
- `COVE_SERVER_PORT`
- `COVE_SERVER_QUOTE_VERIFIER`

Defaults are local to this directory:

- data root: `server/data`
- host: `127.0.0.1`
- port: `8000`
- verifier mode: `phala_dstack`

`COVE_SERVER_QUOTE_VERIFIER` must be `phala_dstack`.

## Container Deployment (Recommended)

The repo root includes a Compose stack that runs the production-shaped Covehub
deployment:

- `covehub-api`: this server
- `covehub-ui`: the read-only public browser UI
- `cloudflared`: the Cloudflare Tunnel connector

From `cove/`:

```bash
cp .env.example .env
$EDITOR .env  # set CLOUDFLARED_TOKEN
docker compose up -d --build
```

Local smoke checks:

```bash
curl -fsS http://127.0.0.1:3518/healthz
curl -fsS http://127.0.0.1:3517/ui-api/healthz
```

Configure Cloudflare public hostname routes to the Docker service names on the
Compose network:

```text
covehub.io      -> http://covehub-ui:8080
api.covehub.io  -> http://covehub-api:8000
```

Do not route the Compose-managed tunnel to `localhost:3517` or
`localhost:3518`; inside the tunnel container those addresses point back at
the tunnel container and produce public 502s.

For the full deployment runbook, including state inspection and migration from
older manual runs, see `docs/internal/operations/covehub_server.md`.

## Development Loopback

For API-only package development, run the server directly from the repo root:

```bash
cd server
uv sync
uv run covehub-server
```

Check health:

```bash
curl -sS http://127.0.0.1:8000/healthz
```

This direct `uv` path does not run the public browser UI or the Cloudflare
Tunnel connector.

Run tests:

```bash
cd server
uv sync --extra dev
uv run --extra dev pytest tests
```

For the full-stack run, use the Compose deployment above.
