#!/usr/bin/env bash
set -Eeuo pipefail

ROLE="vllm_compiler"
COMPILER_VERSION="attested-audit-v1.vllm-compiler.1"

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    echo "ERROR: ${name} is required" >&2
    exit 2
  fi
}

write_result() {
  local result_path="$1"
  local passed="$2"
  local message="${3:-}"
  local failure_line="${4:-}"
  local failure_command="${5:-}"
  python3 - "$result_path" "$passed" "$COMPILER_VERSION" "$message" "$failure_line" "$failure_command" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

result_path = Path(sys.argv[1])
passed = sys.argv[2].lower() == "true"
compiler_version = sys.argv[3]
message = sys.argv[4]
failure_line = sys.argv[5]
failure_command = sys.argv[6]
wheelhouse = Path(os.environ.get("WHEELHOUSE", ""))
wheel_names = sorted(p.name for p in wheelhouse.glob("*.whl")) if wheelhouse.exists() else []
log_path = Path(os.environ.get("LOG_PATH", ""))
log_preview = ""
if log_path.exists():
    text = log_path.read_text(encoding="utf-8", errors="replace")
    log_preview = text[-8000:]
payload = {
    "pass": passed,
    "compiler_version": compiler_version,
    "wheel_names": wheel_names,
    "log_path": str(log_path) if log_path else "",
    "log_preview": log_preview,
}
if message:
    payload["message"] = message
if failure_line:
    payload["failure_line"] = int(failure_line)
if failure_command:
    payload["failure_command"] = failure_command
if not passed:
    payload["failed_at"] = datetime.now(timezone.utc).isoformat()
result_path.parent.mkdir(parents=True, exist_ok=True)
tmp = result_path.with_suffix(result_path.suffix + ".tmp")
tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
tmp.replace(result_path)
PY
}

write_failure_bundle() {
  local bundle_path="${COMPILED_RUNTIME_BUNDLE:-/workspace/output/compiled_serving_runtime_bundle.tar.gz}"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  mkdir -p "$(dirname "$bundle_path")"
  printf '%s\n' "compile failed; see vllm_compiler result certificate" > "${tmp_dir}/README.txt"
  tar -czf "$bundle_path" -C "$tmp_dir" README.txt
  rm -rf "$tmp_dir"
}

write_failure() {
  local line="$1"
  local command="$2"
  local result_path="${RESULT_PATH:-/workspace/output/compile_result.json}"
  echo "ERROR: failed at line ${line}: ${command}" >&2
  write_failure_bundle
  write_result "$result_path" false "failed at line ${line}: ${command}" "$line" "$command"
  exit 0
}

trap 'write_failure "$LINENO" "$BASH_COMMAND"' ERR

RUN_ID="${RUN_ID:-$(date +%Y%m%d%H%M%S)}"
RUN_DIR="${RUN_DIR:-/workspace/runs/${RUN_ID}/compile_vllm}"
PRISTINE_SOURCE="${PRISTINE_SOURCE:-}"
PUBLIC_SOURCE_DIR="${PUBLIC_SOURCE_DIR:-/vllm-workspace}"
SERVING_PATCH="${SERVING_PATCH:-/workspace/inputs/serving_patch.diff}"
COMPILED_RUNTIME_BUNDLE="${COMPILED_RUNTIME_BUNDLE:-${RUN_DIR}/compiled_serving_runtime_bundle.tar.gz}"
RESULT_PATH="${RESULT_PATH:-${RUN_DIR}/compile_result.json}"
BUILD_DIR="${BUILD_DIR:-/workspace/output/build}"
SOURCE_DIR="${SOURCE_DIR:-${BUILD_DIR}/source}"
WHEELHOUSE="${WHEELHOUSE:-/workspace/output/wheelhouse}"
LOG_PATH="${LOG_PATH:-${RUN_DIR}/compile_vllm.log}"
export WHEELHOUSE

mkdir -p "$(dirname "$LOG_PATH")" "$BUILD_DIR" "$WHEELHOUSE" "$(dirname "$COMPILED_RUNTIME_BUNDLE")"
exec > >(tee "$LOG_PATH") 2>&1

echo "==> ${ROLE}: starting"
test -f "$SERVING_PATCH"

rm -rf "$SOURCE_DIR" "$WHEELHOUSE"
mkdir -p "$SOURCE_DIR" "$WHEELHOUSE"

if [[ -n "$PRISTINE_SOURCE" ]]; then
  echo "==> Extracting pristine vLLM source artifact"
  test -f "$PRISTINE_SOURCE"
  tar -xzf "$PRISTINE_SOURCE" -C "$SOURCE_DIR" --strip-components=1
