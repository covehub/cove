from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib.parse import urlencode, urlparse

from .canonical_images import CanonicalImageError, canonical_container_ref
from .config import ensure_local_config
from .covehub import COVEHUB_USER_AGENT
from .owner_identity import OwnerIdentityError, verify_owner_signed_response_payload
from .provisioning_identity import fetch_owner_identity_document, owner_domain_from_url
from .workflow import (
    CustomCertificateFieldDefinition,
    NodeDefinition,
    ServiceDefinition,
    WorkflowDefinition,
    dump_normalized_workflow,
    load_workflow_definition,
    stable_topological_nodes,
    workflow_with_generated_artifact_hub_paths,
)
from .security_policy import evaluate_workflow_security_policy
from .yaml_support import dump_yaml


ROLE_IMAGE_NAMES = {
    "artifact_provisioner": "cove-artifact-provisioner",
    "dependency_certificate_fetcher": "cove-dependency-certificate-fetcher",
    "precondition_checker": "cove-precondition-checker",
    "service_certificate_writer": "cove-service-certificate-writer",
    "key_manager": "cove-key-manager",
    "node_certificate_writer": "cove-node-certificate-writer",
}
_DIGEST_PINNED_IMAGE_RE = re.compile(r"^(?P<name>.+)@(?P<digest>sha256:[0-9a-f]{64})$")
_STATIC_EXACT_HUB_PATH_RE = re.compile(
    r"^v1/artifacts/(?P<owner>[^/]+)/(?P<artifact>[^/]+)/(?P<digest>sha256:[0-9a-f]{64})$"
)
_COMPOSE_ENV_RE = re.compile(r"\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)(:-?(?P<default>[^}]*))?\}")
_COMPOSE_HASH_PLACEHOLDER = "sha256:" + ("0" * 64)
_COVE_RUNTIME_VOLUME = "cove_runtime"
_COVE_METRICS_VOLUME = "cove_metrics"
_X509_COMMON_NAME_MAX_BYTES = 64
_FNV1A64_OFFSET = 0xCBF29CE484222325
_FNV1A64_PRIME = 0x100000001B3
_FNV1A64_MASK = (1 << 64) - 1


class CompileCommandError(RuntimeError):
    """Raised when workflow compilation fails."""


