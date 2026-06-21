#!/usr/bin/env python3

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from cove_demo_common import (
    copy_tree,
    log,
    require_env,
    run,
    sha256_file,
    write_json,
)


SERVICE = "compile_serving_wheel"


def _changed_python_files(patch_path: Path) -> list[str]:
    changed: list[str] = []
    for line in patch_path.read_text(encoding="utf-8").splitlines():
        if not line.startswith("+++ b/"):
            continue
        path = line[len("+++ b/") :]
        if path == "/dev/null":
            continue
        changed.append(path)
    unsupported = [
        path
        for path in changed
        if not (path.startswith("vllm/") and path.endswith(".py"))
    ]
    if unsupported:
        unsupported_text = ", ".join(unsupported)
        raise SystemExit(
            "this prototype compile step only supports Python-file serving patches "
            f"under vllm/: {unsupported_text}"
        )
    return sorted(set(changed))


def main() -> int:
    patch_path = Path(require_env("PATCH_PATH"))
    output_path = Path(require_env("OUTPUT_PATH"))
    result_path = Path(require_env("RESULT_PATH"))
    source_root = Path(require_env("VLLM_SOURCE_ROOT"))
    wheel_path = Path(require_env("VLLM_WHEEL_PATH"))
    vllm_version = require_env("VLLM_VERSION")
    expected_vllm_git_sha = require_env("VLLM_GIT_SHA")

    observed_vllm_git_sha = run(
        ["git", "rev-parse", "HEAD"],
        cwd=source_root,
        capture_output=True,
    ).stdout.strip()
    if observed_vllm_git_sha != expected_vllm_git_sha:
        raise SystemExit(
            "unexpected vLLM source checkout: "
            f"{observed_vllm_git_sha} != {expected_vllm_git_sha}"
        )

    changed_files = _changed_python_files(patch_path)
    with tempfile.TemporaryDirectory(prefix="cove-vllm-build-") as temp_dir:
        temp_root = Path(temp_dir)
        patched_source_root = copy_tree(source_root, temp_root / "vllm-src")
        run(["git", "apply", str(patch_path)], cwd=patched_source_root)

        unpack_root = temp_root / "wheel-unpacked"
        unpack_root.mkdir(parents=True, exist_ok=True)
        run(["python", "-m", "wheel", "unpack", str(wheel_path), "--dest", str(unpack_root)])

        unpacked_dirs = list(unpack_root.iterdir())
        if len(unpacked_dirs) != 1:
            raise SystemExit(f"expected exactly one unpacked wheel directory, found {len(unpacked_dirs)}")
        wheel_tree = unpacked_dirs[0]

        for relative_path in changed_files:
            src = patched_source_root / relative_path
            dst = wheel_tree / relative_path
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)

        build_info_path = wheel_tree / "vllm" / "attested_confidential_eval_build_info.json"
        build_info_path.write_text(
            "{\n"
            f'  "base_vllm_git_sha": "{observed_vllm_git_sha}",\n'
            f'  "base_wheel_sha256": "{sha256_file(wheel_path)}",\n'
            f'  "build_mode": "python_overlay_on_vllm_cpu_wheel",\n'
            f'  "serving_patch_sha256": "{sha256_file(patch_path)}",\n'
            f'  "vllm_version": "{vllm_version}"\n'
            "}\n",
            encoding="utf-8",
        )

        packed_root = temp_root / "wheel-packed"
        packed_root.mkdir(parents=True, exist_ok=True)
        run(["python", "-m", "wheel", "pack", str(wheel_tree), "--dest-dir", str(packed_root)])
        packed_wheels = list(packed_root.glob("*.whl"))
        if len(packed_wheels) != 1:
            raise SystemExit(f"expected exactly one packed wheel, found {len(packed_wheels)}")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(packed_wheels[0], output_path)

    base_wheel_sha256 = sha256_file(wheel_path)
    compiled_wheel_sha256 = sha256_file(output_path)
    serving_patch_sha256 = sha256_file(patch_path)
    write_json(
        result_path,
        {
            "base_wheel_filename": wheel_path.name,
            "base_vllm_git_sha": observed_vllm_git_sha,
            "base_wheel_sha256": base_wheel_sha256,
            "build_mode": "python_overlay_on_vllm_cpu_wheel",
            "changed_files": changed_files,
            "compiled_wheel_sha256": compiled_wheel_sha256,
            "pass": True,
            "serving_patch_sha256": serving_patch_sha256,
            "vllm_version": vllm_version,
        },
    )
    log(SERVICE, f"built patched wheel at {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
