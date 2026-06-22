# Developer Demo Setup

This runbook describes how to run the hello-world demo through a personal
Cloudflare tunnel such as `covehub-orion`, `covehub-hpmv`, or `covehub-erika`.
Use it when you want a dev copy of the public Covehub API/UI plus Alice, Bob,
and Carol owner services without taking over the production `covehub` tunnel.

The important rule is consistency: choose one hostname set, put those same
hostnames in `demos/hello_world/scripts/.env`, and configure the Cloudflare
tunnel routes to point at the matching Compose services.

## 1. Choose Hostnames

Production uses:

```text
covehub.io
api.covehub.io
demo-alice.covehub.io
demo-bob.covehub.io
demo-carol.covehub.io
```

For a personal dev tunnel, choose a prefix and keep it consistent. For Orion:

```text
orion.covehub.io
orion-api.covehub.io
orion-demo-alice.covehub.io
orion-demo-bob.covehub.io
orion-demo-carol.covehub.io
```

The same pattern can be used for `hpmv` or `erika`.

## 2. Create The Cloudflare Tunnel

In Cloudflare Zero Trust:

1. Open **Networks -> Connectors** or **Networks -> Tunnels**.
2. Create a `cloudflared` tunnel.
3. Name it after the host set, for example `covehub-orion`.
4. Choose the Docker connector setup.
5. Copy the tunnel token for `CLOUDFLARED_TUNNEL_TOKEN`.

Add these public hostname routes for the Orion tunnel:

```text
orion.covehub.io             -> http://covehub-ui:8080
orion-api.covehub.io         -> http://covehub-api:8000
orion-demo-alice.covehub.io  -> http://alice:9000
orion-demo-bob.covehub.io    -> http://bob:9000
orion-demo-carol.covehub.io  -> http://carol:9000
```

Use the Compose service names exactly as shown. Do not use `localhost` in
these routes, because `cloudflared` runs inside the Docker Compose network.

## 3. Fill The Demo Environment

From the hello-world scripts directory:

```bash
cd /home/$USER/cove/demos/hello_world/scripts
cp .env.example .env
$EDITOR .env
```

For Orion, set:

```env
ALICE_URL=https://orion-demo-alice.covehub.io
BOB_URL=https://orion-demo-bob.covehub.io
CAROL_URL=https://orion-demo-carol.covehub.io
COVEHUB_API_URL=https://orion-api.covehub.io
COVEHUB_UI_URL=https://orion.covehub.io

PHALA_CLOUD_API_KEY=<carol-phala-cloud-api-key>
DOCKERHUB_USERNAME=covehub
DOCKERHUB_API_KEY=<covehub-docker-hub-access-token>
DOCKERHUB_REGISTRY=

CLOUDFLARED_TUNNEL_TOKEN=<cloudflare-tunnel-token>
PHALA_INSTANCE_TYPE=tdx.medium
PHALA_DISK_SIZE_GB=40
CLIENT_PROXY_LOCAL_PORT=9701
```

Leave `DOCKERHUB_REGISTRY` blank for Docker Hub. It is only for non-Docker-Hub
registry hosts such as `ghcr.io`; the Docker Hub namespace is
`DOCKERHUB_USERNAME=covehub`.

The real `.env` file is gitignored. Do not commit tokens or personal env files.

## 4. Start The End-To-End Demo

From `demos/hello_world/scripts`:

```bash
docker compose down -v  # only for a clean rerun
docker compose up --build
```

The scripted stack does more than start provisioning servers. It runs the
hello-world flow end to end:

- starts `covehub-api` and `covehub-ui`,
- starts Alice, Bob, and Carol owner services,
- starts the `cloudflared` connector for the configured tunnel,
- initializes Alice, Bob, and Carol Cove homes inside the Compose volumes,
- provisions Alice and Bob's static artifacts,
- has Carol check, compile, and push the workflow,
- has Alice and Bob approve the published workflow,
- has Carol deploy the workflow to Phala,
- starts the verified local client proxy after deployment.

The client proxy listens on `CLIENT_PROXY_LOCAL_PORT`, which defaults to
`9701`.

## 5. Verify The Run

Verify the public routes:

```bash
curl -A 'cove-runtime/0.0.1' -fsS https://orion-api.covehub.io/healthz
curl -fsS https://orion.covehub.io/
curl -A 'cove-runtime/0.0.1' -fsS https://orion-demo-alice.covehub.io/identity
curl -A 'cove-runtime/0.0.1' -fsS https://orion-demo-bob.covehub.io/identity
curl -A 'cove-runtime/0.0.1' -fsS https://orion-demo-carol.covehub.io/identity
```

Open the Covehub UI at:

```text
https://orion.covehub.io
```

After Carol deploys and the client proxy starts, query the verified final
service through the local proxy:

```bash
curl -fsS http://127.0.0.1:9701/health
curl -fsS http://127.0.0.1:9701/message
```

## Troubleshooting

If public routes return Cloudflare 502s, check that the Cloudflare routes use
Compose service names, not host loopback addresses:

```text
http://covehub-ui:8080
http://covehub-api:8000
http://alice:9000
http://bob:9000
http://carol:9000
```

If the run uses the wrong public hostnames, stop the stack, update `.env`, and
start again:

```bash
docker compose down -v
docker compose up --build
```

If deployment reaches Phala but image pulls fail, verify that Carol's Docker
Hub token can pull private `covehub/...` images and that
`DOCKERHUB_REGISTRY` is blank for Docker Hub.
