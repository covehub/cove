#!/usr/bin/env python3

import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hello_world_common import log, optional_env, read_secret_word, require_env


SERVICE = "final_server"
TLS_HANDSHAKE_TIMEOUT_SECONDS = 5.0


class ThreadingHTTPSServer(ThreadingHTTPServer):
    daemon_threads = True
    ssl_context: ssl.SSLContext


class Handler(BaseHTTPRequestHandler):
    message = ""

    def setup(self) -> None:
        self.request.settimeout(TLS_HANDSHAKE_TIMEOUT_SECONDS)
        self.request = self.server.ssl_context.wrap_socket(self.request, server_side=True)
        self.request.settimeout(None)
        self.connection = self.request
        self.rfile = self.connection.makefile("rb", self.rbufsize)
        self.wfile = self.connection.makefile("wb", self.wbufsize)

    def log_message(self, fmt: str, *args) -> None:
        return

    def _send_json(self, status_code: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, status_code: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
            return
        if self.path == "/message":
            self._send_text(200, self.message)
            return
        self._send_json(404, {"error": "not found"})


def build_server(
    *,
    host: str,
    port: int,
    tls_cert_path: str,
    tls_key_path: str,
    message: str,
) -> ThreadingHTTPSServer:
    Handler.message = message
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=tls_cert_path, keyfile=tls_key_path)
    server = ThreadingHTTPSServer((host, port), Handler)
    server.ssl_context = ssl_context
    return server


def main() -> int:
    alice_input_path = require_env("ALICE_INPUT_PATH")
    bob_input_path = require_env("BOB_INPUT_PATH")
    tls_cert_path = require_env("TLS_CERT_PATH")
    tls_key_path = require_env("TLS_KEY_PATH")
    host = optional_env("HOST", "0.0.0.0")
    port = int(optional_env("PORT", "8443"))

    alice_word = read_secret_word(alice_input_path)
    bob_word = read_secret_word(bob_input_path)
    message = f"alice's secret word is: {alice_word} and bob's secret word is: {bob_word}"

    server = build_server(
        host=host,
        port=port,
        tls_cert_path=tls_cert_path,
        tls_key_path=tls_key_path,
        message=message,
    )
    log(SERVICE, f"serving https on {host}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
