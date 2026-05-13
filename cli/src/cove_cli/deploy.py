from __future__ import annotations

import asyncio
import copy
import hashlib
import inspect
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .compile import reviewed_compose_hash
from .config import LocalConfig, ensure_local_config
from .publish import MaterializedNode, MaterializedWorkflowBundle, parse_published_ref, pull_workflow_bundle
from .yaml_support import dump_yaml


_SUPPORTED_DSTACK_SOCKET = "/var/run/dstack.sock"
_GENERATED_COMPOSE_FILENAME = "compose.generated.yaml"
_SUPPORTED_TOP_LEVEL_COMPOSE_KEYS = {"services", "volumes"}
_SUPPORTED_SERVICE_KEYS = {
    "command",
    "depends_on",
    "entrypoint",
    "environment",
    "healthcheck",
    "hostname",
    "image",
    "init",
    "labels",
    "network_mode",
    "ports",
    "restart",
    "stdin_open",
    "stop_grace_period",
    "stop_signal",
    "tty",
    "user",
    "volumes",
    "working_dir",
}


class DeployCommandError(RuntimeError):
    """Raised when a pulled workflow bundle cannot be deployed to Phala."""


@dataclass(frozen=True, slots=True)
class DeployNodeResult:
    node_id: str
    deployment_name: str
    cvm_id: str
    status: str
    app_id: str
    compose_hash: str


@dataclass(frozen=True, slots=True)
class PhalaDeployOptions:
    instance_type: str | None = None
    region: str | None = None
    os_image: str | None = None
    node_id: int | None = None
    disk_size_gb: int | None = None
    public_logs: bool | None = None
    public_sysinfo: bool | None = None
    listed: bool | None = None
    docker_username: str | None = None
    docker_access_token: str | None = None
    docker_registry: str | None = None


@dataclass(frozen=True, slots=True)
class _PhalaDockerRegistryCredentials:
    username: str
    access_token: str
    registry: str | None


@dataclass(frozen=True, slots=True)
class _TranslatedNodeDeployment:
    deployment_name: str
    compose_text: str


def deploy_workflow(
    published_ref: str,
    *,
    cove_home: str | Path | None = None,
    phala_options: PhalaDeployOptions | None = None,
) -> str:
    config = ensure_local_config(cove_home)
    if not config.covehub_server_url:
        raise DeployCommandError(
            f"'covehub_server_url' must be configured in {config.path} for cove deploy"
        )
    if not config.phala_cloud_api_key:
        raise DeployCommandError(
            f"'phala_cloud_api_key' must be configured in {config.path} for cove deploy"
        )
    phala_options = phala_options or PhalaDeployOptions()
    if not phala_options.instance_type:
        raise DeployCommandError(
            "cove deploy requires --phala-instance-type, for example --phala-instance-type tdx.small"
        )
    registry_credentials = _resolve_phala_registry_credentials(config, phala_options)
    registry_env = _phala_registry_env(registry_credentials)
    registry_env_keys = [key for key, _value in registry_env]

    parsed_ref = parse_published_ref(published_ref)
    bundle = pull_workflow_bundle(
        server_url=config.covehub_server_url,
        publisher=parsed_ref.publisher,
        workflow_id=parsed_ref.workflow_id,
        reference=parsed_ref.reference,
        cove_home=cove_home,
    )
    nodes = _deployment_nodes(bundle)
    client = _create_phala_client(config.phala_cloud_api_key)
    results: list[DeployNodeResult] = []
    try:
        for node in nodes:
            translated = _translate_node_deployment(bundle, node)
            provision_payload = _phala_provision_payload(
                translated=translated,
                options=phala_options,
                env_keys=registry_env_keys,
            )
            provision_response = client.provision_cvm(provision_payload)
            app_id = _required_model_string(provision_response, "app_id")
            compose_hash = _required_model_string(provision_response, "compose_hash")
            commit_payload: dict[str, Any] = {
                "app_id": app_id,
                "compose_hash": compose_hash,
            }
            if registry_env:
                app_env_encrypt_pubkey = _required_model_string(
                    provision_response,
                    "app_env_encrypt_pubkey",
                )
                commit_payload["encrypted_env"] = _encrypt_phala_env_vars(
                    registry_env,
                    app_env_encrypt_pubkey,
                )
                commit_payload["env_keys"] = registry_env_keys
            commit_response = client.commit_cvm_provision(commit_payload)
            results.append(
                DeployNodeResult(
                    node_id=node.node_id,
                    deployment_name=translated.deployment_name,
                    cvm_id=_required_model_string(commit_response, "id"),
                    status=_required_model_string(commit_response, "status"),
                    app_id=app_id,
                    compose_hash=compose_hash,
                )
            )
    except Exception as exc:
        raise DeployCommandError(f"failed to deploy {published_ref} to Phala: {exc}") from exc
    finally:
        close_method = getattr(client, "close", None)
        if callable(close_method):
            close_method()

    return "\n".join(
        [
            f"Deployed workflow '{parsed_ref.publisher}/{parsed_ref.workflow_id}' to Phala",
            f"Pulled bundle path: {bundle.root_path}",
            "Node deployments:",
            *[
                (
                    f"- {result.node_id}: {result.deployment_name} "
                    f"(cvm_id={result.cvm_id}, status={result.status}, app_id={result.app_id}, "
                    f"compose_hash={result.compose_hash})"
                )
                for result in results
            ],
        ]
    )


