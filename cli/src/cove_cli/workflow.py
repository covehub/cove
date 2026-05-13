from __future__ import annotations

import copy
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from .provisioning_identity import normalize_owner_server_url, owner_domain_from_url


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_LITERAL_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STATIC_HUB_PATH_RE = re.compile(
    r"^v1/artifacts/(?P<owner>[A-Za-z0-9][A-Za-z0-9._-]{0,255})/"
    r"(?P<artifact>[A-Za-z0-9][A-Za-z0-9._-]{0,127})/"
    r"(?P<reference>latest|sha256:[0-9a-f]{64})$"
)
DYNAMIC_HUB_PATH_RE = re.compile(
    r"^runtime/(?P<workflow_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})/artifacts/"
    r"(?P<artifact>[A-Za-z0-9][A-Za-z0-9._-]{0,127})/latest$"
)
INPUT_PLAINTEXT_HASH_VAR_RE = re.compile(
    r"^inputs\.(?P<artifact>[A-Za-z0-9][A-Za-z0-9._-]{0,127})\.plaintext_hash$"
)


@dataclass(frozen=True, slots=True)
class WorkflowLoadResult:
    workflow_path: Path
    workflow: WorkflowDefinition | None
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.workflow is not None and not self.errors


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    path: Path
    workflow_dir: Path
    workflow_id: str
    platform_provider: str
    platform_runtime: str
    owners: dict[str, OwnerDefinition]
    ephemeral_keypairs: dict[str, EphemeralKeypairDefinition]
    artifacts: dict[str, ArtifactDefinition]
    nodes: dict[str, NodeDefinition]
    normalized_data: dict[str, Any]


@dataclass(frozen=True, slots=True)
class OwnerDefinition:
    name: str
    owner_url: str
    owner_domain: str


@dataclass(frozen=True, slots=True)
class EphemeralKeypairDefinition:
    name: str
    algorithm: str


@dataclass(frozen=True, slots=True)
class ArtifactDefinition:
    name: str
    type: str
    owner: str
    owner_domain: str
    hub_path: str
    plaintext_hash: str | None
    producer_node: str | None = None
    producer_service: str | None = None

    @property
    def is_static(self) -> bool:
        return self.type == "static"

    @property
    def is_dynamic(self) -> bool:
        return self.type == "dynamic"

    def materialized_hub_path(self, workflow_publisher: str) -> str:
        if self.is_static:
            match = STATIC_HUB_PATH_RE.fullmatch(self.hub_path)
            if match is None:  # pragma: no cover - validation guarantees this
                return self.hub_path
            return (
                f"v1/artifacts/{self.owner_domain}/"
                f"{match.group('artifact')}/{match.group('reference')}"
            )
        return f"v1/runtime/{workflow_publisher}/{self.hub_path.removeprefix('runtime/')}"


@dataclass(frozen=True, slots=True)
class CustomCertificateFieldDefinition:
    schema: str
    schema_path: Path
    path: str | None


@dataclass(frozen=True, slots=True)
class ServiceDefinition:
    name: str
    compose_service: dict[str, Any]
    inputs: dict[str, str]
    outputs: dict[str, str]
    preconditions: Any | None
    custom_certificate_field: CustomCertificateFieldDefinition | None
    ephemeral_keypairs: list[str]
    should_terminate: bool


@dataclass(frozen=True, slots=True)
class NodeDefinition:
    name: str
    compose_path: Path
    compose_services: dict[str, Any]
    dependencies: list[str]
    services: dict[str, ServiceDefinition]
    used_artifacts: list[str]
    produced_artifacts: list[str]
    used_keypairs: list[str]


