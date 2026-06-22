#!/usr/bin/env python3
"""Run the attested CoveDemoModel OpenAI-compatible model server."""

from __future__ import annotations

import json
import os
import shutil
import signal
import ssl
import subprocess
import tempfile
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cove_demo_common import (
    extract_tarball,
    find_first_existing,
    log,
    optional_env,
    require_env,
    run,
    wait_for_http,
)


SERVICE = "model_server"


def _resolve_model_dir(extract_root: Path) -> Path:
    children = [child for child in extract_root.iterdir() if child.name != "__MACOSX"]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return extract_root


def _build_ld_preload() -> str | None:
    tcmalloc_path = find_first_existing(
        [
            "/usr/lib/x86_64-linux-gnu/libtcmalloc_minimal.so.4",
            "/usr/lib/aarch64-linux-gnu/libtcmalloc_minimal.so.4",
        ]
    )
    iomp_candidates: list[str] = []
    for site_root in Path("/usr/local/lib/python3.12/site-packages").rglob("libiomp5.so"):
        iomp_candidates.append(str(site_root))
    iomp_path = iomp_candidates[0] if iomp_candidates else None
    values = [path for path in [tcmalloc_path, iomp_path, os.environ.get("LD_PRELOAD")] if path]
    if not values:
        return None
    return ":".join(values)


def _start_vllm_server(model_dir: Path, model_name: str) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["VLLM_CPU_KVCACHE_SPACE"] = env.get("VLLM_CPU_KVCACHE_SPACE", "4")
    env["VLLM_CPU_NUM_OF_RESERVED_CPU"] = env.get("VLLM_CPU_NUM_OF_RESERVED_CPU", "1")
    ld_preload = _build_ld_preload()
    if ld_preload is not None:
        env["LD_PRELOAD"] = ld_preload
    process = subprocess.Popen(
        [
            "vllm",
            "serve",
            str(model_dir),
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--dtype",
            "float32",
            "--enforce-eager",
            "--served-model-name",
            model_name,
        ],
        env=env,
        text=True,
    )
    wait_for_http("http://127.0.0.1:8000/health", timeout_seconds=180)
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


class ForwardingHandler(BaseHTTPRequestHandler):
    backend_host = "127.0.0.1"
    backend_port = 8000

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, status_code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        connection = HTTPConnection(self.backend_host, self.backend_port, timeout=120)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }
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
        self._forward()


def main() -> int:
    serving_wheel_path = Path(require_env("SERVING_WHEEL_PATH"))
    model_archive_path = Path(require_env("MODEL_ARCHIVE_PATH"))
    tls_cert_path = require_env("TLS_CERT_PATH")
    tls_key_path = require_env("TLS_KEY_PATH")
    host = optional_env("HOST", "0.0.0.0")
    port = int(optional_env("PORT", "8443"))
    model_name = optional_env("MODEL_NAME", "CoveDemoModel")

    vllm_version = os.environ.get("VLLM_VERSION", "0.17.0")

    with tempfile.TemporaryDirectory(prefix="cove-model-server-") as temp_dir:
        temp_root = Path(temp_dir)
        installable_wheel_path = temp_root / f"vllm-{vllm_version}+cpu-cp38-abi3-linux_x86_64.whl"
        # Alice's private compiled serving wheel is installed before exposing
        # the attested model endpoint.
        shutil.copy2(serving_wheel_path, installable_wheel_path)
        run(
            [
                "python",
                "-m",
                "pip",
                "install",
                "--force-reinstall",
                "--no-deps",
                str(installable_wheel_path),
            ]
        )

        # Alice's private model archive is unpacked only inside this final
        # serving node and loaded by the local vLLM process.
        model_root = extract_tarball(model_archive_path, temp_root / "model")
        model_dir = _resolve_model_dir(model_root)
        process = _start_vllm_server(model_dir, model_name)
        try:
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
