# Cove CLI Release

The release rule is:

```text
runtime sidecars  →  containers/canonical_container_digests.json  →  CLI package
```

If the first-party runtime containers were rebuilt, publish them first
([runtime_release.md](runtime_release.md)), copy the refreshed container
digest JSON into the CLI tree, then build the CLI wheel.

This guide covers the local package flow: out-of-band distribution to a
trusted host, not PyPI or a private index. Operators should run the
release-built `cove` binary from an installed wheel rather than `uv run cove`
from the source tree, so that the CLI uses the canonical digest policy that
shipped with the wheel.

The CLI wheel does not depend on the `cove-container-runtime` Python package.
That package is copied into the runtime Docker images during container
builds. The CLI package only carries the digest policy and the client-side
code needed to compile, review, and deploy workflows.

## Copy Runtime Digests Into The CLI

After publishing runtime containers:

```bash
cd /home/$USER/cove
cp containers/canonical_container_digests.json cli/canonical_container_digests.json
cp containers/canonical_container_digests.json cli/src/cove_cli/canonical_container_digests.json
```

The `cli/src/cove_cli/...` copy is what the installed wheel ships with. The
root `cli/canonical_container_digests.json` keeps source-tree tooling
aligned. Skipping this copy step ships a wheel that pins the previous
canonical digest set.

## Build The Wheel

Build the CLI package into a local wheelhouse:

```bash
rm -rf /tmp/cove-cli-wheelhouse
mkdir -p /tmp/cove-cli-wheelhouse

cd /home/$USER/cove/cli
uv build --out-dir /tmp/cove-cli-wheelhouse
```

The wheelhouse should contain a `cove-cli` wheel. The built wheel includes
the packaged copy of the current runtime digest policy, so the wheel must be
rebuilt after any canonical sidecar digest refresh.

## Install The CLI

Install from the local wheelhouse into a clean virtual environment:

```bash
rm -rf ~/.cove-cli-release
python3 -m venv ~/.cove-cli-release
source ~/.cove-cli-release/bin/activate
python -m pip install --upgrade pip
pip install /tmp/cove-cli-wheelhouse/cove_cli-*.whl
```

Verify the installed executable:

```bash
cove --help
```

Use this installed binary for all operator workflows. The active Cove home
is selected per-invocation by `--cove-home`:

```bash
cove --cove-home <home> check <workflow.cove.yaml>
cove --cove-home <home> provision inspect <publisher>/<workflow_id>
```

## Release Checklist

- Publish runtime sidecars and refresh
  `containers/canonical_container_digests.json` when sidecar image contents
  change.
- Copy the refreshed digest JSON into both
  `cli/canonical_container_digests.json` and
  `cli/src/cove_cli/canonical_container_digests.json`.
- Build the `cove-cli` wheel into the local wheelhouse.
- Install `cove-cli` from that wheelhouse into the release virtual
  environment.
- Run `cove --help` and at least one `cove check` through the installed
  executable.
- Recompile and republish workflows after digest changes; have each owner
  re-run `cove provision inspect` against the republished workflow before
  redeploying.
