from __future__ import annotations

import json
import socket
import subprocess
import shutil
import ssl
from pathlib import Path
from threading import Thread
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

import pytest
import yaml

from cove_cli.attestation import build_key_release_report_data
from cove_cli.canonical_images import canonical_container_ref, parse_digest_from_image_reference
from cove_cli.common import RuntimeErrorBase
from cove_cli.artifact_crypto import ensure_artifact_key, encrypt_artifact_bytes, sha256_literal
from cove_cli.cli import run
from cove_cli.compile import CompileCommandError, resolve_image_reference_to_digest
from cove_cli.config import config_path_for_home, provision_paths_for_home
from cove_cli.provision_server import create_provision_server
from cove_cli.provision_state import ProvisionState
from cove_cli.publish import (
    PublishCommandError,
    load_canonical_container_digests,
    load_workflow_bundle,
)
from cove_cli.provisioning_identity import build_owner_identity_document

from .support import MockCovehubServer, build_test_owner_identity, load_container_main_module, write_test_certificate


ALICE_DOMAIN = "alice.cove-demo-parties.covehub.io"
ALICE_OWNER_URL = f"https://{ALICE_DOMAIN}"
BOB_DOMAIN = "bob.cove-demo-parties.covehub.io"
BOB_OWNER_URL = f"https://{BOB_DOMAIN}"
CAROL_DOMAIN = "carol.cove-demo-parties.covehub.io"
CAROL_OWNER_URL = f"https://{CAROL_DOMAIN}"
PUBLISHER_DOMAIN = CAROL_DOMAIN
PUBLISHER_OWNER_URL = CAROL_OWNER_URL
LOCAL_OWNER_DOMAIN = "127.0.0.1"
ALICE_SECRET_WORD_PLAINTEXT_HASH = "sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03"

_CANONICAL_DIGEST = next(
    parse_digest_from_image_reference(entry["canonical_ref"])
    for entry in json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "cli"
            / "src"
            / "cove_cli"
            / "canonical_container_digests.json"
        ).read_text(encoding="utf-8")
    )["containers"]
    if entry["image_name"] == "cove-artifact-provisioner"
)
assert _CANONICAL_DIGEST is not None
_ARTIFACT_PROVISIONER = load_container_main_module("artifact_provisioner")


@pytest.fixture(autouse=True)
def _stub_phala_attestation(monkeypatch):
    def fake_collect_attestation_bundle(_settings, *, report_data: bytes):
        return {
            "format": "phala_dstack_v1",
            "quote": "phala-quote",
            "report_data": report_data.hex(),
        }

    def fake_verify_attestation_bundle(attestation, *, expected_report_data: bytes):
        if attestation.get("report_data") != expected_report_data.hex():
            raise RuntimeErrorBase("attestation_bundle.report_data does not match expected report data")
        return attestation

    monkeypatch.setattr(
        _ARTIFACT_PROVISIONER,
        "collect_attestation_bundle",
        fake_collect_attestation_bundle,
    )
    monkeypatch.setattr(
        "cove_cli.provision_server.verify_attestation_bundle",
        fake_verify_attestation_bundle,
    )

    def fake_http_bytes_with_owner_identity(*, request, owner_identity, timeout):
        context = ssl._create_unverified_context()
        try:
            with urllib_request.urlopen(request, timeout=timeout, context=context) as response:
                return response.read()
        except urllib_error.HTTPError as exc:
            raise RuntimeErrorBase(f"HTTP {exc.code} for {request.full_url}: {exc.reason}") from exc
        except urllib_error.URLError as exc:
            raise RuntimeErrorBase(f"failed to reach {request.full_url}: {exc.reason}") from exc

    monkeypatch.setattr(
        _ARTIFACT_PROVISIONER,
        "_http_bytes_with_owner_identity",
        fake_http_bytes_with_owner_identity,
    )


