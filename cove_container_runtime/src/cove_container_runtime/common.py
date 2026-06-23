from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ARTIFACT_KEY_BYTES = 32
ARTIFACT_NONCE_BYTES = 12
ARTIFACT_GCM_TAG_BYTES = 16
COVE_RUNTIME_USER_AGENT = "cove-runtime/0.0.1"
_HTTP_STREAM_CHUNK_SIZE_BYTES = 8 * 1024 * 1024
_CHUNKED_UPLOAD_THRESHOLD_BYTES = 64 * 1024 * 1024
_CHUNKED_UPLOAD_CHUNK_SIZE_BYTES = 8 * 1024 * 1024


class RuntimeErrorBase(RuntimeError):
    """Raised when a runtime container fails closed."""


class SidecarConfigError(RuntimeErrorBase):
    """Raised when a generated sidecar config is malformed."""


@dataclass(frozen=True, slots=True)
class SidecarContext:
    service_name: str
    compose_hash: str
    config: dict[str, Any]


def sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def sha256_file_literal(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while True:
            chunk = handle.read(_HTTP_STREAM_CHUNK_SIZE_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def ensure_parent(path: str | Path) -> Path:
    resolved = Path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def write_json_file(path: str | Path, payload: dict[str, Any]) -> None:
    resolved = ensure_parent(path)
    resolved.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_bytes_file(path: str | Path, payload: bytes) -> None:
    resolved = ensure_parent(path)
    resolved.write_bytes(payload)


def write_text_file(path: str | Path, payload: str) -> None:
    resolved = ensure_parent(path)
    resolved.write_text(payload, encoding="utf-8")


def read_json_file(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeErrorBase(f"JSON file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeErrorBase(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeErrorBase(f"JSON file at {path} must contain an object")
    return payload


def load_inline_sidecar_context() -> SidecarContext:
    service_name = os.getenv("COVE_SERVICE_NAME")
    if not isinstance(service_name, str) or not service_name.strip():
        raise SidecarConfigError("COVE_SERVICE_NAME must be set")
    compose_hash = os.getenv("COVE_COMPOSE_HASH")
    if not isinstance(compose_hash, str) or not compose_hash.strip():
        raise SidecarConfigError("COVE_COMPOSE_HASH must be set")
    raw_config = os.getenv("COVE_CONFIG_JSON")
    if not isinstance(raw_config, str) or not raw_config.strip():
        raise SidecarConfigError("COVE_CONFIG_JSON must be a non-empty string")
    try:
        config_payload = json.loads(raw_config)
    except json.JSONDecodeError as exc:
        raise SidecarConfigError("COVE_CONFIG_JSON must be valid JSON") from exc

    return SidecarContext(
        service_name=service_name.strip(),
        compose_hash=compose_hash.strip(),
        config=required_mapping(config_payload, "COVE_CONFIG_JSON"),
    )


def decrypt_ciphertext_bytes(*, ciphertext: bytes, key_bytes: bytes) -> bytes:
    if len(key_bytes) != ARTIFACT_KEY_BYTES:
        raise RuntimeErrorBase(f"artifact key must be {ARTIFACT_KEY_BYTES} bytes")
    if len(ciphertext) <= ARTIFACT_NONCE_BYTES:
        raise RuntimeErrorBase("ciphertext blob is too short")
    nonce = ciphertext[:ARTIFACT_NONCE_BYTES]
    ciphertext_and_tag = ciphertext[ARTIFACT_NONCE_BYTES:]
    try:
        return AESGCM(key_bytes).decrypt(nonce, ciphertext_and_tag, None)
    except Exception as exc:  # pragma: no cover - library exception details are not stable
        raise RuntimeErrorBase("failed to decrypt ciphertext") from exc


def decrypt_ciphertext_file(
    *,
    ciphertext_path: str | Path,
    plaintext_path: str | Path,
    key_bytes: bytes,
) -> str:
    if len(key_bytes) != ARTIFACT_KEY_BYTES:
        raise RuntimeErrorBase(f"artifact key must be {ARTIFACT_KEY_BYTES} bytes")

    source_path = Path(ciphertext_path)
    ciphertext_size = source_path.stat().st_size
    minimum_size = ARTIFACT_NONCE_BYTES + ARTIFACT_GCM_TAG_BYTES
    if ciphertext_size <= minimum_size:
        raise RuntimeErrorBase("ciphertext blob is too short")

    digest = hashlib.sha256()
    target_path = ensure_parent(plaintext_path)
    try:
        with source_path.open("rb") as source_handle:
            nonce = source_handle.read(ARTIFACT_NONCE_BYTES)
            source_handle.seek(ciphertext_size - ARTIFACT_GCM_TAG_BYTES)
            tag = source_handle.read(ARTIFACT_GCM_TAG_BYTES)
            source_handle.seek(ARTIFACT_NONCE_BYTES)

            decryptor = Cipher(
                algorithms.AES(key_bytes),
                modes.GCM(nonce, tag),
            ).decryptor()
            remaining = ciphertext_size - minimum_size

            with target_path.open("wb") as target_handle:
                while remaining > 0:
                    chunk = source_handle.read(min(_HTTP_STREAM_CHUNK_SIZE_BYTES, remaining))
                    if not chunk:
                        raise RuntimeErrorBase("ciphertext truncated during streaming decrypt")
                    remaining -= len(chunk)
                    plaintext_chunk = decryptor.update(chunk)
                    if plaintext_chunk:
                        target_handle.write(plaintext_chunk)
                        digest.update(plaintext_chunk)

                final_chunk = decryptor.finalize()
                if final_chunk:
                    target_handle.write(final_chunk)
                    digest.update(final_chunk)
    except RuntimeErrorBase:
        raise
    except Exception as exc:  # pragma: no cover - library exception details are not stable
        raise RuntimeErrorBase("failed to decrypt ciphertext") from exc

    return f"sha256:{digest.hexdigest()}"


def encrypt_plaintext_bytes(*, plaintext: bytes, key_bytes: bytes) -> bytes:
    if len(key_bytes) != ARTIFACT_KEY_BYTES:
        raise RuntimeErrorBase(f"artifact key must be {ARTIFACT_KEY_BYTES} bytes")
    nonce = os.urandom(ARTIFACT_NONCE_BYTES)
    ciphertext_and_tag = AESGCM(key_bytes).encrypt(nonce, plaintext, None)
    return nonce + ciphertext_and_tag


def decode_key_b64(value: str) -> bytes:
    try:
        key_bytes = base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:  # pragma: no cover - defensive
        raise RuntimeErrorBase("key_b64 is not valid base64") from exc
    if len(key_bytes) != ARTIFACT_KEY_BYTES:
        raise RuntimeErrorBase(
            f"decoded key length must be {ARTIFACT_KEY_BYTES} bytes"
        )
    return key_bytes


def http_get_json(
    *,
    url: str,
    cafile: str | Path | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    body = http_get_bytes(url=url, cafile=cafile, timeout=timeout)
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeErrorBase(f"server returned invalid JSON for {url}") from exc
    if not isinstance(payload, dict):
        raise RuntimeErrorBase(f"server returned a non-object JSON payload for {url}")
    return payload


def http_post_json(
    *,
    url: str,
    payload: dict[str, Any],
    cafile: str | Path | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    body = _http_request_bytes(request=request, cafile=cafile, timeout=timeout)
    try:
        response_payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeErrorBase(f"server returned invalid JSON for {url}") from exc
    if not isinstance(response_payload, dict):
        raise RuntimeErrorBase(f"server returned a non-object JSON payload for {url}")
    return response_payload


def http_put_bytes(
    *,
    url: str,
    payload: bytes,
    headers: dict[str, str] | None = None,
    cafile: str | Path | None = None,
    timeout: float = 60.0,
) -> bytes:
    request_headers = dict(headers or {})
    request = urllib_request.Request(
        url,
        data=payload,
        headers=request_headers,
        method="PUT",
    )
    return _http_request_bytes(request=request, cafile=cafile, timeout=timeout)


def http_put_bytes_resumable(
    *,
    url: str,
    payload: bytes,
    headers: dict[str, str] | None = None,
    session_create_headers: dict[str, str] | None = None,
    session_create_payload: dict[str, Any] | None = None,
    cafile: str | Path | None = None,
    timeout: float = 60.0,
) -> bytes:
    if len(payload) < _CHUNKED_UPLOAD_THRESHOLD_BYTES:
        return http_put_bytes(
            url=url,
            payload=payload,
            headers=headers,
            cafile=cafile,
            timeout=timeout,
        )

    return http_put_bytes_upload_session(
        url=url,
        payload=payload,
        headers=headers,
        session_create_headers=session_create_headers or headers,
        session_create_payload=session_create_payload,
        cafile=cafile,
        timeout=timeout,
    )


def http_put_bytes_upload_session(
    *,
    url: str,
    payload: bytes,
    headers: dict[str, str] | None = None,
    session_create_headers: dict[str, str] | None = None,
    session_create_payload: dict[str, Any] | None = None,
    complete_headers: dict[str, str] | None = None,
    cafile: str | Path | None = None,
    timeout: float = 60.0,
) -> bytes:
    create_headers = dict(session_create_headers or headers or {})
    create_headers["Content-Type"] = "application/json"
    create_payload = session_create_payload or {"upload_length": len(payload)}
    create_body = json.dumps(create_payload).encode("utf-8")
    create_request = urllib_request.Request(
        _session_create_url(url),
        data=create_body,
        headers=create_headers,
        method="POST",
    )
    session_response = _http_request_json(
        request=create_request,
        cafile=cafile,
        timeout=timeout,
    )
    session_id = _required_non_empty_string(session_response.get("session_id"), "session_id")
    offset = _required_int(session_response.get("offset"), "offset")

    while offset < len(payload):
        chunk = payload[offset : offset + _CHUNKED_UPLOAD_CHUNK_SIZE_BYTES]
        chunk_headers = dict(headers or {})
        chunk_headers["X-Cove-Upload-Offset"] = str(offset)
        chunk_request = urllib_request.Request(
            _session_chunk_url(url, session_id),
            data=chunk,
            headers=chunk_headers,
            method="PUT",
        )
        chunk_response = _http_request_json(
            request=chunk_request,
            cafile=cafile,
            timeout=timeout,
        )
        next_offset = _required_int(chunk_response.get("offset"), "offset")
        if next_offset <= offset:
            raise RuntimeErrorBase(
                f"chunked upload session {session_id} did not advance offset"
            )
        offset = next_offset

    complete_request = urllib_request.Request(
        _session_complete_url(url, session_id),
        data=b"",
        headers=dict(complete_headers or {}),
        method="POST",
    )
    return _http_request_bytes(request=complete_request, cafile=cafile, timeout=timeout)


def http_get_bytes(
    *,
    url: str,
    cafile: str | Path | None = None,
    timeout: float = 60.0,
) -> bytes:
    request = urllib_request.Request(url, method="GET")
    return _http_request_bytes(request=request, cafile=cafile, timeout=timeout)


def http_get_to_file(
    *,
    url: str,
    output_path: str | Path,
    cafile: str | Path | None = None,
    timeout: float = 60.0,
) -> None:
    request = urllib_request.Request(url, method="GET")
    _ensure_default_headers(request)
    context = None
    if cafile is not None:
        context = ssl.create_default_context(cafile=str(cafile))
    try:
        with urllib_request.urlopen(request, timeout=timeout, context=context) as response:
            if response.status >= 400:  # pragma: no cover - defensive
                raise urllib_error.HTTPError(
                    request.full_url,
                    response.status,
                    response.reason,
                    response.headers,
                    None,
                )
            target_path = ensure_parent(output_path)
            with target_path.open("wb") as handle:
                while True:
                    chunk = response.read(_HTTP_STREAM_CHUNK_SIZE_BYTES)
                    if not chunk:
                        break
                    handle.write(chunk)
    except urllib_error.HTTPError as exc:
        detail = _extract_http_detail(exc)
        raise RuntimeErrorBase(
            f"HTTP {exc.code} when fetching {request.full_url}: {detail}"
        ) from exc
    except urllib_error.URLError as exc:
        raise RuntimeErrorBase(f"failed to reach {request.full_url}: {exc.reason}") from exc


def join_url(base_url: str, relative_path: str) -> str:
    if relative_path.startswith("http://") or relative_path.startswith("https://"):
        return relative_path
    return f"{base_url.rstrip('/')}/{relative_path.lstrip('/')}"


def url_with_query(base_url: str, path: str, query: dict[str, str]) -> str:
    encoded = urllib_parse.urlencode(query)
    return f"{join_url(base_url, path)}?{encoded}"


def _http_request_json(
    *,
    request: urllib_request.Request,
    cafile: str | Path | None = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    body = _http_request_bytes(request=request, cafile=cafile, timeout=timeout)
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeErrorBase(f"server returned invalid JSON for {request.full_url}") from exc
    if not isinstance(payload, dict):
        raise RuntimeErrorBase(f"server returned a non-object JSON payload for {request.full_url}")
    return payload


def _required_int(value: Any, label: str) -> int:
    if not isinstance(value, int):
        raise RuntimeErrorBase(f"server returned invalid {label}")
    return value


def _required_non_empty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeErrorBase(f"server returned invalid {label}")
    return value.strip()


def _session_create_url(url: str) -> str:
    return f"{url.rstrip('/')}/upload-session"


def _session_chunk_url(url: str, session_id: str) -> str:
    parsed = urllib_parse.urlsplit(url)
    return urllib_parse.urlunsplit((parsed.scheme, parsed.netloc, f"/v1/uploads/sessions/{session_id}", "", ""))


def _session_complete_url(url: str, session_id: str) -> str:
    parsed = urllib_parse.urlsplit(url)
    return urllib_parse.urlunsplit((parsed.scheme, parsed.netloc, f"/v1/uploads/sessions/{session_id}/complete", "", ""))


def _http_request_bytes(
    *,
    request: urllib_request.Request,
    cafile: str | Path | None = None,
    timeout: float = 60.0,
) -> bytes:
    _ensure_default_headers(request)
    context = None
    if cafile is not None:
        context = ssl.create_default_context(cafile=str(cafile))
    try:
        with urllib_request.urlopen(request, timeout=timeout, context=context) as response:
            return response.read()
    except urllib_error.HTTPError as exc:
        detail = _extract_http_detail(exc)
        raise RuntimeErrorBase(f"HTTP {exc.code} for {request.full_url}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeErrorBase(f"failed to reach {request.full_url}: {exc.reason}") from exc


def _extract_http_detail(exc: urllib_error.HTTPError) -> str:
    try:
        body = exc.read().decode("utf-8")
    except Exception:  # pragma: no cover - defensive
        return str(exc.reason)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return body or str(exc.reason)
    detail = payload.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return body or str(exc.reason)


def _ensure_default_headers(request: urllib_request.Request) -> None:
    if not request.has_header("User-agent"):
        request.add_header("User-Agent", COVE_RUNTIME_USER_AGENT)


def required_mapping(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise SidecarConfigError(f"{label} must be an object")
    return payload


def required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SidecarConfigError(f"{key} must be a non-empty string")
    return value.strip()


def optional_string(value: Any, label: str = "optional string") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise SidecarConfigError(f"{label} values must be non-empty strings when present")
    return value.strip()


def wait_for_file(path: str | Path, timeout_seconds: float = 30.0) -> Path:
    candidate = Path(path)
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        if candidate.is_file():
            return candidate
        time.sleep(0.2)
    raise RuntimeErrorBase(f"timed out waiting for file: {path}")


def load_named_json_entries(raw_entries: Any, label: str) -> dict[str, Any]:
    if raw_entries is None:
        return {}
    if not isinstance(raw_entries, list):
        raise SidecarConfigError(f"{label} must be a list")

    loaded: dict[str, Any] = {}
    for raw_entry in raw_entries:
        entry = required_mapping(raw_entry, label[:-1] if label.endswith("s") else label)
        name = required_string(entry, "name")
        path = required_string(entry, "path")
        loaded[name] = read_json_file(wait_for_file(path))
    return loaded


def log(role: str, message: str) -> None:
    print(f"[{role}] {message}", flush=True)