def _phala_provision_payload(
    *,
    translated: _TranslatedNodeDeployment,
    options: PhalaDeployOptions,
    env_keys: list[str],
) -> dict[str, Any]:
    if not options.instance_type:
        raise DeployCommandError("Phala instance_type is required")
    compose_file: dict[str, Any] = {
        "runner": "docker-compose",
        "name": translated.deployment_name,
        "docker_compose_file": translated.compose_text,
    }
    _set_optional(compose_file, "public_logs", options.public_logs)
    _set_optional(compose_file, "public_sysinfo", options.public_sysinfo)
    if env_keys:
        compose_file["allowed_envs"] = env_keys

    payload: dict[str, Any] = {
        "name": translated.deployment_name,
        "instance_type": options.instance_type,
        "compose_file": compose_file,
    }
    _set_optional(payload, "region", options.region)
    _set_optional(payload, "image", options.os_image)
    _set_optional(payload, "node_id", options.node_id)
    _set_optional(payload, "disk_size", options.disk_size_gb)
    _set_optional(payload, "listed", options.listed)
    if env_keys:
        payload["env_keys"] = env_keys
    return payload


def _set_optional(payload: dict[str, Any], key: str, value: Any | None) -> None:
    if value is not None:
        payload[key] = value


def _create_phala_client(api_key: str) -> Any:
    try:
        from phala_cloud import create_client
    except ImportError as exc:  # pragma: no cover - depends on runtime environment
        raise DeployCommandError(
            "phala-cloud must be installed in the CLI environment for cove deploy"
        ) from exc
    return create_client(api_key=api_key)


def _resolve_phala_registry_credentials(
    config: LocalConfig,
    options: PhalaDeployOptions,
) -> _PhalaDockerRegistryCredentials | None:
    username = _normalize_optional_string(
        options.docker_username
        if options.docker_username is not None
        else config.phala_docker_username
    )
    access_token = _normalize_optional_string(
        options.docker_access_token
        if options.docker_access_token is not None
        else config.phala_docker_access_token
    )
    registry = _normalize_phala_docker_registry(
        options.docker_registry
        if options.docker_registry is not None
        else config.phala_docker_registry
    )

    if not username and not access_token:
        if registry:
            raise DeployCommandError(
                "Phala Docker registry requires --phala-docker-username and --phala-docker-access-token"
            )
        return None
    if not username or not access_token:
        raise DeployCommandError(
            "Phala Docker registry username and access token must be configured together"
        )
    return _PhalaDockerRegistryCredentials(
        username=username,
        access_token=access_token,
        registry=registry,
    )


def _phala_registry_env(
    credentials: _PhalaDockerRegistryCredentials | None,
) -> list[tuple[str, str]]:
    if credentials is None:
        return []
    env = [
        ("DSTACK_DOCKER_USERNAME", credentials.username),
        ("DSTACK_DOCKER_PASSWORD", credentials.access_token),
    ]
    if credentials.registry:
        env.append(("DSTACK_DOCKER_REGISTRY", credentials.registry))
    return env