@pytest.fixture(autouse=True)
def _stub_owner_identity_resolution(monkeypatch):
    def fake_fetch_owner_identity_document(
        *,
        owner_url: str | None = None,
        expected_owner_domain: str | None = None,
        timeout: float = 5.0,
        **_legacy_kwargs,
    ):
        assert owner_url is not None
        owner_domain = expected_owner_domain or "owner.example.test"
        return build_test_owner_identity(owner_domain, owner_url)

    monkeypatch.setattr(
        "cove_cli.compile.fetch_owner_identity_document",
        fake_fetch_owner_identity_document,
    )

    def fake_fetch_owner_identity_for_write(
        *,
        owner_url: str,
        owner_private_key_path: Path,
        expected_owner_domain: str | None = None,
        timeout: float = 5.0,
    ):
        assert expected_owner_domain is not None
        return _local_write_identity(owner_url, owner_private_key_path)

    monkeypatch.setattr(
        "cove_cli.publish.fetch_owner_identity_for_write",
        fake_fetch_owner_identity_for_write,
    )
    monkeypatch.setattr(
        "cove_cli.provision.fetch_owner_identity_for_write",
        fake_fetch_owner_identity_for_write,
    )

    def fake_static_resolution(*, owner, artifact_id: str, plaintext_hash: str):
        ciphertext_hash = _generated_static_ciphertext_hash(
            owner.owner_domain,
            artifact_id,
            plaintext_hash,
        )
        return {
            "hub_path": _generated_static_hub_path(
                owner.owner_domain,
                artifact_id,
                plaintext_hash,
            ),
            "artifact_id": artifact_id,
            "owner_domain": owner.owner_domain,
            "owner_url": owner.owner_url,
            "plaintext_hash": plaintext_hash,
            "ciphertext_hash": ciphertext_hash,
        }

    monkeypatch.setattr(
        "cove_cli.compile._fetch_owner_static_artifact_resolution",
        fake_static_resolution,
    )


def test_load_canonical_container_digests_reads_json(monkeypatch, tmp_path) -> None:
    packaged_policy = json.dumps(
        {
            "containers": [
                {
                    "image_name": "cove-artifact-provisioner",
                    "canonical_ref": f"example/cove-artifact-provisioner@{_CANONICAL_DIGEST}",
                },
                {
                    "image_name": "cove-key-manager",
                    "canonical_ref": "example/cove-key-manager@sha256:" + "1" * 64,
                },
            ]
        },
    )
    monkeypatch.setattr(
        "cove_cli.canonical_images._packaged_canonical_text",
        lambda: packaged_policy,
    )

    assert load_canonical_container_digests() == {
        "cove-artifact-provisioner": _CANONICAL_DIGEST,
        "cove-key-manager": "sha256:" + "1" * 64,
    }


def test_load_canonical_container_digests_reads_packaged_json(monkeypatch, tmp_path) -> None:
    packaged_policy = json.dumps(
        {
            "containers": [
                {
                    "image_name": "cove-artifact-provisioner",
                    "canonical_ref": f"example/cove-artifact-provisioner@{_CANONICAL_DIGEST}",
                }
            ]
        }
    )
    monkeypatch.setattr(
        "cove_cli.canonical_images._packaged_canonical_text",
        lambda: packaged_policy,
    )

    assert load_canonical_container_digests() == {
        "cove-artifact-provisioner": _CANONICAL_DIGEST,
    }


def test_load_canonical_container_digests_rejects_invalid_json(monkeypatch, tmp_path) -> None:
    packaged_policy = json.dumps(
        {
            "containers": [
                {
                    "image_name": "cove-artifact-provisioner",
                    "canonical_ref": "example/cove-artifact-provisioner:v1",
                }
            ]
        }
    )
    monkeypatch.setattr(
        "cove_cli.canonical_images._packaged_canonical_text",
        lambda: packaged_policy,
    )

    try:
        load_canonical_container_digests()
    except PublishCommandError as exc:
        assert "invalid canonical_ref" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected invalid canonical container JSON failure")


def test_resolve_image_reference_to_digest_prefers_matching_repo_digest(monkeypatch) -> None:
    image_reference = "covehub/cove-demo-hello-world-word-length-checker:v0.1"
    expected = (
        "covehub/cove-demo-hello-world-word-length-checker"
        "@sha256:58fe3bab59ed5667e78ddcae5c399b070cc549c8d6b169922464c2ae33457a32"
    )

    def fake_run(args, check, capture_output, text):
        assert check is False
        assert capture_output is True
        assert text is True
        if args[-1] == "{{json .RepoDigests}}":
            return subprocess.CompletedProcess(
                args,
                0,
                stdout=json.dumps(
                    [
                        expected,
                        "covehub/hello-world-word-length-checker@sha256:58fe3bab59ed5667e78ddcae5c399b070cc549c8d6b169922464c2ae33457a32",
                    ]
                ),
                stderr="",
            )
        raise AssertionError("unexpected non-RepoDigest fallback should not be used")

    monkeypatch.setattr("cove_cli.compile.subprocess.run", fake_run)

    assert resolve_image_reference_to_digest(image_reference) == expected


def test_resolve_image_reference_to_digest_fails_without_repo_digest(monkeypatch) -> None:
    image_reference = "cove-demo-hello-world-final-server:dev"

    def fake_run(args, check, capture_output, text):
        assert check is False
        assert capture_output is True
        assert text is True
        if args[-1] == "{{json .RepoDigests}}":
            return subprocess.CompletedProcess(args, 0, stdout="[]", stderr="")
        raise AssertionError(f"unexpected docker invocation: {args}")

    monkeypatch.setattr("cove_cli.compile.subprocess.run", fake_run)

    try:
        resolve_image_reference_to_digest(image_reference)
    except CompileCommandError as exc:
        assert "has no local RepoDigest" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected missing RepoDigest failure")