else
  echo "==> Copying public vLLM source from ${PUBLIC_SOURCE_DIR}"
  test -d "$PUBLIC_SOURCE_DIR"
  shopt -s dotglob
  cp -a "${PUBLIC_SOURCE_DIR}"/* "$SOURCE_DIR"/
  shopt -u dotglob
fi

cd "$SOURCE_DIR"
git init
git config user.email "cove@example.invalid"
git config user.name "cove"
git add .
git commit -m pristine

echo "==> Applying serving patch"
git apply --check "$SERVING_PATCH"
git apply "$SERVING_PATCH"

echo "==> Repacking runtime-aligned vLLM wheel with Python overlay"
python3 - "$SERVING_PATCH" "$SOURCE_DIR" "$WHEELHOUSE" <<'PY'
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib import metadata
from pathlib import Path, PurePosixPath


patch_path = Path(sys.argv[1])
source_dir = Path(sys.argv[2])
wheelhouse = Path(sys.argv[3])


def sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def changed_paths_from_patch(path: Path) -> list[str]:
    changed: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("+++ b/"):
            continue
        rel = line[len("+++ b/") :]
        if rel != "/dev/null":
            changed.append(rel)
    return sorted(set(changed))


def copy_path(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, symlinks=True)
    else:
        shutil.copy2(src, dst)


dist = metadata.distribution("vllm")
dist_name = dist.metadata["Name"]
dist_version = dist.version
changed_paths = changed_paths_from_patch(patch_path)
overlay_paths = [
    path for path in changed_paths if path.startswith("vllm/") and path.endswith(".py")
]
unsupported_runtime_paths = [
    path for path in changed_paths if path.startswith("vllm/") and not path.endswith(".py")
]
ignored_non_runtime_paths = [
    path for path in changed_paths if not path.startswith("vllm/")
]

if unsupported_runtime_paths:
    joined = ", ".join(unsupported_runtime_paths)
    raise SystemExit(
        "runtime-aligned overlay compiler only supports Python files under vllm/: "
        + joined
    )

with tempfile.TemporaryDirectory(prefix="cove-vllm-overlay-") as tmp:
    tmp_root = Path(tmp)
    wheel_root = tmp_root / "wheel-root"
    wheel_root.mkdir()

    dist_files = list(dist.files or [])
    copied = 0
    for file in dist_files:
        rel = PurePosixPath(str(file))
        if rel.is_absolute() or ".." in rel.parts:
            continue
        src = Path(dist.locate_file(file))
        if not src.exists() or src.is_dir() or src.name == "RECORD":
            continue
        copy_path(src, wheel_root / Path(*rel.parts))
        copied += 1

    if copied == 0:
        raise SystemExit("could not locate installed vLLM distribution files")

    for rel_text in overlay_paths:
        src = source_dir / rel_text
        if not src.exists():
            raise SystemExit(f"patched file does not exist: {rel_text}")
        copy_path(src, wheel_root / rel_text)

    build_info_path = wheel_root / "vllm" / "attested_audit_build_info.json"
    build_info_path.parent.mkdir(parents=True, exist_ok=True)
    build_info_path.write_text(
        json.dumps(
            {
                "build_mode": "runtime_aligned_python_overlay_wheel",
                "base_distribution": dist_name,
                "base_version": dist_version,
                "serving_patch_sha256": sha256_file(patch_path),
                "overlay_paths": overlay_paths,
                "ignored_non_runtime_paths": ignored_non_runtime_paths,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    dist_info_dirs = sorted(wheel_root.glob("*.dist-info"))
    if len(dist_info_dirs) != 1:
        raise SystemExit(f"expected one dist-info directory, found {len(dist_info_dirs)}")

    packed_root = tmp_root / "packed"
    packed_root.mkdir()
    subprocess.check_call(
        [sys.executable, "-m", "wheel", "pack", str(wheel_root), "--dest-dir", str(packed_root)]
    )
    wheels = sorted(packed_root.glob("*.whl"))
    if len(wheels) != 1:
        raise SystemExit(f"expected one packed wheel, found {len(wheels)}")
    wheelhouse.mkdir(parents=True, exist_ok=True)
    shutil.copy2(wheels[0], wheelhouse / wheels[0].name)

print(
    json.dumps(
        {
            "build_mode": "runtime_aligned_python_overlay_wheel",
            "base_distribution": dist_name,
            "base_version": dist_version,
            "overlay_paths": overlay_paths,
            "ignored_non_runtime_paths": ignored_non_runtime_paths,
            "wheelhouse": str(wheelhouse),
        },
        indent=2,
        sort_keys=True,
    )
)
PY

echo "==> Creating compiled serving runtime bundle"
tar -czf "$COMPILED_RUNTIME_BUNDLE" -C "$WHEELHOUSE" .

write_result "$RESULT_PATH" true
echo "==> Compile complete: ${COMPILED_RUNTIME_BUNDLE}"
