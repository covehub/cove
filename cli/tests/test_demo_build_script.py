from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path


def test_hello_world_build_script_builds_only_workloads(
    tmp_path: Path,
) -> None:
    repo_root = _copy_demo_script_fixture(tmp_path)
    script_path = repo_root / "demos" / "hello_world" / "scripts" / "build_all_containers.sh"
    docker_log_path = tmp_path / "docker.log"
    fake_bin = _write_fake_docker(tmp_path)

    result = subprocess.run(
        ["bash", str(script_path)],
        cwd=repo_root / "demos" / "hello_world",
        env=_script_env(fake_bin, docker_log_path),
        text=True,
        capture_output=True,
        check=False,
    )

    log_lines = docker_log_path.read_text(encoding="utf-8").splitlines()

    assert result.returncode == 0, result.stderr
    assert "Next release checklist:" not in result.stdout
    assert any(
        line.startswith("build -t cove-demo-hello-world-word-length-checker:dev ")
        for line in log_lines
    )
    assert any(
        line.startswith("build -t cove-demo-hello-world-character-set-checker:dev ")
        for line in log_lines
    )
    assert any(
        line.startswith("build -t cove-demo-hello-world-final-server:dev ")
        for line in log_lines
    )
    assert not any(line.startswith("pull covehub/") for line in log_lines)
    assert not any(line.startswith("tag covehub/cove-artifact-provisioner") for line in log_lines)
    assert not any(line.startswith("build -t cove-base:") for line in log_lines)


