from __future__ import annotations

import hashlib
import json
from pathlib import Path

import yaml

from cove_cli.cli import run
from cove_cli.config import config_path_for_home
from cove_cli.covehub import upload_named_artifact, upload_workflow_object
from cove_cli.provisioning_identity import build_owner_identity_document, ensure_owner_signing_key_material

from .support import MockCovehubServer


ALICE_DOMAIN = "alice.example.test"


def test_hub_get_downloads_exact_object_and_checks_digest(tmp_path, capsys) -> None:
    payload = b"artifact bytes"
    digest = _sha256_literal(payload)
    output_path = tmp_path / "artifact.bin"

    with MockCovehubServer() as server:
        server.seed_artifact(f"v1/artifacts/{ALICE_DOMAIN}/model/{digest}", payload=payload)

        exit_code = run(
            [
                "hub",
                "get",
                f"v1/artifacts/{ALICE_DOMAIN}/model/{digest}",
                "--server-url",
                server.url,
                "--output",
                str(output_path),
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output_path.read_bytes() == payload
    assert f"Observed digest: {digest}" in output


def test_hub_get_runtime_latest_reports_observed_exact_path(tmp_path, capsys) -> None:
    payload = b"latest bytes"
    digest = _sha256_literal(payload)
    cove_home = tmp_path / ".cove"
    output_path = tmp_path / "latest.bin"

    with MockCovehubServer() as server:
        server.seed_runtime_artifact(f"v1/runtime/{ALICE_DOMAIN}/demo/artifacts/model/latest", payload=payload)
        _write_config(cove_home, {"covehub_server_url": server.url})

        exit_code = run(
            [
                "--cove-home",
                str(cove_home),
                "hub",
                "get",
                f"v1/runtime/{ALICE_DOMAIN}/demo/artifacts/model/latest",
                "--output",
                str(output_path),
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert output_path.read_bytes() == payload
    assert "WARNING: 'latest' is a mutable convenience alias" in output
    assert f"Observed exact path: v1/runtime/{ALICE_DOMAIN}/demo/artifacts/model/{digest}" in output


def test_hub_inspect_summarizes_workflow_bundle(capsys) -> None:
    payload = {
        "format": "cove.workflow.bundle.v1",
        "manifest": {
            "publisher": ALICE_DOMAIN,
            "workflow_id": "hello_world",
            "manifest_hash": "sha256:" + "1" * 64,
            "files": [],
            "nodes": [
                {
                    "node_id": "node_a",
                    "compose_hash": "sha256:" + "2" * 64,
                }
            ],
        },
        "files": [],
    }
    encoded = json.dumps(payload).encode("utf-8")
    digest = _sha256_literal(encoded)

    with MockCovehubServer() as server:
        server.state.workflow_bundles[f"v1/workflows/{ALICE_DOMAIN}/hello_world/{digest}"] = encoded

        exit_code = run(
            [
                "hub",
                "inspect",
                f"v1/workflows/{ALICE_DOMAIN}/hello_world/{digest}",
                "--server-url",
                server.url,
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Type: workflow" in output
    assert "Workflow bundle:" in output
    assert "Workflow id: hello_world" in output
    assert "node_a" in output
    assert "Independent verification:" in output


def test_hub_inspect_summarizes_runtime_certificate(capsys) -> None:
    certificate = {
        "certificate_body": {
            "workflow_id": "demo",
            "node_id": "final_server",
            "generated_node_compose_hash": "sha256:" + "2" * 64,
            "inputs": {},
            "outputs": {},
            "results": {"service": {"pass": True}},
            "ephemeral_keypairs": {},
        },
        "certificate_body_hash": "sha256:" + "3" * 64,
        "attestation_bundle": {"format": "phala_dstack_v1"},
    }
    encoded = json.dumps(certificate).encode("utf-8")

    with MockCovehubServer() as server:
        server.seed_runtime_certificate(
            f"v1/runtime/{ALICE_DOMAIN}/demo/certificates/final_server/latest",
            payload=encoded,
        )

        exit_code = run(
            [
                "hub",
                "inspect",
                f"v1/runtime/{ALICE_DOMAIN}/demo/certificates/final_server/latest",
                "--server-url",
                server.url,
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Type: runtime certificate" in output
    assert "Node id: final_server" in output
    assert "Attestation format: phala_dstack_v1" in output
    assert "WARNING: 'latest' is a mutable convenience alias" in output


def test_chunked_artifact_and_workflow_uploads_use_session_api(tmp_path, monkeypatch) -> None:
    owner_url = f"https://{ALICE_DOMAIN}"
    private_key_path = tmp_path / "owner-signing-private.pem"
    public_key_path = tmp_path / "owner-signing-public.pem"
    ensure_owner_signing_key_material(
        private_key_path=private_key_path,
        public_key_path=public_key_path,
    )
    identity = build_owner_identity_document(
        owner_url=owner_url,
        owner_private_key_path=private_key_path,
        owner_public_key_path=public_key_path,
    )
    monkeypatch.setattr("cove_cli.covehub._CHUNKED_UPLOAD_THRESHOLD_BYTES", 8)
    monkeypatch.setattr("cove_cli.covehub._CHUNKED_UPLOAD_CHUNK_SIZE_BYTES", 3)

    artifact_payload = b"artifact uploaded in chunks"
    workflow_payload = b'{"workflow":"chunked"}'

    with MockCovehubServer() as server:
        artifact_result = upload_named_artifact(
            server_url=server.url,
            owner_domain=ALICE_DOMAIN,
            artifact_name="model",
            owner_identity=identity,
            owner_private_key_path=private_key_path,
            payload=artifact_payload,
        )
        workflow_result = upload_workflow_object(
            server_url=server.url,
            publisher=ALICE_DOMAIN,
            workflow_id="hello_world",
            owner_identity=identity,
            owner_private_key_path=private_key_path,
            payload=workflow_payload,
        )

    artifact_digest = _sha256_literal(artifact_payload)
    workflow_digest = _sha256_literal(workflow_payload)
    assert artifact_result.status_code == 201
    assert workflow_result.status_code == 201
    assert server.state.artifacts[f"v1/artifacts/{ALICE_DOMAIN}/model/{artifact_digest}"] == artifact_payload
    assert f"v1/artifacts/{ALICE_DOMAIN}/model/latest" not in server.state.artifacts
    assert server.state.workflow_bundles[f"v1/workflows/{ALICE_DOMAIN}/hello_world/{workflow_digest}"] == workflow_payload


def test_hub_get_rejects_digest_mismatch(capsys) -> None:
    payload = b"different bytes"
    wrong_digest = "sha256:" + "0" * 64

    with MockCovehubServer() as server:
        server.seed_artifact(f"v1/artifacts/{ALICE_DOMAIN}/model/{wrong_digest}", payload=payload)

        exit_code = run(
            [
                "hub",
                "get",
                f"v1/artifacts/{ALICE_DOMAIN}/model/{wrong_digest}",
                "--server-url",
                server.url,
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "downloaded payload digest mismatch" in output


def test_hub_get_rejects_static_latest_alias(capsys) -> None:
    with MockCovehubServer() as server:
        exit_code = run(
            [
                "hub",
                "get",
                f"v1/artifacts/{ALICE_DOMAIN}/model/latest",
                "--server-url",
                server.url,
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "static artifact hub paths must use an exact sha256" in output


def test_hub_inspect_reports_missing_object(capsys) -> None:
    digest = "sha256:" + "1" * 64
    with MockCovehubServer() as server:
        exit_code = run(
            [
                "hub",
                "inspect",
                f"v1/artifacts/{ALICE_DOMAIN}/missing/{digest}",
                "--server-url",
                server.url,
            ]
        )

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "request failed with HTTP 404" in output


def _write_config(cove_home: Path, payload: dict[str, object]) -> None:
    path = config_path_for_home(cove_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
