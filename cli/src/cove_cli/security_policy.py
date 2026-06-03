from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from .workflow import WorkflowDefinition


_RESERVED_SERVICE_PREFIX = "cove_"
_RESERVED_ENV_NAMES = {
    "COVE_CONFIG_JSON",
    "COVE_SERVICE_NAME",
    "COVE_COMPOSE_HASH",
}
_BLOCKED_COMPOSE_FIELDS = {
    "privileged",
    "devices",
    "device_cgroup_rules",
    "cap_add",
    "security_opt",
    "pid",
    "ipc",
    "network_mode",
}
_ALLOWED_NVIDIA_GPU_RESERVATION = {
    "driver": "nvidia",
    "count": "all",
    "capabilities": ["gpu"],
}
_FIRST_PARTY_SIDECAR_IMAGES = {
    "cove-artifact-provisioner",
    "cove-dependency-certificate-fetcher",
    "cove-key-manager",
    "cove-node-certificate-writer",
    "cove-precondition-checker",
    "cove-service-certificate-writer",
}
_COMPILER_MANAGED_TARGET_PREFIXES = ("/cove",)
_QUOTE_CHANNEL_HINTS = {
    "vsock": "vsock",
    "/var/run/dstack.sock": "/var/run/dstack.sock",
    "/dev/tdx": "/dev/tdx",
    "/dev/attestation": "/dev/attestation",
    "tdx-guest": "tdx-guest",
    "qgs": "qgs",
}


@dataclass(frozen=True, slots=True)
class SecurityPolicyReport:
    errors: list[str]
    warnings: list[str]


def evaluate_workflow_security_policy(workflow: WorkflowDefinition) -> SecurityPolicyReport:
    errors: list[str] = []
    warnings: list[str] = []
    for node in workflow.nodes.values():
        for service in node.services.values():
            service_label = f"{node.name}.{service.name}"
            compose_service = service.compose_service
            if service.name.startswith(_RESERVED_SERVICE_PREFIX):
                errors.append(
                    f"service '{service_label}' uses reserved compiler-managed prefix '{_RESERVED_SERVICE_PREFIX}'"
                )

            image_value = compose_service.get("image")
            if isinstance(image_value, str):
                image_name = _image_basename(image_value)
                if image_name in _FIRST_PARTY_SIDECAR_IMAGES:
                    errors.append(
                        f"service '{service_label}' reuses reserved first-party sidecar image '{image_name}'"
                    )

            environment = compose_service.get("environment")
            for env_name in _environment_names(environment):
                if env_name in _RESERVED_ENV_NAMES:
                    errors.append(
                        f"service '{service_label}' sets reserved environment variable '{env_name}'"
                    )

            for blocked_field in _BLOCKED_COMPOSE_FIELDS:
                if blocked_field in compose_service:
                    errors.append(
                        f"service '{service_label}' uses blocked Compose field '{blocked_field}'"
                    )
            deploy_issue = _deploy_issue(compose_service.get("deploy"))
            if deploy_issue is not None:
                errors.append(f"service '{service_label}' {deploy_issue}")

            volumes = compose_service.get("volumes")
            if isinstance(volumes, list):
                for raw_volume in volumes:
                    issue = _volume_issue(raw_volume)
                    if issue is not None:
                        errors.append(f"service '{service_label}' {issue}")

            warning_fragments: set[str] = set()
            for string_value in _string_leaves(compose_service):
                lowered = string_value.lower()
                for needle, label in _QUOTE_CHANNEL_HINTS.items():
                    if needle in lowered:
                        warning_fragments.add(label)
            for label in sorted(warning_fragments):
                warnings.append(
                    "service "
                    f"'{service_label}' references quote-channel hint '{label}'; "
                    "Docker policy cannot prove guest code cannot access quote generation if the platform exposes it broadly"
                )

    return SecurityPolicyReport(errors=errors, warnings=warnings)


def _environment_names(raw_environment: Any) -> set[str]:
    if isinstance(raw_environment, dict):
        return {
            key.strip()
            for key in raw_environment.keys()
            if isinstance(key, str) and key.strip()
        }
    if isinstance(raw_environment, list):
        names: set[str] = set()
        for entry in raw_environment:
            if not isinstance(entry, str):
                continue
            name, _, _ = entry.partition("=")
            stripped = name.strip()
            if stripped:
                names.add(stripped)
        return names
    return set()


def _image_basename(image_reference: str) -> str:
    last_segment = image_reference.rsplit("/", 1)[-1]
    without_digest = last_segment.split("@", 1)[0]
    if ":" in without_digest:
        return without_digest.rsplit(":", 1)[0]
    return without_digest


def _volume_issue(raw_volume: Any) -> str | None:
    source_value, target_value = _volume_source_and_target(raw_volume)
    if target_value is not None and _is_compiler_managed_target(target_value):
        return f"binds into compiler-managed target '{target_value}'"
    if source_value is not None and _is_compiler_managed_source(source_value):
        return f"binds compiler-managed source '{source_value}'"
    return None


def _deploy_issue(raw_deploy: Any) -> str | None:
    if raw_deploy is None:
        return None
    if not isinstance(raw_deploy, dict):
        return "uses unsupported Compose field 'deploy'"
    try:
        devices = raw_deploy["resources"]["reservations"]["devices"]
    except (KeyError, TypeError):
        return "uses unsupported Compose field 'deploy'"
    if devices != [_ALLOWED_NVIDIA_GPU_RESERVATION]:
        return "uses unsupported Compose field 'deploy' with non-allowlisted GPU reservation"
    return None


def _volume_source_and_target(raw_volume: Any) -> tuple[str | None, str | None]:
    if isinstance(raw_volume, dict):
        volume_type = raw_volume.get("type")
        if volume_type not in {None, "bind"}:
            return (None, None)
        source_value = raw_volume.get("source")
        target_value = raw_volume.get("target")
        return (
            source_value if isinstance(source_value, str) and source_value else None,
            target_value if isinstance(target_value, str) and target_value else None,
        )
    if isinstance(raw_volume, str) and raw_volume:
        parts = raw_volume.split(":")
        if len(parts) == 1:
            return (parts[0], parts[0])
        return (parts[0], parts[1])
    return (None, None)


def _is_compiler_managed_target(target_value: str) -> bool:
    normalized = target_value.rstrip("/") or target_value
    for prefix in _COMPILER_MANAGED_TARGET_PREFIXES:
        if normalized == prefix or normalized.startswith(prefix + "/"):
            return True
    return False


def _is_compiler_managed_source(source_value: str) -> bool:
    normalized = source_value.strip()
    if not normalized:
        return False
    if normalized.startswith("/"):
        return False
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    if not path.parts:
        return False
    return path.parts[0] in {"runtime", "assets"}


def _string_leaves(payload: Any) -> list[str]:
    leaves: list[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, str):
            leaves.append(node)
            return
        if isinstance(node, dict):
            for key, value in node.items():
                visit(key)
                visit(value)
            return
        if isinstance(node, list):
            for value in node:
                visit(value)

    visit(payload)
    return leaves
