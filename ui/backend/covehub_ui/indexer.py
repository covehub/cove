from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any

import yaml


HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DEFAULT_PREVIEW_BYTES = 4096


@dataclass(frozen=True, slots=True)
class UiSettings:
    data_root: Path
    cache_ttl_seconds: float = 5.0
    preview_bytes: int = DEFAULT_PREVIEW_BYTES

    @classmethod
    def from_env(cls) -> "UiSettings":
        return cls(
            data_root=Path(os.getenv("COVE_UI_DATA_ROOT", "/var/lib/covehub/data")).resolve(),
            cache_ttl_seconds=float(os.getenv("COVE_UI_CACHE_TTL_SECONDS", "5")),
            preview_bytes=int(os.getenv("COVE_UI_PREVIEW_BYTES", str(DEFAULT_PREVIEW_BYTES))),
        )


class IndexerError(RuntimeError):
    """Raised when a requested UI object cannot be read."""


class ObjectNotFoundError(IndexerError):
    """Raised when an object path is absent from the public data root."""


class UnsafeObjectPathError(IndexerError):
    """Raised when a requested object path is not a CoveHub public object path."""


@dataclass(frozen=True, slots=True)
class ObjectRecord:
    hub_path: str
    storage_path: str
    kind: str
    reference: str
    is_latest: bool
    owner: str | None
    publisher: str | None
    workflow_id: str | None
    artifact_name: str | None
    node_id: str | None
    digest: str | None
    observed_digest: str
    observed_exact_hub_path: str
    size: int
    modified_at: str

    def to_payload(self) -> dict[str, object]:
        return {
            "hub_path": self.hub_path,
            "storage_path": self.storage_path,
            "kind": self.kind,
            "reference": self.reference,
            "is_latest": self.is_latest,
            "owner": self.owner,
            "publisher": self.publisher,
            "workflow_id": self.workflow_id,
            "artifact_name": self.artifact_name,
            "node_id": self.node_id,
            "digest": self.digest,
            "observed_digest": self.observed_digest,
            "observed_exact_hub_path": self.observed_exact_hub_path,
            "size": self.size,
            "modified_at": self.modified_at,
        }


