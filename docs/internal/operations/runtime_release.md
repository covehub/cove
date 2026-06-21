# Runtime Sidecar Release

Cove's first-party runtime sidecars are Docker images. The release pipeline
builds them, pushes them to a registry, and emits a canonical digest file
that the CLI bakes into compiled workflows.

The release rule:

```text
runtime sidecars  →  containers/canonical_container_digests.json  →  CLI package
```

`cove compile` writes the pinned sidecar refs into generated node composes;
owner review and key release use the canonical `cove-artifact-provisioner`
digest from the packaged CLI policy. Operators with installed CLIs would
otherwise pin the previous canonical digest set, so a sidecar release is only
complete once the refreshed JSON has been copied into the CLI tree and a new
CLI wheel has been built — see [cli_release.md](cli_release.md).

## Rebuild And Publish Sidecars

Authenticate to the registry first. Anonymous Docker Hub pushes will be
rejected, and anonymous *pulls* on Phala worker IPs hit shared rate limits
once images are deployed (see
[phala_deploy.md](phala_deploy.md) §"Docker Hub authentication is required"):

```bash
docker login
```

Build and push the canonical first-party sidecars:

```bash
cd /home/$USER/cove/containers
./scripts/build_all_containers.sh --docker-namespace covehub --tag <tag> --push
```

With `--push`, the build script pushes each sidecar image and rewrites:

```text
/home/$USER/cove/containers/canonical_container_digests.json
```

## Copy The Digest Policy Into The CLI

After publishing sidecars, copy the emitted container digest policy into both
CLI locations:

```bash
cd /home/$USER/cove
cp containers/canonical_container_digests.json cli/canonical_container_digests.json
cp containers/canonical_container_digests.json cli/src/cove_cli/canonical_container_digests.json
```

The root `cli/canonical_container_digests.json` keeps source-tree tooling
aligned. The `cli/src/cove_cli/...` copy is what gets baked into the
installed CLI wheel.

## Generated Runtime Compose Contract

`cove compile` emits each reviewed `compose.generated.yaml` as a portable
runtime artifact. The compose uses named Docker volumes (`cove_runtime`,
`cove-input-*`, `cove-bind-*`) and contains no repo-local paths. The
compiler also emits `cove_copy_*` helpers directly into the reviewed compose;
those helpers copy provisioned artifacts from `/cove/inputs/...` in the
`cove_runtime` volume into the workload input named volume before the
workload starts. The Phala deploy translator preserves these named volumes
and rejects relative bind sources.

## Verify Published Digests

Inspect each pushed ref from `containers/canonical_container_digests.json`:

```bash
docker manifest inspect covehub/cove-base@sha256:<digest>
docker manifest inspect covehub/cove-artifact-provisioner@sha256:<digest>
docker manifest inspect covehub/cove-precondition-checker@sha256:<digest>
docker manifest inspect covehub/cove-dependency-certificate-fetcher@sha256:<digest>
docker manifest inspect covehub/cove-service-certificate-writer@sha256:<digest>
docker manifest inspect covehub/cove-key-manager@sha256:<digest>
docker manifest inspect covehub/cove-node-certificate-writer@sha256:<digest>
```

Commit the refreshed container policy and the copied CLI policy files in the
same change as the runtime work that required the rebuild.

## Recompile And Reapprove Workflows

Any workflow compiled before the digest refresh still pins the old sidecar
refs. After the CLI package has been rebuilt with the refreshed digest file:

```bash
cove --cove-home <home> check   <workflow.cove.yaml>
cove --cove-home <home> compile <workflow.cove.yaml>
cove --cove-home <home> push    <workflow.cove.yaml>
```

Each owner must then re-run inspection and approval, because the reviewed
compose hash and artifact-provisioner digest are part of the owner key-release
policy:

```bash
cove --cove-home <home> provision inspect <publisher>/<workflow_id>
```

Workload images do not need to be rebuilt unless their own source or
Dockerfiles changed. Runtime sidecar releases update the container-side
digest policy that gets copied into the CLI package; workload image releases
update the workload-owned digest policy under the workload's own repo path.
