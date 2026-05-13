#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
DEMO_CANONICAL_CONTAINERS_JSON="${DEMO_ROOT}/canonical_container_digests.json"

emit_canonical_refs() {
    python3 - "${DEMO_CANONICAL_CONTAINERS_JSON}" <<'PY'
import json
import re
import sys
from pathlib import Path

pattern = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
refs: list[str] = []
filename = sys.argv[1]
payload = json.loads(Path(filename).read_text(encoding="utf-8"))
containers = payload.get("containers")
if not isinstance(containers, list):
    raise SystemExit(f"{filename} must contain a 'containers' list")
for entry in containers:
    if not isinstance(entry, dict):
        raise SystemExit(f"{filename} contains a non-object container entry")
    ref = entry.get("canonical_ref")
    if not isinstance(ref, str) or not pattern.fullmatch(ref):
        raise SystemExit(f"{filename} contains an invalid canonical_ref")
    refs.append(ref)

for ref in sorted(set(refs)):
    print(ref)
PY
}

echo "==> Ensuring pinned hello_world workload images are available locally"
while IFS= read -r image_ref; do
    if docker image inspect "${image_ref}" >/dev/null 2>&1; then
        echo "==> Using local pinned image ${image_ref}"
    else
        echo "==> Pulling ${image_ref}"
        docker pull "${image_ref}"
    fi
done < <(emit_canonical_refs)

echo
echo "Pinned hello_world workload images are available locally."
echo "This helper reads demos/hello_world/canonical_container_digests.json only."