class CoveHubIndexer:
    def __init__(self, settings: UiSettings) -> None:
        self.settings = settings
        self._cached_payload: dict[str, object] | None = None
        self._cached_at = 0.0

    @property
    def data_root(self) -> Path:
        return self.settings.data_root

    def summary(self) -> dict[str, object]:
        records = self._records()
        kind_counts: dict[str, int] = {}
        owners: set[str] = set()
        publishers: set[str] = set()
        workflow_refs: set[str] = set()
        total_bytes = 0
        for record in records:
            kind_counts[record.kind] = kind_counts.get(record.kind, 0) + 1
            if record.owner:
                owners.add(record.owner)
            if record.publisher:
                publishers.add(record.publisher)
            if record.publisher and record.workflow_id:
                workflow_refs.add(f"{record.publisher}/{record.workflow_id}")
            total_bytes += record.size
        return {
            "ok": self.data_root.is_dir(),
            "data_root": str(self.data_root),
            "generated_at": _utc_from_timestamp(time.time()),
            "object_count": len(records),
            "total_bytes": total_bytes,
            "kind_counts": kind_counts,
            "owners": sorted(owners),
            "publishers": sorted(publishers),
            "workflows": sorted(workflow_refs),
        }

    def list_objects(
        self,
        *,
        kind: str | None = None,
        query: str | None = None,
        limit: int = 200,
    ) -> dict[str, object]:
        records = self._records()
        if kind:
            records = [record for record in records if record.kind == kind]
        if query:
            normalized_query = query.casefold()
            records = [
                record
                for record in records
                if normalized_query in " ".join(
                    value
                    for value in [
                        record.hub_path,
                        record.kind,
                        record.owner,
                        record.publisher,
                        record.workflow_id,
                        record.artifact_name,
                        record.node_id,
                        record.observed_digest,
                    ]
                    if value
                ).casefold()
            ]
        limit = max(1, min(limit, 1000))
        records = sorted(records, key=lambda record: (record.kind, record.hub_path))[:limit]
        return {
            "objects": [record.to_payload() for record in records],
            "limit": limit,
        }

    def object_detail(self, hub_path: str) -> dict[str, object]:
        normalized_path = normalize_hub_path(hub_path)
        path = self._storage_path_for_hub_path(normalized_path)
        if not path.is_file():
            raise ObjectNotFoundError(normalized_path)
        record = self._record_for_storage_path(path)
        if record is None:
            raise ObjectNotFoundError(normalized_path)
        record = next(
            (
                candidate
                for candidate in self._records(refresh=True)
                if candidate.hub_path == record.hub_path
            ),
            record,
        )
        payload = path.read_bytes()
        summary, preview = summarize_payload(record, payload, self.settings.preview_bytes)
        return {
            **record.to_payload(),
            "summary": summary,
            "relationships": self._relationships_for(record),
            "preview": preview,
            "cli": {
                "inspect": f"cove hub inspect --server-url https://api.covehub.io {record.hub_path}",
                "get": f"cove hub get --server-url https://api.covehub.io {record.hub_path} --output {suggested_output_name(record)}",
            },
            "trust_notice": (
                "CoveHub and this UI are convenience transport and discovery layers, "
                "not sources of truth. Use the Cove CLI and local verification inputs "
                "before trusting object contents."
            ),
        }

    def glossary(self) -> dict[str, object]:
        return {"terms": GLOSSARY_TERMS}

    def _relationships_for(self, record: ObjectRecord) -> dict[str, object]:
        if record.kind in {"static_artifact", "runtime_artifact"}:
            return self._artifact_relationships(record)
        if record.kind == "runtime_certificate":
            return self._certificate_relationships(record)
        return {}

    def _artifact_relationships(self, record: ObjectRecord) -> dict[str, object]:
        used_by: list[dict[str, object]] = []
        produced_by: list[dict[str, object]] = []
        for workflow_record, workflow in self._workflow_summaries():
            for node in workflow.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                for artifact in node.get("artifacts", []):
                    if not isinstance(artifact, dict) or not _same_artifact(record, artifact):
                        continue
                    entry = {
                        "workflow_hub_path": workflow_record.hub_path,
                        "publisher": workflow_record.publisher,
                        "workflow_id": workflow_record.workflow_id,
                        "node_id": node.get("node_id"),
                        "artifact_name": artifact.get("name"),
                        "artifact_type": artifact.get("type"),
                        "hub_path": artifact.get("hub_path"),
                    }
                    if artifact.get("direction") == "output":
                        produced_by.append(entry)
                    else:
                        used_by.append(entry)
        return {"used_by": used_by, "produced_by": produced_by}

    def _certificate_relationships(self, record: ObjectRecord) -> dict[str, object]:
        generated_by: list[dict[str, object]] = []
        consumed_by: list[dict[str, object]] = []
        if not record.publisher or not record.workflow_id or not record.node_id:
            return {"generated_by": generated_by, "consumed_by": consumed_by}

        for workflow_record, workflow in self._workflow_summaries():
            if (
                workflow_record.publisher != record.publisher
                or workflow_record.workflow_id != record.workflow_id
            ):
                continue
            for node in workflow.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                node_id = node.get("node_id")
                entry = {
                    "workflow_hub_path": workflow_record.hub_path,
                    "publisher": workflow_record.publisher,
                    "workflow_id": workflow_record.workflow_id,
                    "node_id": node_id,
                    "compose_hash": node.get("compose_hash"),
                }
                if node_id == record.node_id:
                    generated_by.append(entry)
                if record.node_id in node.get("dependencies", []):
                    consumed_by.append({**entry, "reason": "declared dependency"})
                elif _references_certificate(node.get("definition"), record.node_id):
                    consumed_by.append({**entry, "reason": "precondition reference"})
        return {"generated_by": generated_by, "consumed_by": consumed_by}

    def _workflow_summaries(self) -> list[tuple[ObjectRecord, dict[str, object]]]:
        workflows: list[tuple[ObjectRecord, dict[str, object]]] = []
        for record in self._records():
            if record.kind != "workflow":
                continue
            path = self.data_root / record.storage_path
            if not path.is_file():
                continue
            parsed = _parse_json(path.read_bytes())
            if isinstance(parsed, dict):
                workflows.append((record, _workflow_summary(parsed)))
        return workflows

    def _records(self, *, refresh: bool = False) -> list[ObjectRecord]:
        now = time.time()
        if (
            not refresh
            and self._cached_payload is not None
            and now - self._cached_at <= self.settings.cache_ttl_seconds
        ):
            cached = self._cached_payload.get("records", [])
            return [record for record in cached if isinstance(record, ObjectRecord)]

        records: list[ObjectRecord] = []
        if self.data_root.is_dir():
            for path in sorted(self.data_root.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                record = self._record_for_storage_path(path)
                if record is not None:
                    records.append(record)
        records = self._enrich_runtime_artifact_owners(records)
        self._cached_payload = {"records": records}
        self._cached_at = now
        return records

    def _enrich_runtime_artifact_owners(self, records: list[ObjectRecord]) -> list[ObjectRecord]:
        runtime_records = [
            record
            for record in records
            if record.kind == "runtime_artifact" and record.owner is None
        ]
        if not runtime_records:
            return records

        certificate_candidates = self._runtime_owner_candidates_from_certificates(
            records,
            runtime_records,
        )
        workflow_candidates = self._runtime_owner_candidates_from_workflows(
            records,
            runtime_records,
        )
        enriched: list[ObjectRecord] = []
        for record in records:
            if record.kind != "runtime_artifact" or record.owner is not None:
                enriched.append(record)
                continue
            owner = _unique_candidate(certificate_candidates.get(record.hub_path, set()))
            if owner is None:
                owner = _unique_candidate(workflow_candidates.get(record.hub_path, set()))
            enriched.append(replace(record, owner=owner) if owner is not None else record)
        return enriched

    def _runtime_owner_candidates_from_certificates(
        self,
        records: list[ObjectRecord],
        runtime_records: list[ObjectRecord],
    ) -> dict[str, set[str]]:
        candidates: dict[str, set[str]] = {}
        for certificate_record in records:
            if certificate_record.kind != "runtime_certificate":
                continue
            parsed = _parse_json((self.data_root / certificate_record.storage_path).read_bytes())
            if not isinstance(parsed, dict):
                continue
            body = parsed.get("certificate_body")
            if not isinstance(body, dict):
                continue
            outputs = body.get("outputs")
            if not isinstance(outputs, dict):
                continue
            for output_metadata in outputs.values():
                if not isinstance(output_metadata, dict):
                    continue
                owner = _valid_owner_or_none(output_metadata.get("owner"))
                if owner is None:
                    continue
                for runtime_record in runtime_records:
                    if _runtime_output_metadata_matches(
                        runtime_record,
                        certificate_record,
                        output_metadata,
                    ):
                        candidates.setdefault(runtime_record.hub_path, set()).add(owner)
        return candidates

    def _runtime_owner_candidates_from_workflows(
        self,
        records: list[ObjectRecord],
        runtime_records: list[ObjectRecord],
    ) -> dict[str, set[str]]:
        candidates: dict[str, set[str]] = {}
        for workflow_record in records:
            if workflow_record.kind != "workflow":
                continue
            parsed = _parse_json((self.data_root / workflow_record.storage_path).read_bytes())
            if not isinstance(parsed, dict):
                continue
            workflow = _workflow_summary(parsed)
            for node in workflow.get("nodes", []):
                if not isinstance(node, dict):
                    continue
                for artifact in node.get("artifacts", []):
                    if not isinstance(artifact, dict):
                        continue
                    owner = _valid_owner_or_none(artifact.get("owner"))
                    if owner is None:
                        continue
                    for runtime_record in runtime_records:
                        if (
                            workflow_record.publisher == runtime_record.publisher
                            and workflow_record.workflow_id == runtime_record.workflow_id
                            and _same_artifact(runtime_record, artifact)
                        ):
                            candidates.setdefault(runtime_record.hub_path, set()).add(owner)
        return candidates

    def _record_for_storage_path(self, path: Path) -> ObjectRecord | None:
        try:
            relative = path.relative_to(self.data_root).as_posix()
        except ValueError:
            return None
        parts = relative.split("/")
        parsed = parse_storage_parts(parts)
        if parsed is None:
            return None
        payload = path.read_bytes()
        observed_digest = sha256_literal(payload)
        reference = parsed["reference"]
        is_latest = reference == "latest"
        digest = None if is_latest else reference
        hub_path = "v1/" + relative
        observed_exact_hub_path = (
            f"{hub_path[: -len('/latest')]}/{observed_digest}"
            if is_latest
            else hub_path
        )
        stat = path.stat()
        return ObjectRecord(
            hub_path=hub_path,
            storage_path=relative,
            kind=parsed["kind"],
            reference=reference,
            is_latest=is_latest,
            owner=parsed.get("owner"),
            publisher=parsed.get("publisher"),
            workflow_id=parsed.get("workflow_id"),
            artifact_name=parsed.get("artifact_name"),
            node_id=parsed.get("node_id"),
            digest=digest,
            observed_digest=observed_digest,
            observed_exact_hub_path=observed_exact_hub_path,
            size=stat.st_size,
            modified_at=_utc_from_timestamp(stat.st_mtime),
        )

    def _storage_path_for_hub_path(self, hub_path: str) -> Path:
        normalized = normalize_hub_path(hub_path)
        relative = normalized.removeprefix("v1/")
        candidate = self.data_root.joinpath(*relative.split("/"))
        resolved = candidate.resolve(strict=False)
        data_root = self.data_root.resolve(strict=False)
        if resolved != data_root and data_root not in resolved.parents:
            raise UnsafeObjectPathError(hub_path)
        if parse_storage_parts(relative.split("/")) is None:
            raise UnsafeObjectPathError(hub_path)
        return resolved


def parse_storage_parts(parts: list[str]) -> dict[str, str] | None:
    if len(parts) == 4 and parts[0] == "artifacts":
        owner, artifact_name, reference = parts[1], parts[2], parts[3]
        if _valid_identifier(owner) and _valid_identifier(artifact_name) and _valid_reference(reference):
            return {
                "kind": "static_artifact",
                "owner": owner,
                "artifact_name": artifact_name,
                "reference": reference,
            }
    if len(parts) == 4 and parts[0] == "workflows":
        publisher, workflow_id, reference = parts[1], parts[2], parts[3]
        if _valid_identifier(publisher) and _valid_identifier(workflow_id) and _valid_reference(reference):
            return {
                "kind": "workflow",
                "publisher": publisher,
                "workflow_id": workflow_id,
                "reference": reference,
            }
    if (
        len(parts) == 6
        and parts[0] == "runtime"
        and parts[3] == "certificates"
    ):
        publisher, workflow_id, node_id, reference = parts[1], parts[2], parts[4], parts[5]
        if (
            _valid_identifier(publisher)
            and _valid_identifier(workflow_id)
            and _valid_identifier(node_id)
            and _valid_reference(reference)
        ):
            return {
                "kind": "runtime_certificate",
                "publisher": publisher,
                "workflow_id": workflow_id,
                "node_id": node_id,
                "reference": reference,
            }
    if len(parts) == 6 and parts[0] == "runtime" and parts[3] == "artifacts":
        publisher, workflow_id, artifact_name, reference = parts[1], parts[2], parts[4], parts[5]
        if (
            _valid_identifier(publisher)
            and _valid_identifier(workflow_id)
            and _valid_identifier(artifact_name)
            and _valid_reference(reference)
        ):
            return {
                "kind": "runtime_artifact",
                "publisher": publisher,
                "workflow_id": workflow_id,
                "artifact_name": artifact_name,
                "reference": reference,
            }
    return None


def normalize_hub_path(hub_path: str) -> str:
    normalized = hub_path.strip().lstrip("/")
    if not normalized:
        raise UnsafeObjectPathError(hub_path)
    pure_path = PurePosixPath(normalized)
    if pure_path.is_absolute() or any(part in {"", ".", ".."} for part in pure_path.parts):
        raise UnsafeObjectPathError(hub_path)
    if not normalized.startswith("v1/"):
        raise UnsafeObjectPathError(hub_path)
    return normalized


def summarize_payload(
    record: ObjectRecord,
    payload: bytes,
    preview_bytes: int,
) -> tuple[dict[str, object], dict[str, object]]:
    parsed_json = _parse_json(payload)
    if record.kind == "workflow" and isinstance(parsed_json, dict):
        summary = _workflow_summary(parsed_json)
        return summary, _json_preview(parsed_json, preview_bytes)
    if record.kind == "runtime_certificate" and isinstance(parsed_json, dict):
        summary = _certificate_summary(parsed_json)
        return summary, _json_preview(parsed_json, preview_bytes)
    if isinstance(parsed_json, dict | list):
        return {"format": "json"}, _json_preview(parsed_json, preview_bytes)
    return {"format": "opaque_bytes"}, _bytes_preview(payload, preview_bytes)


def suggested_output_name(record: ObjectRecord) -> str:
    stem = record.artifact_name or record.node_id or record.workflow_id or "covehub-object"
    suffix = "latest" if record.is_latest else record.observed_digest.removeprefix("sha256:")[:12]
    extension = ".json" if record.kind in {"workflow", "runtime_certificate"} else ".bin"
    return f"{stem}-{suffix}{extension}"


def sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _workflow_summary(payload: dict[str, object]) -> dict[str, object]:
    manifest = payload.get("manifest")
    files = payload.get("files")
    if not isinstance(manifest, dict):
        return {"format": payload.get("format", "json")}
    raw_nodes = manifest.get("nodes")
    nodes = raw_nodes if isinstance(raw_nodes, list) else []
    file_map = _workflow_file_map(payload)
    normalized_raw = _decode_workflow_text(file_map.get("workflow.normalized.cove.yaml"))
    normalized_parsed = _parse_yaml_text(normalized_raw)
    normalized_nodes = _mapping_get(normalized_parsed, "nodes")
    normalized_artifacts = _mapping_get(normalized_parsed, "artifacts")
    compose_files = [
        _compose_file_summary(
            node_id=_string_or_none(node.get("node_id")),
            compose_path=_string_or_none(node.get("compose_path")),
            compose_hash=_string_or_none(node.get("compose_hash")),
            raw_text=_decode_workflow_text(file_map.get(_string_or_none(node.get("compose_path")))),
        )
        for node in nodes
        if isinstance(node, dict)
    ]
    node_payloads = [
        _workflow_node_summary(
            node,
            normalized_node=_mapping_get(normalized_nodes, _string_or_none(node.get("node_id"))),
            compose_file=compose_files[index] if index < len(compose_files) else {},
        )
        for index, node in enumerate(nodes)
        if isinstance(node, dict)
    ]
    return {
        "format": payload.get("format", "json"),
        "publisher": manifest.get("publisher"),
        "workflow_id": manifest.get("workflow_id"),
        "manifest_hash": manifest.get("manifest_hash"),
        "file_count": len(files) if isinstance(files, list) else 0,
        "node_count": len(nodes),
        "files": _manifest_files(manifest),
        "nodes": node_payloads,
        "workflow_definition": {
            "path": "workflow.normalized.cove.yaml",
            "raw": normalized_raw or "",
            "parsed": normalized_parsed,
        },
        "compose_files": compose_files,
        "diagram": _workflow_diagram(
            publisher=_string_or_none(manifest.get("publisher")),
            workflow_id=_string_or_none(manifest.get("workflow_id")),
            nodes=node_payloads,
            artifacts=normalized_artifacts if isinstance(normalized_artifacts, dict) else {},
        ),
    }


def _certificate_summary(payload: dict[str, object]) -> dict[str, object]:
    body = payload.get("certificate_body")
    attestation = payload.get("attestation_bundle")
    if not isinstance(body, dict):
        return {"format": "json"}
    return {
        "format": "cove.runtime.certificate",
        "workflow_id": body.get("workflow_id"),
        "node_id": body.get("node_id"),
        "generated_node_compose_hash": body.get("generated_node_compose_hash"),
        "certificate_body_hash": payload.get("certificate_body_hash"),
        "attestation_format": attestation.get("format") if isinstance(attestation, dict) else None,
        "certificate": payload,
        "certificate_body": body,
        "attestation_bundle": attestation if isinstance(attestation, dict) else None,
        "meaning": _certificate_meaning(payload),
        "input_count": len(body.get("inputs", {})) if isinstance(body.get("inputs"), dict) else 0,
        "output_count": len(body.get("outputs", {})) if isinstance(body.get("outputs"), dict) else 0,
        "result_count": len(body.get("results", {})) if isinstance(body.get("results"), dict) else 0,
        "ephemeral_keypair_count": len(body.get("ephemeral_keypairs", {}))
        if isinstance(body.get("ephemeral_keypairs"), dict)
        else 0,
    }


def _workflow_file_map(payload: dict[str, object]) -> dict[str, bytes]:
    raw_files = payload.get("files")
    if not isinstance(raw_files, list):
        return {}
    files: dict[str, bytes] = {}
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            continue
        path = raw_file.get("path")
        content_b64 = raw_file.get("content_b64")
        if not isinstance(path, str) or not isinstance(content_b64, str):
            continue
        try:
            files[path] = base64.b64decode(content_b64.encode("ascii"), validate=True)
        except Exception:
            continue
    return files


def _decode_workflow_text(payload: bytes | None) -> str | None:
    if payload is None:
        return None
    return payload.decode("utf-8", errors="replace")


def _parse_yaml_text(text: str | None) -> object | None:
    if not text:
        return None
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None


def _mapping_get(payload: object, key: str | None) -> object | None:
    if key is None or not isinstance(payload, dict):
        return None
    return payload.get(key)


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _manifest_files(manifest: dict[str, object]) -> list[dict[str, object]]:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        return []
    files: list[dict[str, object]] = []
    for raw_file in raw_files:
        if isinstance(raw_file, dict):
            files.append(
                {
                    "path": raw_file.get("path"),
                    "sha256": raw_file.get("sha256"),
                }
            )
    return files


def _compose_file_summary(
    *,
    node_id: str | None,
    compose_path: str | None,
    compose_hash: str | None,
    raw_text: str | None,
) -> dict[str, object]:
    parsed = _parse_yaml_text(raw_text)
    services = _mapping_get(parsed, "services")
    return {
        "node_id": node_id,
        "path": compose_path,
        "compose_hash": compose_hash,
        "raw": raw_text or "",
        "parsed": parsed,
        "services": sorted(services.keys()) if isinstance(services, dict) else [],
    }


def _workflow_node_summary(
    node: dict[str, object],
    *,
    normalized_node: object | None,
    compose_file: dict[str, object],
) -> dict[str, object]:
    raw_artifacts = node.get("artifacts")
    artifacts = raw_artifacts if isinstance(raw_artifacts, list) else []
    services = _mapping_get(normalized_node, "services")
    dependencies = _mapping_get(normalized_node, "dependencies")
    return {
        "node_id": node.get("node_id"),
        "compose_path": node.get("compose_path"),
        "compose_hash": node.get("compose_hash"),
        "artifact_provisioner_image": node.get("artifact_provisioner_image"),
        "artifact_provisioner_digest": node.get("artifact_provisioner_digest"),
        "artifacts": [
            artifact
            for artifact in artifacts
            if isinstance(artifact, dict)
        ],
        "artifact_count": len(artifacts),
        "runtime_skeleton": node.get("runtime_skeleton")
        if isinstance(node.get("runtime_skeleton"), list)
        else [],
        "dependencies": dependencies if isinstance(dependencies, list) else [],
        "services": _service_summaries(services if isinstance(services, dict) else {}),
        "definition": normalized_node if isinstance(normalized_node, dict) else {},
        "compose_services": compose_file.get("services", []),
    }


def _service_summaries(services: dict[object, object]) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for service_name, raw_service in services.items():
        if not isinstance(raw_service, dict):
            continue
        summaries.append(
            {
                "service_name": service_name,
                "inputs": _path_map(raw_service.get("inputs")),
                "outputs": _path_map(raw_service.get("outputs")),
                "ephemeral_keypairs": raw_service.get("ephemeral_keypairs")
                if isinstance(raw_service.get("ephemeral_keypairs"), list)
                else [],
                "preconditions": raw_service.get("preconditions"),
                "should_terminate": raw_service.get("should_terminate"),
                "custom_certificate_field": raw_service.get("custom_certificate_field"),
            }
        )
    return summaries


def _path_map(value: object) -> list[dict[str, object]]:
    if not isinstance(value, dict):
        return []
    return [
        {"path": path, "artifact": artifact}
        for path, artifact in value.items()
        if isinstance(path, str) and isinstance(artifact, str)
    ]


def _workflow_diagram(
    *,
    publisher: str | None,
    workflow_id: str | None,
    nodes: list[dict[str, object]],
    artifacts: dict[object, object],
) -> dict[str, object]:
    artifact_metadata = {
        name: raw_artifact
        for name, raw_artifact in artifacts.items()
        if isinstance(name, str) and isinstance(raw_artifact, dict)
    }
    diagram_nodes: list[dict[str, object]] = []
    edges: list[dict[str, object]] = []
    for node in nodes:
        node_id = _string_or_none(node.get("node_id"))
        if node_id is None:
            continue
        inputs: list[dict[str, object]] = []
        outputs: list[dict[str, object]] = []
        for service in node.get("services", []):
            if not isinstance(service, dict):
                continue
            for entry in service.get("inputs", []):
                if isinstance(entry, dict):
                    inputs.append({**entry, "service": service.get("service_name")})
                    artifact_name = _string_or_none(entry.get("artifact"))
                    if artifact_name:
                        edges.append(
                            {
                                "from": f"artifact:{artifact_name}",
                                "to": f"node:{node_id}",
                                "label": f"input to {service.get('service_name')}",
                                "kind": "input",
                            }
                        )
            for entry in service.get("outputs", []):
                if isinstance(entry, dict):
                    outputs.append({**entry, "service": service.get("service_name")})
                    artifact_name = _string_or_none(entry.get("artifact"))
                    if artifact_name:
                        edges.append(
                            {
                                "from": f"node:{node_id}",
                                "to": f"artifact:{artifact_name}",
                                "label": f"output from {service.get('service_name')}",
                                "kind": "output",
                            }
                        )
        for dependency in node.get("dependencies", []):
            if isinstance(dependency, str):
                edges.append(
                    {
                        "from": f"certificate:{dependency}",
                        "to": f"node:{node_id}",
                        "label": "dependency certificate",
                        "kind": "certificate",
                    }
                )
                edges.append(
                    {
                        "from": f"node:{dependency}",
                        "to": f"certificate:{dependency}",
                        "label": "emits certificate",
                        "kind": "certificate",
                    }
                )
        edges.append(
            {
                "from": f"node:{node_id}",
                "to": f"certificate:{node_id}",
                "label": "emits certificate",
                "kind": "certificate",
            }
        )
        diagram_nodes.append(
            {
                "node_id": node_id,
                "compose_hash": node.get("compose_hash"),
                "compose_path": node.get("compose_path"),
                "services": node.get("services", []),
                "dependencies": node.get("dependencies", []),
                "inputs": inputs,
                "outputs": outputs,
                "certificate_hub_path": (
                    f"v1/runtime/{publisher}/{workflow_id}/certificates/{node_id}/latest"
                    if publisher and workflow_id
                    else None
                ),
            }
        )
    return {
        "nodes": diagram_nodes,
        "artifacts": [
            {
                "name": name,
                "type": raw_artifact.get("type"),
                "owner": raw_artifact.get("owner"),
                "hub_path": raw_artifact.get("hub_path"),
                "plaintext_hash": raw_artifact.get("plaintext_hash"),
            }
            for name, raw_artifact in sorted(artifact_metadata.items())
        ],
        "edges": _dedupe_edges(edges),
    }


def _dedupe_edges(edges: list[dict[str, object]]) -> list[dict[str, object]]:
    seen: set[tuple[object, object, object, object]] = set()
    deduped: list[dict[str, object]] = []
    for edge in edges:
        key = (edge.get("from"), edge.get("to"), edge.get("label"), edge.get("kind"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def _same_artifact(record: ObjectRecord, artifact: dict[str, object]) -> bool:
    artifact_hub_path = artifact.get("hub_path")
    artifact_name = artifact.get("name")
    if artifact_hub_path in {record.hub_path, record.observed_exact_hub_path}:
        return True
    if record.artifact_name and artifact_name == record.artifact_name:
        if record.kind == "static_artifact" and artifact.get("owner") == record.owner:
            return True
        if record.kind == "runtime_artifact":
            return (
                isinstance(artifact_hub_path, str)
                and record.publisher is not None
                and record.workflow_id is not None
                and f"/runtime/{record.publisher}/{record.workflow_id}/artifacts/" in f"/{artifact_hub_path}"
            )
    return False


def _runtime_output_metadata_matches(
    record: ObjectRecord,
    certificate_record: ObjectRecord,
    output_metadata: dict[str, object],
) -> bool:
    if (
        record.publisher != certificate_record.publisher
        or record.workflow_id != certificate_record.workflow_id
    ):
        return False
    artifact_name = _string_or_none(output_metadata.get("artifact_name"))
    if artifact_name is not None and artifact_name != record.artifact_name:
        return False
    metadata_paths = {
        path
        for path in [
            _string_or_none(output_metadata.get("hub_path")),
            _string_or_none(output_metadata.get("channel_hub_path")),
        ]
        if path
    }
    record_paths = {record.hub_path, record.observed_exact_hub_path}
    return bool(metadata_paths & record_paths)


def _valid_owner_or_none(value: object) -> str | None:
    owner = _string_or_none(value)
    if owner is None or not _valid_identifier(owner):
        return None
    return owner


def _unique_candidate(candidates: set[str]) -> str | None:
    if len(candidates) != 1:
        return None
    return next(iter(candidates))


def _references_certificate(payload: object, node_id: str) -> bool:
    return f"certificates.{node_id}." in json.dumps(payload, sort_keys=True)


def _certificate_meaning(payload: dict[str, object]) -> str:
    body = payload.get("certificate_body")
    if not isinstance(body, dict):
        return "This JSON object looks like a certificate, but it does not contain a certificate_body mapping."
    node_id = body.get("node_id", "unknown node")
    workflow_id = body.get("workflow_id", "unknown workflow")
    compose_hash = body.get("generated_node_compose_hash", "unknown compose hash")
    return (
        f"This certificate claims that node {node_id!r} in workflow {workflow_id!r} "
        f"emitted the listed inputs, outputs, results, and ephemeral keys while running "
        f"the generated compose hash {compose_hash!r}. CoveHub displays that structure "
        "but does not verify it."
    )


def _parse_json(payload: bytes) -> object | None:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _json_preview(payload: object, preview_bytes: int) -> dict[str, object]:
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    encoded = rendered.encode("utf-8")
    truncated = len(encoded) > preview_bytes
    if truncated:
        rendered = encoded[:preview_bytes].decode("utf-8", errors="replace")
    return {
        "kind": "json",
        "text": rendered,
        "truncated": truncated,
    }


def _bytes_preview(payload: bytes, preview_bytes: int) -> dict[str, object]:
    head = payload[:preview_bytes]
    try:
        text = head.decode("utf-8")
        if any(ord(character) < 9 for character in text):
            raise UnicodeDecodeError("utf-8", head, 0, 1, "control character")
        return {
            "kind": "text",
            "text": text,
            "truncated": len(payload) > preview_bytes,
        }
    except UnicodeDecodeError:
        return {
            "kind": "base64",
            "text": base64.b64encode(head).decode("ascii"),
            "truncated": len(payload) > preview_bytes,
        }


def _valid_identifier(value: str) -> bool:
    return IDENTIFIER_RE.fullmatch(value) is not None


def _valid_reference(value: str) -> bool:
    return value == "latest" or HASH_RE.fullmatch(value) is not None


def _utc_from_timestamp(timestamp: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


GLOSSARY_TERMS: list[dict[str, object]] = [
    {
        "id": "covehub",
        "term": "CoveHub",
        "summary": "Public storage and transport for Cove objects. It is not a trust root.",
        "links": ["workflow-bundle", "exact-digest", "latest"],
    },
    {
        "id": "publisher",
        "term": "Publisher",
        "summary": "The party that publishes a workflow bundle under a CoveHub namespace.",
        "links": ["workflow-bundle", "node"],
    },
    {
        "id": "owner",
        "term": "Owner",
        "summary": "A party that controls private artifact keys and decides which reviewed nodes may receive them.",
        "links": ["artifact", "precondition"],
    },
    {
        "id": "artifact",
        "term": "Artifact",
        "summary": "Encrypted data stored on CoveHub. Static artifacts are uploaded before execution; dynamic artifacts are emitted by runtime nodes.",
        "links": ["owner", "runtime-certificate"],
    },
    {
        "id": "workflow-bundle",
        "term": "Workflow Bundle",
        "summary": "A published object containing the normalized workflow, generated node compose files, and manifest metadata.",
        "links": ["publisher", "node", "exact-digest"],
    },
    {
        "id": "node",
        "term": "Node",
        "summary": "A workflow stage run in a TEE with compiler-injected sidecars and one or more workload services.",
        "links": ["runtime-certificate", "precondition", "tee"],
    },
    {
        "id": "runtime-certificate",
        "term": "Runtime Certificate",
        "summary": "A JSON object emitted by a node that binds results, inputs, outputs, and key material to the measured node compose.",
        "links": ["attestation", "node", "exact-digest"],
    },
    {
        "id": "precondition",
        "term": "Precondition",
        "summary": "A JsonLogic expression evaluated inside the node over admitted inputs and dependency certificates.",
        "links": ["artifact", "runtime-certificate"],
    },
    {
        "id": "tee",
        "term": "TEE",
        "summary": "Trusted Execution Environment. Cove currently targets Phala/dstack on Intel TDX.",
        "links": ["attestation", "node"],
    },
    {
        "id": "attestation",
        "term": "Attestation",
        "summary": "TEE evidence that report data was produced by a measured runtime. The UI does not verify it; use local Cove tooling.",
        "links": ["runtime-certificate", "tee"],
    },
    {
        "id": "latest",
        "term": "latest",
        "summary": "A mutable convenience pointer for a typed object slot. Prefer exact digest paths for reproducible references.",
        "links": ["exact-digest"],
    },
    {
        "id": "exact-digest",
        "term": "Exact Digest Path",
        "summary": "An immutable route ending in sha256:<digest>. Cove clients should recompute the digest after download.",
        "links": ["latest", "workflow-bundle", "artifact"],
    },
]
