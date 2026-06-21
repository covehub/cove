from __future__ import annotations

import http.client
import json
import ssl
import tempfile
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .attestation import build_node_certificate_report_data, verify_attestation_bundle
from .common import RuntimeErrorBase, canonical_json_bytes, http_get_json, sha256_literal
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


@dataclass(frozen=True, slots=True)
class _WorkflowHints:
    long_running_keypair_nodes: dict[str, set[str]]
    keypair_nodes: dict[str, set[str]]


_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def start_client_proxy(
    *,
    remote: str,
    local: str,
    workflow: str,
    write_workflow_to: str | Path | None = None,
    server_url: str | None = None,
    node_id: str | None = None,
    keypair_name: str | None = None,
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
    node_id: str | None = None,
    keypair_name: str | None = None,
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
    )
    node = _select_node(bundle, requested_node_id=node_id)
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
    )
    selected_keypair = _select_keypair_name(
        verified_certificate,
        bundle_root=bundle.root_path,
        node_id=node.node_id,
        requested_keypair_name=keypair_name,
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
        f"  Local: http://{bound_host}:{bound_port}",
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
) -> ThreadingHTTPServer:
    handler_class = _proxy_handler_class(
        target,
        request_timeout_seconds=request_timeout_seconds,
    )
    return ThreadingHTTPServer((target.local.host, target.local.port), handler_class)


def verify_node_certificate(
    certificate: dict[str, Any],
    *,
    expected_workflow_id: str | None = None,
    expected_node_name: str | None = None,
    expected_generated_node_compose_hash: str | None = None,
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
        verify_attestation_bundle(
            attestation_bundle,
            expected_report_data=report_data,
        )
    except RuntimeErrorBase as exc:
        raise ClientProxyCommandError(str(exc)) from exc

    return certificate


def _proxy_handler_class(
    target: VerifiedProxyTarget,
    *,
    request_timeout_seconds: float,
) -> type[BaseHTTPRequestHandler]:
    parsed_remote = urlparse(target.remote_url)
    assert parsed_remote.hostname is not None
    remote_port = parsed_remote.port or 443
    remote_base_path = parsed_remote.path.rstrip("/")

    class ProxyHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_DELETE(self) -> None:  # noqa: N802
            self._forward()

        def do_GET(self) -> None:  # noqa: N802
            self._forward()

        def do_HEAD(self) -> None:  # noqa: N802
            self._forward()

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._forward()

        def do_PATCH(self) -> None:  # noqa: N802
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

        def do_PUT(self) -> None:  # noqa: N802
            self._forward()

        def _forward(self) -> None:
            if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                self._send_json_error(501, "chunked local requests are not supported")
                return

            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length) if content_length else None
            path = _join_remote_request_path(remote_base_path, self.path)
            headers = _forward_request_headers(self.headers, host=parsed_remote.netloc)
            connection = _PinnedHTTPSConnection(
                parsed_remote.hostname,
                remote_port,
                expected_certificate_der=target.expected_certificate_der,
                expected_certificate_hash=target.certificate_hash,
                timeout=request_timeout_seconds,
            )
            try:
                connection.request(self.command, path, body=body, headers=headers)
                response = connection.getresponse()
                payload = response.read()
            except (OSError, http.client.HTTPException, ClientProxyCommandError) as exc:
                connection.close()
                self._send_json_error(502, f"remote proxy request failed: {exc}")
                return

            self.send_response(response.status, response.reason)
            for key, value in response.getheaders():
                if key.lower() in _HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
            connection.close()

        def _send_json_error(self, status_code: int, message: str) -> None:
            payload = json.dumps({"detail": message}).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return ProxyHandler


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self,
        host: str,
        port: int,
        *,
        expected_certificate_der: bytes,
        expected_certificate_hash: str,
        timeout: float,
    ) -> None:
        super().__init__(
            host,
            port=port,
            timeout=timeout,
            context=ssl._create_unverified_context(),
        )
        self._expected_certificate_der = expected_certificate_der
        self._expected_certificate_hash = expected_certificate_hash

    def connect(self) -> None:
        super().connect()
        assert self.sock is not None
        served_der = self.sock.getpeercert(binary_form=True)
        if served_der != self._expected_certificate_der:
            served_hash = _certificate_pem_hash_from_der(served_der)
            self.close()
            raise ClientProxyCommandError(
                "served TLS certificate does not match verified runtime certificate: "
                f"{served_hash} != {self._expected_certificate_hash}"
            )


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
    requested_node_id: str | None,
) -> MaterializedNode:
    nodes_by_id = {node.node_id: node for node in bundle.nodes}
    if requested_node_id is not None:
        try:
            return nodes_by_id[requested_node_id]
        except KeyError as exc:
            raise ClientProxyCommandError(
                f"workflow bundle does not contain node {requested_node_id!r}"
            ) from exc

    hints = _load_workflow_hints(bundle.root_path)
    long_running_candidates = [
        nodes_by_id[node_id]
        for node_id in sorted(hints.long_running_keypair_nodes)
        if node_id in nodes_by_id
    ]
    if len(long_running_candidates) == 1:
        return long_running_candidates[0]
    keypair_candidates = [
        nodes_by_id[node_id]
        for node_id in sorted(hints.keypair_nodes)
        if node_id in nodes_by_id
    ]
    if len(keypair_candidates) == 1:
        return keypair_candidates[0]
    if len(bundle.nodes) == 1:
        return bundle.nodes[0]

    if long_running_candidates:
        node_list = ", ".join(node.node_id for node in long_running_candidates)
        raise ClientProxyCommandError(
            f"multiple long-running keypair nodes found ({node_list}); pass --node"
        )
    raise ClientProxyCommandError("could not infer serving node; pass --node")