def load_workflow_definition(workflow_path: str | Path | None = None) -> WorkflowLoadResult:
    resolved_path = _resolve_workflow_path(workflow_path)
    errors: list[str] = []

    workflow_data = _load_yaml_mapping(resolved_path, "workflow", errors)
    if workflow_data is None:
        return WorkflowLoadResult(
            workflow_path=resolved_path,
            workflow=None,
            errors=errors,
        )

    workflow_dir = resolved_path.parent

    if workflow_data.get("cove_version") != 1:
        errors.append("top-level 'cove_version' must be 1")

    workflow_section = _require_mapping(workflow_data.get("workflow"), "top-level 'workflow'", errors)
    workflow_id = workflow_section.get("id")
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        errors.append("workflow.id must be a non-empty string")
        workflow_id = ""
    elif not _validate_identifier(workflow_id, "workflow.id", errors):
        workflow_id = ""

    platform = _require_mapping(workflow_data.get("platform"), "top-level 'platform'", errors)
    platform_provider = _required_non_empty_string(
        platform.get("provider"),
        "platform.provider",
        errors,
    )
    platform_runtime = _required_non_empty_string(
        platform.get("runtime"),
        "platform.runtime",
        errors,
    )
    if platform_provider and platform_provider != "phala":
        errors.append("platform.provider must be 'phala'")
    if platform_runtime and platform_runtime != "dstack":
        errors.append("platform.runtime must be 'dstack'")

    owners = _validate_owners(workflow_data.get("owners"), workflow_dir, errors)
    ephemeral_keypairs = _validate_ephemeral_keypairs(
        workflow_data.get("ephemeral_keypairs"),
        errors,
    )
    artifacts = _validate_artifacts(
        workflow_data.get("artifacts"),
        owners,
        workflow_id=workflow_id,
        errors=errors,
    )
    nodes_data = _require_mapping(workflow_data.get("nodes"), "top-level 'nodes'", errors)

    nodes: dict[str, NodeDefinition] = {}
    node_dependencies: dict[str, list[str]] = {}
    normalized_nodes: dict[str, Any] = {}
    artifact_producers: dict[str, list[tuple[str, str]]] = {}
    artifact_consumers: dict[str, set[str]] = {}

    for node_name, raw_node in nodes_data.items():
        if not _validate_identifier(node_name, f"node name {node_name!r}", errors):
            continue

        node_data = _require_mapping(raw_node, f"nodes.{node_name}", errors)
        compose_path = _resolve_existing_file(
            workflow_dir,
            node_data.get("compose"),
            f"nodes.{node_name}.compose",
            errors,
        )
        if compose_path is None:
            continue

        compose_data = _load_yaml_mapping(compose_path, "Compose", errors)
        if compose_data is None:
            continue

        compose_services = _require_mapping(
            compose_data.get("services"),
            f"Compose services in {compose_path}",
            errors,
        )
        dependencies = _validate_dependencies(
            node_name,
            node_data.get("dependencies"),
            errors,
        )
        node_dependencies[node_name] = dependencies

        services_data = _require_mapping(
            node_data.get("services"),
            f"nodes.{node_name}.services",
            errors,
        )
        services: dict[str, ServiceDefinition] = {}
        normalized_services: dict[str, Any] = {}
        used_artifacts: set[str] = set()
        produced_artifacts: set[str] = set()
        used_keypairs: set[str] = set()

        for service_name, raw_service in services_data.items():
            if not _validate_identifier(
                service_name,
                f"service name {node_name}.{service_name!r}",
                errors,
            ):
                continue

            if service_name not in compose_services:
                errors.append(
                    f"nodes.{node_name}.services.{service_name} is not defined in {compose_path}"
                )
                continue

            service_data = _require_mapping(
                raw_service,
                f"nodes.{node_name}.services.{service_name}",
                errors,
            )
            inputs = _validate_service_inputs(
                node_name=node_name,
                service_name=service_name,
                raw_inputs=service_data.get("inputs"),
                artifacts=artifacts,
                errors=errors,
            )
            used_artifacts.update(inputs.values())
            for artifact_name in inputs.values():
                artifact_consumers.setdefault(artifact_name, set()).add(node_name)

            outputs = _validate_service_outputs(
                node_name=node_name,
                service_name=service_name,
                raw_outputs=service_data.get("outputs"),
                artifacts=artifacts,
                errors=errors,
            )
            produced_artifacts.update(outputs.values())
            for artifact_name in outputs.values():
                artifact_producers.setdefault(artifact_name, []).append((node_name, service_name))

            preconditions = service_data.get("preconditions")
            should_terminate = _validate_should_terminate(
                node_name=node_name,
                service_name=service_name,
                raw_value=service_data.get("should_terminate"),
                errors=errors,
            )
            service_keypairs = _validate_service_ephemeral_keypairs(
                node_name=node_name,
                service_name=service_name,
                raw_value=service_data.get("ephemeral_keypairs"),
                declared_keypairs=ephemeral_keypairs,
                errors=errors,
            )
            used_keypairs.update(service_keypairs)
            custom_certificate_field = _validate_custom_certificate_field(
                node_name=node_name,
                service_name=service_name,
                raw_field=service_data.get("custom_certificate_field"),
                workflow_dir=workflow_dir,
                should_terminate=should_terminate,
                errors=errors,
            )

            compose_service = compose_services.get(service_name)
            if not isinstance(compose_service, dict):
                errors.append(
                    f"Compose service '{service_name}' in {compose_path} must be a YAML mapping"
                )
                continue

            service = ServiceDefinition(
                name=service_name,
                compose_service=copy.deepcopy(compose_service),
                inputs=inputs,
                outputs=outputs,
                preconditions=copy.deepcopy(preconditions),
                custom_certificate_field=custom_certificate_field,
                ephemeral_keypairs=service_keypairs,
                should_terminate=should_terminate,
            )
            services[service_name] = service
            normalized_services[service_name] = _normalized_service_data(service)

        node = NodeDefinition(
            name=node_name,
            compose_path=compose_path,
            compose_services=copy.deepcopy(compose_services),
            dependencies=dependencies,
            services=services,
            used_artifacts=sorted(used_artifacts),
            produced_artifacts=sorted(produced_artifacts),
            used_keypairs=sorted(used_keypairs),
        )
        nodes[node_name] = node
        normalized_nodes[node_name] = {
            "compose": str(node_data.get("compose")),
            **({"dependencies": dependencies} if dependencies else {}),
            "services": normalized_services,
        }

    _validate_dependency_targets(node_dependencies, nodes, errors)
    cycle = detect_dependency_cycle(node_dependencies)
    if cycle:
        errors.append(f"node dependency cycle detected: {' -> '.join(cycle)}")
    artifacts = _bind_dynamic_artifact_producers(
        artifacts,
        artifact_producers=artifact_producers,
        artifact_consumers=artifact_consumers,
        node_dependencies=node_dependencies,
        errors=errors,
    )

    if errors:
        return WorkflowLoadResult(
            workflow_path=resolved_path,
            workflow=None,
            errors=errors,
        )

    normalized_data = {
        "cove_version": 1,
        "workflow": {"id": workflow_id},
        "platform": {
            "provider": platform_provider,
            "runtime": platform_runtime,
        },
        "owners": {
            owner.name: _normalized_owner_payload(owner)
            for owner in owners.values()
        },
        "ephemeral_keypairs": {
            name: {"algorithm": keypair.algorithm}
            for name, keypair in ephemeral_keypairs.items()
        },
        "artifacts": {
            artifact_name: _normalized_artifact_data(artifact)
            for artifact_name, artifact in artifacts.items()
        },
        "nodes": normalized_nodes,
    }
    if not ephemeral_keypairs:
        normalized_data.pop("ephemeral_keypairs")

    workflow = WorkflowDefinition(
        path=resolved_path,
        workflow_dir=workflow_dir,
        workflow_id=workflow_id,
        platform_provider=platform_provider,
        platform_runtime=platform_runtime,
        owners=owners,
        ephemeral_keypairs=ephemeral_keypairs,
        artifacts=artifacts,
        nodes=nodes,
        normalized_data=normalized_data,
    )
    return WorkflowLoadResult(
        workflow_path=resolved_path,
        workflow=workflow,
        errors=[],
    )


