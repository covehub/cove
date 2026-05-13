from __future__ import annotations

import json
import base64
from pathlib import Path

from fastapi.testclient import TestClient

from covehub_ui.app import create_app
from covehub_ui.indexer import CoveHubIndexer, UiSettings, sha256_literal


def test_indexer_scans_typed_objects_and_ignores_invalid_paths(tmp_path: Path) -> None:
    data = tmp_path / "data"
    _write(data / "artifacts" / "alice" / "model_weights" / "latest", b"artifact")
    digest = sha256_literal(b"workflow")
    _write(data / "workflows" / "alice" / "hello_world" / digest, b"workflow")
    _write(data / "runtime" / "alice" / "hello_world" / "certificates" / "node_a" / "latest", _certificate())
    _write(data / "runtime" / "alice" / "hello_world" / "artifacts" / "output" / "latest", b"ciphertext")
    _write(data / "bad" / "shape", b"ignore")

    indexer = CoveHubIndexer(UiSettings(data_root=data, cache_ttl_seconds=0))
    summary = indexer.summary()
    objects = indexer.list_objects()["objects"]

    assert summary["object_count"] == 4
    assert summary["owners"] == ["alice"]
    assert summary["publishers"] == ["alice"]
    assert {entry["kind"] for entry in objects} == {
        "static_artifact",
        "workflow",
        "runtime_certificate",
        "runtime_artifact",
    }


def test_object_detail_bounds_preview_and_summarizes_certificate(tmp_path: Path) -> None:
    data = tmp_path / "data"
    payload = _certificate(extra_result="x" * 1000)
    _write(data / "runtime" / "alice" / "demo" / "certificates" / "node_a" / "latest", payload)

    app = create_app(UiSettings(data_root=data, cache_ttl_seconds=0, preview_bytes=120))
    with TestClient(app) as client:
        response = client.get("/ui-api/objects/v1/runtime/alice/demo/certificates/node_a/latest")

    assert response.status_code == 200
    body = response.json()
    assert body["is_latest"] is True
    assert body["observed_exact_hub_path"].startswith(
        "v1/runtime/alice/demo/certificates/node_a/sha256:"
    )
    assert body["summary"]["format"] == "cove.runtime.certificate"
    assert body["preview"]["truncated"] is True
    assert "cove hub inspect" in body["cli"]["inspect"]


def test_workflow_detail_summarizes_bundle_without_serving_raw_bytes(tmp_path: Path) -> None:
    data = tmp_path / "data"
    payload = {
        "format": "cove.workflow.bundle.v1",
        "manifest": {
            "publisher": "alice",
            "workflow_id": "hello_world",
            "manifest_hash": "sha256:" + "1" * 64,
            "files": [],
            "nodes": [
                {
                    "node_id": "node_a",
                    "compose_hash": "sha256:" + "2" * 64,
                    "artifacts": [],
                }
            ],
        },
        "files": [],
    }
    encoded = json.dumps(payload).encode("utf-8")
    _write(data / "workflows" / "alice" / "hello_world" / sha256_literal(encoded), encoded)

    app = create_app(UiSettings(data_root=data, cache_ttl_seconds=0))
    with TestClient(app) as client:
        response = client.get(
            f"/ui-api/objects/v1/workflows/alice/hello_world/{sha256_literal(encoded)}"
        )
        raw_response = client.get(
            f"/v1/workflows/alice/hello_world/{sha256_literal(encoded)}"
        )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["format"] == "cove.workflow.bundle.v1"
    assert body["summary"]["node_count"] == 1
    assert raw_response.status_code == 404


def test_workflow_detail_exposes_structured_files_and_artifact_relationships(tmp_path: Path) -> None:
    data = tmp_path / "data"
    workflow_text = """
workflow:
  id: demo
artifacts:
  model:
    type: static
    owner: alice
    hub_path: v1/artifacts/alice/model/latest
nodes:
  node_a:
    services:
      worker:
        inputs:
          /workspace/model.bin: model
""".strip()
    compose_text = """
services:
  worker:
    image: worker@sha256:abc
""".strip()
    payload = {
        "format": "cove.workflow.bundle.v1",
        "manifest": {
            "publisher": "alice",
            "workflow_id": "demo",
            "manifest_hash": "sha256:" + "1" * 64,
            "files": [
                {"path": "workflow.normalized.cove.yaml", "sha256": sha256_literal(workflow_text.encode())},
                {"path": "nodes/node_a.compose.yaml", "sha256": sha256_literal(compose_text.encode())},
            ],
            "nodes": [
                {
                    "node_id": "node_a",
                    "compose_path": "nodes/node_a.compose.yaml",
                    "compose_hash": "sha256:" + "2" * 64,
                    "artifacts": [
                        {
                            "name": "model",
                            "type": "static",
                            "direction": "input",
                            "hub_path": "v1/artifacts/alice/model/latest",
                            "owner": "alice",
                        }
                    ],
                    "runtime_skeleton": [],
                }
            ],
        },
        "files": [
            _bundle_file("workflow.normalized.cove.yaml", workflow_text.encode()),
            _bundle_file("nodes/node_a.compose.yaml", compose_text.encode()),
        ],
    }
    encoded = json.dumps(payload).encode("utf-8")
    _write(data / "workflows" / "alice" / "demo" / "latest", encoded)
    _write(data / "artifacts" / "alice" / "model" / "latest", b"artifact")

    app = create_app(UiSettings(data_root=data, cache_ttl_seconds=0))
    with TestClient(app) as client:
        workflow_response = client.get("/ui-api/objects/v1/workflows/alice/demo/latest")
        artifact_response = client.get("/ui-api/objects/v1/artifacts/alice/model/latest")

    assert workflow_response.status_code == 200
    workflow_body = workflow_response.json()
    assert workflow_body["summary"]["workflow_definition"]["parsed"]["nodes"]["node_a"]
    assert workflow_body["summary"]["compose_files"][0]["services"] == ["worker"]
    assert workflow_body["summary"]["diagram"]["nodes"][0]["inputs"][0]["artifact"] == "model"

    assert artifact_response.status_code == 200
    artifact_body = artifact_response.json()
    assert artifact_body["relationships"]["used_by"][0]["node_id"] == "node_a"


