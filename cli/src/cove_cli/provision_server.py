from __future__ import annotations

import base64
import json
import ssl
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from .attestation import (
    build_key_release_report_data,
    verify_attestation_bundle,
)
from .common import RuntimeErrorBase, canonical_json_bytes
from .owner_identity import (
    OWNER_RESPONSE_SIGNATURE_ALGORITHM_FIELD,
    OWNER_RESPONSE_SIGNATURE_FIELD,
    owner_response_signature_payload,
)

from .canonical_images import is_canonical_artifact_provisioner_digest
from .artifact_crypto import ENCRYPTION_ALGORITHM, load_artifact_key
from .provisioning_identity import (
    build_owner_identity_document,
    ensure_owner_tls_material,
    normalize_owner_server_url,
    owner_domain_from_url,
)
from .provision_state import ProvisionState


DEFAULT_PROVISION_PORT = 9000


class ProvisionServerError(RuntimeError):
    """Raised when the local provision server cannot start."""


class QuoteVerificationError(ValueError):
    """Raised when a key-release attestation payload is invalid."""


class ProvisioningHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        state: ProvisionState,
        keys_dir: Path,
        owner_domain: str,
        quote_verifier_mode: str,
        owner_identity: dict[str, object],
        owner_private_key_path: Path,
    ) -> None:
        super().__init__(server_address, ProvisioningRequestHandler)
        self.state = state
        self.keys_dir = keys_dir
        self.owner_domain = owner_domain
        self.quote_verifier_mode = quote_verifier_mode
        self.owner_identity = owner_identity
        self.owner_private_key_path = owner_private_key_path


