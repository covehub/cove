from __future__ import annotations

import hashlib
import json
import base64
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from cove_container_runtime.attestation import (
    PHALA_DSTACK_ATTESTATION_FORMAT,
    build_node_certificate_report_data,
    build_runtime_artifact_report_data,
)
from cove_container_runtime.common import canonical_json_bytes
from cove_container_runtime.owner_identity import (
    OWNER_IDENTITY_VERSION,
    owner_identity_signature_payload,
)
from cove_server.app import create_app
from cove_server.config import Settings
from cove_server.quote_verifier import QuoteVerificationError
from cove_server.write_auth import domain_write_signature_payload


ALICE_DOMAIN = "alice.example.test"


def test_healthz_reports_app_database_and_data_root(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.get("/healthz")

        assert response.status_code == 200
        assert response.json() == {
            "ok": True,
            "app": "ok",
            "data_root": "ok",
        }


def test_auth_routes_are_removed(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.post(
            "/auth/register",
            json={"username": "alice", "password": "wonderland"},
        ).status_code == 404
        assert client.post(
            "/auth/login",
            json={"username": "alice", "password": "wonderland"},
        ).status_code == 404
        assert client.get("/auth/me").status_code == 404


def test_tunnel_lease_route_is_removed(tmp_path) -> None:
    with _client(tmp_path) as client:
        response = client.post("/v1/tunnels/lease")

        assert response.status_code == 404


def test_static_artifact_exact_write_read_head_and_storage(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        identity, private_key = _owner_identity(ALICE_DOMAIN)
        _stub_current_owner_identity(monkeypatch, identity)
        payload = b"object-bytes"
        digest = _sha256_literal(payload)
        path = f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{digest}"

        missing_auth = client.put(path, content=payload)
        first_put = client.put(
            path,
            content=payload,
            headers=_write_headers(
                method="PUT",
                path=path,
                payload=payload,
                owner_domain=ALICE_DOMAIN,
                identity=identity,
                private_key=private_key,
            ),
        )
        second_put = client.put(
            path,
            content=payload,
            headers=_write_headers(
                method="PUT",
                path=path,
                payload=payload,
                owner_domain=ALICE_DOMAIN,
                identity=identity,
                private_key=private_key,
            ),
        )
        get_response = client.get(path)
        head_response = client.head(path)
        latest_response = client.get(f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/latest")

        assert missing_auth.status_code == 401
        assert first_put.status_code == 201
        assert second_put.status_code == 200
        assert get_response.status_code == 200
        assert get_response.content == payload
        assert latest_response.status_code == 404
        assert head_response.status_code == 200
        assert head_response.headers["content-length"] == str(len(payload))
        assert (
            tmp_path / "data" / "artifacts" / ALICE_DOMAIN / "model_weights" / digest
        ).read_bytes() == payload


def test_static_artifact_upload_session_appends_chunks_and_completes(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        identity, private_key = _owner_identity(ALICE_DOMAIN)
        _stub_current_owner_identity(monkeypatch, identity)
        payload = b"chunked-object-bytes"
        digest = _sha256_literal(payload)
        object_path = f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{digest}"
        session_path = f"{object_path}/upload-session"
        session_payload = json.dumps({"upload_length": len(payload)}).encode("utf-8")

        session_response = client.post(
            session_path,
            content=session_payload,
            headers=_write_headers(
                method="POST",
                path=session_path,
                payload=session_payload,
                owner_domain=ALICE_DOMAIN,
                identity=identity,
                private_key=private_key,
            ),
        )
        assert session_response.status_code == 201
        session = session_response.json()
        assert session["offset"] == 0
        assert session["upload_length"] == len(payload)
        session_id = session["session_id"]

        first_chunk = client.put(
            f"/v1/uploads/sessions/{session_id}",
            content=payload[:7],
            headers={"X-Cove-Upload-Offset": "0"},
        )
        assert first_chunk.status_code == 200
        assert first_chunk.json()["offset"] == 7

        status_response = client.get(f"/v1/uploads/sessions/{session_id}")
        assert status_response.status_code == 200
        assert status_response.json()["offset"] == 7

        second_chunk = client.put(
            f"/v1/uploads/sessions/{session_id}",
            content=payload[7:],
            headers={"X-Cove-Upload-Offset": "7"},
        )
        assert second_chunk.status_code == 200
        assert second_chunk.json()["offset"] == len(payload)

        complete_response = client.post(f"/v1/uploads/sessions/{session_id}/complete")
        assert complete_response.status_code == 201
        completed = complete_response.json()
        assert completed["completed"] is True
        assert completed["created"] is True

        assert client.get(object_path).content == payload
        assert client.get(f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/latest").status_code == 404


def test_static_artifact_upload_session_rejects_offset_conflicts_and_digest_mismatch(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        identity, private_key = _owner_identity(ALICE_DOMAIN)
        _stub_current_owner_identity(monkeypatch, identity)
        payload = b"abcdef"
        digest = _sha256_literal(payload)
        object_path = f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{digest}"
        session_path = f"{object_path}/upload-session"
        session_payload = json.dumps({"upload_length": len(payload)}).encode("utf-8")

        session_response = client.post(
            session_path,
            content=session_payload,
            headers=_write_headers(
                method="POST",
                path=session_path,
                payload=session_payload,
                owner_domain=ALICE_DOMAIN,
                identity=identity,
                private_key=private_key,
            ),
        )
        session_id = session_response.json()["session_id"]

        first_chunk = client.put(
            f"/v1/uploads/sessions/{session_id}",
            content=b"abc",
            headers={"X-Cove-Upload-Offset": "0"},
        )
        assert first_chunk.status_code == 200

        wrong_offset = client.put(
            f"/v1/uploads/sessions/{session_id}",
            content=b"def",
            headers={"X-Cove-Upload-Offset": "1"},
        )
        assert wrong_offset.status_code == 409

        complete_too_early = client.post(f"/v1/uploads/sessions/{session_id}/complete")
        assert complete_too_early.status_code == 409

        mismatch_digest = _sha256_literal(b"ghijkl")
        mismatch_object_path = f"/v1/artifacts/{ALICE_DOMAIN}/other_weights/{mismatch_digest}"
        mismatch_session_path = f"{mismatch_object_path}/upload-session"
        mismatch_payload = json.dumps({"upload_length": 6}).encode("utf-8")
        mismatch_response = client.post(
            mismatch_session_path,
            content=mismatch_payload,
            headers=_write_headers(
                method="POST",
                path=mismatch_session_path,
                payload=mismatch_payload,
                owner_domain=ALICE_DOMAIN,
                identity=identity,
                private_key=private_key,
            ),
        )
        mismatch_session_id = mismatch_response.json()["session_id"]
        client.put(
            f"/v1/uploads/sessions/{mismatch_session_id}",
            content=b"ghijkx",
            headers={"X-Cove-Upload-Offset": "0"},
        )
        mismatch_complete = client.post(f"/v1/uploads/sessions/{mismatch_session_id}/complete")
        assert mismatch_complete.status_code == 400
        assert mismatch_complete.json()["detail"] == "uploaded bytes sha256 does not match digest path"


def test_exact_write_rejects_digest_mismatch_and_bad_digest(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        identity, private_key = _owner_identity(ALICE_DOMAIN)
        _stub_current_owner_identity(monkeypatch, identity)
        payload = b"object-bytes"
        wrong_digest = _sha256_literal(b"different")
        mismatch_path = f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{wrong_digest}"
        uppercase_path = f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{_sha256_literal(payload).upper()}"

        mismatch_response = client.put(
            mismatch_path,
            content=payload,
            headers=_write_headers(
                method="PUT",
                path=mismatch_path,
                payload=payload,
                owner_domain=ALICE_DOMAIN,
                identity=identity,
                private_key=private_key,
            ),
        )
        uppercase_response = client.put(
            uppercase_path,
            content=payload,
            headers=_write_headers(
                method="PUT",
                path=uppercase_path,
                payload=payload,
                owner_domain=ALICE_DOMAIN,
                identity=identity,
                private_key=private_key,
            ),
        )

        assert mismatch_response.status_code == 400
        assert mismatch_response.json()["detail"] == "payload sha256 does not match digest path"
        assert uppercase_response.status_code == 400


def test_domain_write_rejects_identity_for_different_owner_domain(tmp_path) -> None:
    with _client(tmp_path) as client:
        identity, private_key = _owner_identity("bob.example.test")
        payload = b"object-bytes"
        path = f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{_sha256_literal(payload)}"

        response = client.put(
            path,
            content=payload,
            headers=_write_headers(
                method="PUT",
                path=path,
                payload=payload,
                owner_domain=ALICE_DOMAIN,
                identity=identity,
                private_key=private_key,
            ),
        )

        assert response.status_code == 401
        assert (
            "owner_domain 'bob.example.test' does not match expected"
            in response.json()["detail"]
        )


def test_domain_write_accepts_refreshed_identity_with_same_served_public_key(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        served_identity, private_key = _owner_identity(ALICE_DOMAIN)
        request_identity, _private_key = _owner_identity(
            ALICE_DOMAIN,
            private_key=private_key,
            not_before="2026-01-02T00:00:00Z",
        )
        _stub_current_owner_identity(monkeypatch, served_identity)
        payload = b"object-bytes"
        path = f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{_sha256_literal(payload)}"

        response = client.put(
            path,
            content=payload,
            headers=_write_headers(
                method="PUT",
                path=path,
                payload=payload,
                owner_domain=ALICE_DOMAIN,
                identity=request_identity,
                private_key=private_key,
            ),
        )

        assert response.status_code == 201


def test_domain_write_rejects_identity_with_different_served_public_key(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        request_identity, private_key = _owner_identity(ALICE_DOMAIN)
        served_identity, _served_private_key = _owner_identity(ALICE_DOMAIN)
        _stub_current_owner_identity(monkeypatch, served_identity)
        payload = b"object-bytes"
        path = f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{_sha256_literal(payload)}"

        response = client.put(
            path,
            content=payload,
            headers=_write_headers(
                method="PUT",
                path=path,
                payload=payload,
                owner_domain=ALICE_DOMAIN,
                identity=request_identity,
                private_key=private_key,
            ),
        )

        assert response.status_code == 401
        assert (
            response.json()["detail"]
            == "owner identity public key does not match the current served identity"
        )


def test_missing_typed_object_returns_404_for_get_and_head(tmp_path) -> None:
    with _client(tmp_path) as client:
        digest = "sha256:" + "a" * 64

        assert client.get(f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{digest}").status_code == 404
        assert client.head(f"/v1/artifacts/{ALICE_DOMAIN}/model_weights/{digest}").status_code == 404


def test_workflow_exact_and_latest_routes_store_typed_object(tmp_path, monkeypatch) -> None:
    with _client(tmp_path) as client:
        identity, private_key = _owner_identity(ALICE_DOMAIN)
        _stub_current_owner_identity(monkeypatch, identity)
        workflow_payload = b"workflow-bundle"
        workflow_digest = _sha256_literal(workflow_payload)
        path = f"/v1/workflows/{ALICE_DOMAIN}/hello_world/{workflow_digest}"

        workflow_put = client.put(
            path,
            content=workflow_payload,
            headers=_write_headers(
                method="PUT",
                path=path,
                payload=workflow_payload,
                owner_domain=ALICE_DOMAIN,
                identity=identity,
                private_key=private_key,
            ),
        )

        assert workflow_put.status_code == 201
        assert client.get(f"/v1/workflows/{ALICE_DOMAIN}/hello_world/{workflow_digest}").content == workflow_payload
        assert client.get(f"/v1/workflows/{ALICE_DOMAIN}/hello_world/latest").content == workflow_payload
        assert client.head(f"/v1/workflows/{ALICE_DOMAIN}/hello_world/{workflow_digest}").status_code == 200


def test_runtime_certificate_upload_stores_typed_exact_and_latest_object(tmp_path) -> None:
    with _client(tmp_path) as client:
        certificate = _phala_runtime_certificate(
            workflow_id="attested_confidential_eval_demo",
            node_id="node_a",
        )
        payload = json.dumps(certificate).encode("utf-8")
        digest = _sha256_literal(payload)
        path = f"/v1/runtime/{ALICE_DOMAIN}/attested_confidential_eval_demo/certificates/node_a/{digest}"

        put_response = client.put(path, content=payload, headers=_runtime_headers(certificate))
        get_response = client.get(path)
        latest_response = client.get(f"/v1/runtime/{ALICE_DOMAIN}/attested_confidential_eval_demo/certificates/node_a/latest")

        assert put_response.status_code == 201
        assert get_response.status_code == 200
        assert json.loads(get_response.content.decode("utf-8")) == certificate
        assert latest_response.status_code == 200
        assert json.loads(latest_response.content.decode("utf-8")) == certificate


def test_runtime_certificate_rejects_mismatched_node_id_header(tmp_path) -> None:
    with _client(tmp_path) as client:
        certificate = _phala_runtime_certificate(
            workflow_id="attested_confidential_eval_demo",
            node_id="node_a",
        )
        payload = json.dumps(certificate).encode("utf-8")
        digest = _sha256_digest(payload)

        response = client.put(
            f"/v1/runtime/{ALICE_DOMAIN}/attested_confidential_eval_demo/certificates/node_a/{_sha256_literal(payload)}",
            content=payload,
            headers={
                **_runtime_headers(certificate),
                "X-Cove-Node-Id": "node_b",
            },
        )

        assert response.status_code == 401


def test_runtime_artifact_upload_stores_typed_exact_and_latest_object(tmp_path) -> None:
    with _client(tmp_path) as client:
        payload = b"ciphertext-envelope"
        digest = _sha256_literal(payload)
        path = f"/v1/runtime/{ALICE_DOMAIN}/attested_confidential_eval_demo/artifacts/model_output/{digest}"

        put_response = client.put(
            path,
            content=payload,
            headers=_runtime_artifact_headers(
                workflow_id="attested_confidential_eval_demo",
                node_id="node_a",
                artifact_name="model_output",
            ),
        )
        get_response = client.get(path)
        latest_response = client.get(f"/v1/runtime/{ALICE_DOMAIN}/attested_confidential_eval_demo/artifacts/model_output/latest")

        assert put_response.status_code == 201
        assert get_response.status_code == 200
        assert get_response.content == payload
        assert latest_response.status_code == 200
        assert latest_response.content == payload


def test_runtime_artifact_requires_workflow_and_artifact_headers(tmp_path) -> None:
    with _client(tmp_path) as client:
        payload = b"ciphertext-envelope"
        digest = _sha256_digest(payload)
        headers = _runtime_artifact_headers(
            workflow_id="attested_confidential_eval_demo",
            node_id="node_a",
            artifact_name="model_output",
        )

        response = client.put(
            f"/v1/runtime/{ALICE_DOMAIN}/attested_confidential_eval_demo/artifacts/model_output/{_sha256_literal(payload)}",
            content=payload,
            headers={key: value for key, value in headers.items() if key != "X-Cove-Workflow-Id"},
        )

        assert response.status_code == 401


def test_runtime_artifact_rejects_report_data_for_different_artifact(tmp_path) -> None:
    with _client(tmp_path) as client:
        payload = b"ciphertext-envelope"
        digest = _sha256_digest(payload)
        headers = _runtime_artifact_headers(
            workflow_id="attested_confidential_eval_demo",
            node_id="node_a",
            artifact_name="model_output",
        )

        response = client.put(
            f"/v1/runtime/{ALICE_DOMAIN}/attested_confidential_eval_demo/artifacts/model_output/{_sha256_literal(payload)}",
            content=payload,
            headers={**headers, "X-Cove-Artifact-Name": "other_output"},
        )

        assert response.status_code == 401


def test_legacy_mutable_routes_are_removed(tmp_path) -> None:
    with _client(tmp_path) as client:
        assert client.get("/files/exists", params={"path": "users/alice/artifacts/a"}).status_code == 404
        assert client.get("/v1/objects/sha256/" + "a" * 64).status_code == 404
        assert (
            client.put(
                "/users/alice/artifacts/model_weights",
                content=b"artifact",
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/users/alice/workflows/demo/bundle.manifest.json",
                content=b"{}",
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/users/alice/workflows/demo/runtime-certificates/node_a.json",
                content=b"{}",
            ).status_code
            == 404
        )
        assert (
            client.put(
                "/users/alice/workflows/demo/artifacts/model_output",
                content=b"ciphertext",
            ).status_code
            == 404
        )
        assert client.delete("/users/alice/workflows/demo/runtime-state").status_code == 404


def _client(tmp_path) -> TestClient:
    return _client_for_settings(_settings(tmp_path))


def _settings(tmp_path, **overrides) -> Settings:
    payload = {
        "data_root": tmp_path / "data",
        "host": "127.0.0.1",
        "port": 8000,
        "quote_verifier_mode": "phala_dstack",
    }
    payload.update(overrides)
    return Settings(**payload)


def _client_for_settings(settings: Settings) -> TestClient:
    app = create_app(settings)
    app.state.services.quote_verifier = _AcceptingQuoteVerifier()
    return TestClient(app)


def _owner_identity(
    owner_domain: str,
    *,
    private_key: ed25519.Ed25519PrivateKey | None = None,
    not_before: str = "2026-01-01T00:00:00Z",
    not_after: str = "2036-01-01T00:00:00Z",
) -> tuple[dict[str, object], ed25519.Ed25519PrivateKey]:
    private_key = private_key or ed25519.Ed25519PrivateKey.generate()
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    identity: dict[str, object] = {
        "version": OWNER_IDENTITY_VERSION,
        "owner_url": f"https://{owner_domain}",
        "owner_domain": owner_domain,
        "owner_public_key_pem": public_key_pem,
        "owner_public_key_sha256": _sha256_literal(public_key_pem.encode("utf-8")),
        "not_before": not_before,
        "not_after": not_after,
        "signature_algorithm": "ed25519",
    }
    signature = private_key.sign(
        canonical_json_bytes(owner_identity_signature_payload(identity))
    )
    identity["signature"] = base64.b64encode(signature).decode("ascii")
    return identity, private_key


def _stub_current_owner_identity(monkeypatch, identity: dict[str, object]) -> None:
    def fake_fetch_current_owner_identity(*, owner_url: str, owner_domain: str, now):
        assert owner_url == identity["owner_url"]
        assert owner_domain == identity["owner_domain"]
        return identity

    monkeypatch.setattr(
        "cove_server.write_auth._fetch_current_owner_identity",
        fake_fetch_current_owner_identity,
    )


def _write_headers(
    *,
    method: str,
    path: str,
    payload: bytes,
    owner_domain: str,
    identity: dict[str, object],
    private_key: ed25519.Ed25519PrivateKey,
) -> dict[str, str]:
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )
    identity_bytes = canonical_json_bytes(identity)
    identity_hash = _sha256_literal(identity_bytes)
    signature_payload = domain_write_signature_payload(
        method=method,
        path=path,
        payload_hash=_sha256_literal(payload),
        owner_domain=owner_domain,
        owner_identity_hash=identity_hash,
        timestamp=timestamp,
    )
    signature = private_key.sign(canonical_json_bytes(signature_payload))
    return {
        "X-Cove-Owner-Identity": base64.urlsafe_b64encode(identity_bytes)
        .decode("ascii")
        .rstrip("="),
        "X-Cove-Write-Timestamp": timestamp,
        "X-Cove-Write-Signature-Algorithm": "ed25519",
        "X-Cove-Write-Signature": base64.b64encode(signature).decode("ascii"),
    }


def _runtime_headers(certificate: dict[str, object]) -> dict[str, str]:
    certificate_body = certificate["certificate_body"]
    assert isinstance(certificate_body, dict)
    attestation_bundle = certificate["attestation_bundle"]
    assert isinstance(attestation_bundle, dict)
    headers = {
        "X-TDX-Quote": attestation_bundle["quote"],
        "X-Cove-Node-Id": certificate_body["node_id"],
        "X-Cove-Compose-Hash": certificate_body["generated_node_compose_hash"],
    }
    event_log = attestation_bundle.get("event_log")
    if event_log is not None:
        headers["X-TDX-Event-Log"] = (
            event_log if isinstance(event_log, str) else json.dumps(event_log, sort_keys=True)
        )
    return headers


def _runtime_artifact_headers(
    *,
    workflow_id: str,
    node_id: str,
    artifact_name: str,
    compose_hash: str = "sha256:" + "9" * 64,
) -> dict[str, str]:
    report_data = build_runtime_artifact_report_data(
        workflow_id=workflow_id,
        node_id=node_id,
        compose_hash=compose_hash,
        artifact_name=artifact_name,
    )
    return {
        "X-TDX-Quote": "phala-quote",
        "X-Cove-Workflow-Id": workflow_id,
        "X-Cove-Artifact-Name": artifact_name,
        "X-Cove-Node-Id": node_id,
        "X-Cove-Compose-Hash": compose_hash,
        "X-Cove-Attestation-Format": PHALA_DSTACK_ATTESTATION_FORMAT,
        "X-Cove-Report-Data": report_data.hex(),
    }


def _phala_runtime_certificate(
    *,
    workflow_id: str,
    node_id: str,
    compose_hash: str = "sha256:" + ("9" * 64),
    result_marker: str = "ok",
) -> dict[str, object]:
    certificate_body = {
        "workflow_id": workflow_id,
        "node_id": node_id,
        "generated_node_compose_hash": compose_hash,
        "inputs": {},
        "ephemeral_keypairs": {},
        "results": {"worker": {"marker": result_marker}},
    }
    certificate_body_hash = _sha256_literal(_canonical_json_bytes(certificate_body))
    report_data = build_node_certificate_report_data(
        certificate_body_hash=certificate_body_hash,
        compose_hash=compose_hash,
    )
    return {
        "certificate_body": certificate_body,
        "certificate_body_hash": certificate_body_hash,
        "attestation_bundle": {
            "format": PHALA_DSTACK_ATTESTATION_FORMAT,
            "quote": "phala-quote",
            "report_data": report_data.hex(),
            "quoted_certificate_body_hash": certificate_body_hash,
            "generated_node_compose_hash": compose_hash,
            "node_id": node_id,
        },
    }


def _canonical_json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_literal(payload: bytes) -> str:
    return f"sha256:{_sha256_digest(payload)}"


class _AcceptingQuoteVerifier:
    def verify(self, attestation) -> None:
        if attestation.format != PHALA_DSTACK_ATTESTATION_FORMAT:
            raise QuoteVerificationError("phala_dstack attestation format is invalid")
        if attestation.quote != "phala-quote":
            raise QuoteVerificationError("phala_dstack quote is invalid")
        if attestation.report_data != attestation.expected_report_data.hex():
            raise QuoteVerificationError(
                "attestation_bundle.report_data does not match expected report data"
            )