def test_runtime_artifact_owner_is_enriched_from_certificate_output_metadata(tmp_path: Path) -> None:
    data = tmp_path / "data"
    ciphertext = b"bob-ciphertext"
    digest = sha256_literal(ciphertext)
    latest_hub_path = "v1/runtime/alice/hello_world/artifacts/bob_secret_word_transformed/latest"
    exact_hub_path = f"v1/runtime/alice/hello_world/artifacts/bob_secret_word_transformed/{digest}"
    output_metadata = {
        "artifact_name": "bob_secret_word_transformed",
        "owner": "bob",
        "hub_path": exact_hub_path,
        "channel_hub_path": latest_hub_path,
        "plaintext_hash": "sha256:" + "4" * 64,
        "ciphertext_hash": digest,
        "content_type": "text/plain",
        "encryption_algorithm": "aes-256-gcm",
    }
    _write(data / "runtime" / "alice" / "hello_world" / "artifacts" / "bob_secret_word_transformed" / "latest", ciphertext)
    _write(data / "runtime" / "alice" / "hello_world" / "artifacts" / "bob_secret_word_transformed" / digest, ciphertext)
    _write(
        data / "runtime" / "alice" / "hello_world" / "certificates" / "bob_word_length_checker" / "latest",
        _certificate(
            workflow_id="hello_world",
            node_id="bob_word_length_checker",
            outputs={"bob_secret_word_transformed": output_metadata},
        ),
    )

    indexer = CoveHubIndexer(UiSettings(data_root=data, cache_ttl_seconds=0))
    query_objects = indexer.list_objects(query="bob", limit=1000)["objects"]
    runtime_objects = [
        entry for entry in query_objects if entry["kind"] == "runtime_artifact"
    ]

    assert {entry["hub_path"] for entry in runtime_objects} == {latest_hub_path, exact_hub_path}
    assert {entry["owner"] for entry in runtime_objects} == {"bob"}
    assert indexer.object_detail(latest_hub_path)["owner"] == "bob"
    assert indexer.summary()["owners"] == ["bob"]


def test_runtime_artifact_owner_falls_back_to_workflow_bundle_metadata(tmp_path: Path) -> None:
    data = tmp_path / "data"
    ciphertext = b"bob-ciphertext"
    runtime_hub_path = "v1/runtime/alice/hello_world/artifacts/bob_secret_word_transformed/latest"
    workflow_payload = {
        "format": "cove.workflow.bundle.v1",
        "manifest": {
            "publisher": "alice",
            "workflow_id": "hello_world",
            "manifest_hash": "sha256:" + "1" * 64,
            "files": [],
            "nodes": [
                {
                    "node_id": "bob_word_length_checker",
                    "compose_hash": "sha256:" + "2" * 64,
                    "artifacts": [
                        {
                            "name": "bob_secret_word_transformed",
                            "type": "dynamic",
                            "direction": "output",
                            "hub_path": runtime_hub_path,
                            "owner": "bob",
                        }
                    ],
                    "runtime_skeleton": [],
                }
            ],
        },
        "files": [],
    }
    _write(data / "runtime" / "alice" / "hello_world" / "artifacts" / "bob_secret_word_transformed" / "latest", ciphertext)
    _write(data / "workflows" / "alice" / "hello_world" / "latest", json.dumps(workflow_payload).encode("utf-8"))

    indexer = CoveHubIndexer(UiSettings(data_root=data, cache_ttl_seconds=0))

    assert indexer.object_detail(runtime_hub_path)["owner"] == "bob"
    assert indexer.list_objects(query="bob")["objects"][0]["owner"] == "bob"


def test_invalid_and_missing_object_paths_return_errors(tmp_path: Path) -> None:
    app = create_app(UiSettings(data_root=tmp_path / "data", cache_ttl_seconds=0))
    with TestClient(app) as client:
        unsafe = client.get("/ui-api/objects/v1/../covehub.sqlite3")
        missing = client.get("/ui-api/objects/v1/artifacts/alice/missing/latest")

    assert unsafe.status_code == 400
    assert missing.status_code == 404


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def _bundle_file(path: str, payload: bytes) -> dict[str, str]:
    return {
        "path": path,
        "sha256": sha256_literal(payload),
        "content_b64": base64.b64encode(payload).decode("ascii"),
    }


def _certificate(
    *,
    extra_result: str = "ok",
    workflow_id: str = "demo",
    node_id: str = "node_a",
    outputs: dict[str, object] | None = None,
) -> bytes:
    payload = {
        "certificate_body": {
            "workflow_id": workflow_id,
            "node_id": node_id,
            "generated_node_compose_hash": "sha256:" + "2" * 64,
            "inputs": {},
            "ephemeral_keypairs": {},
            "results": {"worker": {"value": extra_result}},
            "outputs": outputs or {},
        },
        "certificate_body_hash": "sha256:" + "3" * 64,
        "attestation_bundle": {
            "format": "phala_dstack_v1",
            "quote": "quote",
            "report_data": "00",
            "quoted_certificate_body_hash": "sha256:" + "3" * 64,
            "generated_node_compose_hash": "sha256:" + "2" * 64,
            "node_id": node_id,
        },
    }
    return json.dumps(payload).encode("utf-8")
