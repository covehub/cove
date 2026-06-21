from __future__ import annotations

import base64
import json
import ssl
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib import request as urllib_request

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.x509.oid import NameOID

from cove_cli.attestation import build_node_certificate_report_data
from cove_cli.cli import run
from cove_cli.client_proxy import (
    ClientProxyCommandError,
    make_verified_proxy_server,
    verify_client_proxy_target,
)
from cove_cli.common import canonical_json_bytes, sha256_literal

from .support import MockCovehubServer


PUBLISHER = "alice.example.test"
WORKFLOW_ID = "demo"
NODE_ID = "final"
KEYPAIR_NAME = "ratls_key"
COMPOSE_HASH = "sha256:" + "1" * 64


def test_client_proxy_verifies_target_and_forwards_http(tmp_path, monkeypatch) -> None:
    tls = _write_ed25519_tls_material(tmp_path / "tls", "demo.final.ratls_key")

    with (
        _HttpsService(tls.cert_path, tls.key_path) as remote,
        MockCovehubServer() as server,
    ):
        _seed_workflow_and_certificate(server, tls.certificate_pem)
        _stub_attestation_verifier(monkeypatch)

        target = verify_client_proxy_target(
            remote=remote.url,
            local="127.0.0.1:0",
            workflow=f"{PUBLISHER}/{WORKFLOW_ID}",
            bundle_root=tmp_path / "pulled",
            server_url=server.url,
        )

        proxy = make_verified_proxy_server(target)
        thread = Thread(target=proxy.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = proxy.server_address[:2]
            request = urllib_request.Request(
                f"http://{host}:{port}/v1/chat/completions",
                data=b'{"prompt":"hello"}',
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib_request.urlopen(request, timeout=5) as response:
                payload = json.loads(response.read().decode("utf-8"))
        finally:
            proxy.shutdown()
            proxy.server_close()
            thread.join(timeout=5)

    assert target.node_id == NODE_ID
    assert target.keypair_name == KEYPAIR_NAME
    assert target.certificate_hash == sha256_literal(tls.certificate_pem)
    assert (tmp_path / "pulled" / "workflow.normalized.cove.yaml").is_file()
    assert payload == {
        "method": "POST",
        "path": "/v1/chat/completions",
        "body": {"prompt": "hello"},
    }


def test_client_proxy_rejects_remote_tls_certificate_mismatch(tmp_path, monkeypatch) -> None:
    expected_tls = _write_ed25519_tls_material(tmp_path / "expected", "demo.final.ratls_key")
    served_tls = _write_ed25519_tls_material(tmp_path / "served", "demo.final.ratls_key")

    with (
        _HttpsService(served_tls.cert_path, served_tls.key_path) as remote,
        MockCovehubServer() as server,
    ):
        _seed_workflow_and_certificate(server, expected_tls.certificate_pem)
        _stub_attestation_verifier(monkeypatch)

        with pytest.raises(ClientProxyCommandError, match="served TLS certificate"):
            verify_client_proxy_target(
                remote=remote.url,
                local="127.0.0.1:0",
                workflow=f"{PUBLISHER}/{WORKFLOW_ID}",
                bundle_root=tmp_path / "pulled",
                server_url=server.url,
            )


def test_client_proxy_cli_does_not_require_cove_init(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_start_client_proxy(**kwargs):
        captured.update(kwargs)
        return 17

    monkeypatch.setattr("cove_cli.cli.start_client_proxy", fake_start_client_proxy)

    exit_code = run(
        [
            "client",
            "proxy",
            "--remote",
            "https://service.example.test",
            "--workflow",
            f"{PUBLISHER}/{WORKFLOW_ID}",
        ]
    )

    assert exit_code == 17
    assert captured["server_url"] is None
    assert captured["local"] == "localhost:8080"
    assert captured["write_workflow_to"] is None


class _TlsMaterial:
    def __init__(self, *, certificate_pem: bytes, cert_path: Path, key_path: Path) -> None:
        self.certificate_pem = certificate_pem
        self.cert_path = cert_path
        self.key_path = key_path


def _write_ed25519_tls_material(root: Path, common_name: str) -> _TlsMaterial:
    root.mkdir(parents=True, exist_ok=True)
    private_key = ed25519.Ed25519PrivateKey.generate()
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(private_key, algorithm=None)
    )
    key_path = root / "key.pem"
    cert_path = root / "cert.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    certificate_pem = certificate.public_bytes(serialization.Encoding.PEM)
    cert_path.write_bytes(certificate_pem)
    return _TlsMaterial(
        certificate_pem=certificate_pem,
        cert_path=cert_path,
        key_path=key_path,
    )


class _HttpsService:
    def __init__(self, cert_path: Path, key_path: Path) -> None:
        self.cert_path = cert_path
        self.key_path = key_path
        self.server: ThreadingHTTPServer | None = None
        self.thread: Thread | None = None
        self.url: str = ""

    def __enter__(self) -> "_HttpsService":
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self._write_json({"method": "GET", "path": self.path, "body": None})

            def do_POST(self) -> None:  # noqa: N802
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                payload = json.loads(body.decode("utf-8")) if body else None
                self._write_json({"method": "POST", "path": self.path, "body": payload})

            def _write_json(self, payload: dict[str, object]) -> None:
                encoded = json.dumps(payload).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.cert_path), str(self.key_path))
        server.socket = context.wrap_socket(server.socket, server_side=True)
        self.server = server
        host, port = server.server_address[:2]
        self.url = f"https://{host}:{port}"
        self.thread = Thread(target=server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_exc_info: object) -> None:
        assert self.server is not None
        assert self.thread is not None
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def _seed_workflow_and_certificate(
    server: MockCovehubServer,
    certificate_pem: bytes,
) -> None:
    workflow_object = _workflow_object_bytes()
    server.state.workflow_bundles[
        f"v1/workflows/{PUBLISHER}/{WORKFLOW_ID}/latest"
    ] = workflow_object
    certificate = _runtime_certificate(certificate_pem)
    server.seed_runtime_certificate(
        f"v1/runtime/{PUBLISHER}/{WORKFLOW_ID}/certificates/{NODE_ID}/latest",
        payload=json.dumps(certificate, sort_keys=True).encode("utf-8"),
    )