def test_push_and_pull_preserve_generated_digest_pinned_bundle(tmp_path, monkeypatch, capsys) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".carol_cove"
    observed_write_identity: dict[str, object] = {}

    def fake_fetch_owner_identity_for_write(
        *,
        owner_url: str,
        owner_private_key_path: Path,
        expected_owner_domain: str | None = None,
        timeout: float = 5.0,
    ):
        observed_write_identity["owner_url"] = owner_url
        observed_write_identity["expected_owner_domain"] = expected_owner_domain
        return _local_write_identity(owner_url, owner_private_key_path)

    monkeypatch.setattr(
        "cove_cli.publish.fetch_owner_identity_for_write",
        fake_fetch_owner_identity_for_write,
    )

    with MockCovehubServer() as server:
        _write_config(
            cove_home,
            _owner_config(server.url),
        )

        push_exit = run(
            ["--cove-home", str(cove_home), "push", str(workflow_dir / "workflow.cove.yaml")]
        )
        push_output = capsys.readouterr().out

        pull_exit = run(
            ["--cove-home", str(cove_home), "pull", f"{PUBLISHER_DOMAIN}/hello_world"]
        )

        pull_output = capsys.readouterr().out
    pulled_root = (
        provision_paths_for_home(cove_home).materialized_workflows_dir
        / PUBLISHER_DOMAIN
        / "hello_world"
    )
    bundle = load_workflow_bundle(pulled_root)
    manifest = json.loads((pulled_root / "bundle.manifest.json").read_text(encoding="utf-8"))
    final_compose_path = pulled_root / "nodes" / "final_server" / "compose.generated.yaml"
    final_compose_hash_path = pulled_root / "nodes" / "final_server" / "compose.generated.sha256"
    built_compose_path = workflow_dir / "build" / "nodes" / "final_server" / "compose.generated.yaml"
    built_compose_hash_path = workflow_dir / "build" / "nodes" / "final_server" / "compose.generated.sha256"
    final_compose_text = final_compose_path.read_text(encoding="utf-8")
    final_compose_payload = yaml.safe_load(final_compose_text)
    final_provisioner_config = _inline_service_config(
        final_compose_payload["services"],
        "cove_provision_alice_secret_word_transformed",
    )

    assert push_exit == 0
    assert pull_exit == 0
    assert observed_write_identity == {
        "owner_url": PUBLISHER_OWNER_URL,
        "expected_owner_domain": PUBLISHER_DOMAIN,
    }
    assert f"Published workflow bundle '{PUBLISHER_DOMAIN}/hello_world'" in push_output
    assert f"Pulled workflow bundle '{PUBLISHER_DOMAIN}/hello_world'" in pull_output
    assert (pulled_root / "bundle.manifest.json").is_file()
    assert bundle.owners == {
        "alice": ALICE_OWNER_URL,
        "bob": BOB_OWNER_URL,
        "carol": CAROL_OWNER_URL,
    }
    assert manifest["owners"] == bundle.owners
    assert final_compose_path.is_file()
    assert final_compose_hash_path.is_file()
    assert final_compose_text == built_compose_path.read_text(encoding="utf-8")
    assert final_compose_hash_path.read_text(encoding="utf-8") == built_compose_hash_path.read_text(
        encoding="utf-8"
    )
    assert "@sha256:" in final_compose_text
    assert "COVE_CONFIG_JSON: |" in final_compose_text
    assert "./runtime" not in final_compose_text
    assert "runtime/service_bindings" not in final_compose_text
    assert "cove_runtime" in final_compose_payload["volumes"]
    assert any(
        service_name.startswith("cove_copy_")
        and service_payload["environment"]["COVE_INPUT_SOURCE"]
        == "/cove/inputs/alice_secret_word_transformed/alice_secret_word_transformed"
        for service_name, service_payload in final_compose_payload["services"].items()
    )
    assert not (pulled_root / "nodes" / "final_server" / "configs").exists()
    assert not (pulled_root / "nodes" / "final_server" / "runtime").exists()
    assert final_provisioner_config["workflow_publisher_domain"] == PUBLISHER_DOMAIN
    assert final_provisioner_config["workflow_id"] == "hello_world"
    assert final_provisioner_config["mode"] == "dynamic_input"
    assert final_provisioner_config["producer_certificate_path"] == (
        "/cove/certificates/alice_word_length_checker/certificate.json"
    )
    provisioner_environment = final_compose_payload["services"][
        "cove_provision_alice_secret_word_transformed"
    ]["environment"]
    assert provisioner_environment["COVE_COMPOSE_HASH"] == final_compose_hash_path.read_text(
        encoding="utf-8"
    ).strip()
    assert "COVE_COMPOSE_PATH" not in provisioner_environment
    assert final_provisioner_config["artifact_provisioner_image"] == canonical_container_ref(
        "cove-artifact-provisioner"
    )
    final_node = next(node for node in bundle.nodes if node.node_id == "final_server")
    assert final_node.runtime_skeleton == []
    assert [(artifact.name, artifact.type, artifact.direction) for artifact in final_node.artifacts] == [
        ("alice_secret_word_transformed", "dynamic", "input"),
        ("bob_secret_word_transformed", "dynamic", "input"),
    ]
    assert [(artifact.name, artifact.owner, artifact.hub_path) for artifact in final_node.artifacts] == [
        (
            "alice_secret_word_transformed",
            "alice",
            f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest",
        ),
        (
            "bob_secret_word_transformed",
            "bob",
            f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/artifacts/bob_secret_word_transformed/latest",
        ),
    ]