def test_hello_world_build_script_push_updates_pinned_workload_refs(
    tmp_path: Path,
) -> None:
    repo_root = _copy_demo_script_fixture(tmp_path)
    script_path = repo_root / "demos" / "hello_world" / "scripts" / "build_all_containers.sh"
    demo_canonical_path = repo_root / "demos" / "hello_world" / "canonical_container_digests.json"
    repo_digest_map_path = tmp_path / "docker-repo-digests.json"
    repo_digest_map_path.write_text(
        json.dumps(
            {
                "covehub/cove-demo-hello-world-word-length-checker:v0.1": [
                    "covehub/cove-demo-hello-world-word-length-checker@sha256:58fe3bab59ed5667e78ddcae5c399b070cc549c8d6b169922464c2ae33457a32"
                ],
                "covehub/cove-demo-hello-world-character-set-checker:v0.1": [
                    "covehub/cove-demo-hello-world-character-set-checker@sha256:73f746f6fe9dedd020e50f36e42e8047b2b656c4786cbb055583826aa68cd6fa"
                ],
                "covehub/cove-demo-hello-world-final-server:v0.1": [
                    "covehub/cove-demo-hello-world-final-server@sha256:e381a37520e3c0db68effff95fc957dfd30910e46290df1a7e4f87efe9d0984f"
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    docker_log_path = tmp_path / "docker.log"
    fake_bin = _write_fake_docker(tmp_path)
    alice_compose_path = (
        repo_root / "demos" / "hello_world" / "workflow" / "nodes" / "alice_word_length_checker.compose.yaml"
    )
    bob_compose_path = (
        repo_root / "demos" / "hello_world" / "workflow" / "nodes" / "bob_word_length_checker.compose.yaml"
    )
    character_compose_path = (
        repo_root / "demos" / "hello_world" / "workflow" / "nodes" / "character_set_checker.compose.yaml"
    )
    final_compose_path = (
        repo_root / "demos" / "hello_world" / "workflow" / "nodes" / "final_server.compose.yaml"
    )

    result = subprocess.run(
        [
            "bash",
            str(script_path),
            "--docker-namespace",
            "covehub",
            "--tag",
            "v0.1",
            "--push",
        ],
        cwd=repo_root / "demos" / "hello_world",
        env=_script_env(fake_bin, docker_log_path, repo_digest_map_path=repo_digest_map_path),
        text=True,
        capture_output=True,
        check=False,
    )

    log_lines = docker_log_path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(demo_canonical_path.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stderr
    assert any(
        line.startswith("build -t cove-demo-hello-world-word-length-checker:v0.1 ")
        for line in log_lines
    )
    assert any(
        line == "push covehub/cove-demo-hello-world-word-length-checker:v0.1"
        for line in log_lines
    )
    assert payload == {
        "containers": [
            {
                "image_name": "cove-demo-hello-world-word-length-checker",
                "canonical_ref": "covehub/cove-demo-hello-world-word-length-checker@sha256:58fe3bab59ed5667e78ddcae5c399b070cc549c8d6b169922464c2ae33457a32",
            },
            {
                "image_name": "cove-demo-hello-world-character-set-checker",
                "canonical_ref": "covehub/cove-demo-hello-world-character-set-checker@sha256:73f746f6fe9dedd020e50f36e42e8047b2b656c4786cbb055583826aa68cd6fa",
            },
            {
                "image_name": "cove-demo-hello-world-final-server",
                "canonical_ref": "covehub/cove-demo-hello-world-final-server@sha256:e381a37520e3c0db68effff95fc957dfd30910e46290df1a7e4f87efe9d0984f",
            },
        ]
    }
    assert "sha256:58fe3bab59ed5667e78ddcae5c399b070cc549c8d6b169922464c2ae33457a32" in alice_compose_path.read_text(encoding="utf-8")
    assert "sha256:58fe3bab59ed5667e78ddcae5c399b070cc549c8d6b169922464c2ae33457a32" in bob_compose_path.read_text(encoding="utf-8")
    assert "sha256:73f746f6fe9dedd020e50f36e42e8047b2b656c4786cbb055583826aa68cd6fa" in character_compose_path.read_text(encoding="utf-8")
    assert "sha256:e381a37520e3c0db68effff95fc957dfd30910e46290df1a7e4f87efe9d0984f" in final_compose_path.read_text(encoding="utf-8")
    assert "Next release checklist:" in result.stdout
    assert "- Commit demos/hello_world/canonical_container_digests.json." in result.stdout
    assert "- Commit demos/hello_world/workflow/nodes/*.compose.yaml." in result.stdout
    assert "- Re-run cove compile for demos/hello_world/workflow/workflow.cove.yaml." in result.stdout
    assert "- Re-push the published workflow bundle if this demo is consumed via cove pull." in result.stdout
    assert "- Re-pull and re-allow reviewed nodes because compose hashes can change." in result.stdout


def test_sidecar_build_script_without_push_does_not_print_release_checklist(
    tmp_path: Path,
) -> None:
    repo_root = _copy_demo_script_fixture(tmp_path)
    script_path = repo_root / "containers" / "scripts" / "build_all_containers.sh"
    docker_log_path = tmp_path / "docker.log"
    fake_bin = _write_fake_docker(tmp_path)

    result = subprocess.run(
        ["bash", str(script_path)],
        cwd=repo_root / "containers",
        env=_script_env(fake_bin, docker_log_path),
        text=True,
        capture_output=True,
        check=False,
    )

    log_lines = docker_log_path.read_text(encoding="utf-8").splitlines()

    assert result.returncode == 0, result.stderr
    assert "Next release checklist:" not in result.stdout
    assert any(line.startswith("build -t cove-base:dev ") for line in log_lines)
    assert any(line.startswith("build -t cove-artifact-provisioner:dev ") for line in log_lines)
    assert not any(line.startswith("push ") for line in log_lines)


def test_sidecar_build_script_push_updates_canonical_digests_and_prints_checklist(
    tmp_path: Path,
) -> None:
    repo_root = _copy_demo_script_fixture(tmp_path)
    script_path = repo_root / "containers" / "scripts" / "build_all_containers.sh"
    canonical_path = repo_root / "containers" / "canonical_container_digests.json"
    repo_digest_map_path = tmp_path / "docker-repo-digests.json"
    repo_digest_map_path.write_text(
        json.dumps(
            {
                "covehub/cove-base:v0.1": [
                    "covehub/cove-base@sha256:" + "1" * 64
                ],
                "covehub/cove-artifact-provisioner:v0.1": [
                    "covehub/cove-artifact-provisioner@sha256:" + "2" * 64
                ],
                "covehub/cove-precondition-checker:v0.1": [
                    "covehub/cove-precondition-checker@sha256:" + "3" * 64
                ],
                "covehub/cove-dependency-certificate-fetcher:v0.1": [
                    "covehub/cove-dependency-certificate-fetcher@sha256:" + "4" * 64
                ],
                "covehub/cove-service-certificate-writer:v0.1": [
                    "covehub/cove-service-certificate-writer@sha256:" + "5" * 64
                ],
                "covehub/cove-key-manager:v0.1": [
                    "covehub/cove-key-manager@sha256:" + "6" * 64
                ],
                "covehub/cove-node-certificate-writer:v0.1": [
                    "covehub/cove-node-certificate-writer@sha256:" + "7" * 64
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    docker_log_path = tmp_path / "docker.log"
    fake_bin = _write_fake_docker(tmp_path)

    result = subprocess.run(
        [
            "bash",
            str(script_path),
            "--docker-namespace",
            "covehub",
            "--tag",
            "v0.1",
            "--push",
        ],
        cwd=repo_root / "containers",
        env=_script_env(fake_bin, docker_log_path, repo_digest_map_path=repo_digest_map_path),
        text=True,
        capture_output=True,
        check=False,
    )

    log_lines = docker_log_path.read_text(encoding="utf-8").splitlines()
    payload = json.loads(canonical_path.read_text(encoding="utf-8"))

    assert result.returncode == 0, result.stderr
    assert any(line == "push covehub/cove-artifact-provisioner:v0.1" for line in log_lines)
    assert payload == {
        "containers": [
            {
                "image_name": "cove-base",
                "canonical_ref": "covehub/cove-base@sha256:" + "1" * 64,
            },
            {
                "image_name": "cove-artifact-provisioner",
                "canonical_ref": "covehub/cove-artifact-provisioner@sha256:" + "2" * 64,
            },
            {
                "image_name": "cove-precondition-checker",
                "canonical_ref": "covehub/cove-precondition-checker@sha256:" + "3" * 64,
            },
            {
                "image_name": "cove-dependency-certificate-fetcher",
                "canonical_ref": "covehub/cove-dependency-certificate-fetcher@sha256:" + "4" * 64,
            },
            {
                "image_name": "cove-service-certificate-writer",
                "canonical_ref": "covehub/cove-service-certificate-writer@sha256:" + "5" * 64,
            },
            {
                "image_name": "cove-key-manager",
                "canonical_ref": "covehub/cove-key-manager@sha256:" + "6" * 64,
            },
            {
                "image_name": "cove-node-certificate-writer",
                "canonical_ref": "covehub/cove-node-certificate-writer@sha256:" + "7" * 64,
            },
        ]
    }
    assert "Next release checklist:" in result.stdout
    assert "- Commit containers/canonical_container_digests.json." in result.stdout
    assert (
        "- Copy containers/canonical_container_digests.json into cli/canonical_container_digests.json and cli/src/cove_cli/canonical_container_digests.json."
        in result.stdout
    )
    assert (
        "- Rebuild and ship a new CLI package because the CLI enforces the canonical sidecar digest policy."
        in result.stdout
    )
    assert (
        "- Update downstream consumers only if this sidecar release also changed their demo or runtime workflow behavior."
        in result.stdout
    )


def test_hello_world_authored_compose_images_match_demo_canonical_digests(tmp_path: Path) -> None:
    repo_root = _copy_demo_script_fixture(tmp_path)
    payload = json.loads(
        (repo_root / "demos" / "hello_world" / "canonical_container_digests.json").read_text(
            encoding="utf-8"
        )
    )
    refs = {entry["image_name"]: entry["canonical_ref"] for entry in payload["containers"]}

    assert refs["cove-demo-hello-world-word-length-checker"] in (
        repo_root / "demos" / "hello_world" / "workflow" / "nodes" / "alice_word_length_checker.compose.yaml"
    ).read_text(encoding="utf-8")
    assert refs["cove-demo-hello-world-word-length-checker"] in (
        repo_root / "demos" / "hello_world" / "workflow" / "nodes" / "bob_word_length_checker.compose.yaml"
    ).read_text(encoding="utf-8")
    assert refs["cove-demo-hello-world-character-set-checker"] in (
        repo_root / "demos" / "hello_world" / "workflow" / "nodes" / "character_set_checker.compose.yaml"
    ).read_text(encoding="utf-8")
    assert refs["cove-demo-hello-world-final-server"] in (
        repo_root / "demos" / "hello_world" / "workflow" / "nodes" / "final_server.compose.yaml"
    ).read_text(encoding="utf-8")


def test_pull_canonical_demo_containers_script_pulls_missing_pinned_images(tmp_path: Path) -> None:
    repo_root = _copy_demo_script_fixture(tmp_path)
    script_path = repo_root / "demos" / "hello_world" / "scripts" / "pull_canonical_containers.sh"
    demo_payload = json.loads(
        (repo_root / "demos" / "hello_world" / "canonical_container_digests.json").read_text(
            encoding="utf-8"
        )
    )
    present_path = tmp_path / "docker-present.json"
    present_path.write_text("[]\n", encoding="utf-8")
    docker_log_path = tmp_path / "docker.log"
    fake_bin = _write_fake_docker(tmp_path)

    result = subprocess.run(
        ["bash", str(script_path)],
        cwd=repo_root / "demos" / "hello_world",
        env=_script_env(fake_bin, docker_log_path, present_path=present_path),
        text=True,
        capture_output=True,
        check=False,
    )

    log_lines = docker_log_path.read_text(encoding="utf-8").splitlines()

    assert result.returncode == 0, result.stderr
    assert all(
        f"pull {entry['canonical_ref']}" in log_lines
        for entry in demo_payload["containers"]
    )
    assert not any(line.startswith("build -t cove-demo-hello-world-") for line in log_lines)
    assert not any(line.startswith("pull covehub/cove-artifact-provisioner@") for line in log_lines)


def test_pull_canonical_repo_containers_script_pulls_missing_pinned_images(tmp_path: Path) -> None:
    repo_root = _copy_demo_script_fixture(tmp_path)
    script_path = repo_root / "scripts" / "pull_canonical_containers.sh"
    cli_payload = json.loads(
        (repo_root / "cli" / "canonical_container_digests.json").read_text(encoding="utf-8")
    )
    present_path = tmp_path / "docker-present.json"
    present_path.write_text("[]\n", encoding="utf-8")
    docker_log_path = tmp_path / "docker.log"
    fake_bin = _write_fake_docker(tmp_path)

    result = subprocess.run(
        ["bash", str(script_path)],
        cwd=repo_root,
        env=_script_env(fake_bin, docker_log_path, present_path=present_path),
        text=True,
        capture_output=True,
        check=False,
    )

    log_lines = docker_log_path.read_text(encoding="utf-8").splitlines()

    assert result.returncode == 0, result.stderr
    assert all(
        f"pull {entry['canonical_ref']}" in log_lines
        for entry in cli_payload["containers"]
    )
    assert not any(line.startswith("pull covehub/cove-demo-hello-world-") for line in log_lines)


def test_pull_canonical_demo_containers_script_uses_local_pinned_images_when_present(tmp_path: Path) -> None:
    repo_root = _copy_demo_script_fixture(tmp_path)
    script_path = repo_root / "demos" / "hello_world" / "scripts" / "pull_canonical_containers.sh"
    demo_payload = json.loads(
        (repo_root / "demos" / "hello_world" / "canonical_container_digests.json").read_text(
            encoding="utf-8"
        )
    )
    present_path = tmp_path / "docker-present.json"
    present_path.write_text(
        json.dumps(
            [entry["canonical_ref"] for entry in demo_payload["containers"]],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    docker_log_path = tmp_path / "docker.log"
    fake_bin = _write_fake_docker(tmp_path)

    result = subprocess.run(
        ["bash", str(script_path)],
        cwd=repo_root / "demos" / "hello_world",
        env=_script_env(fake_bin, docker_log_path, present_path=present_path),
        text=True,
        capture_output=True,
        check=False,
    )

    log_lines = docker_log_path.read_text(encoding="utf-8").splitlines()

    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("pull ") for line in log_lines)


def test_pull_canonical_repo_containers_script_uses_local_pinned_images_when_present(tmp_path: Path) -> None:
    repo_root = _copy_demo_script_fixture(tmp_path)
    script_path = repo_root / "scripts" / "pull_canonical_containers.sh"
    cli_payload = json.loads(
        (repo_root / "cli" / "canonical_container_digests.json").read_text(encoding="utf-8")
    )
    present_path = tmp_path / "docker-present.json"
    present_path.write_text(
        json.dumps(
            [entry["canonical_ref"] for entry in cli_payload["containers"]],
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    docker_log_path = tmp_path / "docker.log"
    fake_bin = _write_fake_docker(tmp_path)

    result = subprocess.run(
        ["bash", str(script_path)],
        cwd=repo_root,
        env=_script_env(fake_bin, docker_log_path, present_path=present_path),
        text=True,
        capture_output=True,
        check=False,
    )

    log_lines = docker_log_path.read_text(encoding="utf-8").splitlines()

    assert result.returncode == 0, result.stderr
    assert not any(line.startswith("pull ") for line in log_lines)


def _copy_demo_script_fixture(tmp_path: Path) -> Path:
    source_repo_root = Path(__file__).resolve().parents[2]
    repo_root = tmp_path / "repo"

    for relative_path in [
        Path("containers/scripts/build_all_containers.sh"),
        Path("scripts/pull_canonical_containers.sh"),
        Path("demos/hello_world/scripts/build_all_containers.sh"),
        Path("demos/hello_world/scripts/pull_canonical_containers.sh"),
        Path("demos/hello_world/canonical_container_digests.json"),
        Path("demos/hello_world/workflow/nodes/alice_word_length_checker.compose.yaml"),
        Path("demos/hello_world/workflow/nodes/bob_word_length_checker.compose.yaml"),
        Path("demos/hello_world/workflow/nodes/character_set_checker.compose.yaml"),
        Path("demos/hello_world/workflow/nodes/final_server.compose.yaml"),
        Path("cli/canonical_container_digests.json"),
    ]:
        source_path = source_repo_root / relative_path
        target_path = repo_root / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path)

    return repo_root


def _script_env(
    fake_bin: Path,
    docker_log_path: Path,
    *,
    repo_digest_map_path: Path | None = None,
    present_path: Path | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"
    env["FAKE_DOCKER_LOG"] = str(docker_log_path)
    if repo_digest_map_path is not None:
        env["FAKE_DOCKER_REPO_DIGESTS"] = str(repo_digest_map_path)
    if present_path is not None:
        env["FAKE_DOCKER_PRESENT"] = str(present_path)
    return env


def _write_fake_docker(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    docker_path = fake_bin / "docker"
    docker_path.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "${FAKE_DOCKER_LOG}"
if [[ "$1" == "pull" ]]; then
  python3 - "${FAKE_DOCKER_PRESENT:-}" "$2" <<'PY'
import json
import os
import sys

path = sys.argv[1]
ref = sys.argv[2]
if not path:
    raise SystemExit(0)
entries = []
if os.path.exists(path):
    entries = json.loads(open(path, encoding='utf-8').read())
if ref not in entries:
    entries.append(ref)
open(path, 'w', encoding='utf-8').write(json.dumps(entries, indent=2) + '\\n')
PY
  exit 0
fi
if [[ "$1" == "build" ]]; then
  exit 0
fi
if [[ "$1" == "tag" ]]; then
  exit 0
fi
if [[ "$1" == "push" ]]; then
  exit 0
fi
if [[ "$1" == "image" && "$2" == "inspect" ]]; then
  python3 - "${FAKE_DOCKER_REPO_DIGESTS:-}" "${FAKE_DOCKER_PRESENT:-}" "$3" "$*" <<'PY'
import json
import os
import sys

repo_digest_path = sys.argv[1]
present_path = sys.argv[2]
ref = sys.argv[3]
full_command = sys.argv[4]

if "--format {{json .RepoDigests}}" in full_command:
    if not repo_digest_path or not os.path.exists(repo_digest_path):
        raise SystemExit(1)
    payload = json.loads(open(repo_digest_path, encoding='utf-8').read())
    digests = payload.get(ref)
    if digests is None:
        raise SystemExit(1)
    print(json.dumps(digests))
    raise SystemExit(0)

if present_path:
    if not os.path.exists(present_path):
        raise SystemExit(1)
    present = json.loads(open(present_path, encoding='utf-8').read())
    if ref not in present:
        raise SystemExit(1)

print('[]')
raise SystemExit(0)
PY
  exit 0
fi
echo "unsupported fake docker command: $*" >&2
exit 1
""",
        encoding="utf-8",
    )
    docker_path.chmod(0o755)
    return fake_bin
