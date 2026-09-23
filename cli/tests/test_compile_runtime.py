from __future__ import annotations

import json
import shutil
import socket
import ssl
import subprocess
import sys
from pathlib import Path
from threading import Thread
from urllib import parse as urllib_parse
from urllib import error as urllib_error
from urllib import request as urllib_request

import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_RUNTIME_SRC = _REPO_ROOT / "cove_container_runtime" / "src"
if str(_RUNTIME_SRC) not in sys.path:
    sys.path.insert(0, str(_RUNTIME_SRC))

from cove_cli.canonical_images import canonical_container_ref
from cove_cli.common import RuntimeErrorBase as CliRuntimeErrorBase
from cove_cli.config import ConfigError
from cove_container_runtime.common import (
    RuntimeErrorBase as ContainerRuntimeErrorBase,
    decrypt_ciphertext_bytes,
    encrypt_plaintext_bytes,
)
from cove_container_runtime.test_support import (
    MOCK_ATTESTATION_FORMAT,
    build_mock_certificate,
    verify_mock_attestation_bundle,
)
from cove_cli.artifact_crypto import (
    ARTIFACT_KEY_BYTES,
    ensure_artifact_key,
    encrypt_artifact_bytes,
    sha256_literal,
)
from cove_cli.check import check_workflow
from cove_cli.cli import run
from cove_cli.compile import (
    _keypair_certificate_common_name,
    compile_workflow,
    compile_workflow_artifact,
    reviewed_compose_hash,
)
from cove_cli.config import provision_paths_for_home
from cove_cli.owner_identity import verify_owner_identity_document
from cove_cli.provision_server import create_provision_server
from cove_cli.provision_state import ProvisionState

from .support import MockCovehubServer, build_test_owner_identity, load_container_main_module, write_test_certificate


ALICE_DOMAIN = "demo-alice.covehub.io"
ALICE_OWNER_URL = f"https://{ALICE_DOMAIN}"
BOB_DOMAIN = "demo-bob.covehub.io"
BOB_OWNER_URL = f"https://{BOB_DOMAIN}"
CAROL_DOMAIN = "demo-carol.covehub.io"
CAROL_OWNER_URL = f"https://{CAROL_DOMAIN}"
PUBLISHER_DOMAIN = CAROL_DOMAIN
PUBLISHER_OWNER_URL = CAROL_OWNER_URL
LOCAL_OWNER_DOMAIN = "127.0.0.1"

_ARTIFACT_PROVISIONER = load_container_main_module("artifact_provisioner")
_DEPENDENCY_CERTIFICATE_FETCHER = load_container_main_module("dependency_certificate_fetcher")
_PRECONDITION_CHECKER = load_container_main_module("precondition_checker")
_SERVICE_CERTIFICATE_WRITER = load_container_main_module("service_certificate_writer")
_KEY_MANAGER = load_container_main_module("key_manager")
_NODE_CERTIFICATE_WRITER = load_container_main_module("node_certificate_writer")


class _RuntimeContractTestError(CliRuntimeErrorBase, ContainerRuntimeErrorBase):
    pass


