# Cove TODOs

Engineering follow-ups that don't have a home in
[architecture.md](architecture.md), [security_model.md](security_model.md),
or the [operations runbooks](operations/README.md). New entries belong here when they
need to outlive the conversation that produced them but aren't yet ready to
land in a tracked issue.

## Sensible Retry On Network Requests

Cove sidecars and the CLI currently issue single-attempt HTTP requests to
Covehub and to owner services. Real deployments hit transient 503/504s from
Cloudflare and connection resets while CVMs are booting; any of these turns
a successful workflow into a permanent failure that would have recovered on
a retry. Prioritize runtime containers and CLI network calls that sit on the
critical path for Phala deployments, because those are the places transient
Cloudflare/Phala failures currently force a full redeploy or manual recovery.

Apply a uniform bounded exponential-backoff-with-jitter policy across:

- the artifact provisioner's key-release `POST` to owner services
  (`cove_artifact_provisioner` in `static_input` and `dynamic_input` modes);
- the dependency certificate fetcher's polling loop (it has its own outer
  timeout, but no per-request retry on individual fetches);
- the node certificate writer's certificate upload to Covehub
  (`cove_node_certificate_writer`);
- the artifact provisioner's ciphertext upload to Covehub in
  `dynamic_output` mode;
- `cove push` and `cove pull` from the CLI;
- `cove deploy`'s `provision_cvm` and `commit_cvm_provision` calls to
  Phala.

Constraints:

- Only retry verbs the server treats as idempotent. Key-release with the
  same quote and certificate upload with the same body hash are safe; a
  blind `POST` retry without that property is not.
- Cap total wall-clock retry time per request below the surrounding
  sidecar/timeout deadline; otherwise the retry budget hides real
  attestation-window failures.
- Surface retry counts and final-failure reasons in logs so serial-log
  triage during CVM boot stays fast.

## Per-Node CVM Configuration In Workflow YAML

`cove deploy` flags
(`--phala-instance-type`, `--phala-disk-size-gb`, `--phala-region`,
`--phala-os-image`, `--phala-node-id`, `--phala-public-logs`,
`--phala-public-sysinfo`, `--phala-listed`) currently apply uniformly to
every node in the workflow. That's wrong for any non-trivial DAG: a GPU
inference node and a small validator sidecar should not share an instance
type. The workflow should be able to express per-node deploy settings such
as GPU vs CPU instance type, memory/instance sizing where Phala exposes it,
disk size, region, node selection, public logs/sysinfo, listing behavior, and
volume-related CVM settings where the platform supports them.

Move resource selection into the authored workflow, per node:

```yaml
nodes:
  final_server:
    platform:
      phala:
        instance_type: tdx.medium
        disk_size_gb: 40
        public_logs: true
  expensive_inference:
    platform:
      phala:
        instance_type: h200.small
```

Open design questions to resolve before implementing:

- **Reviewed-hash boundary.** Resource selection is currently outside the
  reviewed compose hash (see
  [operations/phala_deploy.md](operations/phala_deploy.md) §"Resource
  changes do not invalidate review"). Moving resource selection into the
  authored workflow YAML means it becomes part of the workflow bundle that
  owners review and approve. Decide whether per-node `platform.phala.*`
  fields are part of the reviewed compose hash, or whether they live in the
  workflow object outside the per-node compose hash; both shapes have
  consequences for the no-rebuild/no-reapprove guarantee.
- **CLI flag composition.** Does `--phala-instance-type` on the CLI
  override per-node YAML, only fill defaults for nodes that omit it, or
  become an error when both are set? The right answer depends on the
  reviewed-hash decision above.
- **Provider scope.** Should this generalize as
  `nodes.<name>.platform.<provider>.*` for future TEE providers, or stay
  `nodes.<name>.platform.phala.*`? `architecture.md` already treats the
  manifest format as platform-pluggable in principle even though the
  reference architecture is Phala/dstack today.

## Large Object Transfer Through CoveHub

Covehub currently treats each object upload as a single HTTP request and the
server/client paths read whole payloads into memory. That is not viable for
vLLM artifacts, model weights, and other multi-GB payloads. Cloudflare has
single-request size limits, and even when the origin accepts a request, whole
object buffering creates unnecessary memory pressure in the CLI, runtime
sidecars, and server.

Add chunked and resumable transfer support for large static artifacts,
workflow-bundled large assets if the bundle format grows them, and runtime
dynamic artifacts.

Required capabilities:

- create upload sessions tied to the final exact CoveHub object path and
  expected SHA-256 digest;
- upload chunks with idempotent `PUT`s so clients can safely retry after
  Cloudflare, Phala, or network interruptions;
- stream chunks to temporary storage without buffering full objects in memory;
- finalize by assembling/verifying the exact SHA-256 digest server-side before
  making the object visible;
- update `latest` only after finalization succeeds;
- clean up abandoned or expired temporary chunk state;
- support streamed downloads, and likely ranged downloads, for large reads.

Affected clients include `cove provision`, `cove push` if workflow bundles grow
large payloads, `cove pull`, `cove hub get`, and runtime artifact producer and
consumer sidecars. Chunked transfer should compose with the retry policy above
rather than creating a separate ad hoc retry mechanism.

Open design questions:

- **Chunk size.** Pick a default chunk size below Cloudflare limits that still
  gives reasonable throughput for model weights.
- **Protocol shape.** Decide whether large uploads use new `/uploads/*`
  session routes, content-range `PUT`s on existing exact paths, or a manifest
  object that references chunk objects.
- **Exact-path semantics.** Preserve the invariant that visible exact objects
  are named by `sha256:<digest>` and immutable.
- **Authorization and attestation.** Runtime artifact chunk uploads still need
  the same attestation guarantees as single-request runtime artifact uploads;
  decide whether attestation is checked on session creation, each chunk, or
  finalization.
- **Operational limits.** Decide max upload size, max session age, concurrent
  upload limits, and observability for stuck uploads.

## CoveHub UI External Links And Cross-Navigation

Now that the read-only CoveHub UI exists, it should become the easiest way to
move between related public objects and external artifacts.

Improve the UI so digest-pinned Docker image references in workflow and compose
views link to Docker Hub when the image is clearly hosted there. This should
cover canonical Cove sidecars and demo workload images such as
`covehub/<image>@sha256:<digest>` without guessing for private registries or
unknown hosts.

Artifact detail pages should link workflow names back to the corresponding
workflow object anywhere a relationship is displayed. Prefer exact CoveHub
object links when the UI/indexer knows them; fall back to the latest workflow
route only when no exact workflow object path is available.

Open design questions:

- **Docker Hub URL shape.** Decide whether links should go to the repository
  page, tag/digest search, or another stable Docker Hub view for digest-pinned
  refs.
- **Non-Docker-Hub images.** Avoid false links for private registries until the
  UI can map those registries confidently.
- **Relationship completeness.** Ensure static artifacts, runtime artifacts,
  runtime certificates, diagram nodes, and workflow tree values all use the
  same object-link helper so cross-navigation stays consistent.

## CLI Output Should Prefer CoveHub UI URLs

The CLI currently prints raw hub paths and long local review output. That is
still useful for scripts and offline verification, but humans now have a
better browser surface for reviewing workflow bundles, artifacts, diagrams,
and relationships.

Add CoveHub UI URLs to human-facing CLI output:

- `cove push` should print UI URLs for the exact workflow object and latest
  workflow object alongside raw hub paths.
- `cove provision` should print UI URLs for the exact artifact object and
  latest artifact object after upload.
- `cove provision inspect` should print a concise workflow UI URL and local
  pulled bundle path, and avoid overwhelming the terminal with long review
  text that is easier to inspect in the UI.

Keep raw hub paths, `cove hub ...` commands, and local paths available so the
CLI remains scriptable and verifiable without the UI.

Open design questions:

- **URL derivation.** Decide whether the CLI derives UI URLs from
  `covehub_server_url` (`api.covehub.io` → `covehub.io`) or stores an explicit
  `covehub_ui_url` in local config.
- **Output mode.** Consider a future machine-readable output flag before
  changing default text too much.
- **Local deployments.** Define the UI URL shape for loopback/Compose smoke
  runs where the API URL is `http://127.0.0.1:3518` and the UI is
  `http://127.0.0.1:3517`.
