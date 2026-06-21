# Owner Services

Each Cove owner runs an independent local provisioning service that:

- publishes a signed `/identity` document anchoring the owner's public key,
- accepts attested key-release requests from Cove runtime sidecars, and
- enforces the owner's local allow rules before releasing artifact keys.

Owner identity is the trust root for an owner's artifacts; the public hosting
layer (Cloudflare, DNS, the tunnel container) provides reachability only and
is **not** part of the trust root. See
[../security_model.md](../security_model.md) §"Public owner routing stays
outside the trust root" for the formal boundary, and
[../architecture.md](../architecture.md) §"Owner Provisioning Endpoint
Bindings" for the protocol-level binding the owner signs.

## Run The Owner Service

Each owner runs `cove start` against their own Cove home, choosing a local
bind port:

```bash
/path/to/cove --cove-home /home/$USER/.<owner>_cove start <port>
```

The Cove home must already be initialized (`cove init`) and must declare the
public owner URL — for example:

```yaml
owner_server_url: https://<your domain>
```

If no port is provided, `cove start` defaults to `9000`. Pick distinct ports
when running multiple owners on the same host.

## The `/identity` Document

Each owner service publishes a signed identity document. It is the bootstrap
record runtime sidecars verify before sending any key-release request:

```json
{
  "version": 2,
  "owner_url": "https://<your domain>",
  "owner_domain": "<your domain>",
  "owner_public_key_pem": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----\n",
  "owner_public_key_sha256": "sha256:...",
  "not_before": "<iso8601>",
  "not_after":  "<iso8601>",
  "signature_algorithm": "ed25519",
  "signature": "..."
}
```

The document **must not** include transport certificate fields. The owner
signing key is the application identity.

Verify each owner identity with normal WebPKI (no `-k`):

```bash
curl -A 'cove-runtime/0.0.1' -fsS https://<your domain>/identity
```

## Front With Cloudflare Tunnel

Each owner port is exposed at a dedicated public hostname through the same
`cloudflared` Docker tunnel pattern used by Covehub
([covehub_server.md](covehub_server.md)). One tunnel can carry multiple
hostnames; one container can run with one token.

Add a published-application route per owner. For example:

```text
Hostname:  <your domain>
Type:      HTTP
URL:       127.0.0.1:<owner-port>
```

The public client-facing URL can still terminate TLS at Cloudflare and must
verify without `-k`; the tunnel origin itself is plain HTTP.

If Cloudflare does not create the DNS record automatically, add a proxied
CNAME pointing at `<tunnel-id>.cfargotunnel.com`.

Run the owner tunnel container with host networking, the same way as Covehub:

```bash
export <OWNER>_CLOUDFLARED_TOKEN='<token>'

docker run --rm --network host --name <owner>-cloudflared \
  cloudflare/cloudflared:latest tunnel --no-autoupdate run \
  --token "${<OWNER>_CLOUDFLARED_TOKEN}"
```

## Workflow YAML

Workflows declare each owner's public URL explicitly. The left-hand name is
an authoring alias only; generated paths and published refs use the URL
hostname:

```yaml
owners:
  alice: https://<your domain>
```

`cove compile` fetches each declared URL, verifies the signed `/identity`,
and bakes the owner public key into the generated sidecar config. Runtime
sidecars verify signed key-release responses against that baked key, not
against transport metadata.

## Operational Notes

- Owner services must keep running after workflow approval. Phala runtime
  sidecars call the public owner URL during execution to request artifact
  keys; if the owner service is offline, key release fails and the affected
  nodes will not produce certificates.
- Restarting an owner service does not invalidate previously signed identity
  documents; the validity window in the `/identity` payload governs that.
- Allow rules live in the owner's local Cove home. Rotating the owner home
  without re-running `cove provision inspect` for each workflow drops every
  approval the owner has previously made.
