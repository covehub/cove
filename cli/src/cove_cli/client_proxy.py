from __future__ import annotations

import socket
import socketserver
import ssl
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .attestation import build_node_certificate_report_data
from .client_attestation import verify_client_attestation_bundle
from .common import RuntimeErrorBase, canonical_json_bytes, http_get_json, sha256_literal
from .deploy import translated_node_deployment_compose_text
from .publish import (
    MaterializedNode,
    MaterializedWorkflowBundle,
    parse_published_ref,
    pull_workflow_bundle,
)
from .provisioning_identity import DEFAULT_COVEHUB_SERVER_URL


class ClientProxyCommandError(RuntimeErrorBase):
    """Raised when a client proxy target cannot be verified or served."""


@dataclass(frozen=True, slots=True)
class LocalEndpoint:
    host: str
    port: int

    @property
    def display(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass(frozen=True, slots=True)
class VerifiedProxyTarget:
    remote_url: str
    local: LocalEndpoint
    server_url: str
    publisher: str
    workflow_id: str
    workflow_reference: str
    bundle_root: Path
    manifest_hash: str
    node_id: str
    node_compose_hash: str
    keypair_name: str
    certificate_hash: str
    expected_certificate_der: bytes


_PROXY_BUFFER_BYTES = 64 * 1024


def start_client_proxy(
    *,
    remote: str,
    local: str,
    workflow: str,
    write_workflow_to: str | Path | None = None,
    server_url: str | None = None,
    node_id: str,
    keypair_name: str,
    request_timeout_seconds: float = 120.0,
) -> int:
    resolved_server_url = _resolve_server_url(server_url)
    if write_workflow_to is None:
        with tempfile.TemporaryDirectory(prefix="cove-client-proxy-") as temp_dir:
            target = verify_client_proxy_target(
                remote=remote,
                local=local,
                workflow=workflow,
                bundle_root=Path(temp_dir) / "workflow",
                server_url=resolved_server_url,
                node_id=node_id,
                keypair_name=keypair_name,
            )
            return serve_verified_proxy(
                target,
                request_timeout_seconds=request_timeout_seconds,
            )

    parsed_ref = parse_published_ref(workflow)
    bundle_root = _workflow_output_root(
        Path(write_workflow_to).expanduser().resolve(),
        publisher=parsed_ref.publisher,
        workflow_id=parsed_ref.workflow_id,
    )
    target = verify_client_proxy_target(
        remote=remote,
        local=local,
        workflow=workflow,
        bundle_root=bundle_root,
        server_url=resolved_server_url,
        node_id=node_id,
        keypair_name=keypair_name,
    )
    return serve_verified_proxy(
        target,
        request_timeout_seconds=request_timeout_seconds,
    )


def verify_client_proxy_target(
    *,
    remote: str,
    local: str,
    workflow: str,
    bundle_root: str | Path,
    server_url: str | None = None,
    node_id: str,
    keypair_name: str,
) -> VerifiedProxyTarget:
    remote_url = _normalize_remote_url(remote)
    local_endpoint = _parse_local_endpoint(local)
    parsed_ref = parse_published_ref(workflow)
    resolved_server_url = _resolve_server_url(server_url)

    bundle = pull_workflow_bundle(
        server_url=resolved_server_url,
        publisher=parsed_ref.publisher,
        workflow_id=parsed_ref.workflow_id,
        reference=parsed_ref.reference,
        destination=bundle_root,
        require_publisher_signature=True,
    )
    node = _select_node(bundle, node_id=node_id)
    certificate = _download_runtime_certificate(
        server_url=resolved_server_url,
        publisher=parsed_ref.publisher,
        workflow_id=parsed_ref.workflow_id,
        node_id=node.node_id,
    )
    verified_certificate = verify_node_certificate(
        certificate,
        expected_workflow_id=parsed_ref.workflow_id,
        expected_node_name=node.node_id,
        expected_generated_node_compose_hash=node.compose_hash,
        expected_deployed_compose_text=translated_node_deployment_compose_text(
            bundle,
            node,
        ),
    )
    selected_keypair = _select_keypair_name(
        verified_certificate,
        keypair_name=keypair_name,
    )
    certificate_hash, expected_der = _load_expected_keypair_certificate(
        verified_certificate,
        keypair_name=selected_keypair,
    )

    _verify_live_tls_certificate(
        remote_url=remote_url,
        expected_certificate_der=expected_der,
        expected_certificate_hash=certificate_hash,
    )

    return VerifiedProxyTarget(
        remote_url=remote_url,
        local=local_endpoint,
        server_url=resolved_server_url,
        publisher=parsed_ref.publisher,
        workflow_id=parsed_ref.workflow_id,
        workflow_reference=parsed_ref.reference,
        bundle_root=bundle.root_path,
        manifest_hash=bundle.manifest_hash,
        node_id=node.node_id,
        node_compose_hash=node.compose_hash,
        keypair_name=selected_keypair,
        certificate_hash=certificate_hash,
        expected_certificate_der=expected_der,
    )


def serve_verified_proxy(
    target: VerifiedProxyTarget,
    *,
    request_timeout_seconds: float = 120.0,
) -> int:
    server = make_verified_proxy_server(
        target,
        request_timeout_seconds=request_timeout_seconds,
    )
    bound_host, bound_port = server.server_address[:2]
    lines = [
        "Verified Cove service endpoint",
        f"  Remote: {target.remote_url}",
        f"  Local: tcp://{bound_host}:{bound_port}",
        f"  Workflow: {target.publisher}/{target.workflow_id}/{target.workflow_reference}",
        f"  Bundle path: {target.bundle_root}",
        f"  Manifest hash: {target.manifest_hash}",
        f"  Node: {target.node_id}",
        f"  Compose hash: {target.node_compose_hash}",
        f"  TLS keypair: {target.keypair_name}",
        f"  TLS certificate hash: {target.certificate_hash}",
        "Proxy is running. Press Ctrl-C to stop.",
    ]
    if target.workflow_reference == "latest":
        lines.insert(
            5,
            "  WARNING: workflow reference 'latest' is mutable; inspect the pulled bundle before relying on it.",
        )
    print("\n".join(lines), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def make_verified_proxy_server(
    target: VerifiedProxyTarget,
    *,
    request_timeout_seconds: float = 120.0,
) -> socketserver.ThreadingTCPServer:
    handler_class = _proxy_handler_class(
        target,
        request_timeout_seconds=request_timeout_seconds,
    )
    return _ThreadingTCPProxyServer((target.local.host, target.local.port), handler_class)


def verify_node_certificate(
    certificate: dict[str, Any],
    *,
    expected_workflow_id: str | None = None,
    expected_node_name: str | None = None,
    expected_generated_node_compose_hash: str | None = None,
    expected_deployed_compose_text: str | None = None,
) -> dict[str, Any]:
    certificate_body = _required_mapping(certificate.get("certificate_body"), "certificate_body")
    attestation_bundle = _required_mapping(
        certificate.get("attestation_bundle"),
        "attestation_bundle",
    )
    certificate_body_hash = _required_string(
        certificate.get("certificate_body_hash"),
        "certificate_body_hash",
    )
    observed_body_hash = sha256_literal(canonical_json_bytes(certificate_body))
    if observed_body_hash != certificate_body_hash:
        raise ClientProxyCommandError(
            "certificate_body_hash does not match the canonical certificate body"
        )

    node_name = _required_string(certificate_body.get("node_id"), "certificate_body.node_id")
    if expected_node_name is not None and node_name != expected_node_name:
        raise ClientProxyCommandError(
            f"certificate node id {node_name!r} does not match expected node {expected_node_name!r}"
        )
    workflow_id = _required_string(
        certificate_body.get("workflow_id"),
        "certificate_body.workflow_id",
    )
    if expected_workflow_id is not None and workflow_id != expected_workflow_id:
        raise ClientProxyCommandError(
            f"certificate workflow id {workflow_id!r} does not match expected workflow {expected_workflow_id!r}"
        )

    _required_string(attestation_bundle.get("format"), "attestation_bundle.format")

    quoted_hash = _required_string(
        attestation_bundle.get("quoted_certificate_body_hash"),
        "attestation_bundle.quoted_certificate_body_hash",
    )
    if quoted_hash != certificate_body_hash:
        raise ClientProxyCommandError(
            "quoted certificate body hash does not match certificate_body_hash"
        )

    attested_node_id = _required_string(
        attestation_bundle.get("node_id"),
        "attestation_bundle.node_id",
    )
    if attested_node_id != node_name:
        raise ClientProxyCommandError(
            "attestation node_id does not match certificate body node_id"
        )

    compose_hash = _required_string(
        certificate_body.get("generated_node_compose_hash"),
        "certificate_body.generated_node_compose_hash",
    )
    attested_compose_hash = _required_string(
        attestation_bundle.get("generated_node_compose_hash"),
        "attestation_bundle.generated_node_compose_hash",
    )
    if attested_compose_hash != compose_hash:
        raise ClientProxyCommandError(
            "attestation compose hash does not match certificate body compose hash"
        )
    if (
        expected_generated_node_compose_hash is not None
        and compose_hash != expected_generated_node_compose_hash
    ):
        raise ClientProxyCommandError(
            "certificate compose hash does not match the expected generated compose hash"
        )

    report_data = build_node_certificate_report_data(
        certificate_body_hash=certificate_body_hash,
        compose_hash=compose_hash,
    )
    try:
        verify_client_attestation_bundle(
            attestation_bundle,
            expected_report_data=report_data,
            expected_compose_hash=compose_hash,
            expected_deployed_compose_text=expected_deployed_compose_text,
        )
    except RuntimeErrorBase as exc:
        raise ClientProxyCommandError(str(exc)) from exc

    return certificate


def _proxy_handler_class(
    target: VerifiedProxyTarget,
    *,
    request_timeout_seconds: float,
) -> type[socketserver.BaseRequestHandler]:
    parsed_remote = urlparse(target.remote_url)
    assert parsed_remote.hostname is not None
    remote_port = parsed_remote.port or 443

    class ProxyHandler(socketserver.BaseRequestHandler):
        request: socket.socket

        def handle(self) -> None:
            try:
                upstream = _open_pinned_tls_connection(
                    host=parsed_remote.hostname,
                    port=remote_port,
                    connect_timeout=request_timeout_seconds,
                    expected_certificate_der=target.expected_certificate_der,
                    expected_certificate_hash=target.certificate_hash,
                )
            except (OSError, ClientProxyCommandError):
                return

            try:
                _forward_tcp_bidirectional(self.request, upstream)
            finally:
                upstream.close()

    return ProxyHandler


class _ThreadingTCPProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _open_pinned_tls_connection(
    *,
    host: str,
    port: int,
    connect_timeout: float,
    expected_certificate_der: bytes,
    expected_certificate_hash: str,
) -> ssl.SSLSocket:
    raw_socket = socket.create_connection((host, port), timeout=connect_timeout)
    try:
        tls_socket = ssl._create_unverified_context().wrap_socket(
            raw_socket,
            server_hostname=host,
        )
    except Exception:
        raw_socket.close()
        raise

    served_der = tls_socket.getpeercert(binary_form=True)
    if served_der != expected_certificate_der:
        served_hash = _certificate_pem_hash_from_der(served_der)
        tls_socket.close()
        raise ClientProxyCommandError(
            "served TLS certificate does not match verified runtime certificate: "
            f"{served_hash} != {expected_certificate_hash}"
        )
    tls_socket.settimeout(None)
    return tls_socket


def _forward_tcp_bidirectional(client_socket: socket.socket, upstream: ssl.SSLSocket) -> None:
    client_to_upstream = threading.Thread(
        target=_pipe_tcp,
        args=(client_socket, upstream),
        daemon=True,
    )
    upstream_to_client = threading.Thread(
        target=_pipe_tcp,
        args=(upstream, client_socket),
        daemon=True,
    )
    client_to_upstream.start()
    upstream_to_client.start()
    client_to_upstream.join()
    upstream_to_client.join()


def _pipe_tcp(source: socket.socket, sink: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(_PROXY_BUFFER_BYTES)
            if not chunk:
                break
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        try:
            sink.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def _download_runtime_certificate(
    *,
    server_url: str,
    publisher: str,
    workflow_id: str,
    node_id: str,
) -> dict[str, Any]:
    certificate = http_get_json(
        url=(
            f"{server_url.rstrip('/')}/v1/runtime/"
            f"{publisher}/{workflow_id}/certificates/{node_id}/latest"
        ),
        timeout=30.0,
    )
    return certificate


def _select_node(
    bundle: MaterializedWorkflowBundle,
    *,
    node_id: str,
) -> MaterializedNode:
    nodes_by_id = {node.node_id: node for node in bundle.nodes}
    try:
        return nodes_by_id[node_id]
    except KeyError as exc:
        raise ClientProxyCommandError(
            f"workflow bundle does not contain node {node_id!r}"
        ) from exc


def _select_keypair_name(
    certificate: dict[str, Any],
    *,
    keypair_name: str,
) -> str:
    certificate_body = _required_mapping(certificate.get("certificate_body"), "certificate_body")
    keypairs = _required_mapping(
        certificate_body.get("ephemeral_keypairs"),
        "certificate_body.ephemeral_keypairs",
    )
    if keypair_name not in keypairs:
        raise ClientProxyCommandError(
            f"runtime certificate does not contain keypair {keypair_name!r}"
        )
    return keypair_name


def _load_expected_keypair_certificate(
    certificate: dict[str, Any],
    *,
    keypair_name: str,
) -> tuple[str, bytes]:
    certificate_body = _required_mapping(certificate.get("certificate_body"), "certificate_body")
    keypairs = _required_mapping(
        certificate_body.get("ephemeral_keypairs"),
        "certificate_body.ephemeral_keypairs",
    )
    keypair = _required_mapping(
        keypairs.get(keypair_name),
        f"certificate_body.ephemeral_keypairs.{keypair_name}",
    )
    certificate_pem = _required_pem_string(
        keypair.get("certificate_pem"),
        f"certificate_body.ephemeral_keypairs.{keypair_name}.certificate_pem",
    )
    certificate_hash = _required_string(
        keypair.get("certificate_hash"),
        f"certificate_body.ephemeral_keypairs.{keypair_name}.certificate_hash",
    )
    observed_hash = sha256_literal(certificate_pem)
    if observed_hash != certificate_hash:
        raise ClientProxyCommandError(
            "keypair certificate_hash does not match certificate_pem"
        )
    try:
        parsed_certificate = x509.load_pem_x509_certificate(certificate_pem)
    except ValueError as exc:
        raise ClientProxyCommandError("keypair certificate_pem is not a valid X.509 certificate") from exc
    return certificate_hash, parsed_certificate.public_bytes(serialization.Encoding.DER)


def _certificate_pem_hash_from_der(certificate_der: bytes) -> str:
    try:
        certificate = x509.load_der_x509_certificate(certificate_der)
    except ValueError:
        return sha256_literal(certificate_der)
    return sha256_literal(certificate.public_bytes(serialization.Encoding.PEM))


def _verify_live_tls_certificate(
    *,
    remote_url: str,
    expected_certificate_der: bytes,
    expected_certificate_hash: str,
) -> None:
    parsed = urlparse(remote_url)
    if parsed.hostname is None:
        raise ClientProxyCommandError("remote URL must include a hostname")
    port = parsed.port or 443
    try:
        connection = _open_pinned_tls_connection(
            host=parsed.hostname,
            port=port,
            connect_timeout=30.0,
            expected_certificate_der=expected_certificate_der,
            expected_certificate_hash=expected_certificate_hash,
        )
    except OSError as exc:
        raise ClientProxyCommandError(f"failed to connect to remote endpoint: {exc}") from exc
    connection.close()


def _normalize_remote_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ClientProxyCommandError("--remote must be an https URL")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ClientProxyCommandError("--remote must be an https origin URL")
    return value.strip().rstrip("/")


def _parse_local_endpoint(value: str) -> LocalEndpoint:
    raw_value = value.strip()
    if "://" in raw_value:
        parsed = urlparse(raw_value)
        if parsed.scheme != "tcp" or not parsed.hostname or parsed.port is None:
            raise ClientProxyCommandError("--local URL must use tcp://<host>:<port>")
        return LocalEndpoint(host=parsed.hostname, port=parsed.port)

    if ":" not in raw_value:
        raise ClientProxyCommandError("--local must use <host>:<port>")
    host, raw_port = raw_value.rsplit(":", 1)
    if not host:
        raise ClientProxyCommandError("--local host must not be empty")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ClientProxyCommandError("--local port must be an integer") from exc
    if port < 0 or port > 65535:
        raise ClientProxyCommandError("--local port must be between 0 and 65535")
    return LocalEndpoint(host=host, port=port)


def _resolve_server_url(server_url: str | None) -> str:
    if server_url is not None and server_url.strip():
        return server_url.strip().rstrip("/")
    return DEFAULT_COVEHUB_SERVER_URL


def _workflow_output_root(base: Path, *, publisher: str, workflow_id: str) -> Path:
    return base / publisher / workflow_id


def _required_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ClientProxyCommandError(f"{label} must be an object")
    return value


def _required_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ClientProxyCommandError(f"{label} must be a non-empty string")
    return value.strip()


def _required_pem_string(value: Any, label: str) -> bytes:
    if not isinstance(value, str) or not value.strip():
        raise ClientProxyCommandError(f"{label} must be a non-empty string")
    return value.encode("utf-8")