@dataclass(frozen=True, slots=True)
class CompileArtifact:
    workflow_id: str
    workflow_path: Path
    workflow: WorkflowDefinition
    build_dir: Path
    generated_nodes: list[Path]
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class _ServiceCompilation:
    compose_service: dict[str, Any]
    copy_services: dict[str, dict[str, Any]]
    result_mounts: list[dict[str, Any]]
    result_source_path: str | None
    result_schema: dict[str, Any] | None
    output_source_paths: dict[str, str]
    output_mounts: dict[str, list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class _ResolvedOwnerIdentity:
    name: str
    owner_url: str
    owner_domain: str
    identity_document: dict[str, object]


def _keypair_certificate_common_name(
    *, workflow_id: str, node_name: str, keypair_name: str
) -> str:
    raw_common_name = f"{workflow_id}.{node_name}.{keypair_name}"
    if len(raw_common_name.encode("utf-8")) <= _X509_COMMON_NAME_MAX_BYTES:
        return raw_common_name

    digest = _fnv1a64_hex(raw_common_name.encode("utf-8"))
    suffix = f"-{digest}"
    prefix = f"cove-{node_name}.{keypair_name}"
    max_prefix_bytes = _X509_COMMON_NAME_MAX_BYTES - len(suffix.encode("ascii"))
    return f"{_truncate_utf8(prefix, max_prefix_bytes)}{suffix}"


def _fnv1a64_hex(payload: bytes) -> str:
    value = _FNV1A64_OFFSET
    for byte in payload:
        value ^= byte
        value = (value * _FNV1A64_PRIME) & _FNV1A64_MASK
    return f"{value:016x}"


def _truncate_utf8(value: str, max_bytes: int) -> str:
    output: list[str] = []
    used_bytes = 0
    for char in value:
        char_len = len(char.encode("utf-8"))
        if used_bytes + char_len > max_bytes:
            break
        output.append(char)
        used_bytes += char_len
    return "".join(output)


def resolve_image_reference_to_digest(image_reference: str) -> str:
    expanded = _expand_compose_image_reference(image_reference)
    if _DIGEST_PINNED_IMAGE_RE.fullmatch(expanded):
        return expanded

    repository = _strip_image_tag(expanded)
    repo_digest = _resolve_repo_digest(expanded, repository=repository)
    if repo_digest is not None:
        return repo_digest

    raise CompileCommandError(
        "failed to resolve image digest for "
        f"{image_reference!r}: image is not already pinned and has no local RepoDigest; "
        "publish/pull the image first"
    )


def compile_workflow_artifact(
    workflow_path: str | Path | None = None,
    *,
    cove_home: str | Path | None = None,
) -> CompileArtifact:
    load_result = load_workflow_definition(workflow_path)
    if load_result.workflow is None:
        raise CompileCommandError("\n".join(load_result.errors))

    workflow = load_result.workflow
    policy_report = evaluate_workflow_security_policy(workflow)
    if policy_report.errors:
        raise CompileCommandError("\n".join(policy_report.errors))
    config = ensure_local_config(cove_home)
    if not config.covehub_server_url:
        raise CompileCommandError(
            f"'covehub_server_url' must be configured in {config.path} for cove compile"
        )
    if not config.owner_server_url:
        raise CompileCommandError(
            f"'owner_server_url' must be configured in {config.path} for cove compile"
        )
    workflow_publisher_domain = owner_domain_from_url(config.owner_server_url)
    parsed_server_url = urlparse(config.covehub_server_url)
    if parsed_server_url.scheme not in {"http", "https"} or not parsed_server_url.netloc:
        raise CompileCommandError(
            f"'covehub_server_url' in {config.path} must be an http or https URL"
        )

    _require_phala_dstack_platform(workflow)
    attestation_config = {
        "mode": "phala_dstack",
        "provider": workflow.platform_provider,
        "runtime": workflow.platform_runtime,
    }
    warnings = list(policy_report.warnings)
    resolved_owners = _resolve_owner_identities(workflow)
    workflow = _workflow_with_generated_hub_paths(
        workflow=workflow,
        workflow_publisher=workflow_publisher_domain,
        resolved_owners=resolved_owners,
    )

    build_dir = workflow.workflow_dir / "build"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    normalized_path = build_dir / "workflow.normalized.cove.yaml"
    normalized_path.write_text(
        dump_normalized_workflow(workflow),
        encoding="utf-8",
    )

    generated_nodes: list[Path] = []
    generated_compose_hashes: dict[str, str] = {}
    for node in stable_topological_nodes(workflow.nodes):
        node_dir = build_dir / "nodes" / node.name
        compose_path, compose_hash = _compile_node(
            workflow=workflow,
            node=node,
            covehub_server_url=config.covehub_server_url,
            workflow_publisher=workflow_publisher_domain,
            node_dir=node_dir,
            dependency_compose_hashes=generated_compose_hashes,
            attestation_config=attestation_config,
            resolved_owners=resolved_owners,
        )
        generated_nodes.append(compose_path)
        generated_compose_hashes[node.name] = compose_hash

    return CompileArtifact(
        workflow_id=workflow.workflow_id,
        workflow_path=workflow.path,
        workflow=workflow,
        build_dir=build_dir,
        generated_nodes=generated_nodes,
        warnings=warnings,
    )


def compile_workflow(
    workflow_path: str | Path | None = None,
    *,
    cove_home: str | Path | None = None,
) -> str:
    artifact = compile_workflow_artifact(workflow_path, cove_home=cove_home)
    normalized_path = artifact.build_dir / "workflow.normalized.cove.yaml"
    return "\n".join(
        [
            *[f"WARNING: {warning}" for warning in artifact.warnings],
            f"Compiled workflow '{artifact.workflow_id}'",
            f"Normalized workflow: {normalized_path}",
            *[f"Generated node compose: {path}" for path in artifact.generated_nodes],
        ]
    )


def _compile_node(
    *,
    workflow: WorkflowDefinition,
    node: NodeDefinition,
    covehub_server_url: str,
    workflow_publisher: str,
    node_dir: Path,
    dependency_compose_hashes: dict[str, str],
    attestation_config: dict[str, str],
    resolved_owners: dict[str, _ResolvedOwnerIdentity],
) -> tuple[Path, str]:
    node_dir.mkdir(parents=True, exist_ok=True)
    services: dict[str, Any] = {}
    generated_volumes: dict[str, dict[str, Any]] = {
        _COVE_RUNTIME_VOLUME: {},
        _COVE_METRICS_VOLUME: {},
    }

    if node.used_keypairs:
        key_manager_config = {
            "keypairs": [
                {
                    "name": keypair_name,
                    "algorithm": workflow.ephemeral_keypairs[keypair_name].algorithm,
                    "private_key_path": f"/cove/ephemeral_keypairs/{keypair_name}/private.pem",
                    "public_key_path": f"/cove/ephemeral_keypairs/{keypair_name}/public.pem",
                    "certificate_path": f"/cove/ephemeral_keypairs/{keypair_name}/certificate.pem",
                    "metadata_path": f"/cove/ephemeral_keypairs/{keypair_name}/metadata.json",
                    "certificate_common_name": _keypair_certificate_common_name(
                        workflow_id=workflow.workflow_id,
                        node_name=node.name,
                        keypair_name=keypair_name,
                    ),
                }
                for keypair_name in node.used_keypairs
            ]
        }
        services["cove_key_manager"] = _compiled_sidecar_service(
            service_name="cove_key_manager",
            role="key_manager",
            config_payload=key_manager_config,
        )

    for artifact_name in node.used_artifacts:
        artifact = workflow.artifacts[artifact_name]
        owner = workflow.owners[artifact.owner]
        resolved_owner = resolved_owners[owner.name]
        provisioner_config = {
            "mode": "static_input" if artifact.is_static else "dynamic_input",
            "artifact_name": artifact_name,
            "artifact_provisioner_image": _role_image("artifact_provisioner"),
            "hub_path": artifact.hub_path,
            "owner": owner.name,
            "owners": _owner_url_map(owner),
            "covehub_server_url": covehub_server_url,
            "workflow_publisher_domain": workflow_publisher,
            "workflow_id": workflow.workflow_id,
            "node_id": node.name,
            "staged_plaintext_path": f"/cove/inputs/{artifact_name}/{artifact_name}",
            "metadata_path": f"/cove/inputs/{artifact_name}/metadata.json",
            "attestation": attestation_config,
        }
        _add_owner_identity_config(provisioner_config, resolved_owner)
        if artifact.is_static:
            provisioner_config["expected_plaintext_hash"] = artifact.plaintext_hash
        else:
            producer_node = artifact.producer_node
            if producer_node is None:
                raise CompileCommandError(
                    f"dynamic artifact '{artifact_name}' is missing producer metadata"
                )
            provisioner_config["producer_certificate_path"] = (
                f"/cove/certificates/{producer_node}/certificate.json"
            )
        services[f"cove_provision_{artifact_name}"] = _compiled_sidecar_service(
            service_name=f"cove_provision_{artifact_name}",
            role="artifact_provisioner",
            config_payload=provisioner_config,
            depends_on=(
                {
                    "cove_dependency_certificate_fetcher": {
                        "condition": "service_completed_successfully"
                    }
                }
                if artifact.is_dynamic
                else None
            ),
            network_mode="host",
            extra_volumes=_attestation_sidecar_volumes(attestation_config),
        )

    if node.dependencies:
        services["cove_dependency_certificate_fetcher"] = _compiled_sidecar_service(
            service_name="cove_dependency_certificate_fetcher",
            role="dependency_certificate_fetcher",
            config_payload={
                "node_name": node.name,
                "covehub_server_url": covehub_server_url,
                "workflow_publisher_domain": workflow_publisher,
                "workflow_id": workflow.workflow_id,
                # Multi-minute CPU-bound nodes are normal for real workflows,
                # so downstream dependency polling needs a wider window.
                "timeout_seconds": 600.0,
                "poll_interval_seconds": 0.5,
                "dependencies": [
                    {
                        "node_name": dependency_name,
                        "certificate_path": f"/cove/certificates/{dependency_name}/certificate.json",
                        "expected_workflow_id": workflow.workflow_id,
                        "expected_node_id": dependency_name,
                        "expected_generated_node_compose_hash": dependency_compose_hashes[
                            dependency_name
                        ],
                    }
                    for dependency_name in node.dependencies
                ],
            },
            network_mode="host",
        )

    compiled_services: dict[str, _ServiceCompilation] = {}
    for service in node.services.values():
        compiled_service = _compile_workload_service(
            node=node,
            service=service,
            generated_volumes=generated_volumes,
        )
        compiled_services[service.name] = compiled_service

        precondition_depends_on = {
            f"cove_provision_{artifact_name}": {
                "condition": "service_completed_successfully"
            }
            for artifact_name in sorted(set(service.inputs.values()))
        }
        if node.dependencies:
            precondition_depends_on["cove_dependency_certificate_fetcher"] = {
                "condition": "service_completed_successfully"
            }

        services[f"cove_preconditions_{service.name}"] = _compiled_sidecar_service(
            service_name=f"cove_preconditions_{service.name}",
            role="precondition_checker",
            config_payload={
                "node_name": node.name,
                "service_name": service.name,
                "preconditions": service.preconditions,
                "inputs": {
                    artifact_name: f"/cove/inputs/{artifact_name}/metadata.json"
                    for artifact_name in sorted(set(service.inputs.values()))
                },
                "certificates": {
                    dependency_name: f"/cove/certificates/{dependency_name}/certificate.json"
                    for dependency_name in node.dependencies
                },
            },
            depends_on=precondition_depends_on,
        )

        services.update(compiled_service.copy_services)
        services[service.name] = compiled_service.compose_service

        if service.should_terminate:
            services[f"cove_service_certificate_writer_{service.name}"] = _compiled_sidecar_service(
                service_name=f"cove_service_certificate_writer_{service.name}",
                role="service_certificate_writer",
                config_payload={
                    "node_name": node.name,
                    "service_name": service.name,
                    "result_source_path": compiled_service.result_source_path,
                    "result_output_path": f"/cove/certificates/{node.name}/{service.name}/result.json",
                    "schema": compiled_service.result_schema,
                },
                depends_on={
                    service.name: {
                        "condition": "service_completed_successfully"
                    }
                },
                extra_volumes=compiled_service.result_mounts,
            )

    for artifact_name in node.produced_artifacts:
        artifact = workflow.artifacts[artifact_name]
        owner = workflow.owners[artifact.owner]
        resolved_owner = resolved_owners[owner.name]
        producer_service_name = artifact.producer_service
        if producer_service_name is None:
            raise CompileCommandError(
                f"dynamic artifact '{artifact_name}' is missing producer service metadata"
            )
        producer_service = node.services[producer_service_name]
        producer_compilation = compiled_services[producer_service_name]
        output_source_path = producer_compilation.output_source_paths.get(artifact_name)
        output_mounts = producer_compilation.output_mounts.get(artifact_name)
        if output_source_path is None or output_mounts is None:
            raise CompileCommandError(
                f"dynamic artifact '{artifact_name}' is missing compiled output binding metadata"
            )

        provisioner_config = {
            "mode": "dynamic_output",
            "artifact_name": artifact_name,
            "artifact_provisioner_image": _role_image("artifact_provisioner"),
            "hub_path": artifact.hub_path,
            "owner": owner.name,
            "owners": _owner_url_map(owner),
            "covehub_server_url": covehub_server_url,
            "workflow_publisher_domain": workflow_publisher,
            "workflow_id": workflow.workflow_id,
            "node_id": node.name,
            "output_source_path": output_source_path,
            "metadata_path": f"/cove/outputs/{artifact_name}/metadata.json",
            "attestation": attestation_config,
        }
        _add_owner_identity_config(provisioner_config, resolved_owner)
        services[f"cove_publish_{artifact_name}"] = _compiled_sidecar_service(
            service_name=f"cove_publish_{artifact_name}",
            role="artifact_provisioner",
            config_payload=provisioner_config,
            depends_on={
                producer_service_name: _service_dependency_condition(producer_service)
            },
            extra_volumes=[
                *output_mounts,
                *_attestation_sidecar_volumes(attestation_config),
            ],
            network_mode="host",
        )

    services["cove_node_certificate_writer"] = _compiled_sidecar_service(
        service_name="cove_node_certificate_writer",
        role="node_certificate_writer",
        config_payload={
            "covehub_server_url": covehub_server_url,
            "workflow_publisher_domain": workflow_publisher,
            "workflow_id": workflow.workflow_id,
            "node_name": node.name,
            "certificate_path": f"/cove/certificates/{node.name}/certificate.json",
            "inputs": [
                {
                    "name": artifact_name,
                    "path": f"/cove/inputs/{artifact_name}/metadata.json",
                }
                for artifact_name in node.used_artifacts
            ],
            "ephemeral_keypairs": [
                {
                    "name": keypair_name,
                    "path": f"/cove/ephemeral_keypairs/{keypair_name}/metadata.json",
                }
                for keypair_name in node.used_keypairs
            ],
            "results": [
                {
                    "name": service.name,
                    "path": f"/cove/certificates/{node.name}/{service.name}/result.json",
                }
                for service in node.services.values()
                if service.should_terminate
            ],
            "outputs": [
                {
                    "name": artifact_name,
                    "path": f"/cove/outputs/{artifact_name}/metadata.json",
                }
                for artifact_name in node.produced_artifacts
            ],
            "dependencies": [
                {
                    "name": dependency_name,
                    "path": f"/cove/certificates/{dependency_name}/certificate.json",
                }
                for dependency_name in node.dependencies
            ],
            "runtime_metrics": [
                {
                    "name": service_name,
                    "path": f"/cove_metrics/{service_name}.json",
                }
                for service_name in services
                if service_name != "cove_node_certificate_writer"
            ],
            "attestation": attestation_config,
        },
        depends_on=_node_certificate_writer_dependencies(node),
        network_mode="host",
        extra_volumes=_attestation_sidecar_volumes(attestation_config),
    )

    compose_payload = {"services": services, "volumes": generated_volumes}
    compose_hash = reviewed_compose_hash(compose_payload)
    _inject_compose_hash(compose_payload, compose_hash)
    compose_text = dump_yaml(compose_payload)
    compose_path = node_dir / "compose.generated.yaml"
    compose_path.write_text(compose_text, encoding="utf-8")
    (node_dir / "compose.generated.sha256").write_text(
        compose_hash + "\n",
        encoding="utf-8",
    )
    return compose_path, compose_hash


def _resolve_owner_identities(
    workflow: WorkflowDefinition,
) -> dict[str, _ResolvedOwnerIdentity]:
    resolved: dict[str, _ResolvedOwnerIdentity] = {}
    for owner in workflow.owners.values():
        try:
            identity_document = fetch_owner_identity_document(
                owner_url=owner.owner_url,
                expected_owner_domain=owner.owner_domain,
            )
        except Exception as exc:
            raise CompileCommandError(
                f"failed to resolve owner '{owner.name}' at {owner.owner_url}: {exc}"
            ) from exc

        resolved[owner.name] = _ResolvedOwnerIdentity(
            name=owner.name,
            owner_url=owner.owner_url,
            owner_domain=owner.owner_domain,
            identity_document=identity_document,
        )
    return resolved


def _workflow_with_generated_hub_paths(
    *,
    workflow: WorkflowDefinition,
    workflow_publisher: str,
    resolved_owners: dict[str, _ResolvedOwnerIdentity],
) -> WorkflowDefinition:
    hub_paths: dict[str, str] = {}
    for artifact_name, artifact in workflow.artifacts.items():
        if artifact.is_dynamic:
            hub_paths[artifact_name] = (
                f"v1/runtime/{workflow_publisher}/{workflow.workflow_id}/"
                f"artifacts/{artifact_name}/latest"
            )
            continue

        plaintext_hash = artifact.plaintext_hash
        if plaintext_hash is None:  # pragma: no cover - validation guarantees this
            raise CompileCommandError(f"static artifact '{artifact_name}' is missing plaintext_hash")
        owner = resolved_owners[artifact.owner]
        response = _fetch_owner_static_artifact_resolution(
            owner=owner,
            artifact_id=artifact_name,
            plaintext_hash=plaintext_hash,
        )
        hub_path = _required_owner_response_string(response, "hub_path")
        owner_domain = _required_owner_response_string(response, "owner_domain")
        owner_url = _required_owner_response_string(response, "owner_url")
        response_artifact_id = _required_owner_response_string(response, "artifact_id")
        response_plaintext_hash = _required_owner_response_string(response, "plaintext_hash")
        ciphertext_hash = _required_owner_response_string(response, "ciphertext_hash")
        if owner_domain != owner.owner_domain:
            raise CompileCommandError(
                f"owner '{owner.name}' returned owner_domain {owner_domain!r}, "
                f"expected {owner.owner_domain!r}"
            )
        if owner_url.rstrip("/") != owner.owner_url.rstrip("/"):
            raise CompileCommandError(
                f"owner '{owner.name}' returned owner_url {owner_url!r}, "
                f"expected {owner.owner_url!r}"
            )
        if response_artifact_id != artifact_name:
            raise CompileCommandError(
                f"owner '{owner.name}' returned artifact_id {response_artifact_id!r}, "
                f"expected {artifact_name!r}"
            )
        if response_plaintext_hash != plaintext_hash:
            raise CompileCommandError(
                f"owner '{owner.name}' returned plaintext_hash {response_plaintext_hash!r}, "
                f"expected {plaintext_hash!r}"
            )
        match = _STATIC_EXACT_HUB_PATH_RE.fullmatch(hub_path)
        if (
            match is None
            or match.group("owner") != owner.owner_domain
            or match.group("artifact") != artifact_name
            or match.group("digest") != ciphertext_hash
        ):
            raise CompileCommandError(
                f"owner '{owner.name}' returned invalid static hub_path for {artifact_name}: {hub_path}"
            )
        hub_paths[artifact_name] = hub_path

    return workflow_with_generated_artifact_hub_paths(workflow, hub_paths)


def _fetch_owner_static_artifact_resolution(
    *,
    owner: _ResolvedOwnerIdentity,
    artifact_id: str,
    plaintext_hash: str,
) -> dict[str, Any]:
    query = urlencode({"artifact_id": artifact_id, "plaintext_hash": plaintext_hash})
    url = f"{owner.owner_url.rstrip('/')}/v1/artifacts/by-id?{query}"
    request = urllib_request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": COVEHUB_USER_AGENT,
        },
        method="GET",
    )
    try:
        with urllib_request.urlopen(request, timeout=5.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        detail = _http_error_detail(exc)
        raise CompileCommandError(
            f"owner '{owner.name}' has no provisioned artifact {artifact_id!r} "
            f"for plaintext hash {plaintext_hash}: HTTP {exc.code}: {detail}"
        ) from exc
    except urllib_error.URLError as exc:
        raise CompileCommandError(
            f"failed to reach owner '{owner.name}' at {url}: {exc.reason}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise CompileCommandError(
            f"owner '{owner.name}' returned invalid JSON for artifact lookup"
        ) from exc
    if not isinstance(payload, dict):
        raise CompileCommandError(
            f"owner '{owner.name}' returned a non-object artifact lookup response"
        )
    try:
        verified = verify_owner_signed_response_payload(
            payload,
            owner_identity=owner.identity_document,
        )
    except OwnerIdentityError as exc:
        raise CompileCommandError(
            f"owner '{owner.name}' artifact lookup signature verification failed: {exc}"
        ) from exc
    return verified


def _required_owner_response_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise CompileCommandError(f"owner artifact lookup response field {key!r} must be a non-empty string")
    return value


def _http_error_detail(exc: urllib_error.HTTPError) -> str:
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


def _add_owner_identity_config(
    config_payload: dict[str, Any],
    resolved_owner: _ResolvedOwnerIdentity,
) -> None:
    config_payload["owner_identity"] = resolved_owner.identity_document


def _owner_url_map(owner) -> dict[str, str]:
    return {owner.name: owner.owner_url}


def _compile_workload_service(
    *,
    node: NodeDefinition,
    service: ServiceDefinition,
    generated_volumes: dict[str, dict[str, Any]],
) -> _ServiceCompilation:
    compose_service = copy.deepcopy(service.compose_service)
    image_value = compose_service.get("image")
    if not isinstance(image_value, str) or not image_value.strip():
        raise CompileCommandError(
            f"authored Compose service '{service.name}' for node '{node.name}' must define an image"
        )
    compose_service["image"] = resolve_image_reference_to_digest(image_value)
    environment = compose_service.setdefault("environment", {})
    if not isinstance(environment, dict):
        raise CompileCommandError(
            f"authored Compose service '{service.name}' for node '{node.name}' must use mapping environment"
        )
    environment["COVE_SERVICE_NAME"] = service.name
    environment["COVE_SERVICE_ROLE"] = "workload"
    environment["COVE_RUNTIME_METRICS_PATH"] = f"/cove_metrics/{service.name}.json"
    volumes = _normalize_volumes(compose_service.get("volumes"))
    volumes.append(_named_volume_mount(_COVE_METRICS_VOLUME, "/cove_metrics", read_only=False))
    depends_on = _normalize_depends_on(compose_service.get("depends_on"))

    depends_on[f"cove_preconditions_{service.name}"] = {
        "condition": "service_completed_successfully"
    }
    if service.ephemeral_keypairs:
        depends_on["cove_key_manager"] = {
            "condition": "service_completed_successfully"
        }
    if node.dependencies:
        depends_on["cove_dependency_certificate_fetcher"] = {
            "condition": "service_completed_successfully"
        }

    copy_services: dict[str, dict[str, Any]] = {}
    read_only_parent_mounts: set[str] = set()
    for container_path, artifact_name in service.inputs.items():
        parent_target = str(PurePosixPath(container_path).parent)
        volume_name = _stable_volume_name("cove-input", parent_target)
        generated_volumes.setdefault(volume_name, {})
        copy_service_name = _input_copy_service_name(
            service_name=service.name,
            artifact_name=artifact_name,
            target_path=container_path,
        )
        copy_services[copy_service_name] = _input_copy_service(
            service_name=copy_service_name,
            dependency_service=f"cove_provision_{artifact_name}",
            source_path=f"/cove/inputs/{artifact_name}/{artifact_name}",
            target_path=container_path,
            volume_name=volume_name,
            parent_target=parent_target,
        )
        depends_on[copy_service_name] = {"condition": "service_completed_successfully"}
        if parent_target not in read_only_parent_mounts:
            read_only_parent_mounts.add(parent_target)
            volumes.append(_named_volume_mount(volume_name, parent_target, read_only=True))

    if service.ephemeral_keypairs or node.dependencies:
        volumes.append(_named_volume_mount(_COVE_RUNTIME_VOLUME, "/cove", read_only=True))

    result_mounts: list[dict[str, Any]] = []
    result_source_path: str | None = None
    result_schema: dict[str, Any] | None = None
    output_source_paths: dict[str, str] = {}
    output_mounts: dict[str, list[dict[str, Any]]] = {}
    writable_parent_mounts: dict[str, dict[str, Any]] = {}

    def ensure_writable_parent_mount(target_path: str) -> dict[str, Any]:
        parent_target = str(PurePosixPath(target_path).parent)
        existing = writable_parent_mounts.get(parent_target)
        if existing is not None:
            return existing
        volume_name = _stable_volume_name(
            "cove-bind",
            f"{service.name}:{parent_target}",
        )
        generated_volumes.setdefault(volume_name, {})
        mount = _named_volume_mount(volume_name, parent_target, read_only=False)
        writable_parent_mounts[parent_target] = mount
        volumes.append(mount)
        return mount

    field = service.custom_certificate_field
    if field is not None and field.path is not None:
        result_source_path = field.path
        result_mount = ensure_writable_parent_mount(field.path)
        result_mounts.append(result_mount)
        try:
            result_schema_payload = json.loads(field.schema_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise CompileCommandError(
                f"{node.name}.{service.name}.custom_certificate_field.schema "
                f"must contain valid JSON: {field.schema_path}"
            ) from exc
        if not isinstance(result_schema_payload, dict):
            raise CompileCommandError(
                f"{node.name}.{service.name}.custom_certificate_field.schema "
                "must contain a JSON object"
            )
        result_schema = result_schema_payload

    for output_path, artifact_name in service.outputs.items():
        output_mount = ensure_writable_parent_mount(output_path)
        output_source_paths[artifact_name] = output_path
        output_mounts[artifact_name] = [output_mount]

    if volumes:
        compose_service["volumes"] = volumes
    if depends_on:
        compose_service["depends_on"] = depends_on

    return _ServiceCompilation(
        compose_service=compose_service,
        copy_services=copy_services,
        result_mounts=result_mounts,
        result_source_path=result_source_path,
        result_schema=result_schema,
        output_source_paths=output_source_paths,
        output_mounts=output_mounts,
    )


def _compiled_sidecar_service(
    *,
    service_name: str,
    role: str,
    config_payload: dict[str, Any],
    depends_on: dict[str, Any] | None = None,
    extra_volumes: list[dict[str, Any]] | None = None,
    healthcheck: dict[str, Any] | None = None,
    network_mode: str | None = None,
) -> dict[str, Any]:
    service = {
        "image": _role_image(role),
        "environment": {
            "COVE_CONFIG_JSON": json.dumps(config_payload, indent=2, sort_keys=True),
            "COVE_SERVICE_NAME": service_name,
            "COVE_SERVICE_ROLE": role,
            "COVE_COMPOSE_HASH": _COMPOSE_HASH_PLACEHOLDER,
            "COVE_RUNTIME_METRICS_PATH": f"/cove_metrics/{service_name}.json",
        },
        "volumes": [
            _named_volume_mount(_COVE_RUNTIME_VOLUME, "/cove", read_only=False),
            _named_volume_mount(_COVE_METRICS_VOLUME, "/cove_metrics", read_only=False),
        ],
    }
    if extra_volumes:
        service["volumes"].extend(extra_volumes)
    if depends_on:
        service["depends_on"] = depends_on
    if healthcheck is not None:
        service["healthcheck"] = healthcheck
    if network_mode is not None:
        service["network_mode"] = network_mode
    return service


def _attestation_sidecar_volumes(attestation_config: dict[str, str]) -> list[dict[str, Any]]:
    if not _attestation_uses_dstack(attestation_config):
        return []
    return [
        _bind_mount(
            source="/var/run/dstack.sock",
            target="/var/run/dstack.sock",
            read_only=True,
        )
    ]


def _node_certificate_writer_dependencies(node: NodeDefinition) -> dict[str, Any]:
    dependencies: dict[str, Any] = {}
    for artifact_name in node.used_artifacts:
        dependencies[f"cove_provision_{artifact_name}"] = {
            "condition": "service_completed_successfully"
        }
    for artifact_name in node.produced_artifacts:
        dependencies[f"cove_publish_{artifact_name}"] = {
            "condition": "service_completed_successfully"
        }
    if node.dependencies:
        dependencies["cove_dependency_certificate_fetcher"] = {
            "condition": "service_completed_successfully"
        }
    if node.used_keypairs:
        dependencies["cove_key_manager"] = {
            "condition": "service_completed_successfully"
        }
    for service in node.services.values():
        if service.should_terminate:
            dependencies[f"cove_service_certificate_writer_{service.name}"] = {
                "condition": "service_completed_successfully"
            }
        else:
            dependencies[service.name] = _service_dependency_condition(service)
    return dependencies


def _service_dependency_condition(service: ServiceDefinition) -> dict[str, str]:
    condition = (
        "service_completed_successfully"
        if service.should_terminate
        else (
            "service_healthy"
            if "healthcheck" in service.compose_service
            else "service_started"
        )
    )
    return {"condition": condition}


def _normalize_volumes(raw_volumes: Any) -> list[dict[str, Any]]:
    if raw_volumes is None:
        return []
    if not isinstance(raw_volumes, list):
        raise CompileCommandError("authored Compose volumes must be a list when present")
    normalized: list[dict[str, Any]] = []
    for raw_volume in raw_volumes:
        if isinstance(raw_volume, dict):
            normalized.append(copy.deepcopy(raw_volume))
        elif isinstance(raw_volume, str):
            normalized.append({"type": "bind", "source": raw_volume, "target": raw_volume})
        else:
            raise CompileCommandError("authored Compose volumes entries must be strings or mappings")
    return normalized


def _normalize_depends_on(raw_depends_on: Any) -> dict[str, Any]:
    if raw_depends_on is None:
        return {}
    if isinstance(raw_depends_on, list):
        return {
            str(service_name): {"condition": "service_started"}
            for service_name in raw_depends_on
        }
    if isinstance(raw_depends_on, dict):
        return copy.deepcopy(raw_depends_on)
    raise CompileCommandError("authored Compose depends_on must be a list or mapping when present")


def _input_copy_service(
    *,
    service_name: str,
    dependency_service: str,
    source_path: str,
    target_path: str,
    volume_name: str,
    parent_target: str,
) -> dict[str, Any]:
    metrics_path = f"/cove_metrics/{service_name}.json"
    return {
        "image": _canonical_image("cove-base"),
        "command": [
            "python",
            "-c",
            (
                "import json, os, shutil, time; "
                "from pathlib import Path; "
                "started=time.monotonic(); "
                "started_iso=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()); "
                "source = Path(os.environ['COVE_INPUT_SOURCE']); "
                "target = Path(os.environ['COVE_INPUT_TARGET']); "
                "target.parent.mkdir(parents=True, exist_ok=True); "
                "copy_started=time.monotonic(); "
                "shutil.copyfile(source, target); "
                "payload={"
                "'schema_version':'cove_runtime_metrics_v1',"
                "'service_name':os.environ['COVE_SERVICE_NAME'],"
                "'role':'input_copy',"
                "'started_at':started_iso,"
                "'ended_at':time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),"
                "'wall_seconds':round(time.monotonic()-started,6),"
                "'phase_timings_seconds':{'copy_input_seconds':round(time.monotonic()-copy_started,6)},"
                "'exit_code':0,"
                "'ok':True}; "
                "metrics=Path(os.environ['COVE_RUNTIME_METRICS_PATH']); "
                "metrics.parent.mkdir(parents=True, exist_ok=True); "
                "metrics.write_text(json.dumps(payload, indent=2, sort_keys=True)+'\\n', encoding='utf-8')"
            ),
        ],
        "environment": {
            "COVE_INPUT_SOURCE": source_path,
            "COVE_INPUT_TARGET": target_path,
            "COVE_SERVICE_NAME": service_name,
            "COVE_SERVICE_ROLE": "input_copy",
            "COVE_RUNTIME_METRICS_PATH": metrics_path,
        },
        "depends_on": {
            dependency_service: {
                "condition": "service_completed_successfully"
            }
        },
        "volumes": [
            _named_volume_mount(_COVE_RUNTIME_VOLUME, "/cove", read_only=True),
            _named_volume_mount(_COVE_METRICS_VOLUME, "/cove_metrics", read_only=False),
            _named_volume_mount(volume_name, parent_target, read_only=False),
        ],
    }


def _named_volume_mount(volume_name: str, target: str, read_only: bool) -> dict[str, Any]:
    return {
        "type": "volume",
        "source": volume_name,
        "target": target,
        "read_only": read_only,
    }


def _bind_mount(*, source: str | Path, target: str, read_only: bool) -> dict[str, Any]:
    return {
        "type": "bind",
        "source": str(source),
        "target": target,
        "read_only": read_only,
    }


def _stable_volume_name(prefix: str, seed: str) -> str:
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _input_copy_service_name(service_name: str, artifact_name: str, target_path: str) -> str:
    seed = f"{service_name}:{artifact_name}:{target_path}"
    return f"cove_copy_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"


def _sha256_text(payload: str) -> str:
    return f"sha256:{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"


def reviewed_compose_hash(compose_payload: dict[str, Any]) -> str:
    normalized = copy.deepcopy(compose_payload)
    services = normalized.get("services")
    if isinstance(services, dict):
        for service in services.values():
            if not isinstance(service, dict):
                continue
            environment = service.get("environment")
            if isinstance(environment, dict) and "COVE_COMPOSE_HASH" in environment:
                environment["COVE_COMPOSE_HASH"] = _COMPOSE_HASH_PLACEHOLDER
    return _sha256_text(dump_yaml(normalized))


def _inject_compose_hash(compose_payload: dict[str, Any], compose_hash: str) -> None:
    services = compose_payload.get("services")
    if not isinstance(services, dict):
        return
    for service in services.values():
        if not isinstance(service, dict):
            continue
        environment = service.get("environment")
        if isinstance(environment, dict) and "COVE_COMPOSE_HASH" in environment:
            environment["COVE_COMPOSE_HASH"] = compose_hash


def _require_phala_dstack_platform(workflow: WorkflowDefinition) -> None:
    if workflow.platform_provider != "phala" or workflow.platform_runtime != "dstack":
        raise CompileCommandError(
            "Cove runtime deployment now requires platform.provider=phala and platform.runtime=dstack"
        )


def _attestation_uses_dstack(attestation_config: dict[str, str]) -> bool:
    return (
        attestation_config.get("mode") == "phala_dstack"
        and attestation_config.get("provider") == "phala"
        and attestation_config.get("runtime") == "dstack"
    )


def _expand_compose_image_reference(image_reference: str) -> str:
    def replacer(match: re.Match[str]) -> str:
        name = match.group("name")
        default = match.group("default")
        value = os.getenv(name)
        if value:
            return value
        if default is not None:
            return default
        raise CompileCommandError(
            f"image reference {image_reference!r} depends on unset environment variable {name!r}"
        )

    return _COMPOSE_ENV_RE.sub(replacer, image_reference)


def _strip_image_tag(image_reference: str) -> str:
    if "@" in image_reference:
        return image_reference.split("@", 1)[0]
    last_slash = image_reference.rfind("/")
    last_colon = image_reference.rfind(":")
    if last_colon > last_slash:
        return image_reference[:last_colon]
    return image_reference


def _resolve_repo_digest(image_reference: str, *, repository: str) -> str | None:
    result = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            image_reference,
            "--format",
            "{{json .RepoDigests}}",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None

    raw_payload = (result.stdout or "").strip()
    if not raw_payload:
        return None
    try:
        repo_digests = json.loads(raw_payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(repo_digests, list):
        return None

    for entry in repo_digests:
        if isinstance(entry, str) and entry.startswith(f"{repository}@"):
            return entry
    for entry in repo_digests:
        if isinstance(entry, str) and _DIGEST_PINNED_IMAGE_RE.fullmatch(entry):
            return entry
    return None


def _role_image(role: str) -> str:
    try:
        image_name = ROLE_IMAGE_NAMES[role]
    except KeyError as exc:  # pragma: no cover - defensive
        raise CompileCommandError(f"unsupported sidecar role: {role}") from exc
    return _canonical_image(image_name)


def _canonical_image(image_name: str) -> str:
    try:
        image_reference = canonical_container_ref(image_name)
    except CanonicalImageError as exc:
        raise CompileCommandError(str(exc)) from exc
    if image_reference is None:
        raise CompileCommandError(
            f"canonical sidecar ref not found for image {image_name!r}"
        )
    return image_reference
