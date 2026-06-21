# Covehub Server And UI

Covehub is the convenience storage and transport layer for workflow bundles,
encrypted artifact envelopes, and runtime certificates. It is **not** part of
Cove's trust root: every object stored in Covehub is hash-checkable,
encrypted, or attested so tampering is independently detectable. See
[../security_model.md](../security_model.md) for the formal trust boundary.

A Covehub deployment ships two services together:

- **`covehub-api`** — the FastAPI typed object API, published as
  `api.covehub.io`. This is the only Covehub endpoint runtime sidecars and
  the CLI talk to.
- **`covehub-ui`** — a public read-only React/FastAPI browser, published as
  `covehub.io`. The UI mounts the public data root **read-only**. It is a
  convenience browser, not an
  integrity oracle, and it does not serve raw object downloads — object
  pages show `cove hub ...` commands for local download and inspection
  instead. See [ui.md](ui.md) for the engineering runbook (indexer surface,
  env vars, frontend views, tests) and
  [`cove/ui/README.md`](../../../ui/README.md) for the package quick-start.

Both services sit behind the same `cloudflared` container in the Compose
stack. The host-side ports `3518` (API) and `3517` (UI) are bound to
`127.0.0.1` for local smoke tests; the public internet only reaches Covehub
through the Cloudflare tunnel.

## Compose Stack

The checked-in `cove/compose.yaml` runs `covehub-api`, `covehub-ui`, and
`cloudflared` as separate containers on a shared compose network. The UI
mounts the API's data volume read-only.

From the `cove/` directory, create a private Compose environment file and
start the stack:

```bash
umask 077
cp .env.example .env
$EDITOR .env  # set the Cloudflare tunnel token; optionally tune local ports/cache
docker compose up -d --build
```

The private Compose environment must set the Cloudflare tunnel token. Do not
pass the token on the `docker run` command line and do not commit the real env
file.
`COVEHUB_API_PORT` and `COVEHUB_UI_PORT` in `.env` control the local
loopback ports used for smoke tests; the containers themselves listen on
fixed internal ports `8000` (API) and `8080` (UI).

Compose services and their local smoke URLs:

```text
Service        Purpose                          Local smoke URL
covehub-api    FastAPI typed object API         http://127.0.0.1:3518/healthz
covehub-ui     read-only public browser UI      http://127.0.0.1:3517
cloudflared    Cloudflare Tunnel connector      no local port
```

Local health checks:

```bash
docker compose ps
curl -fsS http://127.0.0.1:3518/healthz
curl -fsS http://127.0.0.1:3517/ui-api/healthz
curl -fsS http://127.0.0.1:3517/ui-api/summary
```

On a fresh Docker volume the UI summary is expected to show no workflows until
owners provision artifacts and the publisher pushes a workflow.

## Front With Cloudflare Tunnel

When using the compose stack, the Cloudflare Tunnel public hostname routes
must point at the Docker service names on the Compose network:

```text
Hostname          Service
covehub.io        http://covehub-ui:8080
api.covehub.io    http://covehub-api:8000
```

In the Cloudflare Zero Trust dashboard:

1. **Networks → Tunnels** — create or select the tunnel for this host.
2. Choose the Docker connector setup; copy the tunnel token into the private
   Compose environment.
3. Under the tunnel's public hostname routes, add the two routes above.

If Cloudflare does not create the DNS records automatically, add proxied
CNAMEs for `covehub.io` and `api.covehub.io` pointing at
`<tunnel-id>.cfargotunnel.com`.

Keep any existing owner-service routes
unchanged unless you are intentionally redesigning those services.

### Cloudflare 502 From `localhost` Origins

If `curl https://api.covehub.io/healthz` or `curl https://covehub.io/`
returns 502 while local loopback checks pass, inspect the tunnel logs:

```bash
docker compose logs --tail=120 cloudflared
```

When `cloudflared` runs inside Compose, `localhost` means the `cloudflared`
container itself. Routes such as `http://localhost:3518` or
`http://localhost:3517` therefore fail with `connect: connection refused`.
Change the tunnel origins to `http://covehub-api:8000` and
`http://covehub-ui:8080`.

## Verify Public Reachability

The public routes must verify under normal WebPKI without `-k`:

```bash
curl -A 'cove-runtime/0.0.1' -fsS https://api.covehub.io/healthz
curl -fsS https://covehub.io/
```

## Inspect Server State

Covehub state is the object data root held in the `covehub-data` Compose
volume. There is no auth database. Inspect stored namespaces through the
running container:

```bash
docker compose exec -T covehub-api find /var/lib/covehub/data/artifacts -type f | sort
docker compose exec -T covehub-api find /var/lib/covehub/data/workflows -type f | sort
docker compose exec -T covehub-api find /var/lib/covehub/data/runtime   -type f | sort
```

## Owner Services

Owner provisioning services are separate processes with their own runbook;
they are not run by Covehub. See [owner_services.md](owner_services.md).
