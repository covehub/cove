#!/usr/bin/env python3
"""Run the attested CoveDemoModel OpenAI-compatible model server."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import ssl
import subprocess
import tempfile
from typing import Any
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from cove_demo_common import (
    extract_tarball,
    install_private_wheel,
    log,
    optional_env,
    require_env,
    require_cuda_preflight,
    wait_for_http,
)


SERVICE = "model_server"
RECEIPT_VERSION = "cove_model_response_receipt_v1"
SAMPLING_FIELD_NAMES = (
    "max_tokens",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "presence_penalty",
    "frequency_penalty",
    "repetition_penalty",
    "seed",
    "stream",
)


def _resolve_model_dir(extract_root: Path) -> Path:
    children = [child for child in extract_root.iterdir() if child.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_root


def _start_vllm_server(model_dir: Path, model_name: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["VLLM_DEVICE"] = env.get("VLLM_DEVICE", "cuda")
    command = [
        "vllm",
        "serve",
        str(model_dir),
        "--host",
        "127.0.0.1",
        "--port",
        "8000",
        "--dtype",
        env.get("VLLM_DTYPE", "auto"),
        "--gpu-memory-utilization",
        env.get("VLLM_GPU_MEMORY_UTILIZATION", "0.90"),
        "--tensor-parallel-size",
        env.get("VLLM_TENSOR_PARALLEL_SIZE", "1"),
        "--served-model-name",
        model_name,
    ]
    max_model_len = env.get("VLLM_MAX_MODEL_LEN")
    if max_model_len:
        command.extend(["--max-model-len", max_model_len])
    if env.get("VLLM_ENFORCE_EAGER", "0").strip().lower() in {"1", "true", "yes"}:
        command.append("--enforce-eager")
    process = subprocess.Popen(
        command,
        env=env,
        text=True,
    )
    wait_for_http("http://127.0.0.1:8000/health", timeout_seconds=300)
    return process


def _stop_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _load_receipt_private_key(path: str) -> ed25519.Ed25519PrivateKey:
    private_key = load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise RuntimeError("TLS_KEY_PATH must contain an Ed25519 private key")
    return private_key


def _public_key_hash(private_key: ed25519.Ed25519PrivateKey) -> str:
    public_key_pem = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return _sha256_literal(public_key_pem)


def _keypair_name_from_path(path: str) -> str:
    parent_name = Path(path).parent.name
    return parent_name or "ratls_key"


def _receipt_payload(
    *,
    private_key: ed25519.Ed25519PrivateKey,
    keypair_name: str,
    public_key_hash: str,
    nonce: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    model_name: str,
) -> dict[str, Any]:
    model = request_payload.get("model")
    if not isinstance(model, str) or not model:
        model = response_payload.get("model") if isinstance(response_payload.get("model"), str) else model_name
    sampling_fields = {
        name: request_payload[name]
        for name in SAMPLING_FIELD_NAMES
        if name in request_payload
    }
    unsigned_receipt: dict[str, Any] = {
        "version": RECEIPT_VERSION,
        "nonce": nonce,
        "request_hash": _sha256_literal(_canonical_json_bytes(request_payload)),
        "response_hash": _sha256_literal(_canonical_json_bytes(response_payload)),
        "model": model,
        "sampling": sampling_fields,
        "no_hidden_server_context": True,
        "keypair_name": keypair_name,
        "public_key_hash": public_key_hash,
    }
    signature = private_key.sign(_canonical_json_bytes(unsigned_receipt))
    return {
        **unsigned_receipt,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


class ForwardingHandler(BaseHTTPRequestHandler):
    backend_host = "127.0.0.1"
    backend_port = 8000
    model_name = "CoveDemoModel"
    receipt_private_key: ed25519.Ed25519PrivateKey | None = None
    receipt_keypair_name = "ratls_key"
    receipt_public_key_hash = ""

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, status_code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _backend_headers(self) -> dict[str, str]:
        return {
            key: value
            for key, value in self.headers.items()
            if key.lower()
            not in {"host", "content-length", "connection", "x-cove-nonce"}
        }

    def _forward(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        connection = HTTPConnection(self.backend_host, self.backend_port, timeout=120)
        headers = self._backend_headers()
        connection.request(self.command, self.path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in {"transfer-encoding", "connection", "content-length"}:
                continue
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        connection.close()

    def _forward_signed_chat_completion(self) -> None:
        nonce = self.headers.get("X-Cove-Nonce", "").strip()
        if not nonce:
            self._send_json(400, {"error": "X-Cove-Nonce header is required"})
            return
        if self.receipt_private_key is None:
            self._send_json(500, {"error": "receipt signer is not initialized"})
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        try:
            request_payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(400, {"error": "chat request body must be JSON"})
            return
        if not isinstance(request_payload, dict):
            self._send_json(400, {"error": "chat request body must be a JSON object"})
            return
        if request_payload.get("stream") is not False:
            self._send_json(400, {"error": "response receipts require stream=false"})
            return

        connection = HTTPConnection(self.backend_host, self.backend_port, timeout=120)
        connection.request(
            self.command,
            self.path,
            body=body,
            headers=self._backend_headers(),
        )
        backend_response = connection.getresponse()
        backend_payload = backend_response.read()
        backend_headers = backend_response.getheaders()
        connection.close()

        if backend_response.status >= 400:
            self.send_response(backend_response.status)
            for key, value in backend_headers:
                if key.lower() in {"transfer-encoding", "connection", "content-length"}:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(backend_payload)))
            self.end_headers()
            self.wfile.write(backend_payload)
            return

        try:
            response_payload = json.loads(backend_payload.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json(502, {"error": "backend chat response was not JSON"})
            return
        if not isinstance(response_payload, dict):
            self._send_json(502, {"error": "backend chat response was not a JSON object"})
            return

        receipt = _receipt_payload(
            private_key=self.receipt_private_key,
            keypair_name=self.receipt_keypair_name,
            public_key_hash=self.receipt_public_key_hash,
            nonce=nonce,
            request_payload=request_payload,
            response_payload=response_payload,
            model_name=self.model_name,
        )
        signed_payload = {
            **response_payload,
            "cove_receipt": receipt,
        }
        encoded = json.dumps(signed_payload, separators=(",", ":")).encode("utf-8")
        self.send_response(backend_response.status)
        for key, value in backend_headers:
            if key.lower() in {
                "transfer-encoding",
                "connection",
                "content-length",
                "content-encoding",
            }:
                continue
            self.send_header(key, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            connection = HTTPConnection(self.backend_host, self.backend_port, timeout=10)
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                payload = response.read()
            except OSError:
                self._send_json(503, {"status": "backend_unavailable"})
                return
            finally:
                connection.close()
            if response.status >= 400:
                self.send_response(response.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self._send_json(200, {"status": "ok"})
            return
        self._forward()

    def do_POST(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] == "/v1/chat/completions":
            self._forward_signed_chat_completion()
            return
        self._forward()


def main() -> int:
    serving_wheel_path = Path(require_env("SERVING_WHEEL_PATH"))
    model_archive_path = Path(require_env("MODEL_ARCHIVE_PATH"))
    tls_cert_path = require_env("TLS_CERT_PATH")
    tls_key_path = require_env("TLS_KEY_PATH")
    host = optional_env("HOST", "0.0.0.0")
    port = int(optional_env("PORT", "8443"))
    model_name = optional_env("MODEL_NAME", "CoveDemoModel")

    gpu_status = require_cuda_preflight(SERVICE)
    log(SERVICE, f"GPU status: {json.dumps(gpu_status, sort_keys=True)}")

    with tempfile.TemporaryDirectory(prefix="cove-model-server-") as temp_dir:
        temp_root = Path(temp_dir)
        # Alice's private compiled serving wheel is installed before exposing
        # the attested model endpoint.
        install_private_wheel(serving_wheel_path, temp_root)

        # Alice's private model archive is unpacked only inside this final
        # serving node and loaded by the local vLLM process.
        model_root = extract_tarball(model_archive_path, temp_root / "model")
        model_dir = _resolve_model_dir(model_root)
        process = _start_vllm_server(model_dir, model_name)
        try:
            receipt_private_key = _load_receipt_private_key(tls_key_path)
            ForwardingHandler.model_name = model_name
            ForwardingHandler.receipt_private_key = receipt_private_key
            ForwardingHandler.receipt_keypair_name = _keypair_name_from_path(tls_key_path)
            ForwardingHandler.receipt_public_key_hash = _public_key_hash(receipt_private_key)
            server = ThreadingHTTPServer((host, port), ForwardingHandler)
            ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_context.load_cert_chain(certfile=tls_cert_path, keyfile=tls_key_path)
            server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
            log(SERVICE, f"serving RA-TLS proxy on https://{host}:{port}")
            server.serve_forever()
        finally:
            _stop_process(process)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