def detect_dependency_cycle(node_dependencies: Mapping[str, Sequence[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []

    def visit(node_name: str) -> list[str] | None:
        if node_name in visited:
            return None
        if node_name in visiting:
            cycle_start = stack.index(node_name)
            return stack[cycle_start:] + [node_name]

        visiting.add(node_name)
        stack.append(node_name)
        for dependency in node_dependencies.get(node_name, []):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        stack.pop()
        visiting.remove(node_name)
        visited.add(node_name)
        return None

    for node_name in node_dependencies:
        cycle = visit(node_name)
        if cycle is not None:
            return cycle
    return []


def stable_topological_node_names(nodes: Mapping[str, NodeDefinition]) -> list[str]:
    authored_order = {
        node_name: index
        for index, node_name in enumerate(nodes.keys())
    }
    indegree = {
        node_name: len(node.dependencies)
        for node_name, node in nodes.items()
    }
    dependents: dict[str, list[str]] = {node_name: [] for node_name in nodes}
    for node_name, node in nodes.items():
        for dependency_name in node.dependencies:
            dependents.setdefault(dependency_name, []).append(node_name)

    ready = sorted(
        [
            node_name
            for node_name, dependency_count in indegree.items()
            if dependency_count == 0
        ],
        key=authored_order.__getitem__,
    )
    ordered: list[str] = []
    while ready:
        node_name = ready.pop(0)
        ordered.append(node_name)
        for dependent_name in sorted(
            dependents.get(node_name, []),
            key=authored_order.__getitem__,
        ):
            indegree[dependent_name] -= 1
            if indegree[dependent_name] == 0:
                ready.append(dependent_name)
                ready.sort(key=authored_order.__getitem__)

    if len(ordered) != len(nodes):  # pragma: no cover - load_workflow_definition already checks cycles
        raise ValueError("node dependency cycle detected")
    return ordered


def stable_topological_nodes(nodes: Mapping[str, NodeDefinition]) -> list[NodeDefinition]:
    return [nodes[node_name] for node_name in stable_topological_node_names(nodes)]


def anchored_input_literals(preconditions: Any | None) -> dict[str, set[str]]:
    anchors: dict[str, set[str]] = {}
    if preconditions is None:
        return anchors

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if set(node.keys()) == {"=="} and isinstance(node["=="], list) and len(node["=="]) == 2:
                left, right = node["=="]
                _record_anchor(left, right, anchors)
                _record_anchor(right, left, anchors)
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(preconditions)
    return anchors


def dump_normalized_workflow(workflow: WorkflowDefinition) -> str:
    return yaml.safe_dump(workflow.normalized_data, sort_keys=False)


def _record_anchor(
    candidate_var: Any,
    candidate_literal: Any,
    anchors: dict[str, set[str]],
) -> None:
    if not isinstance(candidate_var, dict) or set(candidate_var.keys()) != {"var"}:
        return
    raw_var = candidate_var.get("var")
    if not isinstance(raw_var, str):
        return
    match = INPUT_PLAINTEXT_HASH_VAR_RE.fullmatch(raw_var)
    if match is None:
        return
    if not isinstance(candidate_literal, str) or SHA256_LITERAL_RE.fullmatch(candidate_literal) is None:
        return
    anchors.setdefault(match.group("artifact"), set()).add(candidate_literal)


def _normalized_service_data(service: ServiceDefinition) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if service.inputs:
        payload["inputs"] = dict(service.inputs)
    if service.outputs:
        payload["outputs"] = dict(service.outputs)
    if service.ephemeral_keypairs:
        payload["ephemeral_keypairs"] = list(service.ephemeral_keypairs)
    if not service.should_terminate:
        payload["should_terminate"] = False
    if service.custom_certificate_field is not None:
        field: dict[str, Any] = {
            "schema": service.custom_certificate_field.schema,
        }
        if service.custom_certificate_field.path is not None:
            field["path"] = service.custom_certificate_field.path
        payload["custom_certificate_field"] = field
    if service.preconditions is not None:
        payload["preconditions"] = copy.deepcopy(service.preconditions)
    return payload


def _normalized_artifact_data(artifact: ArtifactDefinition) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": artifact.type,
        "owner": artifact.owner,
        "hub_path": artifact.hub_path,
    }
    if artifact.plaintext_hash is not None:
        payload["plaintext_hash"] = artifact.plaintext_hash
    return payload


def _resolve_workflow_path(workflow_path: str | Path | None) -> Path:
    if workflow_path is None:
        return (Path.cwd() / "workflow.cove.yaml").resolve()
    return Path(workflow_path).expanduser().resolve()


def _load_yaml_mapping(
    path: Path,
    label: str,
    errors: list[str],
) -> dict[str, Any] | None:
    if not path.exists():
        errors.append(f"{label} file not found: {path}")
        return None

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        errors.append(f"failed to parse {label} YAML at {path}: {exc}")
        return None

    if payload is None:
        errors.append(f"{label} file at {path} is empty")
        return None

    if not isinstance(payload, dict):
        errors.append(f"{label} file at {path} must be a YAML mapping")
        return None

    return payload


def _required_non_empty_string(value: Any, label: str, errors: list[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{label} must be a non-empty string")
        return ""
    return value.strip()


def _validate_owners(
    raw_owners: Any,
    workflow_dir: Path,
    errors: list[str],
) -> dict[str, OwnerDefinition]:
    owners = _require_mapping(raw_owners, "top-level 'owners'", errors)
    validated: dict[str, OwnerDefinition] = {}
    for owner_name, raw_owner in owners.items():
        if not _validate_identifier(owner_name, f"owner name {owner_name!r}", errors):
            continue
        if not isinstance(raw_owner, str):
            owner = _require_mapping(raw_owner, f"owners.{owner_name}", errors)
            for removed_key in ("provisioning_url", "provisioning_tls_certificate"):
                if removed_key in owner:
                    errors.append(
                        f"owners.{owner_name}.{removed_key} is no longer supported; set owners.{owner_name} to an HTTPS owner URL"
                    )
            errors.append(f"owners.{owner_name} must be an HTTPS owner URL string")
            continue
        owner_url = _required_non_empty_string(
            raw_owner,
            f"owners.{owner_name}",
            errors,
        )
        try:
            owner_url = normalize_owner_server_url(owner_url)
            owner_domain = owner_domain_from_url(owner_url)
        except ValueError as exc:
            errors.append(f"owners.{owner_name} {exc}")
            continue

        validated[owner_name] = OwnerDefinition(
            name=owner_name,
            owner_url=owner_url,
            owner_domain=owner_domain,
        )
    return validated


def _normalized_owner_payload(owner: OwnerDefinition) -> str:
    return owner.owner_url


def _validate_ephemeral_keypairs(
    raw_keypairs: Any,
    errors: list[str],
) -> dict[str, EphemeralKeypairDefinition]:
    if raw_keypairs is None:
        return {}

    keypairs = _require_mapping(raw_keypairs, "top-level 'ephemeral_keypairs'", errors)
    validated: dict[str, EphemeralKeypairDefinition] = {}
    for keypair_name, raw_keypair in keypairs.items():
        if not _validate_identifier(
            keypair_name,
            f"ephemeral keypair name {keypair_name!r}",
            errors,
        ):
            continue
        keypair = _require_mapping(
            raw_keypair,
            f"ephemeral_keypairs.{keypair_name}",
            errors,
        )
        algorithm = _required_non_empty_string(
            keypair.get("algorithm"),
            f"ephemeral_keypairs.{keypair_name}.algorithm",
            errors,
        )
        if algorithm and algorithm != "ed25519":
            errors.append(
                f"ephemeral_keypairs.{keypair_name}.algorithm '{algorithm}' is not supported in this pass"
            )
            continue
        validated[keypair_name] = EphemeralKeypairDefinition(
            name=keypair_name,
            algorithm=algorithm,
        )
    return validated


def _validate_artifacts(
    raw_artifacts: Any,
    owners: Mapping[str, OwnerDefinition],
    *,
    workflow_id: str,
    errors: list[str],
) -> dict[str, ArtifactDefinition]:
    artifacts = _require_mapping(raw_artifacts, "top-level 'artifacts'", errors)
    validated: dict[str, ArtifactDefinition] = {}
    for artifact_name, raw_artifact in artifacts.items():
        if not _validate_identifier(artifact_name, f"artifact name {artifact_name!r}", errors):
            continue
        artifact = _require_mapping(raw_artifact, f"artifacts.{artifact_name}", errors)
        artifact_type = artifact.get("type")
        owner_name = artifact.get("owner")
        if not isinstance(owner_name, str) or owner_name not in owners:
            errors.append(f"artifacts.{artifact_name}.owner must reference a declared owner")
            continue

        hub_path = _required_non_empty_string(
            artifact.get("hub_path"),
            f"artifacts.{artifact_name}.hub_path",
            errors,
        )
        if artifact_type == "static":
            match = STATIC_HUB_PATH_RE.fullmatch(hub_path)
            if match is None:
                errors.append(
                    f"artifacts.{artifact_name}.hub_path must be a concrete static artifact path"
                )
                continue
            owner = owners[owner_name]
            path_owner = match.group("owner")
            if path_owner not in {owner_name, owner.owner_domain} or match.group("artifact") != artifact_name:
                errors.append(
                    f"artifacts.{artifact_name}.hub_path must match "
                    f"'v1/artifacts/{owner_name}/{artifact_name}/<sha256:digest-or-latest>' "
                    f"or 'v1/artifacts/{owner.owner_domain}/{artifact_name}/<sha256:digest-or-latest>'"
                )
                continue
            hub_path = f"v1/artifacts/{owner.name}/{artifact_name}/{match.group('reference')}"

            plaintext_hash = artifact.get("plaintext_hash")
            if (
                not isinstance(plaintext_hash, str)
                or SHA256_LITERAL_RE.fullmatch(plaintext_hash) is None
            ):
                errors.append(
                    f"artifacts.{artifact_name}.plaintext_hash must be a sha256:<hex> string"
                )
                continue

            validated[artifact_name] = ArtifactDefinition(
                name=artifact_name,
                type="static",
                owner=owner_name,
                owner_domain=owner.owner_domain,
                hub_path=hub_path,
                plaintext_hash=plaintext_hash,
            )
            continue

        if artifact_type == "dynamic":
            match = DYNAMIC_HUB_PATH_RE.fullmatch(hub_path)
            if (
                match is None
                or match.group("workflow_id") != workflow_id
                or match.group("artifact") != artifact_name
            ):
                errors.append(
                    "artifacts."
                    f"{artifact_name}.hub_path must match "
                    f"'runtime/{workflow_id}/artifacts/{artifact_name}/latest'"
                )
                continue
            if artifact.get("plaintext_hash") is not None:
                errors.append(
                    f"artifacts.{artifact_name}.plaintext_hash is not allowed for dynamic artifacts"
                )
                continue

            validated[artifact_name] = ArtifactDefinition(
                name=artifact_name,
                type="dynamic",
                owner=owner_name,
                owner_domain=owners[owner_name].owner_domain,
                hub_path=hub_path,
                plaintext_hash=None,
            )
            continue

        if artifact_type == "workflow_output":
            errors.append(
                f"artifacts.{artifact_name}.type 'workflow_output' is not supported; use 'dynamic'"
            )
        else:
            errors.append(
                f"artifacts.{artifact_name}.type must be 'static' or 'dynamic'"
            )
    return validated


def _validate_dependencies(
    node_name: str,
    raw_dependencies: Any,
    errors: list[str],
) -> list[str]:
    if raw_dependencies is None:
        return []
    if not isinstance(raw_dependencies, list):
        errors.append(f"nodes.{node_name}.dependencies must be a list when present")
        return []

    dependencies: list[str] = []
    seen: set[str] = set()
    for index, dependency in enumerate(raw_dependencies):
        if not isinstance(dependency, str):
            errors.append(f"nodes.{node_name}.dependencies[{index}] must be a node name string")
            continue
        if dependency == node_name:
            errors.append(f"nodes.{node_name}.dependencies must not include itself")
            continue
        if dependency in seen:
            errors.append(f"nodes.{node_name}.dependencies contains duplicate '{dependency}'")
            continue
        seen.add(dependency)
        dependencies.append(dependency)
    return dependencies


def _validate_dependency_targets(
    node_dependencies: Mapping[str, Sequence[str]],
    nodes: Mapping[str, NodeDefinition],
    errors: list[str],
) -> None:
    node_names = set(nodes.keys())
    for node_name, dependencies in node_dependencies.items():
        for dependency in dependencies:
            if dependency not in node_names:
                errors.append(
                    f"nodes.{node_name}.dependencies references unknown node '{dependency}'"
                )


def _validate_service_inputs(
    *,
    node_name: str,
    service_name: str,
    raw_inputs: Any,
    artifacts: Mapping[str, ArtifactDefinition],
    errors: list[str],
) -> dict[str, str]:
    if raw_inputs is None:
        return {}

    inputs = _require_mapping(
        raw_inputs,
        f"nodes.{node_name}.services.{service_name}.inputs",
        errors,
    )
    validated: dict[str, str] = {}
    for raw_path, raw_artifact_name in inputs.items():
        if not isinstance(raw_path, str) or not raw_path:
            errors.append(
                f"nodes.{node_name}.services.{service_name}.inputs keys must be non-empty strings"
            )
            continue
        if not PurePosixPath(raw_path).is_absolute():
            errors.append(
                f"nodes.{node_name}.services.{service_name}.inputs path '{raw_path}' must be absolute"
            )
        if not isinstance(raw_artifact_name, str):
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.inputs['{raw_path}'] "
                "must reference an artifact name"
            )
            continue
        if raw_artifact_name not in artifacts:
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.inputs['{raw_path}'] "
                f"references unknown or unsupported artifact '{raw_artifact_name}'"
            )
            continue
        validated[raw_path] = raw_artifact_name
    return validated


def _validate_service_outputs(
    *,
    node_name: str,
    service_name: str,
    raw_outputs: Any,
    artifacts: Mapping[str, ArtifactDefinition],
    errors: list[str],
) -> dict[str, str]:
    if raw_outputs is None:
        return {}

    outputs = _require_mapping(
        raw_outputs,
        f"nodes.{node_name}.services.{service_name}.outputs",
        errors,
    )
    validated: dict[str, str] = {}
    for raw_path, raw_artifact_name in outputs.items():
        if not isinstance(raw_path, str) or not raw_path:
            errors.append(
                f"nodes.{node_name}.services.{service_name}.outputs keys must be non-empty strings"
            )
            continue
        if not PurePosixPath(raw_path).is_absolute():
            errors.append(
                f"nodes.{node_name}.services.{service_name}.outputs path '{raw_path}' must be absolute"
            )
        if not isinstance(raw_artifact_name, str):
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.outputs['{raw_path}'] "
                "must reference an artifact name"
            )
            continue
        artifact = artifacts.get(raw_artifact_name)
        if artifact is None:
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.outputs['{raw_path}'] "
                f"references unknown artifact '{raw_artifact_name}'"
            )
            continue
        if not artifact.is_dynamic:
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.outputs['{raw_path}'] "
                f"must reference a dynamic artifact, got '{raw_artifact_name}'"
            )
            continue
        validated[raw_path] = raw_artifact_name
    return validated


