from __future__ import annotations

import getpass
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import (
    LocalConfig,
    load_local_config,
    provision_paths_for_home,
    resolve_cove_home,
    update_local_config,
)
from .provisioning_identity import (
    DEFAULT_COVEHUB_SERVER_URL,
    ensure_owner_signing_key_material,
    normalize_owner_server_url,
    owner_domain_from_url,
)
from .provision_server import DEFAULT_PROVISION_PORT


class InitCommandError(RuntimeError):
    """Raised when `cove init` cannot complete."""


@dataclass(frozen=True, slots=True)
class _PhalaDockerRegistryAuth:
    username: str | None
    access_token: str | None
    registry: str | None


def initialize_cove_home(*, cove_home: str | None = None) -> str:
    initial_home = resolve_cove_home(cove_home)
    selected_home = _prompt_home(initial_home)
    existing_config = load_local_config(selected_home)
    existing_phala_cloud_api_key = existing_config.phala_cloud_api_key if existing_config else None

    server_url_default = _server_url_prompt_default(
        existing_config.covehub_server_url if existing_config else None
    )
    server_url = _prompt_server_url(server_url_default)

    provision_paths = provision_paths_for_home(selected_home)
    owner_server_url = _prompt_owner_server_url(
        existing_config.owner_server_url if existing_config else None,
    )
    owner_domain = owner_domain_from_url(owner_server_url)
    try:
        ensure_owner_signing_key_material(
            private_key_path=provision_paths.owner_private_key_path,
            public_key_path=provision_paths.owner_public_key_path,
        )
    except ValueError as exc:
        raise InitCommandError(str(exc)) from exc

    phala_cloud_api_key = _prompt_optional_secret(
        "Phala Cloud API key",
        existing_value=existing_phala_cloud_api_key,
    )
    phala_docker_auth = _prompt_phala_docker_registry_auth(
        existing_config=existing_config,
        phala_cloud_api_key=phala_cloud_api_key,
    )

    updated = update_local_config(
        cove_home=selected_home,
        covehub_server_url=server_url,
        phala_cloud_api_key=phala_cloud_api_key,
        phala_docker_username=phala_docker_auth.username,
        phala_docker_access_token=phala_docker_auth.access_token,
        phala_docker_registry=phala_docker_auth.registry,
        owner_server_url=owner_server_url,
    )
    return "\n".join(
        [
            f"Initialized owner domain '{owner_domain}' against {server_url}",
            f"Cove home: {updated.cove_home}",
            f"Config: {updated.path}",
            f"Owner server URL: {owner_server_url}",
            f"Owner public key: {provision_paths.owner_public_key_path}",
            *_owner_runtime_lines(
                owner_server_url=owner_server_url,
                cove_home=updated.cove_home,
            ),
            (
                "Phala Cloud API key: configured"
                if updated.phala_cloud_api_key
                else "Phala Cloud API key: not configured"
            ),
            (
                "Phala Docker registry auth: configured"
                if updated.phala_docker_username and updated.phala_docker_access_token
                else "Phala Docker registry auth: not configured"
            ),
        ]
    )


def _prompt_home(default_home: Path) -> Path:
    raw_value = _prompt("Cove home path", default=str(default_home))
    return resolve_cove_home(raw_value)


def _prompt_server_url(default_server_url: str | None) -> str:
    while True:
        server_url = _prompt("Covehub server URL", default=default_server_url)
        parsed = urlparse(server_url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return server_url
        print("Please enter a valid http or https URL.")


def _prompt_owner_server_url(existing_owner_server_url: str | None) -> str:
    while True:
        raw_value = _prompt("Owner server URL", default=existing_owner_server_url)
        try:
            return normalize_owner_server_url(raw_value)
        except ValueError as exc:
            print(str(exc))


def _server_url_prompt_default(existing_server_url: str | None) -> str:
    if existing_server_url is None:
        return DEFAULT_COVEHUB_SERVER_URL
    parsed = urlparse(existing_server_url)
    if (
        parsed.scheme == "https"
        and parsed.hostname == "covehub.io"
        and parsed.path in {"", "/"}
        and parsed.query == ""
        and parsed.fragment == ""
    ):
        return DEFAULT_COVEHUB_SERVER_URL
    if parsed.hostname in {"127.0.0.1", "localhost", "0.0.0.0"} and parsed.port == 8000:
        return DEFAULT_COVEHUB_SERVER_URL
    return existing_server_url


def _prompt_required(label: str, *, default: str | None = None) -> str:
    while True:
        value = _prompt(label, default=default)
        if value:
            return value
        print(f"{label} must not be empty.")


def _prompt_optional_secret(label: str, *, existing_value: str | None = None) -> str | None:
    if existing_value:
        prompt = f"{label} [stored; press Enter to keep]: "
    else:
        prompt = f"{label} (optional): "
    value = _getpass(prompt).strip()
    if value:
        return value
    if existing_value:
        return existing_value
    return None


def _prompt_phala_docker_registry_auth(
    *,
    existing_config: LocalConfig | None,
    phala_cloud_api_key: str | None,
) -> _PhalaDockerRegistryAuth:
    existing_username = (
        existing_config.phala_docker_username if existing_config else None
    )
    existing_access_token = (
        existing_config.phala_docker_access_token if existing_config else None
    )
    existing_registry = (
        existing_config.phala_docker_registry if existing_config else None
    )

    if not phala_cloud_api_key:
        return _PhalaDockerRegistryAuth(
            username=existing_username,
            access_token=existing_access_token,
            registry=existing_registry,
        )

    while True:
        username = _normalize_optional_prompt_value(
            _prompt(
                "Phala Docker registry username",
                default=existing_username,
            )
        )
        access_token = _prompt_optional_secret(
            "Phala Docker registry access token",
            existing_value=existing_access_token,
        )
        registry = _normalize_optional_prompt_value(
            _prompt(
                "Phala Docker registry (optional; blank for Docker Hub)",
                default=existing_registry,
            )
        )

        if bool(username) == bool(access_token):
            if not username and registry:
                print("Phala Docker registry requires both username and access token.")
                continue
            return _PhalaDockerRegistryAuth(
                username=username,
                access_token=access_token,
                registry=registry,
            )
        print("Phala Docker registry username and access token must be provided together.")


def _normalize_optional_prompt_value(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _prompt(label: str, *, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    try:
        value = input(f"{label}{suffix}: ")
    except EOFError as exc:
        raise InitCommandError("interactive input was interrupted") from exc
    stripped = value.strip()
    if stripped:
        return stripped
    if default is not None:
        return default
    return ""


def _getpass(prompt: str) -> str:
    try:
        return getpass.getpass(prompt)
    except EOFError as exc:
        raise InitCommandError("interactive input was interrupted") from exc


def _owner_runtime_lines(
    *,
    owner_server_url: str,
    cove_home: Path,
) -> list[str]:
    local_url = f"http://127.0.0.1:{DEFAULT_PROVISION_PORT}"
    return [
        f"Cove server configured for: {owner_server_url}",
        f"Default local URL: {local_url}",
        "Owner service is not started automatically.",
        "Run owner service in a terminal:",
        f"cove --cove-home {cove_home} start {DEFAULT_PROVISION_PORT}",
        "Pass a different port to cove start if this owner should bind elsewhere.",
        "Stop owner service with Ctrl-C in that terminal.",
    ]
