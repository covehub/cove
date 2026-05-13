# Covehub UI

Covehub ships a public read-only browser at `https://covehub.io`. The UI is
a convenience discovery layer over the Covehub data root — it lets people
see what workflows, artifacts, and runtime certificates exist, surfaces
their hashes, and offers copyable `cove hub …` commands for local
verification. It is **not** an integrity oracle: it never serves raw object
bytes for download, and it does not verify attestations. See
[../security_model.md](../security_model.md) for the formal trust boundary
and [`cove/ui/README.md`](../../../ui/README.md) for the package quick-start.

The UI consists of two pieces in one container: a React/Vite frontend and a
small FastAPI indexer that scans the public Covehub data root. Deployment
sits behind the same `cloudflared` tunnel as the API; see
[covehub_server.md](covehub_server.md) for the full compose stack and DNS
routing.

The canonical deployment is the Docker Compose stack from `cove/`:

```bash
cd /home/$USER/mats/cove
cp .env.example .env
$EDITOR .env  # set CLOUDFLARED_TOKEN
docker compose up -d --build
```

Smoke checks:

```bash
curl -fsS http://127.0.0.1:3517/ui-api/healthz
curl -fsS http://127.0.0.1:3517/ui-api/summary
curl -fsS https://covehub.io/
```

On a fresh Docker volume, an empty UI is expected until owners publish
artifacts and the publisher pushes at least one workflow to the API.

## What The UI Mounts And Does Not Mount

The container mounts only the public Covehub data root, **read-only**:

```text
covehub-data:/var/lib/covehub/data:ro
```

It does not authenticate visitors, does not write to Covehub, and does not
serve raw object downloads. Object
detail pages render summaries, structured metadata, and a bounded preview;
they do not expose the raw payload bytes over HTTP.

## Indexer API Surface

The FastAPI app (`cove/ui/backend/covehub_ui/app.py`) exposes five JSON
routes under `/ui-api/`. They are read by the SPA and are also useful when
debugging from the host.

| Route | Purpose |
| --- | --- |
| `GET /ui-api/healthz` | `{ok, data_root}` for readiness checks. |
| `GET /ui-api/summary` | Object count, total bytes, kind counts, owners, publishers, workflow refs. |
| `GET /ui-api/objects` | Paginated list. Optional `kind` filter and `q` full-text query; `limit` clamped to `[1, 1000]`, default `200`. |
| `GET /ui-api/objects/{hub_path:path}` | Detail view for one object. Returns 404 if absent, 400 if the path isn't a valid Covehub object path. |
| `GET /ui-api/glossary` | Twelve domain terms with cross-references; powers the Glossary screen. |

The SPA itself is served from `/`, with built assets under `/assets/` and
`cove/docs/` mounted read-only at `/docs/`.

The indexer scans the data root recursively and only recognizes four typed
object shapes (`cove/ui/backend/covehub_ui/indexer.py:339`):

```text
artifacts/{owner}/{artifact_name}/{reference}                 → static_artifact
workflows/{publisher}/{workflow_id}/{reference}               → workflow
runtime/{publisher}/{workflow_id}/certificates/{node_id}/{ref} → runtime_certificate
runtime/{publisher}/{workflow_id}/artifacts/{artifact_name}/{ref} → runtime_artifact
```

`{reference}` is either the literal string `latest` or `sha256:<64 hex>`.
Files that don't match a typed shape, or whose names contain `..` or
absolute paths, are silently ignored. Object detail rejects unsafe paths
with 400 (`UnsafeObjectPathError`).

For each indexed object the indexer recomputes the SHA256 on read and
returns both `observed_digest` and `observed_exact_hub_path`. When a
`latest` alias points at a payload whose digest doesn't match the
ostensible exact path, the UI surfaces the mismatch but does not "reject"
it — the bar is "make it visible," not "be the verifier."

The indexer caches the full record list for `COVE_UI_CACHE_TTL_SECONDS`
(default `5`) so a single browser refresh doesn't re-stat the whole tree.