def _validate_service_ephemeral_keypairs(
    *,
    node_name: str,
    service_name: str,
    raw_value: Any,
    declared_keypairs: Mapping[str, EphemeralKeypairDefinition],
    errors: list[str],
) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list):
        errors.append(
            f"nodes.{node_name}.services.{service_name}.ephemeral_keypairs must be a list when present"
        )
        return []

    names: list[str] = []
    seen: set[str] = set()
    for index, raw_name in enumerate(raw_value):
        if not isinstance(raw_name, str):
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.ephemeral_keypairs[{index}] "
                "must be a keypair name string"
            )
            continue
        if raw_name not in declared_keypairs:
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.ephemeral_keypairs[{index}] "
                f"references unknown keypair '{raw_name}'"
            )
            continue
        if raw_name in seen:
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.ephemeral_keypairs "
                f"contains duplicate '{raw_name}'"
            )
            continue
        seen.add(raw_name)
        names.append(raw_name)
    return names


def _validate_should_terminate(
    *,
    node_name: str,
    service_name: str,
    raw_value: Any,
    errors: list[str],
) -> bool:
    if raw_value is None:
        return True
    if not isinstance(raw_value, bool):
        errors.append(
            f"nodes.{node_name}.services.{service_name}.should_terminate must be a boolean when present"
        )
        return True
    return raw_value