class ProvisioningRequestHandler(BaseHTTPRequestHandler):
    server: ProvisioningHTTPServer

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != "/v1/artifacts/key-release":
            self._write_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        body = self.rfile.read(content_length)
        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            detail = "request body must be valid JSON"
            self._log_key_release_event(
                "bad_request",
                status=HTTPStatus.BAD_REQUEST.value,
                detail=detail,
            )
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"detail": detail},
            )
            return
        if not isinstance(payload, dict):
            detail = "request body must be a JSON object"
            self._log_key_release_event(
                "bad_request",
                status=HTTPStatus.BAD_REQUEST.value,
                detail=detail,
            )
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"detail": detail},
            )
            return

        try:
            hub_path = _required_payload_string(payload, "hub_path")
            workflow_publisher_domain = _required_payload_string(
                payload,
                "workflow_publisher_domain",
            )
            workflow_id = _required_payload_string(payload, "workflow_id")
            node_id = _required_payload_string(payload, "node_id")
            compose_hash = _required_payload_string(payload, "compose_hash")
            artifact_provisioner_image = _required_payload_string(
                payload,
                "artifact_provisioner_image",
            )
            artifact_provisioner_digest = _parse_digest_from_image_reference(
                artifact_provisioner_image
            )
        except ValueError as exc:
            self._log_key_release_event(
                "bad_request",
                status=HTTPStatus.BAD_REQUEST.value,
                detail=str(exc),
            )
            self._write_json(
                HTTPStatus.BAD_REQUEST,
                {"detail": str(exc)},
            )
            return

        request_context = {
            "hub_path": hub_path,
            "workflow_publisher_domain": workflow_publisher_domain,
            "workflow_id": workflow_id,
            "node_id": node_id,
            "compose_hash": compose_hash,
            "artifact_provisioner_digest": artifact_provisioner_digest,
        }
        self._log_key_release_event(
            "request",
            **request_context,
        )
        if not is_canonical_artifact_provisioner_digest(artifact_provisioner_digest):
            detail = "artifact provisioner digest is not canonical"
            self._log_key_release_event(
                "denied",
                status=HTTPStatus.FORBIDDEN.value,
                detail=detail,
                **request_context,
            )
            self._write_json(
                HTTPStatus.FORBIDDEN,
                {"detail": detail},
            )
            return

        attestation = payload.get("attestation")
        if not isinstance(attestation, dict):
            detail = "attestation payload is required"
            self._log_key_release_event(
                "denied",
                status=HTTPStatus.UNAUTHORIZED.value,
                detail=detail,
                **request_context,
            )
            self._write_json(
                HTTPStatus.UNAUTHORIZED,
                {"detail": detail},
            )
            return
        try:
            _verify_key_release_attestation(
                attestation,
                mode=self.server.quote_verifier_mode,
                expected_report_data=build_key_release_report_data(
                    workflow_publisher_domain=workflow_publisher_domain,
                    workflow_id=workflow_id,
                    node_id=node_id,
                    compose_hash=compose_hash,
                    artifact_provisioner_digest=artifact_provisioner_digest,
                ),
            )
        except QuoteVerificationError as exc:
            self._log_key_release_event(
                "denied",
                status=HTTPStatus.UNAUTHORIZED.value,
                detail=str(exc),
                **request_context,
            )
            self._write_json(
                HTTPStatus.UNAUTHORIZED,
                {"detail": str(exc)},
            )
            return

        allow_rule = self.server.state.get_allow_rule(
            hub_path=hub_path,
            publisher=workflow_publisher_domain,
            workflow_id=workflow_id,
            node_id=node_id,
            compose_hash=compose_hash,
            artifact_provisioner_digest=artifact_provisioner_digest,
        )
        if allow_rule is None:
            detail = "no matching allow rule for this node request"
            self._log_key_release_event(
                "denied",
                status=HTTPStatus.FORBIDDEN.value,
                detail=detail,
                **request_context,
            )
            self._write_json(
                HTTPStatus.FORBIDDEN,
                {"detail": detail},
            )
            return

        artifact = self.server.state.get_registered_artifact(hub_path)
        dynamic_channel = None if artifact is not None else self.server.state.get_dynamic_artifact_channel(hub_path)
        if artifact is None and dynamic_channel is None:
            detail = "artifact not found"
            self._log_key_release_event(
                "denied",
                status=HTTPStatus.NOT_FOUND.value,
                detail=detail,
                **request_context,
            )
            self._write_json(
                HTTPStatus.NOT_FOUND,
                {"detail": detail},
            )
            return

        try:
            key_bytes = load_artifact_key(
                keys_dir=self.server.keys_dir,
                artifact_id=artifact.key_path if artifact is not None else dynamic_channel.key_path,
            )
        except Exception as exc:
            self._log_key_release_event(
                "error",
                status=HTTPStatus.INTERNAL_SERVER_ERROR.value,
                detail=str(exc),
                **request_context,
            )
            self._write_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"detail": str(exc)},
            )
            return

        self._log_key_release_event(
            "success",
            status=HTTPStatus.OK.value,
            artifact_id=artifact.artifact_id if artifact is not None else dynamic_channel.artifact_name,
            artifact_kind="static" if artifact is not None else "dynamic",
            **request_context,
        )
        self._write_json(
            HTTPStatus.OK,
            _signed_key_release_payload(
                _key_release_payload(artifact, dynamic_channel, key_bytes),
                owner_private_key_path=self.server.owner_private_key_path,
            ),
        )

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._write_json(HTTPStatus.OK, {"ok": True})
            return

        if parsed.path == "/identity":
            self._write_json(HTTPStatus.OK, self.server.owner_identity)
            return

        if parsed.path == "/v1/artifacts/by-hub-path":
            hub_path = parse_qs(parsed.query).get("hub_path", [None])[0]
            if not isinstance(hub_path, str) or not hub_path:
                self._write_json(
                    HTTPStatus.BAD_REQUEST,
                    {"detail": "hub_path query parameter is required"},
                )
                return

            artifact = self.server.state.get_registered_artifact(hub_path)
            if artifact is None:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"detail": "artifact not found"},
                )
                return

            try:
                key_bytes = load_artifact_key(
                    keys_dir=self.server.keys_dir,
                    artifact_id=artifact.key_path,
                )
            except Exception as exc:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    {"detail": str(exc)},
                )
                return

            self._write_json(
                HTTPStatus.OK,
                _signed_key_release_payload(
                    _key_release_payload(artifact, None, key_bytes),
                    owner_private_key_path=self.server.owner_private_key_path,
                ),
            )
            return

        self._write_json(HTTPStatus.NOT_FOUND, {"detail": "not found"})

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return None

    def _write_json(self, status_code: HTTPStatus, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _log_key_release_event(self, event: str, **fields: object) -> None:
        remote_host = self.client_address[0] if self.client_address else None
        payload = {
            "event": event,
            "owner_domain": self.server.owner_domain,
            "remote": remote_host,
            **fields,
        }
        print(
            "cove key-release " + json.dumps(payload, sort_keys=True),
            file=sys.stderr,
            flush=True,
        )


def create_provision_server(
    *,
    state: ProvisionState,
    keys_dir: Path,
    cert_path: Path,
    tls_key_path: Path,
    owner_private_key_path: Path | None = None,
    owner_public_key_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PROVISION_PORT,
    owner_domain: str | None = None,
    owner_url: str | None = None,
    quote_verifier_mode: str = "phala_dstack",
) -> ProvisioningHTTPServer:
    if quote_verifier_mode != "phala_dstack":
        raise ProvisionServerError(
            "quote verifier mode must be 'phala_dstack'; mock verifier modes are not supported"
        )
    effective_server_url = normalize_owner_server_url(owner_url)
    effective_owner_domain = owner_domain_from_url(effective_server_url)
    if owner_domain is not None and owner_domain != effective_owner_domain:
        raise ProvisionServerError(
            f"owner_domain {owner_domain!r} does not match owner URL domain {effective_owner_domain!r}"
        )
    owner_private_key_path = owner_private_key_path or cert_path.parent / "owner-signing-private.pem"
    owner_public_key_path = owner_public_key_path or cert_path.parent / "owner-signing-public.pem"
    ensure_owner_tls_material(
        cert_path=cert_path,
        tls_key_path=tls_key_path,
        owner_private_key_path=owner_private_key_path,
        owner_public_key_path=owner_public_key_path,
        owner_url=effective_server_url,
    )
    owner_identity = build_owner_identity_document(
        owner_url=effective_server_url,
        owner_private_key_path=owner_private_key_path,
        owner_public_key_path=owner_public_key_path,
    )
    server = ProvisioningHTTPServer(
        (host, port),
        state,
        keys_dir,
        effective_owner_domain,
        quote_verifier_mode,
        owner_identity,
        owner_private_key_path,
    )
    ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_context.load_cert_chain(certfile=cert_path, keyfile=tls_key_path)
    server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    return server


def _required_payload_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _parse_digest_from_image_reference(image_reference: str) -> str:
    name, separator, digest = image_reference.rpartition("@")
    if not separator or not name or not digest.startswith("sha256:"):
        raise ValueError("artifact_provisioner_image must be digest-pinned")
    return digest


def ensure_provision_tls_material(
    *,
    cert_path: Path,
    tls_key_path: Path,
    owner_url: str,
) -> None:
    ensure_owner_tls_material(
        cert_path=cert_path,
        tls_key_path=tls_key_path,
        owner_private_key_path=cert_path.parent / "owner-signing-private.pem",
        owner_public_key_path=cert_path.parent / "owner-signing-public.pem",
        owner_url=owner_url,
    )


def _verify_key_release_attestation(
    attestation: dict[str, object],
    *,
    mode: str,
    expected_report_data: bytes,
) -> None:
    if mode != "phala_dstack":
        raise QuoteVerificationError(f"unsupported quote verifier mode: {mode}")
    try:
        verify_attestation_bundle(
            attestation,
            expected_report_data=expected_report_data,
        )
    except RuntimeErrorBase as exc:
        raise QuoteVerificationError(str(exc)) from exc


def _required_non_empty_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuoteVerificationError(f"{label} must be a non-empty string")
    return value.strip()


def _key_release_payload(
    artifact,
    dynamic_channel,
    key_bytes: bytes,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "hub_path": artifact.hub_path if artifact is not None else dynamic_channel.hub_path,
        "artifact_id": artifact.artifact_id if artifact is not None else dynamic_channel.artifact_name,
        "key_path": artifact.key_path if artifact is not None else dynamic_channel.key_path,
        "key_b64": base64.b64encode(key_bytes).decode("ascii"),
        "encryption_algorithm": ENCRYPTION_ALGORITHM,
        "transport_mode": "encrypted",
        "updated_at": artifact.updated_at if artifact is not None else dynamic_channel.updated_at,
    }
    if artifact is not None:
        payload.update(
            {
                "owner_domain": artifact.owner_domain,
                "owner_url": artifact.owner_url,
                "plaintext_hash": artifact.plaintext_hash,
                "ciphertext_hash": artifact.ciphertext_hash,
                "content_type": artifact.content_type,
                "source_path": artifact.source_path,
                "server_url": artifact.server_url,
            }
        )
    else:
        payload.update(
            {
                "owner_domain": dynamic_channel.owner_domain,
                "publisher": dynamic_channel.publisher,
                "workflow_id": dynamic_channel.workflow_id,
            }
        )
    return payload


def _signed_key_release_payload(
    payload: dict[str, object],
    *,
    owner_private_key_path: Path,
) -> dict[str, object]:
    owner_private_key = _load_owner_private_key(owner_private_key_path)
    signed_payload = {
        **payload,
        OWNER_RESPONSE_SIGNATURE_ALGORITHM_FIELD: "ed25519",
    }
    signature = owner_private_key.sign(
        canonical_json_bytes(owner_response_signature_payload(signed_payload))
    )
    signed_payload[OWNER_RESPONSE_SIGNATURE_FIELD] = base64.b64encode(signature).decode("ascii")
    return signed_payload


def _load_owner_private_key(path: Path) -> ed25519.Ed25519PrivateKey:
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ProvisionServerError(f"owner private key at {path} must be Ed25519")
    return key