def _select_keypair_name(
    certificate: dict[str, Any],
    *,
    bundle_root: Path,
    node_id: str,
    requested_keypair_name: str | None,
) -> str:
    certificate_body = _required_mapping(certificate.get("certificate_body"), "certificate_body")
    keypairs = _required_mapping(
        certificate_body.get("ephemeral_keypairs"),
        "certificate_body.ephemeral_keypairs",
    )
    if requested_keypair_name is not None:
        if requested_keypair_name not in keypairs:
            raise ClientProxyCommandError(
                f"runtime certificate does not contain keypair {requested_keypair_name!r}"
            )
        return requested_keypair_name

    hints = _load_workflow_hints(bundle_root)
    hinted_names = sorted(hints.keypair_nodes.get(node_id, set()))
    hinted_present = [name for name in hinted_names if name in keypairs]
    if len(hinted_present) == 1:
        return hinted_present[0]

    if len(keypairs) == 1:
        keypair_name = next(iter(keypairs))
        if not isinstance(keypair_name, str):  # pragma: no cover - dict keys from JSON
            raise ClientProxyCommandError("runtime certificate keypair name must be a string")
        return keypair_name

    keypair_list = ", ".join(str(name) for name in keypairs)
    raise ClientProxyCommandError(
        f"could not infer TLS keypair from certificate ({keypair_list}); pass --keypair"
    )


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
    connection = _PinnedHTTPSConnection(
        parsed.hostname,
        port,
        expected_certificate_der=expected_certificate_der,
        expected_certificate_hash=expected_certificate_hash,
        timeout=30.0,
    )
    try:
        connection.connect()
    except OSError as exc:
        raise ClientProxyCommandError(f"failed to connect to remote endpoint: {exc}") from exc
    finally:
        connection.close()


def _load_workflow_hints(bundle_root: Path) -> _WorkflowHints:
    workflow_path = bundle_root / "workflow.normalized.cove.yaml"
    if not workflow_path.is_file():
        return _WorkflowHints(long_running_keypair_nodes={}, keypair_nodes={})
    try:
        payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ClientProxyCommandError(f"failed to parse pulled workflow at {workflow_path}: {exc}") from exc
    if not isinstance(payload, dict):
        return _WorkflowHints(long_running_keypair_nodes={}, keypair_nodes={})
    raw_nodes = payload.get("nodes")
    if not isinstance(raw_nodes, dict):
        return _WorkflowHints(long_running_keypair_nodes={}, keypair_nodes={})

    long_running_keypair_nodes: dict[str, set[str]] = {}
    keypair_nodes: dict[str, set[str]] = {}
    for node_id, raw_node in raw_nodes.items():
        if not isinstance(node_id, str) or not isinstance(raw_node, dict):
            continue
        raw_services = raw_node.get("services")
        if not isinstance(raw_services, dict):
            continue
        for raw_service in raw_services.values():
            if not isinstance(raw_service, dict):
                continue
            keypairs = _string_list(raw_service.get("ephemeral_keypairs"))
            if not keypairs:
                continue
            keypair_nodes.setdefault(node_id, set()).update(keypairs)
            if raw_service.get("should_terminate") is False:
                long_running_keypair_nodes.setdefault(node_id, set()).update(keypairs)
    return _WorkflowHints(
        long_running_keypair_nodes=long_running_keypair_nodes,
        keypair_nodes=keypair_nodes,
    )


def _forward_request_headers(headers: Any, *, host: str) -> dict[str, str]:
    forwarded = {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS and key.lower() != "host"
    }
    forwarded["Host"] = host
    return forwarded


def _join_remote_request_path(remote_base_path: str, local_path: str) -> str:
    if not remote_base_path:
        return local_path
    if local_path == "/":
        return f"{remote_base_path}/"
    if local_path.startswith("/"):
        return f"{remote_base_path}{local_path}"
    return f"{remote_base_path}/{local_path}"


def _normalize_remote_url(value: str) -> str:
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ClientProxyCommandError("--remote must be an https URL")
    if parsed.params or parsed.query or parsed.fragment:
        raise ClientProxyCommandError("--remote must be an https origin or base path URL")
    return value.strip().rstrip("/")


def _parse_local_endpoint(value: str) -> LocalEndpoint:
    raw_value = value.strip()
    if "://" in raw_value:
        parsed = urlparse(raw_value)
        if parsed.scheme != "http" or not parsed.hostname or parsed.port is None:
            raise ClientProxyCommandError("--local URL must use http://<host>:<port>")
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


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


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