def _validate_custom_certificate_field(
    *,
    node_name: str,
    service_name: str,
    raw_field: Any,
    workflow_dir: Path,
    should_terminate: bool,
    errors: list[str],
) -> CustomCertificateFieldDefinition | None:
    if raw_field is None:
        return None
    if not should_terminate:
        errors.append(
            "nodes."
            f"{node_name}.services.{service_name}.custom_certificate_field "
            "is not allowed when should_terminate is false"
        )
        return None

    field = _require_mapping(
        raw_field,
        f"nodes.{node_name}.services.{service_name}.custom_certificate_field",
        errors,
    )
    schema = _required_non_empty_string(
        field.get("schema"),
        "nodes."
        f"{node_name}.services.{service_name}.custom_certificate_field.schema",
        errors,
    )
    schema_path = _resolve_file_like_path(workflow_dir, schema)
    if not schema_path.is_file():
        errors.append(
            "nodes."
            f"{node_name}.services.{service_name}.custom_certificate_field.schema file not found: {schema_path}"
        )
        return None

    path = field.get("path")
    if path is not None:
        if not isinstance(path, str) or not path.strip():
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.custom_certificate_field.path "
                "must be a non-empty string when present"
            )
            return None
        if not PurePosixPath(path).is_absolute():
            errors.append(
                "nodes."
                f"{node_name}.services.{service_name}.custom_certificate_field.path "
                "must be absolute when present"
            )
            return None
        path = path.strip()

    return CustomCertificateFieldDefinition(
        schema=schema,
        schema_path=schema_path,
        path=path,
    )