def _encrypt_phala_env_vars(
    env_vars: list[tuple[str, str]],
    public_key_hex: str,
) -> str:
    try:
        from dstack_sdk.encrypt_env_vars import EnvVar
        from phala_cloud.utils import encrypt_env_vars

        result = encrypt_env_vars(
            [EnvVar(key=key, value=value) for key, value in env_vars],
            public_key_hex,
        )
        if inspect.isawaitable(result):
            result = asyncio.run(result)
        if not isinstance(result, str) or not result:
            raise DeployCommandError("Phala registry credential encryption returned no data")
        return result
    except DeployCommandError:
        raise
    except Exception as exc:  # pragma: no cover - depends on Phala SDK internals
        raise DeployCommandError(
            "failed to encrypt Phala registry credentials"
        ) from exc


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _normalize_phala_docker_registry(value: str | None) -> str | None:
    registry = _normalize_optional_string(value)
    if registry is None:
        return None
    normalized = registry.removeprefix("https://").removeprefix("http://").rstrip("/")
    if normalized in {"docker.io", "index.docker.io", "registry-1.docker.io"}:
        return None
    if normalized == "index.docker.io/v1":
        return None
    return registry


def _translate_node_deployment(
    bundle: MaterializedWorkflowBundle,
    node: MaterializedNode,
) -> _TranslatedNodeDeployment:
    if Path(node.compose_path).name != _GENERATED_COMPOSE_FILENAME:
        raise DeployCommandError(
            f"node {node.node_id} must point to {_GENERATED_COMPOSE_FILENAME}, got {node.compose_path!r}"
        )
    original_compose_path = bundle.root_path / node.compose_path
    original_compose_text = original_compose_path.read_text(encoding="utf-8")
    try:
        compose_payload = yaml.safe_load(original_compose_text)
    except yaml.YAMLError as exc:
        raise DeployCommandError(
            f"pulled compose for node {node.node_id} is not valid YAML: {original_compose_path}"
        ) from exc
    if not isinstance(compose_payload, dict):
        raise DeployCommandError(f"pulled compose for node {node.node_id} must be a YAML mapping")
    observed_compose_hash = reviewed_compose_hash(compose_payload)
    if observed_compose_hash != node.compose_hash:
        raise DeployCommandError(
            f"pulled compose hash mismatch for node {node.node_id}: {observed_compose_hash} != {node.compose_hash}"
        )
    _validate_compose_payload(compose_payload, node_id=node.node_id)
    services = compose_payload.get("services")
    if not isinstance(services, dict) or not services:
        raise DeployCommandError(f"pulled compose for node {node.node_id} must define services")
    named_volumes = _compose_named_volumes(compose_payload, node_id=node.node_id)

    translated_services: dict[str, Any] = {}
    for service_name, raw_service in services.items():
        if not isinstance(raw_service, dict):
            raise DeployCommandError(
                f"pulled compose service {service_name!r} for node {node.node_id} must be a mapping"
            )
        _validate_service_payload(service_name, raw_service, node_id=node.node_id)
        translated_services[service_name] = _translate_service(
            service_name=service_name,
            service=raw_service,
            named_volumes=named_volumes,
        )

    translated_compose = dump_yaml(
        {
            "services": translated_services,
            "volumes": named_volumes,
        }
    )
    return _TranslatedNodeDeployment(
        deployment_name=_deployment_name(
            publisher=bundle.publisher,
            workflow_id=bundle.workflow_id,
            node_id=node.node_id,
        ),
        compose_text=translated_compose,
    )


