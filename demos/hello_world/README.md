# `hello_world`

`hello_world` is the checked-in workflow that exercises every Cove
primitive end-to-end. It has four nodes:

- `alice_word_length_checker`
- `bob_word_length_checker`
- `character_set_checker`
- `final_server`

The workflow has two data owners (Alice and Bob) and one publisher/deployer
(Carol), static and dynamic artifacts, dependency-certificate gating,
JsonLogic preconditions, and a long-running HTTPS service that exposes an
enclave-generated TLS keypair.

## Where To Read Next

- `docs/internal/architecture.md` — architecture, trust model, workflow
  lifecycle, sidecars, certificates, and verification.
- `docs/internal/hello_world.md` — the operational runbook for bringing this
  demo up against Phala Cloud, including the sidecar inventory and the hash
  chain from secret-word ingestion to RA-TLS.
- `docs/internal/todos.md` — known engineering follow-ups.

## Key Files

- `workflow/workflow.cove.yaml` — the authored workflow.
- `workflow/nodes/*.compose.yaml` — authored node composes pinning the
  demo workload images by digest.
- `canonical_container_digests.json` — pinned `covehub/...@sha256:...`
  refs for the demo workload images.
- `scripts/build_all_containers.sh` — maintainer helper for rebuilding
  and publishing the demo workload images.
- `scripts/pull_canonical_containers.sh` — local-run helper that pulls
  the pinned demo workload images.

## Demo Runtime Notes

- The scripted run starts Covehub API/UI, Alice, Bob, Carol, the client proxy,
  and the Cloudflare connector from `demos/hello_world/scripts`.
- Copy `scripts/.env.example` to `scripts/.env`, then fill in Carol's Phala
  Cloud API key, the Docker Hub token for the `covehub` namespace, and the
  Cloudflare tunnel token.
- Configure the Cloudflare tunnel public hostname routes to the Compose service
  origins. Production uses:

```text
covehub.io               -> http://covehub-ui:8080
api.covehub.io           -> http://covehub-api:8000
demo-alice.covehub.io    -> http://alice:9000
demo-bob.covehub.io      -> http://bob:9000
demo-carol.covehub.io    -> http://carol:9000
```

- For an individual dev tunnel, choose one coherent hostname set and update
  both `scripts/.env` and the Cloudflare routes to match. Examples include
  `orion-api.covehub.io` with `orion.covehub.io`,
  `hpmv-api.covehub.io` with `hpmv.covehub.io`, or the equivalent `erika`
  hostnames, plus matching owner hostnames for Alice, Bob, and Carol.
- Start or reset the scripted run from `demos/hello_world/scripts`:

```bash
docker compose down -v  # if this is not the first run
docker compose up --build
```

- The local demo flow does not rebuild first-party Cove sidecars. Pull
  them with the top-level `containers/scripts/pull_canonical_containers.sh`
  helper, and pull the demo workloads with this directory's
  `scripts/pull_canonical_containers.sh`.
- The supported execution path is Carol publishing and deploying
  `demo-carol.covehub.io/hello_world` after Alice and Bob
  provision static inputs and approve the generated bundle.
- Carol's Cove home must have `phala_cloud_api_key` stored in
  `<cove_home>/config.yaml` by `cove init`. It must also carry a Docker
  Hub access token; Alice and Bob leave those deploy credentials blank. See
  `docs/internal/operations/phala_deploy.md` §"Docker Hub authentication
  is required".
- Phala runs require a public HTTPS Covehub URL plus public owner URLs
  reachable from Phala. The checked-in workflow declares Alice and Bob at
  `https://demo-alice.covehub.io` and `https://demo-bob.covehub.io`; Carol's
  publisher URL comes from `CAROL_URL` in `scripts/.env`.
- Authored workflows do not contain artifact `hub_path` fields. Static
  artifacts are pinned by `owner + artifact_id + plaintext_hash`; compile
  generates exact static ciphertext paths in `workflow.normalized.cove.yaml`.
- Owners keep `cove start` running while Phala executes, because runtime
  sidecars call the owner services for signed key release.
