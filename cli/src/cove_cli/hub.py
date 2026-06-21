from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request

from .config import ensure_local_config
from .covehub import COVEHUB_USER_AGENT


HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REQUEST_TIMEOUT_SECONDS = 10


class HubCommandError(RuntimeError):
    """Raised when a read-only CoveHub object command fails."""


@dataclass(frozen=True, slots=True)
class HubObject:
    hub_path: str
    payload: bytes
    observed_digest: str
    observed_exact_hub_path: str
    is_latest: bool
    kind: str


def get_hub_object_command(
    hub_path: str,
    *,
    server_url: str | None = None,
    output: str | Path | None = None,
    cove_home: str | Path | None = None,
) -> str:
    obj = download_hub_object(
        hub_path,
        server_url=_resolve_server_url(server_url, cove_home),
    )
    output_path = Path(output).expanduser().resolve() if output is not None else Path(
        _default_output_name(obj)
    ).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(obj.payload)
    lines = [
        f"Downloaded {obj.hub_path}",
        f"Observed digest: {obj.observed_digest}",
    ]
    if obj.is_latest:
        lines.extend(
            [
                "WARNING: 'latest' is a mutable convenience alias; prefer the observed exact path for reproducible references.",
                f"Observed exact path: {obj.observed_exact_hub_path}",
            ]
        )
    lines.append(f"Wrote {len(obj.payload)} bytes to {output_path}")
    return "\n".join(lines)


def inspect_hub_object_command(
    hub_path: str,
    *,
    server_url: str | None = None,
    cove_home: str | Path | None = None,
) -> str:
    obj = download_hub_object(
        hub_path,
        server_url=_resolve_server_url(server_url, cove_home),
    )
    lines = [
        f"CoveHub object: {obj.hub_path}",
        f"Type: {obj.kind}",
        f"Size: {len(obj.payload)} bytes",
        f"Observed digest: {obj.observed_digest}",
    ]
    if obj.is_latest:
        lines.extend(
            [
                "WARNING: 'latest' is a mutable convenience alias; prefer exact digest paths for verification.",
                f"Observed exact path: {obj.observed_exact_hub_path}",
            ]
        )

    parsed_json = _parse_json(obj.payload)
    if obj.kind == "workflow" and isinstance(parsed_json, dict):
        lines.extend(_workflow_lines(parsed_json))
    elif obj.kind == "runtime certificate" and isinstance(parsed_json, dict):
        lines.extend(_certificate_lines(parsed_json))
    elif isinstance(parsed_json, dict):
        lines.append("JSON object: yes")
        lines.append(f"Top-level keys: {', '.join(sorted(parsed_json)[:12])}")
    else:
        lines.append("Payload: opaque bytes")

    lines.extend(
        [
            "Independent verification:",
            f"- Save bytes: cove hub get {obj.hub_path} --output {_default_output_name(obj)}",
            "- Recompute hashes and run Cove verification locally before trusting object contents.",
        ]
    )
    return "\n".join(lines)


def download_hub_object(hub_path: str, *, server_url: str) -> HubObject:
    normalized_path = _normalize_hub_path(hub_path)
    payload = _request_bytes(server_url=server_url, hub_path=normalized_path)
    observed_digest = _sha256_literal(payload)
    reference = normalized_path.rsplit("/", 1)[-1]
    is_latest = reference == "latest"
    if not is_latest and HASH_RE.fullmatch(reference) is not None and reference != observed_digest:
        raise HubCommandError(
            f"downloaded payload digest mismatch for {normalized_path}: {observed_digest} != {reference}"
        )
    observed_exact_hub_path = (
        f"{normalized_path[: -len('/latest')]}/{observed_digest}"
        if is_latest
        else normalized_path
    )
    return HubObject(
        hub_path=normalized_path,
        payload=payload,
        observed_digest=observed_digest,
        observed_exact_hub_path=observed_exact_hub_path,
        is_latest=is_latest,
        kind=_object_kind(normalized_path),
    )


def _resolve_server_url(server_url: str | None, cove_home: str | Path | None) -> str:
    if server_url is not None and server_url.strip():
        return server_url.strip()
    config = ensure_local_config(cove_home)
    if config.covehub_server_url:
        return config.covehub_server_url
    raise HubCommandError(
        "CoveHub server URL is required; pass --server-url or configure covehub_server_url with cove init"
    )


