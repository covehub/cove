from __future__ import annotations

import importlib.util
import base64
import json
import hashlib
import sys
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
from pathlib import Path
from threading import Thread
from types import ModuleType
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from cove_cli.common import canonical_json_bytes, sha256_literal
from cove_cli.owner_identity import (
    OWNER_IDENTITY_VERSION,
    owner_identity_signature_payload,
)
from cove_cli.provisioning_identity import owner_domain_from_url


@dataclass(slots=True)
class MockCovehubState:
    artifacts: dict[str, bytes] = field(default_factory=dict)
    workflow_bundles: dict[str, bytes] = field(default_factory=dict)
    runtime_certificates: dict[str, bytes] = field(default_factory=dict)
    runtime_artifacts: dict[str, bytes] = field(default_factory=dict)
    request_user_agents: list[str | None] = field(default_factory=list)


class MockCovehubServer:
    def __init__(self) -> None:
        self.state = MockCovehubState()
        self.server: ThreadingHTTPServer | None = None
        self.thread: Thread | None = None
        self.url: str | None = None

    def __enter__(self) -> "MockCovehubServer":
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                state.request_user_agents.append(self.headers.get("User-Agent"))
                _write_json(self, HTTPStatus.NOT_FOUND, {"detail": "not found"})

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                if parsed.path.startswith("/v1/"):
                    parts = parsed.path.strip("/").split("/")
                    if (
                        len(parts) == 7
                        and parts[:1] == ["v1"]
                        and parts[1] == "runtime"
                        and parts[4] == "certificates"
                    ):
                        relative_path = parsed.path.lstrip("/")
                        payload = state.runtime_certificates.get(relative_path)
                        if payload is None:
                            _write_json(
                                self,
                                HTTPStatus.NOT_FOUND,
                                {"detail": "object not found"},
                            )
                            return

                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    if (
                        len(parts) == 7
                        and parts[:1] == ["v1"]
                        and parts[1] == "runtime"
                        and parts[4] == "artifacts"
                    ):
                        relative_path = parsed.path.lstrip("/")
                        payload = state.runtime_artifacts.get(relative_path)
                        if payload is None:
                            _write_json(
                                self,
                                HTTPStatus.NOT_FOUND,
                                {"detail": "object not found"},
                            )
                            return

                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return
                    if len(parts) == 5 and parts[1] == "workflows":
                        relative_path = parsed.path.lstrip("/")
                        payload = state.workflow_bundles.get(relative_path)
                        if payload is None:
                            _write_json(
                                self,
                                HTTPStatus.NOT_FOUND,
                                {"detail": "object not found"},
                            )
                            return

                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return

                    if len(parts) == 5 and parts[1] == "artifacts":
                        relative_path = parsed.path.lstrip("/")
                        payload = state.artifacts.get(relative_path)
                        if payload is None:
                            _write_json(
                                self,
                                HTTPStatus.NOT_FOUND,
                                {"detail": "object not found"},
                            )
                            return

                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/octet-stream")
                        self.send_header("Content-Length", str(len(payload)))
                        self.end_headers()
                        self.wfile.write(payload)
                        return

                _write_json(self, HTTPStatus.NOT_FOUND, {"detail": "not found"})

            def do_PUT(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                content_length = int(self.headers.get("Content-Length", "0"))
                payload = self.rfile.read(content_length)
                if (
                    len(parts) == 7
                    and parts[0] == "v1"
                    and parts[1] == "runtime"
                    and parts[4] == "certificates"
                ):
                    if not self.headers.get("X-TDX-Quote"):
                        _write_json(
                            self,
                            HTTPStatus.UNAUTHORIZED,
                            {"detail": "missing runtime attestation headers"},
                        )
                        return
                    relative_path = parsed.path.lstrip("/")
                    digest = parts[6]
                    if _sha256_literal(payload) != digest:
                        _write_json(
                            self,
                            HTTPStatus.BAD_REQUEST,
                            {"detail": "payload sha256 does not match digest path"},
                        )
                        return
                    latest_path = "/".join([*parts[:6], "latest"])
                    existing = state.runtime_certificates.get(relative_path)
                    created = existing is None
                    state.runtime_certificates[relative_path] = payload
                    state.runtime_certificates[latest_path] = payload
                    self.send_response(HTTPStatus.CREATED if created else HTTPStatus.OK)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if (
                    len(parts) == 7
                    and parts[0] == "v1"
                    and parts[1] == "runtime"
                    and parts[4] == "artifacts"
                ):
                    if (
                        not self.headers.get("X-TDX-Quote")
                        or not self.headers.get("X-Cove-Workflow-Id")
                        or not self.headers.get("X-Cove-Artifact-Name")
                        or not self.headers.get("X-Cove-Node-Id")
                        or not self.headers.get("X-Cove-Compose-Hash")
                        or not self.headers.get("X-Cove-Attestation-Format")
                        or not self.headers.get("X-Cove-Report-Data")
                    ):
                        _write_json(
                            self,
                            HTTPStatus.UNAUTHORIZED,
                            {"detail": "missing runtime attestation headers"},
                        )
                        return
                    relative_path = parsed.path.lstrip("/")
                    digest = parts[6]
                    if _sha256_literal(payload) != digest:
                        _write_json(
                            self,
                            HTTPStatus.BAD_REQUEST,
                            {"detail": "payload sha256 does not match digest path"},
                        )
                        return
                    latest_path = "/".join([*parts[:6], "latest"])
                    existing = state.runtime_artifacts.get(relative_path)
                    created = existing is None
                    state.runtime_artifacts[relative_path] = payload
                    state.runtime_artifacts[latest_path] = payload
                    self.send_response(HTTPStatus.CREATED if created else HTTPStatus.OK)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if len(parts) == 5 and parts[0] == "v1" and parts[1] == "artifacts":
                    owner_domain = parts[2]
                    artifact_name = parts[3]
                    digest = parts[4]
                    if _sha256_literal(payload) != digest:
                        _write_json(
                            self,
                            HTTPStatus.BAD_REQUEST,
                            {"detail": "payload sha256 does not match digest path"},
                        )
                        return
                    relative_path = f"v1/artifacts/{owner_domain}/{artifact_name}/{digest}"
                    latest_path = f"v1/artifacts/{owner_domain}/{artifact_name}/latest"
                    created = relative_path not in state.artifacts
                    state.artifacts[relative_path] = payload
                    state.artifacts[latest_path] = payload
                    self.send_response(
                        HTTPStatus.CREATED if created else HTTPStatus.OK
                    )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if len(parts) == 5 and parts[0] == "v1" and parts[1] == "workflows":
                    publisher_domain = parts[2]
                    workflow_id = parts[3]
                    digest = parts[4]
                    if _sha256_literal(payload) != digest:
                        _write_json(
                            self,
                            HTTPStatus.BAD_REQUEST,
                            {"detail": "payload sha256 does not match digest path"},
                        )
                        return
                    relative_path = parsed.path.lstrip("/")
                    latest_path = f"v1/workflows/{publisher_domain}/{workflow_id}/latest"
                    existing = state.workflow_bundles.get(relative_path)
                    created = existing is None
                    state.workflow_bundles[relative_path] = payload
                    state.workflow_bundles[latest_path] = payload
                    self.send_response(HTTPStatus.CREATED if created else HTTPStatus.OK)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                _write_json(self, HTTPStatus.NOT_FOUND, {"detail": "not found"})

            def do_DELETE(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                if (
                    len(parts) == 4
                    and parts[0] == "v1"
                    and parts[1] == "runtime"
                ):
                    publisher_domain = parts[2]
                    workflow_id = parts[3]
                    artifacts_prefix = f"v1/runtime/{publisher_domain}/{workflow_id}/artifacts/"
                    certificates_prefix = f"v1/runtime/{publisher_domain}/{workflow_id}/certificates/"
                    deleted_artifacts = False
                    deleted_certificates = False
                    for path in list(state.runtime_artifacts):
                        if path.startswith(artifacts_prefix):
                            deleted_artifacts = True
                            del state.runtime_artifacts[path]
                    for path in list(state.runtime_certificates):
                        if path.startswith(certificates_prefix):
                            deleted_certificates = True
                            del state.runtime_certificates[path]
                    _write_json(
                        self,
                        HTTPStatus.OK,
                        {
                            "workflow_id": workflow_id,
                            "deleted_runtime_artifacts": deleted_artifacts,
                            "deleted_runtime_certificates": deleted_certificates,
                        },
                    )
                    return

                _write_json(self, HTTPStatus.NOT_FOUND, {"detail": "not found"})

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
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

    def seed_artifact(self, path: str, payload: bytes = b"seeded") -> None:
        self.state.artifacts[path] = payload

    def seed_runtime_artifact(self, path: str, payload: bytes = b"seeded") -> None:
        self.state.runtime_artifacts[path] = payload

    def seed_runtime_certificate(self, path: str, payload: bytes) -> None:
        self.state.runtime_certificates[path] = payload

def load_container_main_module(container_name: str) -> ModuleType:
    repo_root = Path(__file__).resolve().parents[2]
    runtime_src = repo_root / "cove_container_runtime" / "src"
    if str(runtime_src) not in sys.path:
        sys.path.insert(0, str(runtime_src))
    module_path = repo_root / "containers" / container_name / "main.py"
    spec = importlib.util.spec_from_file_location(
        f"cove_containers_{container_name}_main",
        module_path,
    )
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"failed to load module spec for {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_test_certificate(
    cert_path: Path,
    *,
    dns_names: list[str] | None = None,
    ip_addresses: list[str] | None = None,
) -> Path:
    resolved_dns_names = [name.strip().lower() for name in (dns_names or []) if name.strip()]
    resolved_ip_addresses = [ipaddress.ip_address(value) for value in (ip_addresses or [])]
    common_name = (
        resolved_dns_names[0]
        if resolved_dns_names
        else str(resolved_ip_addresses[0])
        if resolved_ip_addresses
        else "localhost"
    )

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    builder = (
        x509.CertificateBuilder()
        .subject_name(
            x509.Name(
                [
                    x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name),
                ]
            )
        )
        .issuer_name(
            x509.Name(
                [
                    x509.NameAttribute(x509.NameOID.COMMON_NAME, common_name),
                ]
            )
        )
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
    )

    subject_alt_names: list[x509.GeneralName] = [
        *[x509.DNSName(name) for name in resolved_dns_names],
        *[x509.IPAddress(address) for address in resolved_ip_addresses],
    ]
    if subject_alt_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(subject_alt_names),
            critical=False,
        )

    certificate = builder.sign(private_key=private_key, algorithm=hashes.SHA256())
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return cert_path


def build_test_owner_identity(
    _owner_domain_hint: str | None = None,
    owner_url: str | None = None,
) -> dict[str, object]:
    if not isinstance(_owner_domain_hint, str) or not isinstance(owner_url, str):
        raise TypeError("owner_domain_hint and owner_url are required")
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    owner_url = owner_url.rstrip("/")
    owner_domain = owner_domain_from_url(owner_url)
    document: dict[str, object] = {
        "version": OWNER_IDENTITY_VERSION,
        "owner_url": owner_url,
        "owner_domain": owner_domain,
        "owner_public_key_pem": public_key_pem,
        "owner_public_key_sha256": sha256_literal(public_key_pem.encode("utf-8")),
        "not_before": "2026-01-01T00:00:00Z",
        "not_after": "2036-01-01T00:00:00Z",
        "signature_algorithm": "ed25519",
    }
    signature = private_key.sign(
        canonical_json_bytes(owner_identity_signature_payload(document))
    )
    document["signature"] = base64.b64encode(signature).decode("ascii")
    return document


def _write_json(
    handler: BaseHTTPRequestHandler,
    status_code: HTTPStatus,
    payload: dict[str, object],
) -> None:
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)

def _sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
