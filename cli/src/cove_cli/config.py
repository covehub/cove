from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from .provisioning_identity import (
    normalize_owner_server_url,
)


class ConfigError(ValueError):
    """Raised when the local Cove config is malformed."""


_UNSET = object()


@dataclass(frozen=True, slots=True)
class LocalConfig:
    cove_home: Path
    path: Path
    covehub_server_url: str | None
    phala_cloud_api_key: str | None
    phala_docker_username: str | None
    phala_docker_access_token: str | None
    phala_docker_registry: str | None
    owner_server_url: str | None


@dataclass(frozen=True, slots=True)
class ProvisionPaths:
    cove_home: Path
    config_path: Path
    keys_dir: Path
    database_path: Path
    owner_private_key_path: Path
    owner_public_key_path: Path
    materialized_workflows_dir: Path


def default_cove_home() -> Path:
    return Path.home() / ".cove"


def resolve_cove_home(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path).expanduser().resolve()

    env_path = os.getenv("COVE_HOME")
    if env_path:
        return Path(env_path).expanduser().resolve()

    return default_cove_home().resolve()


def config_path_for_home(cove_home: str | Path | None = None) -> Path:
    return resolve_cove_home(cove_home) / "config.yaml"


def load_local_config(cove_home: str | Path | None = None) -> LocalConfig | None:
    resolved_home = resolve_cove_home(cove_home)
    config_path = config_path_for_home(resolved_home)
    if not config_path.exists():
        return None

    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(
            f"failed to parse local config YAML at {config_path}: {exc}"
        ) from exc

    if payload is None:
        payload = {}

    if not isinstance(payload, dict):
        raise ConfigError(f"local config at {config_path} must be a YAML mapping")
    if "attestation_mode" in payload:
        raise ConfigError(
            f"'attestation_mode' is no longer supported in {config_path}; Cove now uses Phala/dstack attestation only"
        )
    legacy_auth_keys = {"username", "access_token"}
    present_legacy_auth_keys = sorted(legacy_auth_keys.intersection(payload))
    if present_legacy_auth_keys:
        keys = ", ".join(repr(key) for key in present_legacy_auth_keys)
        raise ConfigError(
            f"{keys} are no longer supported in {config_path}; remove this Cove home and re-run cove init"
        )
    removed_keys = {
        "provisioning_public_base_domain",
        "provisioning_tunnel_server",
        "provisioning_tunnel_port",
    }
    present_removed_keys = sorted(removed_keys.intersection(payload))
    if present_removed_keys:
        keys = ", ".join(repr(key) for key in present_removed_keys)
        raise ConfigError(
            f"{keys} are no longer supported in {config_path}; configure explicit owner_server_url values instead"
        )

    return LocalConfig(
        cove_home=resolved_home,
        path=config_path,
        covehub_server_url=_optional_string(
            payload,
            "covehub_server_url",
            config_path,
        ),
        phala_cloud_api_key=_optional_string(
            payload,
            "phala_cloud_api_key",
            config_path,
        ),
        phala_docker_username=_optional_string(
            payload,
            "phala_docker_username",
            config_path,
        ),
        phala_docker_access_token=_optional_string(
            payload,
            "phala_docker_access_token",
            config_path,
        ),
        phala_docker_registry=_optional_string(
            payload,
            "phala_docker_registry",
            config_path,
        ),
        owner_server_url=_optional_string(
            payload,
            "owner_server_url",
            config_path,
        ),
    )


def ensure_local_config(cove_home: str | Path | None = None) -> LocalConfig:
    resolved_home = resolve_cove_home(cove_home)
    return load_local_config(resolved_home) or LocalConfig(
        cove_home=resolved_home,
        path=config_path_for_home(resolved_home),
        covehub_server_url=None,
        phala_cloud_api_key=None,
        phala_docker_username=None,
        phala_docker_access_token=None,
        phala_docker_registry=None,
        owner_server_url=None,
    )


def write_local_config(config: LocalConfig) -> None:
    payload: dict[str, object] = {}
    _set_optional_string(payload, "covehub_server_url", config.covehub_server_url)
    _set_optional_string(payload, "phala_cloud_api_key", config.phala_cloud_api_key)
    _set_optional_string(payload, "phala_docker_username", config.phala_docker_username)
    _set_optional_string(
        payload,
        "phala_docker_access_token",
        config.phala_docker_access_token,
    )
    _set_optional_string(payload, "phala_docker_registry", config.phala_docker_registry)
    _set_optional_string(payload, "owner_server_url", config.owner_server_url)

    config.path.parent.mkdir(parents=True, exist_ok=True)
    config.path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )
    try:
        config.path.chmod(0o600)
    except OSError:
        pass


def update_local_config(
    *,
    cove_home: str | Path | None = None,
    covehub_server_url: str | None | object = _UNSET,
    phala_cloud_api_key: str | None | object = _UNSET,
    phala_docker_username: str | None | object = _UNSET,
    phala_docker_access_token: str | None | object = _UNSET,
    phala_docker_registry: str | None | object = _UNSET,
    owner_server_url: str | None | object = _UNSET,
) -> LocalConfig:
    current = ensure_local_config(cove_home)
    updated = LocalConfig(
        cove_home=current.cove_home,
        path=current.path,
        covehub_server_url=current.covehub_server_url
        if covehub_server_url is _UNSET
        else _normalize_optional_value(covehub_server_url),
        phala_cloud_api_key=current.phala_cloud_api_key
        if phala_cloud_api_key is _UNSET
        else _normalize_optional_value(phala_cloud_api_key),
        phala_docker_username=current.phala_docker_username
        if phala_docker_username is _UNSET
        else _normalize_optional_value(phala_docker_username),
        phala_docker_access_token=current.phala_docker_access_token
        if phala_docker_access_token is _UNSET
        else _normalize_optional_value(phala_docker_access_token),
        phala_docker_registry=current.phala_docker_registry
        if phala_docker_registry is _UNSET
        else _normalize_optional_value(phala_docker_registry),
        owner_server_url=current.owner_server_url
        if owner_server_url is _UNSET
        else _normalize_optional_owner_server_url(owner_server_url),
    )
    write_local_config(updated)
    return updated


def provision_paths_for_home(cove_home: str | Path | None = None) -> ProvisionPaths:
    resolved_home = resolve_cove_home(cove_home)
    return ProvisionPaths(
        cove_home=resolved_home,
        config_path=config_path_for_home(resolved_home),
        keys_dir=resolved_home / "keys",
        database_path=resolved_home / "provision.sqlite3",
        owner_private_key_path=resolved_home / "owner-signing-private.pem",
        owner_public_key_path=resolved_home / "owner-signing-public.pem",
        materialized_workflows_dir=resolved_home / "materialized_workflows",
    )


def _optional_string(
    payload: dict[str, object],
    key: str,
    config_path: Path,
) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key!r} in {config_path} must be a string")
    return _normalize_optional_value(value)


def _set_optional_string(
    payload: dict[str, object],
    key: str,
    value: str | None,
) -> None:
    if value is None:
        payload.pop(key, None)
        return
    payload[key] = value


def _normalize_optional_value(value: str | None | object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("config updates must use string values")
    stripped = value.strip()
    return stripped or None


def _normalize_optional_owner_server_url(value: str | None | object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError("config updates for owner_server_url must use string values")
    try:
        return normalize_owner_server_url(value)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
