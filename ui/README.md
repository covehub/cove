# CoveHub UI

This package contains the public read-only CoveHub browser.

The UI has two pieces in one container:

- a React/Vite frontend
- a small FastAPI indexer under `/ui-api/*`

The indexer scans the CoveHub public data root only. It does not mount or read
`covehub.sqlite3`, does not authenticate users, does not write to CoveHub, and
does not serve raw object downloads. Object pages show `cove hub ...` commands
for local download and inspection.

## Compose Deployment

Run the full API, UI, and Cloudflare Tunnel stack from `cove/`:

```bash
cd /path/to/cove
cp .env.example .env
$EDITOR .env  # set the Cloudflare tunnel token
docker compose up -d --build
```

Local smoke URLs:

```bash
curl -fsS http://127.0.0.1:3518/healthz
curl -fsS http://127.0.0.1:3517/ui-api/healthz
curl -fsS http://127.0.0.1:3517/ui-api/summary
```

Cloudflare Tunnel routes for the Compose deployment must target Docker service
names, not `localhost`:

```text
covehub.io      -> http://covehub-ui:8080
api.covehub.io  -> http://covehub-api:8000
```

An empty UI is expected on fresh Docker volumes until owners publish artifacts
and the publisher pushes at least one workflow.

## Package Development

Backend tests:

```bash
cd cove/ui
uv sync --extra dev
uv run --extra dev pytest tests
```

Frontend build:

```bash
cd cove/ui
npm install
npm run build
```
