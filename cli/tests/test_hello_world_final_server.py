from __future__ import annotations

import importlib.util
import json
import socket
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from ipaddress import IPv4Address
from pathlib import Path
from types import ModuleType
from urllib.request import urlopen

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def test_final_server_half_open_tls_client_does_not_block_healthcheck(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module = _load_final_server_module(monkeypatch)
    cert_path, key_path = _write_self_signed_cert(tmp_path)
    server = module.build_server(
        host="127.0.0.1",
        port=0,
        tls_cert_path=str(cert_path),
        tls_key_path=str(key_path),
        message="hello",
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    half_open_socket = socket.create_connection(("127.0.0.1", port), timeout=3)
    try:
        time.sleep(0.1)
        assert _get_health(port) == {"status": "ok"}
    finally:
        half_open_socket.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _load_final_server_module(monkeypatch) -> ModuleType:
    repo_root = Path(__file__).resolve().parents[2]
    common_dir = repo_root / "demos" / "hello_world" / "containers" / "common"
    module_path = (
        repo_root / "demos" / "hello_world" / "containers" / "final_server" / "main.py"
    )
    monkeypatch.syspath_prepend(str(common_dir))
    spec = importlib.util.spec_from_file_location("hello_world_final_server", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        ]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _get_health(port: int) -> dict[str, object]:
    context = ssl._create_unverified_context()
    with urlopen(
        f"https://127.0.0.1:{port}/health",
        timeout=3,
        context=context,
    ) as response:
        return json.loads(response.read().decode("utf-8"))
