from __future__ import annotations

import json
import hashlib
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from .covehub import COVEHUB_USER_AGENT


class RuntimeErrorBase(RuntimeError):
    """Raised when a runtime-contract operation fails closed."""


def sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def http_post_json(
    *,
    url: str,
    payload: dict[str, Any],
    timeout: float = 5.0,
) -> dict[str, Any]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": COVEHUB_USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib_error.HTTPError as exc:
        raise RuntimeErrorBase(f"HTTP {exc.code} for {url}: {_extract_http_detail(exc)}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeErrorBase(f"failed to reach {url}: {exc.reason}") from exc
    try:
        response_payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeErrorBase(f"server returned invalid JSON for {url}") from exc
    if not isinstance(response_payload, dict):
        raise RuntimeErrorBase(f"server returned a non-object JSON payload for {url}")
    return response_payload


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
