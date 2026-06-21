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
- `docs/internal/hello_world.md` — full per-node walkthrough with the
  sidecar inventory and the hash chain from secret-word ingestion to
  RA-TLS.
- `docs/internal/operations/end_to_end.md` — the operational runbook for
  bringing this demo up against Phala Cloud.
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

- Before provisioning artifacts, pushing the workflow, or deploying to Phala,
  run the Docker Compose Covehub stack from `/home/$USER/cove`. The demo
  expects `https://api.covehub.io` to reach `covehub-api` and
  `https://covehub.io` to reach the read-only browser UI through the Covehub
  Cloudflare Tunnel; see `docs/internal/operations/covehub_server.md`.
- Run the separate parties Cloudflare tunnel command from
  `~/.cloudflare_parties_token` for owner services:
  `alice.cove-demo-parties.covehub.io -> 127.0.0.1:9600`,
  `bob.cove-demo-parties.covehub.io -> 127.0.0.1:9601`, and
  `carol.cove-demo-parties.covehub.io -> 127.0.0.1:9602`.
- The local demo flow does not rebuild first-party Cove sidecars. Pull
  them with the top-level `containers/scripts/pull_canonical_containers.sh`
  helper, and pull the demo workloads with this directory's
  `scripts/pull_canonical_containers.sh`.
- The supported execution path is Carol publishing and deploying
  `carol.cove-demo-parties.covehub.io/hello_world` after Alice and Bob
  provision static inputs and approve the generated bundle.
- Carol's Cove home must have `phala_cloud_api_key` stored in
  `<cove_home>/config.yaml` by `cove init`. It must also carry a Docker
  Hub access token; Alice and Bob leave those deploy credentials blank. See
  `docs/internal/operations/phala_deploy.md` §"Docker Hub authentication
  is required".
- Phala runs require a public HTTPS Covehub URL plus public owner URLs
  reachable from Phala. The checked-in workflow declares
  `https://alice.cove-demo-parties.covehub.io`,
  `https://bob.cove-demo-parties.covehub.io`, and
  `https://carol.cove-demo-parties.covehub.io`.
- Authored workflows do not contain artifact `hub_path` fields. Static
  artifacts are pinned by `owner + artifact_id + plaintext_hash`; compile
  generates exact static ciphertext paths in `workflow.normalized.cove.yaml`.
- Owners keep `cove start` running while Phala executes, because runtime
  sidecars call the owner services for signed key release.