def _translate_service(
    *,
    service_name: str,
    service: dict[str, Any],
    named_volumes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    translated = copy.deepcopy(service)
    raw_volumes = translated.get("volumes")
    translated_volumes: list[dict[str, Any]] = []

    if raw_volumes is not None:
        if not isinstance(raw_volumes, list):
            raise DeployCommandError(f"service {service_name!r} volumes must be a list")
        for raw_volume in raw_volumes:
            translated_volumes.append(
                _translate_volume(
                    service_name=service_name,
                    raw_volume=raw_volume,
                    named_volumes=named_volumes,
                )
            )

    if translated_volumes:
        translated["volumes"] = translated_volumes
    else:
        translated.pop("volumes", None)

    network_mode = translated.pop("network_mode", None)
    if network_mode is not None and network_mode != "host":
        raise DeployCommandError(
            f"service {service_name!r} uses unsupported network_mode {network_mode!r}"
        )

    return translated


def _compose_named_volumes(
    compose_payload: dict[str, Any],
    *,
    node_id: str,
) -> dict[str, dict[str, Any]]:
    raw_volumes = compose_payload.get("volumes")
    if raw_volumes is None:
        return {}
    if not isinstance(raw_volumes, dict):
        raise DeployCommandError(f"pulled compose for node {node_id} volumes must be a mapping")

    named_volumes: dict[str, dict[str, Any]] = {}
    for volume_name, raw_config in raw_volumes.items():
        if not isinstance(volume_name, str) or not volume_name:
            raise DeployCommandError(
                f"pulled compose for node {node_id} has an invalid named volume"
            )
        if raw_config is None:
            named_volumes[volume_name] = {}
        elif isinstance(raw_config, dict):
            named_volumes[volume_name] = copy.deepcopy(raw_config)
        else:
            raise DeployCommandError(
                f"pulled compose volume {volume_name!r} for node {node_id} must be a mapping"
            )
    return named_volumes


def _translate_volume(
    *,
    service_name: str,
    raw_volume: Any,
    named_volumes: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not isinstance(raw_volume, dict):
        raise DeployCommandError(f"service {service_name!r} volume entries must be mappings")
    volume_type = raw_volume.get("type")
    source = _required_non_empty_string(raw_volume, "source", service_name)
    target = _required_non_empty_string(raw_volume, "target", service_name)
    read_only = bool(raw_volume.get("read_only", False))

    if volume_type == "volume":
        if source not in named_volumes:
            raise DeployCommandError(
                f"service {service_name!r} uses undeclared named volume {source!r}"
            )
        return {
            "type": "volume",
            "source": source,
            "target": target,
            "read_only": read_only,
        }

    if volume_type != "bind":
        raise DeployCommandError(
            f"service {service_name!r} uses unsupported volume type {volume_type!r}"
        )

    if source != _SUPPORTED_DSTACK_SOCKET:
        if source.startswith(".") or not Path(source).is_absolute():
            raise DeployCommandError(
                f"service {service_name!r} uses unsupported relative bind source {source!r}"
            )
        raise DeployCommandError(
            f"service {service_name!r} uses unsupported absolute bind source {source!r}"
        )

    if target != _SUPPORTED_DSTACK_SOCKET:
        raise DeployCommandError(
            f"service {service_name!r} mounts {_SUPPORTED_DSTACK_SOCKET} at unsupported target {target!r}"
        )
    if not _service_may_mount_dstack_socket(service_name):
        raise DeployCommandError(
            f"service {service_name!r} is not allowed to mount {_SUPPORTED_DSTACK_SOCKET}"
        )
    return {
        "type": "bind",
        "source": source,
        "target": target,
        "read_only": read_only,
    }


def _deployment_name(*, publisher: str, workflow_id: str, node_id: str) -> str:
    raw = f"cove-{publisher}-{workflow_id}-{node_id}".lower()
    sanitized = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in raw).strip("-")
    sanitized = re.sub(r"-{2,}", "-", sanitized)
    if len(sanitized) <= 63:
        return sanitized
    digest = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()[:10]
    prefix = sanitized[:52].rstrip("-")
    return f"{prefix}-{digest}"


def _required_non_empty_string(payload: dict[str, Any], key: str, service_name: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise DeployCommandError(
            f"service {service_name!r} volume field {key!r} must be a non-empty string"
        )
    return value


def _required_model_string(model: Any, key: str) -> str:
    value = _model_value(model, key)
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str) or not value:
        raise DeployCommandError(f"Phala response is missing {key!r}")
    return value


def _model_value(model: Any, key: str) -> Any:
    if isinstance(model, dict):
        return model.get(key)
    if hasattr(model, key):
        return getattr(model, key)
    if hasattr(model, "model_dump"):
        payload = model.model_dump()
        if isinstance(payload, dict):
            return payload.get(key)
    return None


def _deployment_nodes(bundle: MaterializedWorkflowBundle) -> list[MaterializedNode]:
    normalized_payload = _load_normalized_workflow_payload(bundle.root_path)
    platform = normalized_payload.get("platform")
    if not isinstance(platform, dict):
        raise DeployCommandError("pulled workflow.normalized.cove.yaml must define platform")
    provider = platform.get("provider")
    runtime = platform.get("runtime")
    if provider != "phala" or runtime != "dstack":
        raise DeployCommandError(
            "cove deploy requires workflow.platform.provider='phala' and workflow.platform.runtime='dstack'"
        )

    nodes_payload = normalized_payload.get("nodes")
    if not isinstance(nodes_payload, dict) or not nodes_payload:
        raise DeployCommandError("pulled workflow.normalized.cove.yaml must define nodes")

    bundle_nodes_by_id = {node.node_id: node for node in bundle.nodes}
    indegree = {node_id: 0 for node_id in nodes_payload}
    dependents: dict[str, list[str]] = {node_id: [] for node_id in nodes_payload}
    authored_order = {
        node_id: index
        for index, node_id in enumerate(nodes_payload.keys())
    }
    for node_id, raw_node in nodes_payload.items():
        if node_id not in bundle_nodes_by_id:
            raise DeployCommandError(
                f"bundle manifest is missing node {node_id!r} from workflow.normalized.cove.yaml"
            )
        if not isinstance(raw_node, dict):
            raise DeployCommandError(f"workflow.normalized.cove.yaml node {node_id!r} must be a mapping")
        raw_dependencies = raw_node.get("dependencies", [])
        if not isinstance(raw_dependencies, list):
            raise DeployCommandError(
                f"workflow.normalized.cove.yaml node {node_id!r} dependencies must be a list"
            )
        for dependency in raw_dependencies:
            if not isinstance(dependency, str) or dependency not in nodes_payload:
                raise DeployCommandError(
                    f"workflow.normalized.cove.yaml node {node_id!r} has unknown dependency {dependency!r}"
                )
            indegree[node_id] += 1
            dependents.setdefault(dependency, []).append(node_id)

    ready = sorted(
        [node_id for node_id, dependency_count in indegree.items() if dependency_count == 0],
        key=authored_order.__getitem__,
    )
    ordered_node_ids: list[str] = []
    while ready:
        node_id = ready.pop(0)
        ordered_node_ids.append(node_id)
        for dependent in sorted(dependents.get(node_id, []), key=authored_order.__getitem__):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=authored_order.__getitem__)

    if len(ordered_node_ids) != len(nodes_payload):
        raise DeployCommandError("pulled workflow contains a node dependency cycle")
    return [bundle_nodes_by_id[node_id] for node_id in ordered_node_ids]


