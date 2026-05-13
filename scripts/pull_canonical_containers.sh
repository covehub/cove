#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CLI_CANONICAL_CONTAINERS_JSON="${REPO_ROOT}/cli/canonical_container_digests.json"

emit_canonical_refs() {
    python3 - "${CLI_CANONICAL_CONTAINERS_JSON}" <<'PY'
import json
import re
import sys
from pathlib import Path

pattern = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
containers = payload.get("containers")
if not isinstance(containers, list):
    raise SystemExit("cli/canonical_container_digests.json must contain a 'containers' list")

refs: list[str] = []
for entry in containers:
    if not isinstance(entry, dict):
        raise SystemExit("cli/canonical_container_digests.json contains a non-object container entry")
    ref = entry.get("canonical_ref")
    if not isinstance(ref, str) or not pattern.fullmatch(ref):
        raise SystemExit("cli/canonical_container_digests.json contains an invalid canonical_ref")
    refs.append(ref)

for ref in sorted(set(refs)):
    print(ref)
PY
}

echo "==> Ensuring pinned canonical sidecar images are available locally"
while IFS= read -r image_ref; do
    if docker image inspect "${image_ref}" >/dev/null 2>&1; then
        echo "==> Using local pinned image ${image_ref}"
    else
        echo "==> Pulling ${image_ref}"
        docker pull "${image_ref}"
    fi
done < <(emit_canonical_refs)

echo
echo "Pinned first-party sidecar images are available locally."
echo "This helper reads cli/canonical_container_digests.json only."