@pytest.fixture(autouse=True)
def _stub_attestation_runtime(monkeypatch):
    def fake_collect_attestation_bundle(_settings, *, report_data: bytes):
        return {
            "format": "phala_dstack_v1",
            "quote": "phala-quote",
            "event_log": [{"event": "phala_test"}],
            "report_data": report_data.hex(),
        }

    def fake_verify_attestation_bundle(
        attestation_bundle,
        *,
        expected_report_data: bytes,
        expected_compose_hash: str,
        expected_deployed_compose_text: str | None = None,
    ):
        attestation_format = attestation_bundle.get("format")
        if attestation_format == "phala_dstack_v1":
            report_data = attestation_bundle.get("report_data")
            if not isinstance(report_data, str) or report_data.strip().lower() != expected_report_data.hex():
                raise _RuntimeContractTestError(
                    "attestation_bundle.report_data does not match expected report data"
                )
            return attestation_bundle
        if attestation_format == MOCK_ATTESTATION_FORMAT:
            return verify_mock_attestation_bundle(
                attestation_bundle,
                expected_report_data=expected_report_data,
            )
        raise _RuntimeContractTestError(f"unsupported attestation format: {attestation_format}")

    monkeypatch.setattr(
        "cove_cli.provision_server.verify_attestation_bundle",
        fake_verify_attestation_bundle,
    )
    monkeypatch.setattr(
        "cove_container_runtime.certificates.collect_attestation_bundle",
        fake_collect_attestation_bundle,
    )
    monkeypatch.setattr(
        "cove_container_runtime.certificates.verify_attestation_bundle",
        fake_verify_attestation_bundle,
    )
    monkeypatch.setattr(
        _ARTIFACT_PROVISIONER,
        "collect_attestation_bundle",
        fake_collect_attestation_bundle,
    )

    def fake_http_bytes_with_owner_identity(*, request, owner_identity, timeout):
        context = ssl._create_unverified_context()
        try:
            with urllib_request.urlopen(request, timeout=timeout, context=context) as response:
                return response.read()
        except urllib_error.HTTPError as exc:
            raise _RuntimeContractTestError(f"HTTP {exc.code} for {request.full_url}: {exc.reason}") from exc
        except urllib_error.URLError as exc:
            raise _RuntimeContractTestError(f"failed to reach {request.full_url}: {exc.reason}") from exc

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
        server_url = owner_url
        owner_domain = expected_owner_domain or "owner.example.test"
        return build_test_owner_identity(owner_domain, server_url)

    monkeypatch.setattr(
        "cove_cli.compile.fetch_owner_identity_document",
        fake_fetch_owner_identity_document,
    )

    def fake_static_resolution(*, owner, artifact_id: str, plaintext_hash: str):
        ciphertext_hash = sha256_literal(
            f"{owner.owner_domain}:{artifact_id}:{plaintext_hash}".encode("utf-8")
        )
        return {
            "hub_path": f"v1/artifacts/{owner.owner_domain}/{artifact_id}/{ciphertext_hash}",
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


def test_compile_uses_default_workflow_path(tmp_path, monkeypatch, capsys) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = _write_local_config(tmp_path, "http://127.0.0.1:8000")

    monkeypatch.chdir(workflow_dir)
    exit_code = run(["--cove-home", str(cove_home), "compile"])

    captured = capsys.readouterr()
    normalized_path = workflow_dir / "build" / "workflow.normalized.cove.yaml"
    normalized = yaml.safe_load(normalized_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert "Compiled workflow 'hello_world'" in captured.out
    assert normalized_path.is_file()
    assert normalized["owners"] == {
        "alice": ALICE_OWNER_URL,
        "bob": BOB_OWNER_URL,
    }
    assert normalized["artifacts"]["alice_secret_word"]["owner"] == "alice"
    expected_ciphertext_hash = sha256_literal(
        f"{ALICE_DOMAIN}:alice_secret_word:{normalized['artifacts']['alice_secret_word']['plaintext_hash']}".encode("utf-8")
    )
    assert (
        normalized["artifacts"]["alice_secret_word"]["hub_path"]
        == f"v1/artifacts/{ALICE_DOMAIN}/alice_secret_word/{expected_ciphertext_hash}"
    )
    assert normalized["artifacts"]["alice_secret_word_transformed"]["hub_path"] == (
        f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest"
    )


def test_compile_rejects_unknown_service_keypair(tmp_path) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    workflow_path = workflow_dir / "workflow.cove.yaml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace("- session_key", "- missing_key"),
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("references unknown keypair" in error for error in report.errors)


def test_compile_rejects_nonboolean_should_terminate(tmp_path) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    workflow_path = workflow_dir / "workflow.cove.yaml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace("should_terminate: False", "should_terminate: nope"),
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("should_terminate must be a boolean" in error for error in report.errors)


def test_compile_rejects_legacy_owner_provisioning_fields(tmp_path) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    workflow_path = workflow_dir / "workflow.cove.yaml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            f"  alice: {ALICE_OWNER_URL}",
            "\n".join(
                [
                    "  alice:",
                    "    provisioning_url: http://127.0.0.1:9001",
                    "    provisioning_tls_certificate: certs/alice.pem",
                ]
            ),
        ),
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("provisioning_url is no longer supported" in error for error in report.errors)
    assert any("provisioning_tls_certificate is no longer supported" in error for error in report.errors)


def test_compile_rejects_non_phala_dstack_platform(tmp_path) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    workflow_path = workflow_dir / "workflow.cove.yaml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace("provider: phala", "provider: local"),
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert "platform.provider must be 'phala'" in report.errors


def test_compile_emits_generated_compose_hash_and_sidecars(tmp_path) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = _write_local_config(tmp_path, "http://127.0.0.1:8000")

    compile_workflow(workflow_dir / "workflow.cove.yaml", cove_home=cove_home)

    final_node_dir = workflow_dir / "build" / "nodes" / "final_server"
    alice_node_dir = workflow_dir / "build" / "nodes" / "alice_character_set_checker"
    bob_node_dir = workflow_dir / "build" / "nodes" / "bob_character_set_checker"
    character_node_dir = workflow_dir / "build" / "nodes" / "word_length_checker"
    compose_path = final_node_dir / "compose.generated.yaml"
    compose_hash_path = final_node_dir / "compose.generated.sha256"

    assert compose_path.is_file()
    assert compose_hash_path.is_file()
    assert not (final_node_dir / "configs").exists()

    compose_text = compose_path.read_text(encoding="utf-8")
    compose_payload = yaml.safe_load(compose_text)
    services = compose_payload["services"]
    alice_compose_text = (alice_node_dir / "compose.generated.yaml").read_text(encoding="utf-8")
    alice_payload = yaml.safe_load(alice_compose_text)
    alice_services = alice_payload["services"]

    assert "cove_key_manager" in services
    assert "cove_provision_alice_secret_word_transformed" in services
    assert "cove_provision_bob_secret_word_transformed" in services
    assert "cove_dependency_certificate_fetcher" in services
    assert "cove_preconditions_final_server" in services
    assert "cove_service_certificate_writer_final_server" not in services
    assert "cove_node_certificate_writer" in services
    assert "cove_publish_alice_secret_word_transformed" in alice_services
    assert services["final_server"]["depends_on"]["cove_key_manager"]["condition"] == "service_completed_successfully"
    assert (
        services["cove_provision_alice_secret_word_transformed"]["image"]
        == canonical_container_ref("cove-artifact-provisioner")
    )
    assert (
        services["cove_dependency_certificate_fetcher"]["image"]
        == canonical_container_ref("cove-dependency-certificate-fetcher")
    )
    assert (
        services["cove_preconditions_final_server"]["image"]
        == canonical_container_ref("cove-precondition-checker")
    )
    assert (
        services["cove_key_manager"]["image"]
        == canonical_container_ref("cove-key-manager")
    )
    key_manager_config = _inline_service_config(services, "cove_key_manager")
    assert key_manager_config["keypairs"][0]["certificate_common_name"] == (
        "hello_world.final_server.session_key"
    )
    assert (
        services["cove_node_certificate_writer"]["image"]
        == canonical_container_ref("cove-node-certificate-writer")
    )
    assert services["final_server"]["image"].startswith(
        "covehub/cove-demo-hello-world-final-server@sha256:"
    )
    assert (
        alice_services["cove_service_certificate_writer_character_set_checker"]["image"]
        == canonical_container_ref("cove-service-certificate-writer")
    )
    assert services["cove_dependency_certificate_fetcher"]["network_mode"] == "host"
    assert services["cove_provision_alice_secret_word_transformed"]["network_mode"] == "host"
    assert services["cove_node_certificate_writer"]["network_mode"] == "host"
    assert "healthcheck" not in services["cove_node_certificate_writer"]
    assert "command" not in services["cove_provision_alice_secret_word_transformed"]
    assert (
        services["cove_provision_alice_secret_word_transformed"]["environment"]["COVE_SERVICE_NAME"]
        == "cove_provision_alice_secret_word_transformed"
    )
    assert (
        services["cove_provision_alice_secret_word_transformed"]["environment"]["COVE_COMPOSE_HASH"]
        == compose_hash_path.read_text(encoding="utf-8").strip()
    )
    assert "COVE_COMPOSE_PATH" not in services["cove_provision_alice_secret_word_transformed"]["environment"]
    assert compose_payload["volumes"]["cove_runtime"] == {}
    assert alice_payload["volumes"]["cove_runtime"] == {}
    assert services["cove_provision_alice_secret_word_transformed"]["volumes"][0] == {
        "type": "volume",
        "source": "cove_runtime",
        "target": "/cove",
        "read_only": False,
    }
    alice_copy_services = {
        service_name: service_payload
        for service_name, service_payload in alice_services.items()
        if service_name.startswith("cove_copy_")
    }
    assert any(
        service_payload["environment"]["COVE_INPUT_SOURCE"]
        == "/cove/inputs/alice_secret_word/alice_secret_word"
        and service_payload["environment"]["COVE_INPUT_TARGET"]
        == "/workspace/input/alice_secret_word.txt"
        for service_payload in alice_copy_services.values()
    )
    assert any(
        volume["type"] == "volume"
        and volume["source"].startswith("cove-input-")
        and volume["target"] == "/workspace/input"
        and volume["read_only"] is True
        for volume in alice_services["character_set_checker"]["volumes"]
    )
    assert any(
        volume["type"] == "volume"
        and volume["source"].startswith("cove-bind-")
        and volume["target"] == "/workspace/output"
        and volume["read_only"] is False
        for volume in alice_services["character_set_checker"]["volumes"]
    )
    assert any(
        service_name.startswith("cove_copy_")
        and service_payload["environment"]["COVE_INPUT_SOURCE"]
        == "/cove/inputs/alice_secret_word_transformed/alice_secret_word_transformed"
        and service_payload["environment"]["COVE_INPUT_TARGET"]
        == "/workspace/input/alice_secret_word_transformed.txt"
        for service_name, service_payload in services.items()
    )
    assert any(
        volume["type"] == "volume"
        and volume["source"] == "cove_runtime"
        and volume["target"] == "/cove"
        and volume["read_only"] is True
        for volume in services["final_server"]["volumes"]
    )
    assert "COVE_RUNTIME_IMAGE" not in compose_text
    assert "cove-runtime-sidecar" not in compose_text
    assert "COVE_CONFIG_JSON: |" in compose_text
    assert "COVE_COMPOSE_HASH:" in compose_text
    assert "/generated" not in compose_text
    assert "assets/schemas" not in compose_text
    assert "./runtime" not in compose_text
    assert "runtime/service_bindings" not in compose_text
    assert "cove-input-" in compose_text
    assert "cove-bind-" in alice_compose_text
    assert "COVE_IMAGE_REGISTRY_PREFIX" not in compose_text
    assert "COVE_DOCKER_TAG" not in compose_text
    assert str(workflow_dir / "build") not in compose_text

    expected_hash = reviewed_compose_hash(compose_payload)
    assert compose_hash_path.read_text(encoding="utf-8").strip() == expected_hash
    node_certificate_writer_config = _inline_service_config(
        services,
        "cove_node_certificate_writer",
    )
    assert node_certificate_writer_config["covehub_server_url"] == "http://127.0.0.1:8000"
    assert node_certificate_writer_config["workflow_publisher_domain"] == PUBLISHER_DOMAIN
    assert "generated_node_compose_hash" not in node_certificate_writer_config
    assert node_certificate_writer_config["attestation"] == {
        "mode": "phala_dstack",
        "provider": "phala",
        "runtime": "dstack",
    }
    artifact_provisioner_config = _inline_service_config(
        services,
        "cove_provision_alice_secret_word_transformed",
    )
    assert artifact_provisioner_config["covehub_server_url"] == "http://127.0.0.1:8000"
    assert artifact_provisioner_config["owner"] == "alice"
    assert artifact_provisioner_config["owners"] == {"alice": ALICE_OWNER_URL}
    assert artifact_provisioner_config["hub_path"] == (
        f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest"
    )
    assert "owner_url" not in artifact_provisioner_config
    assert "owner_domain" not in artifact_provisioner_config
    assert artifact_provisioner_config["owner_identity"]["owner_domain"] == ALICE_DOMAIN
    assert artifact_provisioner_config["owner_identity"]["owner_url"] == ALICE_OWNER_URL
    assert "provisioning_tls_certificate" not in artifact_provisioner_config
    assert "provisioning_url" not in artifact_provisioner_config
    assert "compose_hash" not in artifact_provisioner_config
    assert artifact_provisioner_config["artifact_provisioner_image"] == canonical_container_ref(
        "cove-artifact-provisioner"
    )
    assert artifact_provisioner_config["mode"] == "dynamic_input"
    assert (
        artifact_provisioner_config["producer_certificate_path"]
        == "/cove/certificates/alice_character_set_checker/certificate.json"
    )
    assert artifact_provisioner_config["attestation"] == {
        "mode": "phala_dstack",
        "provider": "phala",
        "runtime": "dstack",
    }
    precondition_config = _inline_service_config(
        services,
        "cove_preconditions_final_server",
    )
    assert precondition_config["service_name"] == "final_server"
    assert precondition_config["inputs"] == {
        "alice_secret_word_transformed": "/cove/inputs/alice_secret_word_transformed/metadata.json",
        "bob_secret_word_transformed": "/cove/inputs/bob_secret_word_transformed/metadata.json",
    }
    assert (
        precondition_config["preconditions"]["and"][0]["=="][0]["var"]
        == "certificates.word_length_checker.certificate_body.results.word_length_checker.pass"
    )
    assert (
        precondition_config["certificates"]["word_length_checker"]
        == "/cove/certificates/word_length_checker/certificate.json"
    )
    assert any(
        volume["source"] == "cove_runtime"
        and volume["target"] == "/cove"
        and volume["read_only"] is True
        for volume in services["final_server"]["volumes"]
    )
    final_dependency_config = _inline_service_config(
        services,
        "cove_dependency_certificate_fetcher",
    )
    assert final_dependency_config["dependencies"] == [
        {
            "node_name": "alice_character_set_checker",
            "certificate_path": "/cove/certificates/alice_character_set_checker/certificate.json",
            "expected_workflow_id": "hello_world",
            "expected_node_id": "alice_character_set_checker",
            "expected_generated_node_compose_hash": (
                alice_node_dir / "compose.generated.sha256"
            ).read_text(encoding="utf-8").strip(),
        },
        {
            "node_name": "bob_character_set_checker",
            "certificate_path": "/cove/certificates/bob_character_set_checker/certificate.json",
            "expected_workflow_id": "hello_world",
            "expected_node_id": "bob_character_set_checker",
            "expected_generated_node_compose_hash": (
                bob_node_dir / "compose.generated.sha256"
            ).read_text(encoding="utf-8").strip(),
        },
        {
            "node_name": "word_length_checker",
            "certificate_path": "/cove/certificates/word_length_checker/certificate.json",
            "expected_workflow_id": "hello_world",
            "expected_node_id": "word_length_checker",
            "expected_generated_node_compose_hash": (
                character_node_dir / "compose.generated.sha256"
            ).read_text(encoding="utf-8").strip(),
        }
    ]
    character_services = yaml.safe_load(
        (character_node_dir / "compose.generated.yaml").read_text(encoding="utf-8")
    )["services"]
    character_dependency_config = _inline_service_config(
        character_services,
        "cove_dependency_certificate_fetcher",
    )
    assert character_dependency_config["dependencies"] == [
        {
            "node_name": "alice_character_set_checker",
            "certificate_path": "/cove/certificates/alice_character_set_checker/certificate.json",
            "expected_workflow_id": "hello_world",
            "expected_node_id": "alice_character_set_checker",
            "expected_generated_node_compose_hash": (
                alice_node_dir / "compose.generated.sha256"
            ).read_text(encoding="utf-8").strip(),
        },
        {
            "node_name": "bob_character_set_checker",
            "certificate_path": "/cove/certificates/bob_character_set_checker/certificate.json",
            "expected_workflow_id": "hello_world",
            "expected_node_id": "bob_character_set_checker",
            "expected_generated_node_compose_hash": (
                bob_node_dir / "compose.generated.sha256"
            ).read_text(encoding="utf-8").strip(),
        },
    ]
    alice_node_certificate_config = _inline_service_config(
        alice_services,
        "cove_node_certificate_writer",
    )
    assert alice_node_certificate_config["outputs"] == [
        {
            "name": "alice_secret_word_transformed",
            "path": "/cove/outputs/alice_secret_word_transformed/metadata.json",
        }
    ]
    alice_output_config = _inline_service_config(
        alice_services,
        "cove_publish_alice_secret_word_transformed",
    )
    assert alice_output_config["mode"] == "dynamic_output"
    assert alice_output_config["output_source_path"] == "/workspace/output/alice_secret_word_transformed.txt"
    assert alice_output_config["owner"] == "alice"
    assert alice_output_config["owners"] == {"alice": ALICE_OWNER_URL}
    assert alice_output_config["hub_path"] == (
        f"v1/runtime/{PUBLISHER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest"
    )
    assert "owner_domain" not in alice_output_config
    assert "owner_url" not in alice_output_config
    assert alice_output_config["owner_identity"]["owner_domain"] == ALICE_DOMAIN
    assert "provisioning_tls_certificate" not in alice_output_config
    assert "provisioning_url" not in alice_output_config

    alice_input_config = _inline_service_config(
        alice_services,
        "cove_provision_alice_secret_word",
    )
    assert alice_input_config["owner"] == "alice"
    assert alice_input_config["owners"] == {"alice": ALICE_OWNER_URL}
    expected_ciphertext_hash = sha256_literal(
        f"{ALICE_DOMAIN}:alice_secret_word:sha256:5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03".encode("utf-8")
    )
    assert alice_input_config["hub_path"] == (
        f"v1/artifacts/{ALICE_DOMAIN}/alice_secret_word/{expected_ciphertext_hash}"
    )


def test_compile_bounds_long_keypair_certificate_common_names() -> None:
    assert (
        _keypair_certificate_common_name(
            workflow_id="hello_world",
            node_name="final_server",
            keypair_name="session_key",
        )
        == "hello_world.final_server.session_key"
    )

    common_name = _keypair_certificate_common_name(
        workflow_id="attested_confidential_benchmark__vllm_cpu",
        node_name="final_server",
        keypair_name="session_key",
    )

    assert common_name == "cove-final_server.session_key-0f8736dfed29b034"
    assert len(common_name.encode("utf-8")) <= 64


def test_compile_orders_generated_nodes_topologically_and_stably(tmp_path) -> None:
    workflow_dir = tmp_path / "workflow"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    _write_schema_file(workflow_dir)
    for node_name in ("final", "mid_two", "leaf", "mid_one"):
        compose_path = workflow_dir / "nodes" / f"{node_name}.compose.yaml"
        compose_path.parent.mkdir(parents=True, exist_ok=True)
        compose_path.write_text(
            "services:\n  worker:\n    image: example/demo@sha256:" + ("1" * 64) + "\n",
            encoding="utf-8",
        )
    (workflow_dir / "workflow.cove.yaml").write_text(
        """
cove_version: 1
workflow:
  id: ordered_demo
platform:
  provider: phala
  runtime: dstack
owners: {}
artifacts: {}
nodes:
  final:
    compose: nodes/final.compose.yaml
    dependencies: [mid_one, mid_two]
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
  mid_two:
    compose: nodes/mid_two.compose.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
  leaf:
    compose: nodes/leaf.compose.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
  mid_one:
    compose: nodes/mid_one.compose.yaml
    dependencies: [leaf]
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        encoding="utf-8",
    )
    cove_home = _write_local_config(tmp_path, "http://127.0.0.1:8000")

    artifact = compile_workflow_artifact(
        workflow_dir / "workflow.cove.yaml",
        cove_home=cove_home,
    )

    assert [path.parent.name for path in artifact.generated_nodes] == [
        "mid_two",
        "leaf",
        "mid_one",
        "final",
    ]


def test_compile_mounts_dstack_sock_only_into_attesting_sidecars(tmp_path) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = _write_local_config(tmp_path, "http://127.0.0.1:8000")

    compile_workflow(workflow_dir / "workflow.cove.yaml", cove_home=cove_home)

    final_services = yaml.safe_load(
        (workflow_dir / "build" / "nodes" / "final_server" / "compose.generated.yaml").read_text(
            encoding="utf-8"
        )
    )["services"]
    assert _has_bind_mount(
        final_services["cove_provision_alice_secret_word_transformed"],
        "/var/run/dstack.sock",
    )
    assert _has_bind_mount(
        final_services["cove_node_certificate_writer"],
        "/var/run/dstack.sock",
    )
    assert not _has_bind_mount(final_services["final_server"], "/var/run/dstack.sock")
    assert not _has_bind_mount(
        final_services["cove_dependency_certificate_fetcher"],
        "/var/run/dstack.sock",
    )
    assert not _has_bind_mount(final_services["cove_key_manager"], "/var/run/dstack.sock")


def test_compile_rejects_removed_attestation_mode_config_field(tmp_path) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".cove"
    config_path = cove_home / "config.yaml"
    cove_home.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                "covehub_server_url: http://127.0.0.1:8000",
                f"owner_server_url: {PUBLISHER_OWNER_URL}",
                "attestation_mode: mock",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="attestation_mode"):
        compile_workflow(workflow_dir / "workflow.cove.yaml", cove_home=cove_home)


def test_artifact_provisioner_decrypts_and_writes_metadata(tmp_path) -> None:
    cove_home = tmp_path / ".alice_cove"
    provision_paths = provision_paths_for_home(cove_home)
    state = ProvisionState(provision_paths.database_path)
    state.initialize()

    plaintext = b"hello\n"
    plaintext_hash = sha256_literal(plaintext)
    artifact_id = "alice_secret_word"
    _key_path, key_bytes = ensure_artifact_key(
        keys_dir=provision_paths.keys_dir,
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

    with MockCovehubServer() as server, _running_provision_server(
        state=state,
        keys_dir=provision_paths.keys_dir,
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
                "owners": {"alice": provision_server.url},
                "covehub_server_url": server.url,
                "owner_identity": provision_server.owner_identity,
                "expected_plaintext_hash": plaintext_hash,
                "staged_plaintext_path": str(staged_plaintext_path),
                "metadata_path": str(metadata_path),
            }
        )

    assert staged_plaintext_path.read_bytes() == plaintext
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["plaintext_hash"] == plaintext_hash
    assert metadata["ciphertext_hash"] == ciphertext_hash


def test_artifact_provisioner_accepts_baked_owner_identity(tmp_path) -> None:
    cove_home = tmp_path / ".alice_cove"
    provision_paths = provision_paths_for_home(cove_home)
    state = ProvisionState(provision_paths.database_path)
    state.initialize()

    plaintext = b"hello\n"
    plaintext_hash = sha256_literal(plaintext)
    artifact_id = "alice_secret_word"
    _key_path, key_bytes = ensure_artifact_key(
        keys_dir=provision_paths.keys_dir,
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

    with MockCovehubServer() as server, _running_provision_server(
        state=state,
        keys_dir=provision_paths.keys_dir,
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
                "owners": {"alice": owner_url},
                "covehub_server_url": server.url,
                "owner_identity": provision_server.server.owner_identity,
                "expected_plaintext_hash": plaintext_hash,
                "staged_plaintext_path": str(staged_plaintext_path),
                "metadata_path": str(metadata_path),
            }
        )

    assert staged_plaintext_path.read_bytes() == plaintext


def test_artifact_provisioner_rejects_tampered_baked_owner_identity(tmp_path) -> None:
    cove_home = tmp_path / ".alice_cove"
    provision_paths = provision_paths_for_home(cove_home)
    state = ProvisionState(provision_paths.database_path)
    state.initialize()

    plaintext = b"hello\n"
    artifact_id = "alice_secret_word"
    _key_path, key_bytes = ensure_artifact_key(
        keys_dir=provision_paths.keys_dir,
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
        keys_dir=provision_paths.keys_dir,
        port=port,
        owner_url=owner_url,
    ) as provision_server:
        server.seed_artifact(hub_path, payload=ciphertext)
        tampered_identity = dict(provision_server.server.owner_identity)
        tampered_identity["owner_url"] = "https://alice.example.com"
        with pytest.raises(ContainerRuntimeErrorBase, match="invalid owner_identity"):
            _ARTIFACT_PROVISIONER.run(
                {
                    "artifact_name": artifact_id,
                    "hub_path": hub_path,
                    "owner": "alice",
                    "owners": {"alice": owner_url},
                    "covehub_server_url": server.url,
                    "owner_identity": tampered_identity,
                    "expected_plaintext_hash": sha256_literal(plaintext),
                    "staged_plaintext_path": str(tmp_path / "runtime" / "cove" / "inputs" / artifact_id / artifact_id),
                    "metadata_path": str(tmp_path / "runtime" / "cove" / "inputs" / artifact_id / "metadata.json"),
                }
            )


def test_artifact_provisioner_publishes_dynamic_output_and_writes_metadata(tmp_path) -> None:
    cove_home = tmp_path / ".alice_cove"
    provision_paths = provision_paths_for_home(cove_home)
    state = ProvisionState(provision_paths.database_path)
    state.initialize()

    artifact_name = "alice_secret_word_transformed"
    hub_path = f"v1/runtime/{LOCAL_OWNER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest"
    key_path = f"dynamic/{LOCAL_OWNER_DOMAIN}/hello_world/alice_secret_word_transformed"
    _key_file, key_bytes = ensure_artifact_key(
        keys_dir=provision_paths.keys_dir,
        artifact_id=key_path,
    )
    state.upsert_dynamic_artifact_channel(
        hub_path=hub_path,
        artifact_name=artifact_name,
        owner_domain=LOCAL_OWNER_DOMAIN,
        publisher=LOCAL_OWNER_DOMAIN,
        workflow_id="hello_world",
        key_path=key_path,
    )
    artifact_provisioner_image = canonical_container_ref("cove-artifact-provisioner")
    artifact_provisioner_digest = artifact_provisioner_image.rsplit("@", 1)[1]
    compose_hash = "sha256:" + "2" * 64
    state.upsert_allow_rule(
        artifact_id=artifact_name,
        hub_path=hub_path,
        publisher=LOCAL_OWNER_DOMAIN,
        workflow_id="hello_world",
        node_id="alice_character_set_checker",
        compose_hash=compose_hash,
        artifact_provisioner_digest=artifact_provisioner_digest,
    )

    plaintext = b"HELLO\n"
    output_source_path = tmp_path / "runtime" / "service_bindings" / "character_set_checker" / "workspace_output" / "alice_secret_word_transformed.txt"
    metadata_path = tmp_path / "runtime" / "cove" / "outputs" / artifact_name / "metadata.json"
    output_source_path.parent.mkdir(parents=True, exist_ok=True)
    output_source_path.write_bytes(plaintext)

    with MockCovehubServer() as server, _running_provision_server(
        state=state,
        keys_dir=provision_paths.keys_dir,
    ) as provision_server:
        configured_hub_path = "runtime/hello_world/artifacts/alice_secret_word_transformed/latest"
        _ARTIFACT_PROVISIONER.run(
            {
                "mode": "dynamic_output",
                "artifact_name": artifact_name,
                "hub_path": configured_hub_path,
                "owner": "alice",
                "owners": {"alice": provision_server.url},
                "covehub_server_url": server.url,
                "owner_identity": provision_server.owner_identity,
                "workflow_publisher_domain": LOCAL_OWNER_DOMAIN,
                "workflow_id": "hello_world",
                "node_id": "alice_character_set_checker",
                "output_source_path": str(output_source_path),
                "metadata_path": str(metadata_path),
                "attestation": {
                    "mode": "phala_dstack",
                    "provider": "phala",
                    "runtime": "dstack",
                },
            },
            compose_hash=compose_hash,
            artifact_provisioner_image=artifact_provisioner_image,
        )

        ciphertext = server.state.runtime_artifacts[hub_path]

    assert ciphertext != plaintext
    assert decrypt_ciphertext_bytes(ciphertext=ciphertext, key_bytes=key_bytes) == plaintext
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["artifact_name"] == artifact_name
    assert metadata["plaintext_hash"] == sha256_literal(plaintext)
    assert metadata["ciphertext_hash"] == sha256_literal(ciphertext)
    assert metadata["content_type"] == "text/plain"


def test_artifact_provisioner_fetches_dynamic_input_from_producer_certificate(tmp_path) -> None:
    cove_home = tmp_path / ".alice_cove"
    provision_paths = provision_paths_for_home(cove_home)
    state = ProvisionState(provision_paths.database_path)
    state.initialize()

    artifact_name = "alice_secret_word_transformed"
    hub_path = f"v1/runtime/{LOCAL_OWNER_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest"
    key_path = f"dynamic/{LOCAL_OWNER_DOMAIN}/hello_world/alice_secret_word_transformed"
    _key_file, key_bytes = ensure_artifact_key(
        keys_dir=provision_paths.keys_dir,
        artifact_id=key_path,
    )
    state.upsert_dynamic_artifact_channel(
        hub_path=hub_path,
        artifact_name=artifact_name,
        owner_domain=LOCAL_OWNER_DOMAIN,
        publisher=LOCAL_OWNER_DOMAIN,
        workflow_id="hello_world",
        key_path=key_path,
    )
    artifact_provisioner_image = canonical_container_ref("cove-artifact-provisioner")
    artifact_provisioner_digest = artifact_provisioner_image.rsplit("@", 1)[1]
    compose_hash = "sha256:" + "4" * 64
    state.upsert_allow_rule(
        artifact_id=artifact_name,
        hub_path=hub_path,
        publisher=LOCAL_OWNER_DOMAIN,
        workflow_id="hello_world",
        node_id="final_server",
        compose_hash=compose_hash,
        artifact_provisioner_digest=artifact_provisioner_digest,
    )

    plaintext = b"HELLO\n"
    ciphertext = encrypt_plaintext_bytes(plaintext=plaintext, key_bytes=key_bytes)
    certificate_path = tmp_path / "runtime" / "cove" / "certificates" / "alice_character_set_checker" / "certificate.json"
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name="alice_character_set_checker",
        generated_node_compose_hash="sha256:" + "3" * 64,
        inputs={},
        outputs={
            artifact_name: {
                "artifact_name": artifact_name,
                "owner_domain": LOCAL_OWNER_DOMAIN,
                "hub_path": hub_path,
                "plaintext_hash": sha256_literal(plaintext),
                "ciphertext_hash": sha256_literal(ciphertext),
                "content_type": "text/plain",
                "encryption_algorithm": "aes-256-gcm",
            }
        },
        ephemeral_keypairs={},
        results={"character_set_checker": {"pass": True}},
    )
    certificate_path.parent.mkdir(parents=True, exist_ok=True)
    certificate_path.write_text(json.dumps(certificate, indent=2) + "\n", encoding="utf-8")

    with MockCovehubServer() as server, _running_provision_server(
        state=state,
        keys_dir=provision_paths.keys_dir,
    ) as provision_server:
        server.seed_runtime_artifact(hub_path, payload=ciphertext)
        configured_hub_path = "runtime/hello_world/artifacts/alice_secret_word_transformed/latest"
        staged_plaintext_path = tmp_path / "runtime" / "cove" / "inputs" / artifact_name / artifact_name
        metadata_path = tmp_path / "runtime" / "cove" / "inputs" / artifact_name / "metadata.json"
        _ARTIFACT_PROVISIONER.run(
            {
                "mode": "dynamic_input",
                "artifact_name": artifact_name,
                "hub_path": configured_hub_path,
                "owner": "alice",
                "owners": {"alice": provision_server.url},
                "covehub_server_url": server.url,
                "owner_identity": provision_server.owner_identity,
                "workflow_publisher_domain": LOCAL_OWNER_DOMAIN,
                "workflow_id": "hello_world",
                "node_id": "final_server",
                "producer_certificate_path": str(certificate_path),
                "staged_plaintext_path": str(staged_plaintext_path),
                "metadata_path": str(metadata_path),
                "attestation": {
                    "mode": "phala_dstack",
                    "provider": "phala",
                    "runtime": "dstack",
                },
            },
            compose_hash=compose_hash,
            artifact_provisioner_image=artifact_provisioner_image,
        )

    assert staged_plaintext_path.read_bytes() == plaintext
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["artifact_name"] == artifact_name
    assert metadata["owner_domain"] == LOCAL_OWNER_DOMAIN
    assert metadata["ciphertext_hash"] == sha256_literal(ciphertext)
    assert metadata["plaintext_hash"] == sha256_literal(plaintext)


def test_artifact_provisioner_rejects_ciphertext_hash_mismatch(tmp_path) -> None:
    cove_home = tmp_path / ".alice_cove"
    provision_paths = provision_paths_for_home(cove_home)
    state = ProvisionState(provision_paths.database_path)
    state.initialize()

    plaintext = b"hello\n"
    artifact_id = "alice_secret_word"
    expected_ciphertext_hash = "sha256:" + "f" * 64
    hub_path = f"v1/artifacts/{LOCAL_OWNER_DOMAIN}/{artifact_id}/{expected_ciphertext_hash}"
    _key_path, key_bytes = ensure_artifact_key(
        keys_dir=provision_paths.keys_dir,
        artifact_id=artifact_id,
    )
    ciphertext = encrypt_artifact_bytes(plaintext=plaintext, key_bytes=key_bytes)
    port = _free_port()
    owner_url = f"http://127.0.0.1:{port}"
    state.upsert_registered_artifact(
        hub_path=hub_path,
        artifact_id=artifact_id,
        owner_domain=LOCAL_OWNER_DOMAIN,
        owner_url=owner_url,
        plaintext_hash=sha256_literal(plaintext),
        ciphertext_hash=expected_ciphertext_hash,
        content_type="text/plain",
        source_path=str(tmp_path / "fixture.txt"),
        server_url="http://unused",
        transport_mode="encrypted",
        key_path=artifact_id,
    )

    with MockCovehubServer() as server, _running_provision_server(
        state=state,
        keys_dir=provision_paths.keys_dir,
        port=port,
        owner_url=owner_url,
    ) as provision_server:
        server.seed_artifact(hub_path, payload=ciphertext)
        try:
            _ARTIFACT_PROVISIONER.run(
                {
                    "artifact_name": artifact_id,
                    "hub_path": hub_path,
                    "owner_domain": LOCAL_OWNER_DOMAIN,
                    "owner_url": provision_server.url,
                    "covehub_server_url": server.url,
                    "owner_identity": provision_server.owner_identity,
                    "expected_plaintext_hash": sha256_literal(plaintext),
                    "staged_plaintext_path": str(tmp_path / "runtime" / "cove" / "inputs" / artifact_id / artifact_id),
                    "metadata_path": str(tmp_path / "runtime" / "cove" / "inputs" / artifact_id / "metadata.json"),
                }
            )
        except ContainerRuntimeErrorBase as exc:
            assert "ciphertext hash mismatch" in str(exc)
        else:  # pragma: no cover - defensive
            raise AssertionError("expected ciphertext hash mismatch")


def test_precondition_checker_passes_and_fails_on_demo_subset(tmp_path) -> None:
    metadata_path = tmp_path / "input.json"
    metadata_path.write_text(
        json.dumps({"plaintext_hash": "sha256:abc"}, indent=2) + "\n",
        encoding="utf-8",
    )

    _PRECONDITION_CHECKER.run(
        {
            "node_name": "demo",
            "service_name": "worker",
            "inputs": {"input_a": str(metadata_path)},
            "certificates": {
                "dependency_a": str(_write_certificate_file(tmp_path / "dependency_a.json", pass_value=True))
            },
            "preconditions": {
                "and": [
                    {
                        "==": [
                            {"var": "inputs.input_a.plaintext_hash"},
                            "sha256:abc",
                        ]
                    },
                    {
                        "==": [
                            {
                                "var": "certificates.dependency_a.certificate_body.results.worker.pass"
                            },
                            True,
                        ]
                    },
                ]
            },
        }
    )

    try:
        _PRECONDITION_CHECKER.run(
            {
                "node_name": "demo",
                "service_name": "worker",
                "inputs": {"input_a": str(metadata_path)},
                "certificates": {
                    "dependency_a": str(_write_certificate_file(tmp_path / "dependency_b.json", pass_value=False))
                },
                "preconditions": {
                    "and": [
                        {
                            "==": [
                                {"var": "inputs.input_a.plaintext_hash"},
                                "sha256:abc",
                            ]
                        },
                        {
                            "==": [
                                {
                                    "var": "certificates.dependency_a.certificate_body.results.worker.pass"
                                },
                                True,
                            ]
                        },
                    ]
                },
            }
        )
    except ContainerRuntimeErrorBase as exc:
        assert "preconditions failed" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected precondition failure")


def test_provision_server_serves_public_key_identity_document(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".alice_cove"
    provision_paths = provision_paths_for_home(cove_home)
    state = ProvisionState(provision_paths.database_path)
    state.initialize()

    with _running_provision_server(
        state=state,
        keys_dir=provision_paths.keys_dir,
        owner_domain=ALICE_DOMAIN,
        owner_url=ALICE_OWNER_URL,
    ) as provision_server:
        port = urllib_parse.urlparse(provision_server.url).port
        assert port is not None
        original_getaddrinfo = socket.getaddrinfo

        def fake_getaddrinfo(host, port_arg, family=0, type=0, proto=0, flags=0):
            if host in {ALICE_DOMAIN, "wrong.example.test"}:
                return original_getaddrinfo(
                    "127.0.0.1",
                    port_arg,
                    family,
                    type,
                    proto,
                    flags,
                )
            return original_getaddrinfo(host, port_arg, family, type, proto, flags)

        monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
        with urllib_request.urlopen(
            f"http://{ALICE_DOMAIN}:{port}/identity",
            timeout=5,
        ) as response:
            identity_document = json.loads(response.read().decode("utf-8"))

        verified = verify_owner_identity_document(
            identity_document,
            expected_owner_url=ALICE_OWNER_URL,
            expected_owner_domain=ALICE_DOMAIN,
        )
        assert verified["owner_domain"] == ALICE_DOMAIN
        assert verified["owner_url"] == ALICE_OWNER_URL
        assert "provisioning_tls_certificate_pem" not in verified
        assert "provisioning_tls_certificate_sha256" not in verified


def test_service_certificate_writer_supports_default_empty_result_and_custom_schema(
    tmp_path,
) -> None:
    default_result_path = tmp_path / "default.json"
    _SERVICE_CERTIFICATE_WRITER.run(
        {
            "node_name": "demo",
            "service_name": "worker",
            "result_output_path": str(default_result_path),
        }
    )
    assert json.loads(default_result_path.read_text(encoding="utf-8")) == {}

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": ["pass"],
        "properties": {"pass": {"type": "boolean"}},
        "additionalProperties": False,
    }
    source_path = tmp_path / "result.json"
    source_path.write_text(json.dumps({"pass": True}) + "\n", encoding="utf-8")
    output_path = tmp_path / "written.json"
    _SERVICE_CERTIFICATE_WRITER.run(
        {
            "node_name": "demo",
            "service_name": "worker",
            "result_source_path": str(source_path),
            "result_output_path": str(output_path),
            "schema": schema,
        }
    )
    assert json.loads(output_path.read_text(encoding="utf-8")) == {"pass": True}


def test_key_manager_generates_expected_files(tmp_path) -> None:
    private_key_path = tmp_path / "runtime" / "cove" / "ephemeral_keypairs" / "session_key" / "private.pem"
    public_key_path = tmp_path / "runtime" / "cove" / "ephemeral_keypairs" / "session_key" / "public.pem"
    certificate_path = tmp_path / "runtime" / "cove" / "ephemeral_keypairs" / "session_key" / "certificate.pem"
    metadata_path = tmp_path / "runtime" / "cove" / "ephemeral_keypairs" / "session_key" / "metadata.json"

    _KEY_MANAGER.run(
        {
            "keypairs": [
                {
                    "name": "session_key",
                    "algorithm": "ed25519",
                    "private_key_path": str(private_key_path),
                    "public_key_path": str(public_key_path),
                    "certificate_path": str(certificate_path),
                    "metadata_path": str(metadata_path),
                    "certificate_common_name": "hello_world.final_server.session_key",
                }
            ]
        }
    )

    assert "BEGIN PRIVATE KEY" in private_key_path.read_text(encoding="utf-8")
    assert "BEGIN PUBLIC KEY" in public_key_path.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["name"] == "session_key"
    assert metadata["algorithm"] == "ed25519"


def test_node_certificate_writer_binds_mock_quote_to_body_hash() -> None:
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name="node_one",
        generated_node_compose_hash="sha256:1234",
        inputs={"alice_secret_word": {"plaintext_hash": "sha256:abc"}},
        ephemeral_keypairs={"session_key": {"public_key_hash": "sha256:def"}},
        results={"worker": {"pass": True}},
    )

    assert certificate["attestation_bundle"]["format"] == "mock_tdx_v1"
    assert (
        certificate["attestation_bundle"]["quoted_certificate_body_hash"]
        == certificate["certificate_body_hash"]
    )
    assert certificate["attestation_bundle"]["quote"] == (
        "mock-tdx-quote:"
        f"{sha256_literal(bytes.fromhex(certificate['attestation_bundle']['report_data']))}"
    )


def test_node_certificate_writer_writes_local_certificate_and_uploads_to_covehub(
    tmp_path,
) -> None:
    certificate_path = tmp_path / "runtime" / "cove" / "certificates" / "final_server" / "certificate.json"
    input_metadata_path = tmp_path / "runtime" / "cove" / "inputs" / "alice_secret_word" / "metadata.json"
    output_metadata_path = tmp_path / "runtime" / "cove" / "outputs" / "alice_secret_word_transformed" / "metadata.json"
    key_metadata_path = tmp_path / "runtime" / "cove" / "ephemeral_keypairs" / "session_key" / "metadata.json"
    service_result_path = tmp_path / "runtime" / "cove" / "certificates" / "final_server" / "worker" / "result.json"
    input_metadata_path.parent.mkdir(parents=True, exist_ok=True)
    output_metadata_path.parent.mkdir(parents=True, exist_ok=True)
    key_metadata_path.parent.mkdir(parents=True, exist_ok=True)
    service_result_path.parent.mkdir(parents=True, exist_ok=True)
    input_metadata_path.write_text(
        json.dumps({"plaintext_hash": "sha256:abc"}, indent=2) + "\n",
        encoding="utf-8",
    )
    output_metadata_path.write_text(
        json.dumps(
            {
                "artifact_name": "alice_secret_word_transformed",
                "owner_domain": ALICE_DOMAIN,
                "hub_path": f"v1/runtime/{ALICE_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest",
                "plaintext_hash": "sha256:ghi",
                "ciphertext_hash": "sha256:jkl",
                "content_type": "text/plain",
                "encryption_algorithm": "aes-256-gcm",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    key_metadata_path.write_text(
        json.dumps({"public_key_hash": "sha256:def"}, indent=2) + "\n",
        encoding="utf-8",
    )
    service_result_path.write_text(
        json.dumps({"pass": True}, indent=2) + "\n",
        encoding="utf-8",
    )
    compose_hash = "sha256:" + "1" * 64

    with MockCovehubServer() as server:
        _NODE_CERTIFICATE_WRITER.run(
            {
                "covehub_server_url": server.url,
                "workflow_publisher_domain": ALICE_DOMAIN,
                "workflow_id": "hello_world",
                "node_name": "final_server",
                "certificate_path": str(certificate_path),
                "inputs": [{"name": "alice_secret_word", "path": str(input_metadata_path)}],
                "outputs": [
                    {
                        "name": "alice_secret_word_transformed",
                        "path": str(output_metadata_path),
                    }
                ],
                "ephemeral_keypairs": [{"name": "session_key", "path": str(key_metadata_path)}],
                "results": [{"name": "worker", "path": str(service_result_path)}],
            },
            compose_hash=compose_hash,
        )

        uploaded = server.state.runtime_certificates[
            f"v1/runtime/{ALICE_DOMAIN}/hello_world/certificates/final_server/latest"
        ]

    written_certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    uploaded_certificate = json.loads(uploaded.decode("utf-8"))
    assert written_certificate["certificate_body"]["node_id"] == "final_server"
    assert written_certificate["certificate_body"]["generated_node_compose_hash"] == compose_hash
    assert written_certificate["certificate_body"]["outputs"] == {
        "alice_secret_word_transformed": {
            "artifact_name": "alice_secret_word_transformed",
            "owner_domain": ALICE_DOMAIN,
            "hub_path": f"v1/runtime/{ALICE_DOMAIN}/hello_world/artifacts/alice_secret_word_transformed/latest",
            "plaintext_hash": "sha256:ghi",
            "ciphertext_hash": "sha256:jkl",
            "content_type": "text/plain",
            "encryption_algorithm": "aes-256-gcm",
        }
    }
    assert uploaded_certificate == written_certificate


def test_dependency_certificate_fetcher_downloads_and_verifies_runtime_certificates(
    tmp_path,
) -> None:
    certificate_path = tmp_path / "runtime" / "cove" / "certificates" / "alice_character_set_checker" / "certificate.json"
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name="alice_character_set_checker",
        generated_node_compose_hash="sha256:" + "3" * 64,
        inputs={},
        ephemeral_keypairs={},
        results={"character_set_checker": {"pass": True}},
    )

    with MockCovehubServer() as server:
        server.seed_runtime_certificate(
            f"v1/runtime/{ALICE_DOMAIN}/hello_world/certificates/alice_character_set_checker/latest",
            payload=json.dumps(certificate, indent=2).encode("utf-8"),
        )

        _DEPENDENCY_CERTIFICATE_FETCHER.run(
            {
                "node_name": "word_length_checker",
                "covehub_server_url": server.url,
                "workflow_publisher_domain": ALICE_DOMAIN,
                "workflow_id": "hello_world",
                "timeout_seconds": 2.0,
                "poll_interval_seconds": 0.05,
                "dependencies": [
                    {
                        "node_name": "alice_character_set_checker",
                        "certificate_path": str(certificate_path),
                        "expected_workflow_id": "hello_world",
                        "expected_node_id": "alice_character_set_checker",
                        "expected_generated_node_compose_hash": "sha256:" + "3" * 64,
                    }
                ],
            }
        )

    written_certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    assert written_certificate["certificate_body"]["node_id"] == "alice_character_set_checker"


def test_dependency_certificate_fetcher_rejects_invalid_runtime_certificate(
    tmp_path,
    capsys,
) -> None:
    certificate_path = tmp_path / "runtime" / "cove" / "certificates" / "alice_character_set_checker" / "certificate.json"
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name="alice_character_set_checker",
        generated_node_compose_hash="sha256:" + "3" * 64,
        inputs={},
        ephemeral_keypairs={},
        results={"character_set_checker": {"pass": True}},
    )
    certificate["attestation_bundle"]["quote"] = "mock-tdx-quote:wrong"

    with MockCovehubServer() as server:
        server.seed_runtime_certificate(
            f"v1/runtime/{ALICE_DOMAIN}/hello_world/certificates/alice_character_set_checker/latest",
            payload=json.dumps(certificate, indent=2).encode("utf-8"),
        )

        try:
            _DEPENDENCY_CERTIFICATE_FETCHER.run(
                {
                    "node_name": "word_length_checker",
                    "covehub_server_url": server.url,
                    "workflow_publisher_domain": ALICE_DOMAIN,
                    "workflow_id": "hello_world",
                    "timeout_seconds": 2.0,
                    "poll_interval_seconds": 0.05,
                    "dependencies": [
                        {
                            "node_name": "alice_character_set_checker",
                            "certificate_path": str(certificate_path),
                            "expected_workflow_id": "hello_world",
                            "expected_node_id": "alice_character_set_checker",
                            "expected_generated_node_compose_hash": "sha256:" + "3" * 64,
                        }
                    ],
                }
            )
        except ContainerRuntimeErrorBase as exc:
            assert "timed out waiting for runtime certificate" in str(exc)
            captured = capsys.readouterr()
            assert "mock quote does not match" in captured.out
        else:  # pragma: no cover - defensive
            raise AssertionError("expected invalid dependency certificate failure")


def test_dependency_certificate_fetcher_rejects_mismatched_compose_hash(
    tmp_path,
    capsys,
) -> None:
    certificate_path = tmp_path / "runtime" / "cove" / "certificates" / "alice_character_set_checker" / "certificate.json"
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name="alice_character_set_checker",
        generated_node_compose_hash="sha256:" + "3" * 64,
        inputs={},
        ephemeral_keypairs={},
        results={"character_set_checker": {"pass": True}},
    )

    with MockCovehubServer() as server:
        server.seed_runtime_certificate(
            f"v1/runtime/{ALICE_DOMAIN}/hello_world/certificates/alice_character_set_checker/latest",
            payload=json.dumps(certificate, indent=2).encode("utf-8"),
        )

        try:
            _DEPENDENCY_CERTIFICATE_FETCHER.run(
                {
                    "node_name": "word_length_checker",
                    "covehub_server_url": server.url,
                    "workflow_publisher_domain": ALICE_DOMAIN,
                    "workflow_id": "hello_world",
                    "timeout_seconds": 2.0,
                    "poll_interval_seconds": 0.05,
                    "dependencies": [
                        {
                            "node_name": "alice_character_set_checker",
                            "certificate_path": str(certificate_path),
                            "expected_workflow_id": "hello_world",
                            "expected_node_id": "alice_character_set_checker",
                            "expected_generated_node_compose_hash": "sha256:" + "4" * 64,
                        }
                    ],
                }
            )
        except ContainerRuntimeErrorBase as exc:
            assert "timed out waiting for runtime certificate" in str(exc)
            captured = capsys.readouterr()
            assert "expected generated compose hash" in captured.out
        else:  # pragma: no cover - defensive
            raise AssertionError("expected mismatched compose hash failure")


def test_dependency_certificate_fetcher_retries_transient_fetch_errors(
    tmp_path,
    monkeypatch,
) -> None:
    certificate_path = tmp_path / "runtime" / "cove" / "certificates" / "alice_character_set_checker" / "certificate.json"
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name="alice_character_set_checker",
        generated_node_compose_hash="sha256:" + "3" * 64,
        inputs={},
        ephemeral_keypairs={},
        results={"character_set_checker": {"pass": True}},
    )
    calls = {"count": 0}

    def flaky_http_get_json(*, url: str, cafile=None, timeout: float = 60.0):
        del cafile, timeout
        calls["count"] += 1
        if calls["count"] == 1:
            raise ContainerRuntimeErrorBase(f"failed to reach {url}: temporary failure")
        return certificate

    monkeypatch.setattr(_DEPENDENCY_CERTIFICATE_FETCHER, "http_get_json", flaky_http_get_json)

    _DEPENDENCY_CERTIFICATE_FETCHER.run(
        {
            "node_name": "word_length_checker",
            "covehub_server_url": "https://example.invalid",
            "workflow_publisher_domain": ALICE_DOMAIN,
            "workflow_id": "hello_world",
            "timeout_seconds": 2.0,
            "poll_interval_seconds": 0.01,
            "dependencies": [
                {
                    "node_name": "alice_character_set_checker",
                    "certificate_path": str(certificate_path),
                    "expected_workflow_id": "hello_world",
                    "expected_node_id": "alice_character_set_checker",
                    "expected_generated_node_compose_hash": "sha256:" + "3" * 64,
                }
            ],
        }
    )

    written_certificate = json.loads(certificate_path.read_text(encoding="utf-8"))
    assert written_certificate["certificate_body"]["node_id"] == "alice_character_set_checker"
    assert calls["count"] >= 2


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


def _write_local_config(
    root: Path,
    server_url: str,
) -> Path:
    cove_home = root / ".cove"
    config_path = cove_home / "config.yaml"
    cove_home.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        "\n".join(
            [
                f"covehub_server_url: {server_url}",
                f"owner_server_url: {PUBLISHER_OWNER_URL}",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return cove_home


def _compile_hello_world_workflow(tmp_path: Path) -> Path:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = _write_local_config(tmp_path, "http://127.0.0.1:8000")
    compile_workflow(workflow_dir / "workflow.cove.yaml", cove_home=cove_home)
    return workflow_dir


def _inline_service_config(services: dict[str, object], service_name: str) -> dict[str, object]:
    service = services[service_name]
    assert isinstance(service, dict)
    environment = service["environment"]
    assert isinstance(environment, dict)
    return json.loads(environment["COVE_CONFIG_JSON"])


def _has_bind_mount(service: dict[str, object], target: str) -> bool:
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        return False
    return any(
        isinstance(volume, dict) and volume.get("target") == target
        for volume in volumes
    )


def _write_owner_cert(root: Path, owner_name: str) -> None:
    cert_path = root / "certs" / f"{owner_name}.pem"
    write_test_certificate(
        cert_path,
        dns_names=["example.test"],
    )


def _write_schema_file(root: Path) -> None:
    schema_path = root / "schemas" / "result.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        '{\n  "$schema": "https://json-schema.org/draft/2020-12/schema",\n'
        '  "type": "object"\n}\n',
        encoding="utf-8",
    )


def _write_certificate_file(path: Path, *, pass_value: bool) -> Path:
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name=path.stem,
        generated_node_compose_hash="sha256:" + "9" * 64,
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": pass_value}},
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(certificate, indent=2) + "\n", encoding="utf-8")
    return path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _running_provision_server:
    def __init__(
        self,
        *,
        state: ProvisionState,
        keys_dir: Path,
        owner_domain: str = LOCAL_OWNER_DOMAIN,
        port: int = 0,
        owner_url: str | None = None,
    ) -> None:
        self.state = state
        self.keys_dir = keys_dir
        self.owner_domain = owner_domain
        if owner_url is None and port == 0:
            port = _free_port()
        self.port = port
        self.owner_url = owner_url or f"http://127.0.0.1:{port}"
        self.server = None
        self.thread = None
        self.url = ""
        self.owner_identity: dict[str, object] | None = None

    def __enter__(self):
        self.server = create_provision_server(
            state=self.state,
            keys_dir=self.keys_dir,
            host="127.0.0.1",
            port=self.port,
            owner_domain=self.owner_domain,
            owner_url=self.owner_url,
        )
        self.owner_identity = self.server.owner_identity
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address
        self.url = f"http://{host}:{port}"
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        assert self.server is not None
        assert self.thread is not None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