def _load_normalized_workflow_payload(bundle_root: Path) -> dict[str, Any]:
    normalized_path = bundle_root / "workflow.normalized.cove.yaml"
    if not normalized_path.is_file():
        raise DeployCommandError(
            f"pulled workflow bundle is missing workflow.normalized.cove.yaml: {normalized_path}"
        )
    try:
        payload = yaml.safe_load(normalized_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DeployCommandError(
            f"workflow.normalized.cove.yaml is not valid YAML: {normalized_path}"
        ) from exc
    if not isinstance(payload, dict):
        raise DeployCommandError("workflow.normalized.cove.yaml must be a YAML mapping")
    return payload


def _validate_compose_payload(compose_payload: dict[str, Any], *, node_id: str) -> None:
    unsupported_keys = sorted(
        key
        for key in compose_payload
        if key not in _SUPPORTED_TOP_LEVEL_COMPOSE_KEYS
    )
    if unsupported_keys:
        raise DeployCommandError(
            f"pulled compose for node {node_id} uses unsupported top-level keys: {', '.join(unsupported_keys)}"
        )


def _validate_service_payload(service_name: str, service: dict[str, Any], *, node_id: str) -> None:
    unsupported_keys = sorted(
        key
        for key in service
        if key not in _SUPPORTED_SERVICE_KEYS
    )
    if unsupported_keys:
        raise DeployCommandError(
            "pulled compose service "
            f"{service_name!r} for node {node_id} uses unsupported keys: {', '.join(unsupported_keys)}"
        )
    image = service.get("image")
    if not isinstance(image, str) or "@sha256:" not in image:
        raise DeployCommandError(
            f"pulled compose service {service_name!r} for node {node_id} must use a digest-pinned image"
        )


def _service_may_mount_dstack_socket(service_name: str) -> bool:
    return (
        service_name == "cove_node_certificate_writer"
        or service_name.startswith("cove_provision_")
        or service_name.startswith("cove_publish_")
    )