def _normalize_hub_path(hub_path: str) -> str:
    normalized = hub_path.strip().lstrip("/")
    if not normalized.startswith("v1/"):
        raise HubCommandError("hub path must start with v1/")
    parts = normalized.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise HubCommandError("hub path contains unsafe path traversal")
    kind = _object_kind(normalized)
    if kind == "unknown":
        raise HubCommandError("hub path is not a supported CoveHub typed object route")
    reference = parts[-1]
    if kind == "static artifact" and reference == "latest":
        raise HubCommandError("static artifact hub paths must use an exact sha256:<hex> reference")
    if reference != "latest" and HASH_RE.fullmatch(reference) is None:
        raise HubCommandError("hub path reference must be 'latest' or sha256:<hex>")
    return normalized


def _object_kind(hub_path: str) -> str:
    parts = hub_path.split("/")
    if len(parts) == 5 and parts[0] == "v1" and parts[1] == "artifacts":
        return "static artifact"
    if len(parts) == 5 and parts[0] == "v1" and parts[1] == "workflows":
        return "workflow"
    if (
        len(parts) == 7
        and parts[0] == "v1"
        and parts[1] == "runtime"
        and parts[4] == "certificates"
    ):
        return "runtime certificate"
    if (
        len(parts) == 7
        and parts[0] == "v1"
        and parts[1] == "runtime"
        and parts[4] == "artifacts"
    ):
        return "runtime artifact"
    return "unknown"


def _request_bytes(*, server_url: str, hub_path: str) -> bytes:
    request = urllib_request.Request(
        f"{server_url.rstrip('/')}/{hub_path}",
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": COVEHUB_USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib_error.HTTPError as exc:
        detail = _extract_http_detail(exc)
        raise HubCommandError(f"request failed with HTTP {exc.code}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise HubCommandError(f"failed to reach CoveHub server: {exc.reason}") from exc


def _workflow_lines(payload: dict[str, object]) -> list[str]:
    if payload.get("format") != "cove.workflow.bundle.v1":
        return ["Workflow JSON: unsupported or legacy format"]
    manifest = payload.get("manifest")
    files = payload.get("files")
    if not isinstance(manifest, dict):
        return ["Workflow bundle: missing manifest object"]
    nodes = manifest.get("nodes")
    node_count = len(nodes) if isinstance(nodes, list) else 0
    file_count = len(files) if isinstance(files, list) else 0
    lines = [
        "Workflow bundle:",
        f"  Publisher: {manifest.get('publisher')}",
        f"  Workflow id: {manifest.get('workflow_id')}",
        f"  Manifest hash: {manifest.get('manifest_hash')}",
        f"  Files: {file_count}",
        f"  Nodes: {node_count}",
    ]
    if isinstance(nodes, list):
        for node in nodes[:12]:
            if isinstance(node, dict):
                lines.append(
                    f"  - {node.get('node_id')}: compose {node.get('compose_hash')}"
                )
    return lines


def _certificate_lines(payload: dict[str, object]) -> list[str]:
    body = payload.get("certificate_body")
    attestation = payload.get("attestation_bundle")
    if not isinstance(body, dict):
        return ["Runtime certificate: missing certificate_body object"]
    lines = [
        "Runtime certificate:",
        f"  Workflow id: {body.get('workflow_id')}",
        f"  Node id: {body.get('node_id')}",
        f"  Compose hash: {body.get('generated_node_compose_hash')}",
        f"  Certificate body hash: {payload.get('certificate_body_hash')}",
    ]
    if isinstance(attestation, dict):
        lines.append(f"  Attestation format: {attestation.get('format')}")
    for label, key in [
        ("Inputs", "inputs"),
        ("Outputs", "outputs"),
        ("Results", "results"),
        ("Ephemeral keypairs", "ephemeral_keypairs"),
    ]:
        value = body.get(key)
        if isinstance(value, dict):
            lines.append(f"  {label}: {len(value)}")
    return lines


def _parse_json(payload: bytes) -> object | None:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


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


def _default_output_name(obj: HubObject) -> str:
    parts = obj.hub_path.split("/")
    if obj.kind == "runtime certificate":
        stem = parts[5]
        extension = ".json"
    elif obj.kind == "workflow":
        stem = parts[3]
        extension = ".json"
    elif obj.kind in {"static artifact", "runtime artifact"}:
        stem = parts[-2]
        extension = ".bin"
    else:
        stem = "covehub-object"
        extension = ".bin"
    suffix = "latest" if obj.is_latest else obj.observed_digest.removeprefix("sha256:")[:12]
    return f"{stem}-{suffix}{extension}"


def _sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