def _bind_dynamic_artifact_producers(
    artifacts: Mapping[str, ArtifactDefinition],
    *,
    artifact_producers: Mapping[str, list[tuple[str, str]]],
    artifact_consumers: Mapping[str, set[str]],
    node_dependencies: Mapping[str, Sequence[str]],
    errors: list[str],
) -> dict[str, ArtifactDefinition]:
    validated: dict[str, ArtifactDefinition] = {}
    for artifact_name, artifact in artifacts.items():
        if artifact.is_static:
            validated[artifact_name] = artifact
            continue

        producers = artifact_producers.get(artifact_name, [])
        if len(producers) != 1:
            if not producers:
                errors.append(
                    f"dynamic artifact '{artifact_name}' must be produced by exactly one service output"
                )
            else:
                producers_text = ", ".join(
                    f"{node_name}.{service_name}" for node_name, service_name in producers
                )
                errors.append(
                    f"dynamic artifact '{artifact_name}' is produced by multiple services: {producers_text}"
                )
            validated[artifact_name] = artifact
            continue

        producer_node, producer_service = producers[0]
        for consumer_node in sorted(artifact_consumers.get(artifact_name, set())):
            if consumer_node == producer_node:
                errors.append(
                    f"dynamic artifact '{artifact_name}' cannot be consumed by its producer node '{producer_node}'"
                )
                continue
            if producer_node not in set(node_dependencies.get(consumer_node, [])):
                errors.append(
                    "node "
                    f"'{consumer_node}' consumes dynamic artifact '{artifact_name}' "
                    f"but does not declare producer node '{producer_node}' in dependencies"
                )

        validated[artifact_name] = ArtifactDefinition(
            name=artifact.name,
            type=artifact.type,
            owner=artifact.owner,
            owner_domain=artifact.owner_domain,
            hub_path=artifact.hub_path,
            plaintext_hash=artifact.plaintext_hash,
            producer_node=producer_node,
            producer_service=producer_service,
        )
    return validated


def _require_mapping(value: Any, label: str, errors: list[str]) -> dict[str, Any]:
    if value is None:
        errors.append(f"{label} is required")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label} must be a YAML mapping")
        return {}
    return value


def _validate_identifier(value: str, label: str, errors: list[str]) -> bool:
    if IDENTIFIER_RE.fullmatch(value) is None:
        errors.append(f"{label} must match {IDENTIFIER_RE.pattern}")
        return False
    return True


def _resolve_existing_file(
    workflow_dir: Path,
    raw_path: Any,
    label: str,
    errors: list[str],
) -> Path | None:
    path_text = _required_non_empty_string(raw_path, label, errors)
    if not path_text:
        return None
    path = _resolve_file_like_path(workflow_dir, path_text)
    if not path.is_file():
        errors.append(f"{label} file not found: {path}")
        return None
    return path


def _resolve_file_like_path(workflow_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = workflow_dir / path
    return path.resolve()