def test_push_republishes_workflow_and_advances_latest(tmp_path, capsys) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".carol_cove"

    with MockCovehubServer() as server:
        _write_config(
            cove_home,
            _owner_config(server.url),
        )
        assert run(
            ["--cove-home", str(cove_home), "push", str(workflow_dir / "workflow.cove.yaml")]
        ) == 0
        exit_code = run(
            ["--cove-home", str(cove_home), "push", str(workflow_dir / "workflow.cove.yaml")]
        )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output.count(f"Published workflow bundle '{PUBLISHER_DOMAIN}/hello_world'") == 2
    assert f"Latest workflow: v1/workflows/{PUBLISHER_DOMAIN}/hello_world/latest" in output


def test_push_overwrite_replaces_existing_workflow_slot(tmp_path, capsys) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".carol_cove"

    with MockCovehubServer() as server:
        _write_config(
            cove_home,
            _owner_config(server.url),
        )
        assert run(
            ["--cove-home", str(cove_home), "push", str(workflow_dir / "workflow.cove.yaml")]
        ) == 0
        capsys.readouterr()

        exit_code = run(
            [
                "--cove-home",
                str(cove_home),
                "push",
                "--overwrite",
                str(workflow_dir / "workflow.cove.yaml"),
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert f"Published workflow bundle '{PUBLISHER_DOMAIN}/hello_world'" in output


def test_push_fails_when_images_cannot_be_digest_pinned(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".carol_cove"

    with MockCovehubServer() as server:
        _write_config(
            cove_home,
            _owner_config(server.url),
        )
        final_server_compose_path = workflow_dir / "nodes" / "final_server.compose.yaml"
        current_text = final_server_compose_path.read_text(encoding="utf-8")
        current_image_line = next(
            line for line in current_text.splitlines() if line.strip().startswith('image: "covehub/cove-demo-hello-world-final-server@sha256:')
        )
        final_server_compose_path.write_text(
            current_text.replace(
                current_image_line,
                '    image: "covehub/cove-demo-hello-world-final-server:dev"',
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            "cove_cli.compile.resolve_image_reference_to_digest",
            lambda _image: (_ for _ in ()).throw(CompileCommandError("unreachable")),
        )

        exit_code = run(
            ["--cove-home", str(cove_home), "push", str(workflow_dir / "workflow.cove.yaml")]
        )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "unreachable" in output


def test_push_fails_when_artifact_provisioner_digest_is_not_canonical(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".carol_cove"

    with MockCovehubServer() as server:
        _write_config(
            cove_home,
            _owner_config(server.url),
        )
        original_canonical_container_ref = canonical_container_ref
        monkeypatch.setattr(
            "cove_cli.compile.canonical_container_ref",
            lambda image_name: (
                "registry.example/cove-artifact-provisioner@"
                "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
                if image_name == "cove-artifact-provisioner"
                else original_canonical_container_ref(image_name)
            ),
        )

        exit_code = run(
            ["--cove-home", str(cove_home), "push", str(workflow_dir / "workflow.cove.yaml")]
        )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "artifact provisioner digest is not canonical" in output


def test_provision_allow_records_rule_from_pulled_compose(tmp_path, capsys) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".alice_cove"
    pulled_root = _push_and_pull_hello_world(tmp_path, cove_home, workflow_dir)

    paths = provision_paths_for_home(cove_home)
    state = ProvisionState(paths.database_path)
    state.initialize()

    compose_path = pulled_root / "nodes" / "final_server" / "compose.generated.yaml"
    exit_code = run(
        [
            "--cove-home",
            str(cove_home),
            "provision",
            "allow",
            "alice_secret_word_transformed",
            str(compose_path),
        ]
    )

    output = capsys.readouterr().out
    bundle = load_workflow_bundle(pulled_root)
    final_node = next(node for node in bundle.nodes if node.node_id == "final_server")
    dynamic_hub_path = f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest"
    rule = state.get_allow_rule(
        hub_path=dynamic_hub_path,
        publisher=PUBLISHER_DOMAIN,
        workflow_id="hello_world",
        node_id="final_server",
        compose_hash=final_node.compose_hash,
        artifact_provisioner_digest=_CANONICAL_DIGEST,
    )
    channel = state.get_dynamic_artifact_channel(dynamic_hub_path)

    assert exit_code == 0
    assert rule is not None
    assert channel is not None
    assert channel.key_path == f"dynamic/{PUBLISHER_DOMAIN}/hello_world/alice_secret_word_transformed"
    assert "Allowed artifact 'alice_secret_word_transformed'" in output


def test_provision_inspect_pulls_bundle_and_only_allows_yes_nodes(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".alice_cove"
    with MockCovehubServer() as server:
        carol_home = tmp_path / ".carol_cove"
        _write_config(
            carol_home,
            _owner_config(server.url),
        )
        assert run(
            ["--cove-home", str(carol_home), "push", str(workflow_dir / "workflow.cove.yaml")]
        ) == 0
        capsys.readouterr()

        _write_config(
            cove_home,
            _owner_config(server.url, owner_server_url=ALICE_OWNER_URL),
        )
        paths = provision_paths_for_home(cove_home)
        state = ProvisionState(paths.database_path)
        state.initialize()
        static_hub_path = _generated_static_hub_path(
            ALICE_DOMAIN,
            "alice_secret_word",
            ALICE_SECRET_WORD_PLAINTEXT_HASH,
        )
        state.upsert_registered_artifact(
            hub_path=static_hub_path,
            artifact_id="alice_secret_word",
            owner_domain=ALICE_DOMAIN,
            owner_url=ALICE_OWNER_URL,
            plaintext_hash=ALICE_SECRET_WORD_PLAINTEXT_HASH,
            ciphertext_hash=_generated_static_ciphertext_hash(
                ALICE_DOMAIN,
                "alice_secret_word",
                ALICE_SECRET_WORD_PLAINTEXT_HASH,
            ),
            content_type="text/plain",
            source_path=str(tmp_path / "alice.txt"),
            server_url=server.url,
            transport_mode="encrypted",
            key_path="alice_secret_word",
        )
        answers = iter(["y", "n", "y"])
        monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

        exit_code = run(
            [
                "--cove-home",
                str(cove_home),
                "provision",
                "inspect",
                f"{PUBLISHER_DOMAIN}/hello_world",
            ]
        )

    output = capsys.readouterr().out
    pulled_root = (
        provision_paths_for_home(cove_home).materialized_workflows_dir
        / PUBLISHER_DOMAIN
        / "hello_world"
    )
    bundle = load_workflow_bundle(pulled_root)
    static_rule_nodes = sorted(
        rule.node_id
        for node in bundle.nodes
        for rule in [
            state.get_allow_rule(
                hub_path=static_hub_path,
                publisher=PUBLISHER_DOMAIN,
                workflow_id="hello_world",
                node_id=node.node_id,
                compose_hash=node.compose_hash,
                artifact_provisioner_digest=_CANONICAL_DIGEST,
            )
        ]
        if rule is not None
    )
    dynamic_rule_nodes = sorted(
        rule.node_id
        for node in bundle.nodes
        for rule in [
            state.get_allow_rule(
                hub_path=f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest",
                publisher=PUBLISHER_DOMAIN,
                workflow_id="hello_world",
                node_id=node.node_id,
                compose_hash=node.compose_hash,
                artifact_provisioner_digest=_CANONICAL_DIGEST,
            )
        ]
        if rule is not None
    )
    dynamic_channel = state.get_dynamic_artifact_channel(
        f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest"
    )

    assert exit_code == 0
    assert "Reviewed nodes: 3" in output
    assert static_rule_nodes == ["alice_word_length_checker"]
    assert dynamic_rule_nodes == ["alice_word_length_checker", "final_server"]
    assert dynamic_channel is not None
    assert "Node: bob_word_length_checker" not in output


def test_reset_runtime_command_deletes_remote_artifacts_and_certificates(tmp_path, monkeypatch, capsys) -> None:
    cove_home = tmp_path / ".carol_cove"
    observed_write_identity: dict[str, object] = {}

    def fake_fetch_owner_identity_for_write(
        *,
        owner_url: str,
        owner_private_key_path: Path,
        expected_owner_domain: str | None = None,
        timeout: float = 5.0,
    ):
        observed_write_identity["owner_url"] = owner_url
        observed_write_identity["expected_owner_domain"] = expected_owner_domain
        return _local_write_identity(owner_url, owner_private_key_path)

    monkeypatch.setattr(
        "cove_cli.provision.fetch_owner_identity_for_write",
        fake_fetch_owner_identity_for_write,
    )

    with MockCovehubServer() as server:
        _write_config(
            cove_home,
            _owner_config(server.url),
        )
        server.seed_runtime_artifact(
            f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest",
            payload=b"ciphertext",
        )
        server.seed_runtime_certificate(
            f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/certificates/final_server/latest",
            payload=b"{}",
        )

        exit_code = run(
            ["--cove-home", str(cove_home), "reset-runtime", f"{PUBLISHER_DOMAIN}/hello_world"]
        )

        assert server.state.runtime_artifacts == {}
        assert server.state.runtime_certificates == {}

    output = capsys.readouterr().out
    assert exit_code == 0
    assert observed_write_identity == {
        "owner_url": PUBLISHER_OWNER_URL,
        "expected_owner_domain": PUBLISHER_DOMAIN,
    }
    assert f"Cleared runtime state for '{PUBLISHER_DOMAIN}/hello_world'" in output


def test_allow_gated_key_release_supports_artifact_provisioner_sidecar(tmp_path) -> None:
    cove_home = tmp_path / ".alice_cove"
    paths = provision_paths_for_home(cove_home)
    state = ProvisionState(paths.database_path)
    state.initialize()

    plaintext = b"hello\n"
    plaintext_hash = sha256_literal(plaintext)
    artifact_id = "alice_secret_word"
    _key_path, key_bytes = ensure_artifact_key(
        keys_dir=paths.keys_dir,
        artifact_id=artifact_id,
    )
    ciphertext = encrypt_artifact_bytes(plaintext=plaintext, key_bytes=key_bytes)
    ciphertext_hash = sha256_literal(ciphertext)
    hub_path = f"v1/artifacts/{LOCAL_OWNER_DOMAIN}/{artifact_id}/{ciphertext_hash}"
    port = _free_port()
    owner_url = f"http://127.0.0.1:{port}"
    state.upsert_registered_artifact(
        hub_path=hub_path,
        artifact_id=artifact_id,
        owner_domain=LOCAL_OWNER_DOMAIN,
        owner_url=owner_url,
        plaintext_hash=plaintext_hash,
        ciphertext_hash=ciphertext_hash,
        content_type="text/plain",
        source_path=str(tmp_path / "fixture.txt"),
        server_url="http://unused",
        transport_mode="encrypted",
        key_path=artifact_id,
    )
    state.upsert_allow_rule(
        artifact_id=artifact_id,
        hub_path=hub_path,
        publisher=LOCAL_OWNER_DOMAIN,
        workflow_id="hello_world",
        node_id="final_server",
        compose_hash="sha256:" + "2" * 64,
        artifact_provisioner_digest=_CANONICAL_DIGEST,
    )

    with MockCovehubServer() as server, _running_provision_server(
        state=state,
        cove_home=cove_home,
        owner_domain=LOCAL_OWNER_DOMAIN,
        port=port,
        owner_url=owner_url,
    ) as provision_server:
        server.seed_artifact(hub_path, payload=ciphertext)
        staged_plaintext_path = tmp_path / "runtime" / "cove" / "inputs" / artifact_id / artifact_id
        metadata_path = tmp_path / "runtime" / "cove" / "inputs" / artifact_id / "metadata.json"
        _ARTIFACT_PROVISIONER.run(
            {
                "artifact_name": artifact_id,
                "hub_path": hub_path,
                "owner": "alice",
                "owners": {"alice": provision_server["url"]},
                "covehub_server_url": server.url,
                "owner_identity": provision_server["identity"],
                "expected_plaintext_hash": plaintext_hash,
                "staged_plaintext_path": str(staged_plaintext_path),
                "metadata_path": str(metadata_path),
                "workflow_publisher_domain": LOCAL_OWNER_DOMAIN,
                "workflow_id": "hello_world",
                "node_id": "final_server",
                "compose_hash": "sha256:" + "2" * 64,
                "artifact_provisioner_image": f"registry.example/cove-artifact-provisioner@{_CANONICAL_DIGEST}",
            }
        )

    assert staged_plaintext_path.read_bytes() == plaintext
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["ciphertext_hash"] == ciphertext_hash


def test_allow_gated_key_release_rejects_mismatched_attestation_identity(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".alice_cove"
    paths = provision_paths_for_home(cove_home)
    state = ProvisionState(paths.database_path)
    state.initialize()

    artifact_id = "alice_secret_word"
    _key_path, key_bytes = ensure_artifact_key(
        keys_dir=paths.keys_dir,
        artifact_id=artifact_id,
    )
    ciphertext = encrypt_artifact_bytes(plaintext=b"hello\n", key_bytes=key_bytes)
    ciphertext_hash = sha256_literal(ciphertext)
    hub_path = f"v1/artifacts/{LOCAL_OWNER_DOMAIN}/{artifact_id}/{ciphertext_hash}"
    port = _free_port()
    owner_url = f"http://127.0.0.1:{port}"
    state.upsert_registered_artifact(
        hub_path=hub_path,
        artifact_id=artifact_id,
        owner_domain=LOCAL_OWNER_DOMAIN,
        owner_url=owner_url,
        plaintext_hash=sha256_literal(b"hello\n"),
        ciphertext_hash=ciphertext_hash,
        content_type="text/plain",
        source_path=str(tmp_path / "fixture.txt"),
        server_url="http://unused",
        transport_mode="encrypted",
        key_path=artifact_id,
    )
    state.upsert_allow_rule(
        artifact_id=artifact_id,
        hub_path=hub_path,
        publisher=LOCAL_OWNER_DOMAIN,
        workflow_id="hello_world",
        node_id="final_server",
        compose_hash="sha256:" + "2" * 64,
        artifact_provisioner_digest=_CANONICAL_DIGEST,
    )
    monkeypatch.setattr(
        "cove_cli.provision_server.verify_attestation_bundle",
        lambda attestation, *, expected_report_data: (
            attestation
            if attestation.get("report_data") == expected_report_data.hex()
            else (_raise_runtime_error("attestation_bundle.report_data does not match expected report data"))
        ),
    )

    with _running_provision_server(
        state=state,
        cove_home=cove_home,
        owner_domain=LOCAL_OWNER_DOMAIN,
    ) as provision_server:
        request_payload = {
            "hub_path": hub_path,
            "workflow_publisher_domain": LOCAL_OWNER_DOMAIN,
            "workflow_id": "hello_world",
            "node_id": "final_server",
            "compose_hash": "sha256:" + "2" * 64,
            "artifact_provisioner_image": f"registry.example/cove-artifact-provisioner@{_CANONICAL_DIGEST}",
            "attestation": {
                "format": "phala_dstack_v1",
                "quote": "phala-quote",
                "report_data": build_key_release_report_data(
                    workflow_publisher_domain=LOCAL_OWNER_DOMAIN,
                    workflow_id="hello_world",
                    node_id="final_server",
                    compose_hash="sha256:" + "3" * 64,
                    artifact_provisioner_digest=_CANONICAL_DIGEST,
                ).hex(),
            },
        }
        request = urllib_request.Request(
            provision_server["url"] + "/v1/artifacts/key-release",
            data=json.dumps(request_payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        try:
            with urllib_request.urlopen(
                request,
                context=ssl._create_unverified_context(),
                timeout=5,
            ) as response:
                response.read()
        except urllib_request.HTTPError as exc:
            assert exc.code == 401
            payload = json.loads(exc.read().decode("utf-8"))
            assert "report data" in payload["detail"]
        else:  # pragma: no cover - defensive
            raise AssertionError("expected attestation identity verification failure")


def test_allow_gated_key_release_rejects_missing_allow_rule(tmp_path) -> None:
    cove_home = tmp_path / ".alice_cove"
    paths = provision_paths_for_home(cove_home)
    state = ProvisionState(paths.database_path)
    state.initialize()

    plaintext = b"hello\n"
    artifact_id = "alice_secret_word"
    _key_path, key_bytes = ensure_artifact_key(
        keys_dir=paths.keys_dir,
        artifact_id=artifact_id,
    )
    ciphertext = encrypt_artifact_bytes(plaintext=plaintext, key_bytes=key_bytes)
    ciphertext_hash = sha256_literal(ciphertext)
    hub_path = f"v1/artifacts/{LOCAL_OWNER_DOMAIN}/{artifact_id}/{ciphertext_hash}"
    port = _free_port()
    owner_url = f"http://127.0.0.1:{port}"
    state.upsert_registered_artifact(
        hub_path=hub_path,
        artifact_id=artifact_id,
        owner_domain=LOCAL_OWNER_DOMAIN,
        owner_url=owner_url,
        plaintext_hash=sha256_literal(plaintext),
        ciphertext_hash=ciphertext_hash,
        content_type="text/plain",
        source_path=str(tmp_path / "fixture.txt"),
        server_url="http://unused",
        transport_mode="encrypted",
        key_path=artifact_id,
    )

    with MockCovehubServer() as server, _running_provision_server(
        state=state,
        cove_home=cove_home,
        owner_domain=LOCAL_OWNER_DOMAIN,
        port=port,
        owner_url=owner_url,
    ) as provision_server:
        server.seed_artifact(hub_path, payload=ciphertext)
        try:
            _ARTIFACT_PROVISIONER.run(
                {
                    "artifact_name": artifact_id,
                    "hub_path": hub_path,
                    "owner": "alice",
                    "owners": {"alice": provision_server["url"]},
                    "covehub_server_url": server.url,
                    "owner_identity": provision_server["identity"],
                    "expected_plaintext_hash": sha256_literal(plaintext),
                    "staged_plaintext_path": str(tmp_path / "runtime" / "cove" / "inputs" / artifact_id / artifact_id),
                    "metadata_path": str(tmp_path / "runtime" / "cove" / "inputs" / artifact_id / "metadata.json"),
                    "workflow_publisher_domain": LOCAL_OWNER_DOMAIN,
                    "workflow_id": "hello_world",
                    "node_id": "final_server",
                    "compose_hash": "sha256:" + "2" * 64,
                    "artifact_provisioner_image": f"registry.example/cove-artifact-provisioner@{_CANONICAL_DIGEST}",
                }
            )
        except RuntimeErrorBase as exc:
            assert "HTTP 403" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected allow-gated key release failure")


def _copy_hello_world_workflow(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2] / "demos" / "hello_world" / "workflow"
    target = tmp_path / "workflow"
    shutil.copytree(source, target)
    certs_dir = target / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    write_test_certificate(
        certs_dir / "alice.pem",
        dns_names=["localhost"],
        ip_addresses=["127.0.0.1"],
    )
    write_test_certificate(
        certs_dir / "bob.pem",
        dns_names=["localhost"],
        ip_addresses=["127.0.0.1"],
    )
    return target


def _write_config(cove_home: Path, payload: dict[str, object]) -> None:
    config_path = config_path_for_home(cove_home)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _owner_config(server_url: str, **overrides: object) -> dict[str, object]:
    return {
        "covehub_server_url": server_url,
        "owner_server_url": PUBLISHER_OWNER_URL,
        **overrides,
    }


def _push_and_pull_hello_world(
    tmp_path: Path,
    cove_home: Path,
    workflow_dir: Path,
) -> Path:
    with MockCovehubServer() as server:
        publisher_home = tmp_path / ".carol_cove"
        _write_config(
            publisher_home,
            _owner_config(server.url),
        )
        assert run(
            ["--cove-home", str(publisher_home), "push", str(workflow_dir / "workflow.cove.yaml")]
        ) == 0
        _write_config(
            cove_home,
            _owner_config(server.url, owner_server_url=ALICE_OWNER_URL),
        )
        assert run(
            ["--cove-home", str(cove_home), "pull", f"{PUBLISHER_DOMAIN}/hello_world"]
        ) == 0

    return (
        provision_paths_for_home(cove_home).materialized_workflows_dir
        / PUBLISHER_DOMAIN
        / "hello_world"
    )


def _inline_service_config(services: dict[str, object], service_name: str) -> dict[str, object]:
    service = services[service_name]
    assert isinstance(service, dict)
    environment = service["environment"]
    assert isinstance(environment, dict)
    return json.loads(environment["COVE_CONFIG_JSON"])


def _raise_runtime_error(message: str):
    raise RuntimeErrorBase(message)


def _generated_static_ciphertext_hash(owner_domain: str, artifact_id: str, plaintext_hash: str) -> str:
    return sha256_literal(f"{owner_domain}:{artifact_id}:{plaintext_hash}".encode("utf-8"))


def _generated_static_hub_path(owner_domain: str, artifact_id: str, plaintext_hash: str) -> str:
    ciphertext_hash = _generated_static_ciphertext_hash(owner_domain, artifact_id, plaintext_hash)
    return f"v1/artifacts/{owner_domain}/{artifact_id}/{ciphertext_hash}"


def _local_write_identity(owner_url: str, owner_private_key_path: Path) -> dict[str, object]:
    return build_owner_identity_document(
        owner_url=owner_url,
        owner_private_key_path=owner_private_key_path,
        owner_public_key_path=owner_private_key_path.parent / "owner-signing-public.pem",
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _running_provision_server:
    def __init__(
        self,
        *,
        state: ProvisionState,
        cove_home: Path,
        owner_domain: str,
        port: int | None = None,
        owner_url: str | None = None,
    ) -> None:
        self.state = state
        self.cove_home = cove_home
        self.owner_domain = owner_domain
        self.port = port if port is not None else _free_port()
        self.owner_url = owner_url or f"http://127.0.0.1:{self.port}"
        self.server = None
        self.thread = None
        self.info: dict[str, object] | None = None

    def __enter__(self) -> dict[str, object]:
        paths = provision_paths_for_home(self.cove_home)
        self.server = create_provision_server(
            state=self.state,
            keys_dir=paths.keys_dir,
            port=self.port,
            owner_domain=self.owner_domain,
            owner_url=self.owner_url,
        )
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.info = {
            "url": f"http://{host}:{port}",
            "identity": self.server.owner_identity,
        }
        return self.info

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.server is not None
        assert self.thread is not None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