## Frontend Views

The SPA (`cove/ui/src/main.jsx`) is a hash-routed React app with four
screens:

- **Workflows** — table of workflow bundles, drilling into five tabs:
  *Definition* (normalized YAML), *Compose* (per-node compose files),
  *Diagram* (dagre-laid-out node/artifact/certificate graph with
  click-through), *Raw workflow*, and *Raw compose*.
- **Artifacts** — static and runtime artifacts, with version history
  (`latest` vs exact-digest paths grouped together) and inferred
  `used_by` / `produced_by` relations from workflow manifests.
- **Certificates** — runtime certificates with the generating node and any
  consuming nodes (declared dependency or precondition reference).
- **Glossary** — twelve cross-linked terms (CoveHub, Publisher, Owner,
  Artifact, Workflow Bundle, Node, Runtime Certificate, Precondition, TEE,
  Attestation, latest, Exact Digest Path).

UX details that aren't obvious from the code: hash chips and `cove hub …`
commands are click-to-copy, the latest-alias warning surfaces the observed
exact path, the bounded preview always shows whether it was truncated, and
preconditions on node cards in the diagram render their JsonLogic
expression with clickable artifact and certificate references.

## Environment Variables

The indexer reads its config from environment variables; container
defaults are baked into `cove/ui/Dockerfile`. The Compose service threads
them through `cove/.env.example`.

| Variable | Default | Purpose |
| --- | --- | --- |
| `COVE_UI_DATA_ROOT` | `/var/lib/covehub/data` | Covehub public data root to scan. Must be the same volume the API writes to (read-only mount on the UI side). |
| `COVE_UI_CACHE_TTL_SECONDS` | `5` | In-memory record cache lifetime in seconds. |
| `COVE_UI_PREVIEW_BYTES` | `4096` | Max bytes returned in the object detail preview. |
| `COVE_UI_STATIC_ROOT` | `/app/static` | Built React bundle directory. The SPA is mounted only when this directory exists. |
| `COVE_UI_DOCS_ROOT` | `/app/docs` | Markdown docs directory mounted at `/docs/`. Optional. |

## Compose Integration

In `cove/compose.yaml`, the UI service builds from `ui/Dockerfile` (Node
20 multi-stage frontend → Python 3.12 slim backend), listens on internal
port `8080`, and is published on the host at `127.0.0.1:${COVEHUB_UI_PORT}`
(default `3517`). It mounts the shared `covehub-data` volume read-only and
is routed at `https://covehub.io` via `cloudflared`.

The Cloudflare Tunnel route for the UI must target the Compose service name:

```text
covehub.io -> http://covehub-ui:8080
```

Do not route the public hostname to `http://localhost:3517` when
`cloudflared` runs inside Compose. In that container, `localhost` is the
tunnel container itself, so the public route will return 502 even while
`http://127.0.0.1:3517` works from the host.

`depends_on: [covehub-api]` sets startup order only; there is no runtime
call from the UI to the API.

## Local Development

Backend tests:

```bash
cd cove/ui
uv sync --extra dev
uv run --extra dev pytest tests
```

Frontend dev server (Vite, host-bound to loopback on port `5173`):

```bash
cd cove/ui
npm install
npm run dev
```

Frontend production build:

```bash
cd cove/ui
npm run build
```

Playwright UI tests run against the dev server in two device profiles
(`chromium-desktop`, `chromium-mobile` Pixel 7):

```bash
cd cove/ui
npm run test:ui
```

## What The UI Deliberately Does Not Do

- **No raw downloads.** Object detail returns metadata + bounded preview,
  not the raw bytes. Use the `cove hub get` command the detail page shows.
- **No auth surface.** The container does not expose login or write routes.
- **No verification.** The UI displays the certificate body and
  attestation format string but does not verify the attestation; that is
  the verifier's job, with local Cove tooling.
- **No mutation of the alias-vs-digest relationship.** When `latest` is
  stale, the UI shows the observed digest alongside the alias; it does not
  rewrite or reject the alias.
