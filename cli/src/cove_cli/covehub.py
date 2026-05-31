from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key


COVEHUB_USER_AGENT = "cove-cli/0.0.1"
_REQUEST_TIMEOUT_SECONDS = 5
WRITE_AUTH_PURPOSE = "covehub_domain_write_v1"
_CHUNKED_UPLOAD_THRESHOLD_BYTES = 64 * 1024 * 1024
_CHUNKED_UPLOAD_CHUNK_SIZE_BYTES = 8 * 1024 * 1024


class CovehubError(RuntimeError):
    """Raised when a Covehub request fails."""


@dataclass(frozen=True, slots=True)
class ObjectUploadResult:
    status_code: int
    digest: str
    hub_path: str
    latest_hub_path: str


def upload_named_artifact(
    *,
    server_url: str,
    owner_domain: str,
    artifact_name: str,
    owner_identity: dict[str, object],
    owner_private_key_path: Path,
    payload: bytes,
    overwrite: bool = False,  # retained for CLI flag compatibility
) -> ObjectUploadResult:
    del overwrite
    digest = _sha256_literal(payload)
    hub_path = f"v1/artifacts/{owner_domain}/{artifact_name}/{digest}"
    latest_hub_path = f"v1/artifacts/{owner_domain}/{artifact_name}/latest"
    return _upload_typed_object(
        label="artifact",
        server_url=server_url,
        owner_domain=owner_domain,
        owner_identity=owner_identity,
        owner_private_key_path=owner_private_key_path,
        payload=payload,
        hub_path=hub_path,
        latest_hub_path=latest_hub_path,
        content_type="application/octet-stream",
    )


def upload_workflow_object(
    *,
    server_url: str,
    publisher: str,
    workflow_id: str,
    owner_identity: dict[str, object],
    owner_private_key_path: Path,
    payload: bytes,
    overwrite: bool = False,  # retained for CLI flag compatibility
) -> ObjectUploadResult:
    del overwrite
    digest = _sha256_literal(payload)
    hub_path = f"v1/workflows/{publisher}/{workflow_id}/{digest}"
    latest_hub_path = f"v1/workflows/{publisher}/{workflow_id}/latest"
    return _upload_typed_object(
        label="workflow",
        server_url=server_url,
        owner_domain=publisher,
        owner_identity=owner_identity,
        owner_private_key_path=owner_private_key_path,
        payload=payload,
        hub_path=hub_path,
        latest_hub_path=latest_hub_path,
        content_type="application/json",
    )


def download_workflow_object(
    *,
    server_url: str,
    publisher: str,
    workflow_id: str,
    reference: str = "latest",
) -> bytes:
    request = urllib_request.Request(
        _join_url(server_url, f"/v1/workflows/{publisher}/{workflow_id}/{reference}"),
        headers={"Accept": "application/octet-stream"},
        method="GET",
    )
    return _bytes_request(request)


def delete_runtime_state(
    *,
    server_url: str,
    publisher: str,
    workflow_id: str,
    owner_identity: dict[str, object],
    owner_private_key_path: Path,
) -> int:
    request_path = f"/v1/runtime/{publisher}/{workflow_id}"
    request = urllib_request.Request(
        _join_url(server_url, request_path),
        headers=signed_write_headers(
            method="DELETE",
            path=request_path,
            payload=b"",
            owner_domain=publisher,
            owner_identity=owner_identity,
            owner_private_key_path=owner_private_key_path,
        ),
        method="DELETE",
    )
    try:
        with _urlopen(request) as response:
            return int(response.status)
    except urllib_error.HTTPError as exc:
        detail = _extract_error_detail(exc)
        raise CovehubError(
            f"runtime-state delete failed with HTTP {exc.code}: {detail}"
        ) from exc
    except urllib_error.URLError as exc:
        raise CovehubError(
            f"failed to reach Covehub server during runtime-state delete: {exc.reason}"
        ) from exc


def _upload_typed_object(
    *,
    label: str,
    server_url: str,
    owner_domain: str,
    owner_identity: dict[str, object],
    owner_private_key_path: Path,
    payload: bytes,
    hub_path: str,
    latest_hub_path: str,
    content_type: str,
) -> ObjectUploadResult:
    try:
        if len(payload) < _CHUNKED_UPLOAD_THRESHOLD_BYTES:
            status_code = _direct_put_typed_object(
                server_url=server_url,
                owner_domain=owner_domain,
                owner_identity=owner_identity,
                owner_private_key_path=owner_private_key_path,
                payload=payload,
                hub_path=hub_path,
                content_type=content_type,
            )
        else:
            status_code = _chunked_put_typed_object(
                server_url=server_url,
                owner_domain=owner_domain,
                owner_identity=owner_identity,
                owner_private_key_path=owner_private_key_path,
                payload=payload,
                hub_path=hub_path,
                content_type=content_type,
            )
    except urllib_error.HTTPError as exc:
        detail = _extract_error_detail(exc)
        raise CovehubError(
            f"{label} upload failed with HTTP {exc.code}: {detail}"
        ) from exc
    except urllib_error.URLError as exc:
        raise CovehubError(
            f"failed to reach Covehub server during {label} upload: {exc.reason}"
        ) from exc

    return ObjectUploadResult(
        status_code=status_code,
        digest=_sha256_literal(payload),
        hub_path=hub_path,
        latest_hub_path=latest_hub_path,
    )


