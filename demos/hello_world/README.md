# `hello_world`

`hello_world` is the checked-in workflow that exercises every Cove
primitive end-to-end. It has four nodes:

- `alice_word_length_checker`
- `bob_word_length_checker`
- `character_set_checker`
- `final_server`

The workflow has two owners (Alice and Bob), static and dynamic
artifacts, dependency-certificate gating, JsonLogic preconditions, and a
long-running HTTPS service that exposes an enclave-generated TLS
keypair.

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
  run the Docker Compose CoveHub stack from `/home/$USER/mats/cove`. The demo
  expects `https://api.covehub.io` to reach `covehub-api` and
  `https://covehub.io` to reach the read-only browser UI through the same
  Cloudflare Tunnel; see
  `docs/internal/operations/covehub_server.md`.
- The local demo flow does not rebuild first-party Cove sidecars. Pull
  them with the top-level `containers/scripts/pull_canonical_containers.sh`
  helper, and pull the demo workloads with this directory's
  `scripts/pull_canonical_containers.sh`.
- The supported execution path is
  `cove deploy <publisher>/<workflow_id> --phala-instance-type tdx.medium`
  after `uv sync`, `cove init`, compile, push, pull/review, owner allow
  rules, and `cove start`.
- The deploying Cove home must have `phala_cloud_api_key` stored in
  `<cove_home>/config.yaml` by `cove init`. It must also carry a Docker
  Hub access token; see
  `docs/internal/operations/phala_deploy.md` §"Docker Hub authentication
  is required".
- Phala runs require a public HTTPS Covehub URL plus public owner URLs
  reachable from Phala. The checked-in workflow declares
  `https://cove-demo-hello-world-alice-provisioning.covehub.io` and
  `https://cove-demo-hello-world-bob-provisioning.covehub.io`.
- Owners review pulled `compose.generated.yaml` files before starting
  their owner services and before the DAG run releases any keys.