def _workflow_object_bytes() -> bytes:
    files = {
        "workflow.normalized.cove.yaml": (
            "cove_version: 1\n"
            "workflow:\n"
            f"  id: {WORKFLOW_ID}\n"
            "platform:\n"
            "  provider: phala\n"
            "  runtime: dstack\n"
            "ephemeral_keypairs:\n"
            f"  {KEYPAIR_NAME}:\n"
            "    algorithm: ed25519\n"
            "artifacts: {}\n"
            "nodes:\n"
            f"  {NODE_ID}:\n"
            "    compose: nodes/final.compose.yaml\n"
            "    services:\n"
            "      serve:\n"
            "        should_terminate: false\n"
            "        ephemeral_keypairs:\n"
            f"        - {KEYPAIR_NAME}\n"
        ).encode("utf-8"),
        f"nodes/{NODE_ID}/compose.generated.yaml": b"services: {}\nvolumes: {}\n",
        f"nodes/{NODE_ID}/compose.generated.sha256": f"{COMPOSE_HASH}\n".encode("utf-8"),
    }
    file_entries = [
        {"path": path, "sha256": sha256_literal(payload)}
        for path, payload in sorted(files.items())
    ]
    manifest_without_hash = {
        "publisher": PUBLISHER,
        "workflow_id": WORKFLOW_ID,
        "owners": {},
        "files": file_entries,
        "nodes": [
            {
                "node_id": NODE_ID,
                "compose_path": f"nodes/{NODE_ID}/compose.generated.yaml",
                "compose_hash": COMPOSE_HASH,
                "artifact_provisioner_image": None,
                "artifact_provisioner_digest": None,
                "artifacts": [],
                "runtime_skeleton": [],
            }
        ],
    }
    manifest = {
        **manifest_without_hash,
        "manifest_hash": sha256_literal(canonical_json_bytes(manifest_without_hash)),
    }
    workflow_object = {
        "format": "cove.workflow.bundle.v1",
        "manifest": manifest,
        "files": [
            {
                "path": path,
                "sha256": sha256_literal(payload),
                "content_b64": base64.b64encode(payload).decode("ascii"),
            }
            for path, payload in sorted(files.items())
        ],
    }
    return canonical_json_bytes(workflow_object)


def _runtime_certificate(certificate_pem: bytes) -> dict[str, object]:
    certificate_body = {
        "workflow_id": WORKFLOW_ID,
        "node_id": NODE_ID,
        "generated_node_compose_hash": COMPOSE_HASH,
        "inputs": {},
        "ephemeral_keypairs": {
            KEYPAIR_NAME: {
                "name": KEYPAIR_NAME,
                "algorithm": "ed25519",
                "certificate_pem": certificate_pem.decode("utf-8"),
                "certificate_hash": sha256_literal(certificate_pem),
            }
        },
        "results": {},
        "outputs": {},
    }
    certificate_body_hash = sha256_literal(canonical_json_bytes(certificate_body))
    report_data = build_node_certificate_report_data(
        certificate_body_hash=certificate_body_hash,
        compose_hash=COMPOSE_HASH,
    )
    return {
        "certificate_body": certificate_body,
        "certificate_body_hash": certificate_body_hash,
        "attestation_bundle": {
            "format": "phala_dstack_v1",
            "quote": "test-quote",
            "report_data": report_data.hex(),
            "quoted_certificate_body_hash": certificate_body_hash,
            "generated_node_compose_hash": COMPOSE_HASH,
            "node_id": NODE_ID,
        },
    }


def _stub_attestation_verifier(monkeypatch) -> None:
    def fake_verify_attestation_bundle(attestation_bundle, *, expected_report_data):
        assert attestation_bundle["report_data"] == expected_report_data.hex()
        return attestation_bundle

    monkeypatch.setattr(
        "cove_cli.client_proxy.verify_attestation_bundle",
        fake_verify_attestation_bundle,
    )