def _direct_put_typed_object(
    *,
    server_url: str,
    owner_domain: str,
    owner_identity: dict[str, object],
    owner_private_key_path: Path,
    payload: bytes,
    hub_path: str,
    content_type: str,
) -> int:
    request_path = f"/{hub_path}"
    request = urllib_request.Request(
        _join_url(server_url, request_path),
        data=payload,
        headers={
            "Content-Type": content_type,
            **signed_write_headers(
                method="PUT",
                path=request_path,
                payload=payload,
                owner_domain=owner_domain,
                owner_identity=owner_identity,
                owner_private_key_path=owner_private_key_path,
            ),
        },
        method="PUT",
    )
    with _urlopen(request) as response:
        return int(response.status)


def _chunked_put_typed_object(
    *,
    server_url: str,
    owner_domain: str,
    owner_identity: dict[str, object],
    owner_private_key_path: Path,
    payload: bytes,
    hub_path: str,
    content_type: str,
) -> int:
    session_request_path = f"/{hub_path}/upload-session"
    session_payload = _canonical_json_bytes({"upload_length": len(payload)})
    session_request = urllib_request.Request(
        _join_url(server_url, session_request_path),
        data=session_payload,
        headers={
            "Content-Type": "application/json",
            **signed_write_headers(
                method="POST",
                path=session_request_path,
                payload=session_payload,
                owner_domain=owner_domain,
                owner_identity=owner_identity,
                owner_private_key_path=owner_private_key_path,
            ),
        },
        method="POST",
    )
    session_response = _json_request(session_request)
    session_id = _required_string(session_response.get("session_id"), "session_id")
    offset = _required_int(session_response.get("offset"), "offset")

    while offset < len(payload):
        chunk = payload[offset : offset + _CHUNKED_UPLOAD_CHUNK_SIZE_BYTES]
        request = urllib_request.Request(
            _join_url(server_url, f"/v1/uploads/sessions/{session_id}"),
            data=chunk,
            headers={
                "Content-Type": content_type,
                "X-Cove-Upload-Offset": str(offset),
            },
            method="PUT",
        )
        response_payload = _json_request(request)
        next_offset = _required_int(response_payload.get("offset"), "offset")
        if next_offset <= offset:
            raise CovehubError(
                f"chunked upload session {session_id} did not advance offset"
            )
        offset = next_offset

    complete_request = urllib_request.Request(
        _join_url(server_url, f"/v1/uploads/sessions/{session_id}/complete"),
        data=b"",
        method="POST",
    )
    with _urlopen(complete_request) as response:
        return int(response.status)


def _json_request(request: urllib_request.Request) -> dict[str, Any]:
    body = _bytes_request(request)
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise CovehubError("server returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CovehubError("server returned a non-object JSON payload")
    return payload


def _bytes_request(request: urllib_request.Request) -> bytes:
    try:
        with _urlopen(request) as response:
            return response.read()
    except urllib_error.HTTPError as exc:
        detail = _extract_error_detail(exc)
        raise CovehubError(f"request failed with HTTP {exc.code}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise CovehubError(f"failed to reach Covehub server: {exc.reason}") from exc


def signed_write_headers(
    *,
    method: str,
    path: str,
    payload: bytes,
    owner_domain: str,
    owner_identity: dict[str, object],
    owner_private_key_path: Path,
) -> dict[str, str]:
    timestamp = _format_utc(datetime.now(timezone.utc))
    owner_identity_bytes = _canonical_json_bytes(owner_identity)
    owner_identity_hash = _sha256_literal(owner_identity_bytes)
    signature_payload = {
        "purpose": WRITE_AUTH_PURPOSE,
        "method": method.upper(),
        "path": path,
        "payload_hash": _sha256_literal(payload),
        "owner_domain": owner_domain,
        "owner_identity_hash": owner_identity_hash,
        "timestamp": timestamp,
    }
    private_key = load_pem_private_key(owner_private_key_path.read_bytes(), password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise CovehubError(f"owner private key at {owner_private_key_path} must be Ed25519")
    signature = private_key.sign(_canonical_json_bytes(signature_payload))
    return {
        "X-Cove-Owner-Identity": base64.urlsafe_b64encode(owner_identity_bytes)
        .decode("ascii")
        .rstrip("="),
        "X-Cove-Write-Timestamp": timestamp,
        "X-Cove-Write-Signature-Algorithm": "ed25519",
        "X-Cove-Write-Signature": base64.b64encode(signature).decode("ascii"),
    }


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00",
        "Z",
    )


def _extract_error_detail(exc: urllib_error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
    except Exception:  # pragma: no cover - extremely defensive
        return exc.reason if isinstance(exc.reason, str) else str(exc.reason)

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body or (exc.reason if isinstance(exc.reason, str) else str(exc.reason))

    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return body or (exc.reason if isinstance(exc.reason, str) else str(exc.reason))


def _required_int(value: object, label: str) -> int:
    if not isinstance(value, int):
        raise CovehubError(f"server returned invalid {label}")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise CovehubError(f"server returned invalid {label}")
    return value


def _join_url(server_url: str, suffix: str) -> str:
    return f"{server_url.rstrip('/')}{suffix}"


def _sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _urlopen(request: urllib_request.Request):
    _ensure_default_headers(request)
    return urllib_request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS)


def _ensure_default_headers(request: urllib_request.Request) -> None:
    if not request.has_header("User-agent"):
        request.add_header("User-Agent", COVEHUB_USER_AGENT)
