from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import urlparse

from .canonical_images import is_canonical_artifact_provisioner_digest
from .artifact_crypto import (
    ArtifactCryptoError,
    ensure_artifact_key,
    encrypt_artifact_bytes,
    sha256_literal,
)
from .config import (
    ensure_local_config,
    provision_paths_for_home,
)
from .covehub import CovehubError, delete_runtime_state as delete_covehub_runtime_state, upload_named_artifact
from .publish import (
    MaterializedArtifact,
    MaterializedWorkflowBundle,
    PublishCommandError,
    load_workflow_bundle_for_compose,
    materialized_artifact_hub_path,
    materialized_artifact_owner_domain,
    parse_published_ref,
    pull_workflow_bundle,
)
from .provisioning_identity import (
    ensure_owner_signing_key_material,
    fetch_owner_identity_for_write,
    normalize_owner_server_url,
    owner_domain_from_url,
)
from .provision_server import DEFAULT_PROVISION_PORT, create_provision_server
from .provision_state import ProvisionState


IDENTIFIER_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class ProvisionCommandError(RuntimeError):
    """Raised when provision-related CLI commands fail."""


def provision_artifact(
    artifact_name: str,
    file_path: str | Path,
    *,
    cove_home: str | None = None,
    overwrite: bool = False,
) -> str:
    artifact_id = artifact_name.strip()
    if not _is_identifier(artifact_id):
        raise ProvisionCommandError("artifact_name is invalid")

    local_file = Path(file_path).expanduser().resolve()
    if not local_file.is_file():
        raise ProvisionCommandError(f"file not found: {local_file}")

    config = ensure_local_config(cove_home)
    server_url = _require_server_url(config.covehub_server_url, config.path)
    owner_url = _owner_server_url(config)
    owner_domain = owner_domain_from_url(owner_url)

    plaintext = local_file.read_bytes()
    plaintext_hash = sha256_literal(plaintext)
    provision_paths = provision_paths_for_home(config.cove_home)
    ensure_owner_signing_key_material(
        private_key_path=provision_paths.owner_private_key_path,
        public_key_path=provision_paths.owner_public_key_path,
    )
    try:
        _key_file, key_bytes = ensure_artifact_key(
            keys_dir=provision_paths.keys_dir,
            artifact_id=artifact_id,
        )
        ciphertext = encrypt_artifact_bytes(
            plaintext=plaintext,
            key_bytes=key_bytes,
        )
    except ArtifactCryptoError as exc:
        raise ProvisionCommandError(str(exc)) from exc
    ciphertext_hash = sha256_literal(ciphertext)

    try:
        owner_identity = _served_owner_identity_for_write(
            owner_url=owner_url,
            owner_domain=owner_domain,
            owner_private_key_path=provision_paths.owner_private_key_path,
        )
        upload_result = upload_named_artifact(
            server_url=server_url,
            owner_domain=owner_domain,
            artifact_name=artifact_id,
            owner_identity=owner_identity,
            owner_private_key_path=provision_paths.owner_private_key_path,
            payload=ciphertext,
            overwrite=overwrite,
        )
    except CovehubError as exc:
        raise ProvisionCommandError(str(exc)) from exc

    state = ProvisionState(provision_paths.database_path)
    state.initialize()
    content_type = mimetypes.guess_type(local_file.name)[0] or "application/octet-stream"
    state.upsert_registered_artifact(
        hub_path=upload_result.hub_path,
        artifact_id=artifact_id,
        owner_domain=owner_domain,
        owner_url=owner_url,
        plaintext_hash=plaintext_hash,
        ciphertext_hash=ciphertext_hash,
        content_type=content_type,
        source_path=str(local_file),
        server_url=server_url,
        transport_mode="encrypted",
        key_path=artifact_id,
    )
    action = "Created" if upload_result.status_code == 201 else "Updated"
    return "\n".join(
        [
            f"{action} named artifact '{upload_result.hub_path}'",
            "Workflow snippet:",
            "type: static",
            "owner: <owner-handle>",
            f"plaintext_hash: {plaintext_hash}",
            f"Owner domain: {owner_domain}",
            f"Generated hub path: {upload_result.hub_path}",
            f"ciphertext_hash: {ciphertext_hash}",
            f"key_path: {artifact_id}",
        ]
    )


def serve_provisioner(
    port: int | None = None,
    *,
    cove_home: str | None = None,
) -> int:
    return start_owner_service(port, cove_home=cove_home)


def start_owner_service(
    port: int | None = None,
    *,
    cove_home: str | None = None,
) -> int:
    config = ensure_local_config(cove_home)
    resolved_port = DEFAULT_PROVISION_PORT if port is None else port
    _validate_port(resolved_port)

    provision_paths = provision_paths_for_home(config.cove_home)
    ensure_owner_signing_key_material(
        private_key_path=provision_paths.owner_private_key_path,
        public_key_path=provision_paths.owner_public_key_path,
    )
    state = ProvisionState(provision_paths.database_path)
    state.initialize()

    local_provisioning_url = _local_provisioning_url(resolved_port)
    public_url = _owner_server_url(config)
    owner_domain = owner_domain_from_url(public_url)
    server = create_provision_server(
        state=state,
        keys_dir=provision_paths.keys_dir,
        owner_private_key_path=provision_paths.owner_private_key_path,
        owner_public_key_path=provision_paths.owner_public_key_path,
        host="127.0.0.1",
        port=resolved_port,
        owner_domain=owner_domain,
        owner_url=public_url,
        quote_verifier_mode="phala_dstack",
    )
    print(
        _serve_provisioner_output(
            owner_domain=owner_domain,
            local_provisioning_url=local_provisioning_url,
            owner_server_url=public_url,
        )
    )

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def allow_artifact_for_compose(
    artifact_id: str,
    compose_file_path: str | Path,
    *,
    cove_home: str | None = None,
) -> str:
    normalized_artifact_id = artifact_id.strip()
    if not _is_identifier(normalized_artifact_id):
        raise ProvisionCommandError("artifact_id is invalid")

    config = ensure_local_config(cove_home)
    owner_url = _owner_server_url(config)
    owner_domain = owner_domain_from_url(owner_url)
    provision_paths = provision_paths_for_home(config.cove_home)
    state = ProvisionState(provision_paths.database_path)
    state.initialize()

    try:
        bundle, node = load_workflow_bundle_for_compose(compose_file_path)
    except PublishCommandError as exc:
        raise ProvisionCommandError(str(exc)) from exc

    if node.artifact_provisioner_digest is None:
        raise ProvisionCommandError(
            f"node '{node.node_id}' does not define a digest-pinned artifact provisioner image"
        )
    if not is_canonical_artifact_provisioner_digest(node.artifact_provisioner_digest):
        raise ProvisionCommandError(
            "artifact provisioner digest is not canonical: "
            f"{node.artifact_provisioner_digest}"
        )

    dynamic_artifact = _find_owned_dynamic_artifact(
        bundle,
        node.artifacts,
        artifact_name=normalized_artifact_id,
        owner_domain=owner_domain,
    )
    if dynamic_artifact is not None:
        channel = _ensure_dynamic_artifact_channel(
            bundle=bundle,
            artifact=dynamic_artifact,
            owner_domain=owner_domain,
            publisher=bundle.publisher,
            workflow_id=bundle.workflow_id,
            provision_paths=provision_paths,
            state=state,
        )
        state.upsert_allow_rule(
            artifact_id=channel.artifact_name,
            hub_path=channel.hub_path,
            publisher=bundle.publisher,
            workflow_id=bundle.workflow_id,
            node_id=node.node_id,
            compose_hash=node.compose_hash,
            artifact_provisioner_digest=node.artifact_provisioner_digest,
        )
    else:
        record = state.get_registered_artifact_by_artifact_id(normalized_artifact_id)
        if record is None:
            raise ProvisionCommandError(
                f"artifact_id not found in local provision state: {normalized_artifact_id}"
            )
        if record.owner_domain != owner_domain:
            raise ProvisionCommandError(
                f"artifact_id '{normalized_artifact_id}' is not owned by configured domain '{owner_domain}'"
            )
        node_hub_paths = {
            materialized_artifact_hub_path(bundle, artifact)
            for artifact in node.artifacts
            if artifact.type == "static"
        }
        if record.hub_path not in node_hub_paths:
            matching_record = _find_registered_artifact_for_hub_paths(
                state,
                artifact_id=normalized_artifact_id,
                owner_domain=owner_domain,
                hub_paths=node_hub_paths,
            )
            if matching_record is not None:
                record = matching_record
        if record.hub_path not in node_hub_paths:
            raise ProvisionCommandError(
                f"node '{node.node_id}' does not reference artifact '{normalized_artifact_id}'"
            )

        state.upsert_allow_rule(
            artifact_id=record.artifact_id,
            hub_path=record.hub_path,
            publisher=bundle.publisher,
            workflow_id=bundle.workflow_id,
            node_id=node.node_id,
            compose_hash=node.compose_hash,
            artifact_provisioner_digest=node.artifact_provisioner_digest,
        )

    return "\n".join(
        [
            f"Allowed artifact '{normalized_artifact_id}' for node '{bundle.publisher}/{bundle.workflow_id}:{node.node_id}'",
            f"Compose file: {Path(compose_file_path).expanduser().resolve()}",
            f"Compose hash: {node.compose_hash}",
            f"Artifact provisioner digest: {node.artifact_provisioner_digest}",
        ]
    )


def inspect_and_allow(
    published_ref: str,
    *,
    cove_home: str | None = None,
) -> str:
    config = ensure_local_config(cove_home)
    server_url = _require_server_url(config.covehub_server_url, config.path)
    owner_url = _owner_server_url(config)
    owner_domain = owner_domain_from_url(owner_url)

    try:
        parsed_ref = parse_published_ref(published_ref)
        bundle = pull_workflow_bundle(
            server_url=server_url,
            publisher=parsed_ref.publisher,
            workflow_id=parsed_ref.workflow_id,
            reference=parsed_ref.reference,
            cove_home=cove_home,
        )
    except PublishCommandError as exc:
        raise ProvisionCommandError(str(exc)) from exc

    provision_paths = provision_paths_for_home(config.cove_home)
    state = ProvisionState(provision_paths.database_path)
    state.initialize()
    owned_artifacts = {
        artifact.hub_path: artifact
        for artifact in state.list_registered_artifacts()
        if artifact.owner_domain == owner_domain
    }

    approved: list[str] = []
    review_count = 0
    for node in bundle.nodes:
        matching_records = [
            owned_artifacts[materialized_artifact_hub_path(bundle, artifact)]
            for artifact in node.artifacts
            if artifact.type == "static"
            and materialized_artifact_hub_path(bundle, artifact) in owned_artifacts
        ]
        matching_dynamic_artifacts = _unique_dynamic_artifacts_for_owner(
            bundle,
            node.artifacts,
            owner_domain=owner_domain,
        )
        if not matching_records and not matching_dynamic_artifacts:
            continue
        review_count += 1

        compose_path = bundle.root_path / node.compose_path
        compose_text = compose_path.read_text(encoding="utf-8")
        print(f"Node: {node.node_id}")
        print(f"Compose: {compose_path}")
        print(compose_text.rstrip())
        prompt_artifacts = [
            *[record.artifact_id for record in matching_records],
            *[artifact.name for artifact in matching_dynamic_artifacts],
        ]
        prompt = (
            f"Allow {', '.join(prompt_artifacts)} "
            f"for node {node.node_id}? [y/N]: "
        )
        if input(prompt).strip().lower() != "y":
            continue

        if node.artifact_provisioner_digest is None:
            raise ProvisionCommandError(
                f"node '{node.node_id}' does not define a digest-pinned artifact provisioner image"
            )
        if not is_canonical_artifact_provisioner_digest(node.artifact_provisioner_digest):
            raise ProvisionCommandError(
                "artifact provisioner digest is not canonical: "
                f"{node.artifact_provisioner_digest}"
            )

        for record in matching_records:
            state.upsert_allow_rule(
                artifact_id=record.artifact_id,
                hub_path=record.hub_path,
                publisher=bundle.publisher,
                workflow_id=bundle.workflow_id,
                node_id=node.node_id,
                compose_hash=node.compose_hash,
                artifact_provisioner_digest=node.artifact_provisioner_digest,
            )
            approved.append(f"{record.artifact_id} -> {node.node_id}")
        for artifact in matching_dynamic_artifacts:
            channel = _ensure_dynamic_artifact_channel(
                bundle=bundle,
                artifact=artifact,
                owner_domain=owner_domain,
                publisher=bundle.publisher,
                workflow_id=bundle.workflow_id,
                provision_paths=provision_paths,
                state=state,
            )
            state.upsert_allow_rule(
                artifact_id=channel.artifact_name,
                hub_path=channel.hub_path,
                publisher=bundle.publisher,
                workflow_id=bundle.workflow_id,
                node_id=node.node_id,
                compose_hash=node.compose_hash,
                artifact_provisioner_digest=node.artifact_provisioner_digest,
            )
            approved.append(f"{channel.artifact_name} -> {node.node_id}")

    return "\n".join(
        [
            f"Inspected published workflow '{parsed_ref.publisher}/{parsed_ref.workflow_id}'",
            f"Pulled bundle path: {bundle.root_path}",
            f"Reviewed nodes: {review_count}",
            "Approved rules:",
            *([f"- {entry}" for entry in approved] or ["- none"]),
        ]
    )


def reset_runtime_state(
    published_ref: str,
    *,
    cove_home: str | None = None,
) -> str:
    config = ensure_local_config(cove_home)
    server_url = _require_server_url(config.covehub_server_url, config.path)
    owner_url = _owner_server_url(config)
    owner_domain = owner_domain_from_url(owner_url)
    provision_paths = provision_paths_for_home(config.cove_home)
    ensure_owner_signing_key_material(
        private_key_path=provision_paths.owner_private_key_path,
        public_key_path=provision_paths.owner_public_key_path,
    )

    try:
        parsed_ref = parse_published_ref(published_ref)
    except PublishCommandError as exc:
        raise ProvisionCommandError(str(exc)) from exc
    if parsed_ref.publisher != owner_domain:
        raise ProvisionCommandError(
            f"reset-runtime requires configured owner domain '{owner_domain}' to match publisher '{parsed_ref.publisher}'"
        )

    try:
        owner_identity = _served_owner_identity_for_write(
            owner_url=owner_url,
            owner_domain=owner_domain,
            owner_private_key_path=provision_paths.owner_private_key_path,
        )
        delete_covehub_runtime_state(
            server_url=server_url,
            publisher=parsed_ref.publisher,
            workflow_id=parsed_ref.workflow_id,
            owner_identity=owner_identity,
            owner_private_key_path=provision_paths.owner_private_key_path,
        )
    except CovehubError as exc:
        raise ProvisionCommandError(str(exc)) from exc

    return f"Cleared runtime state for '{parsed_ref.publisher}/{parsed_ref.workflow_id}'"


def _require_server_url(value: str | None, config_path: Path) -> str:
    server_url = _require_field("covehub_server_url", value, config_path)
    parsed_url = urlparse(server_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise ProvisionCommandError(
            f"'covehub_server_url' in {config_path} must be an http or https URL"
        )
    return server_url


def _require_field(name: str, value: str | None, config_path: Path) -> str:
    if value:
        return value
    raise ProvisionCommandError(f"'{name}' must be configured in {config_path}")


def _validate_port(port: int) -> None:
    if not (1 <= port <= 65535):
        raise ProvisionCommandError("port must be between 1 and 65535")


def _local_provisioning_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _owner_server_url(config) -> str:
    try:
        return normalize_owner_server_url(config.owner_server_url)
    except ValueError as exc:
        raise ProvisionCommandError(str(exc)) from exc


def _served_owner_identity_for_write(
    *,
    owner_url: str,
    owner_domain: str,
    owner_private_key_path: Path,
) -> dict[str, object]:
    try:
        return fetch_owner_identity_for_write(
            owner_url=owner_url,
            expected_owner_domain=owner_domain,
            owner_private_key_path=owner_private_key_path,
        )
    except Exception as exc:
        raise ProvisionCommandError(
            f"failed to resolve served owner identity at {owner_url}: {exc}"
        ) from exc


def _serve_provisioner_output(
    *,
    owner_domain: str,
    local_provisioning_url: str,
    owner_server_url: str,
) -> str:
    snippet = "\n".join(
        [
            "owners:",
            f"  {owner_domain}: {owner_server_url}",
        ]
    )
    lines = [
        f"Local owner service URL: {local_provisioning_url}",
        f"Owner identity URL: {owner_server_url.rstrip('/')}/identity",
        f"Public owner server URL: {owner_server_url}",
    ]
    lines.extend(
        [
            "Workflow snippet:",
            snippet,
        ]
    )
    return "\n".join(lines)


def _is_identifier(value: str) -> bool:
    if not value or len(value) > 128:
        return False
    if value[0] not in IDENTIFIER_CHARS or value[0] == ".":
        return False
    return all(character in IDENTIFIER_CHARS for character in value)


def _find_owned_dynamic_artifact(
    bundle: MaterializedWorkflowBundle,
    artifacts: list[MaterializedArtifact],
    *,
    artifact_name: str,
    owner_domain: str,
) -> MaterializedArtifact | None:
    for artifact in artifacts:
        if (
            artifact.type == "dynamic"
            and materialized_artifact_owner_domain(bundle, artifact) == owner_domain
            and artifact.name == artifact_name
        ):
            return artifact
    return None


def _unique_dynamic_artifacts_for_owner(
    bundle: MaterializedWorkflowBundle,
    artifacts: list[MaterializedArtifact],
    *,
    owner_domain: str,
) -> list[MaterializedArtifact]:
    unique: dict[str, MaterializedArtifact] = {}
    for artifact in artifacts:
        if (
            artifact.type != "dynamic"
            or materialized_artifact_owner_domain(bundle, artifact) != owner_domain
        ):
            continue
        unique.setdefault(materialized_artifact_hub_path(bundle, artifact), artifact)
    return list(unique.values())


def _ensure_dynamic_artifact_channel(
    *,
    bundle: MaterializedWorkflowBundle,
    artifact: MaterializedArtifact,
    owner_domain: str,
    publisher: str,
    workflow_id: str,
    provision_paths,
    state: ProvisionState,
):
    key_path = f"dynamic/{publisher}/{workflow_id}/{artifact.name}"
    try:
        ensure_artifact_key(
            keys_dir=provision_paths.keys_dir,
            artifact_id=key_path,
        )
    except ArtifactCryptoError as exc:
        raise ProvisionCommandError(str(exc)) from exc
    return state.upsert_dynamic_artifact_channel(
        hub_path=materialized_artifact_hub_path(bundle, artifact),
        artifact_name=artifact.name,
        owner_domain=owner_domain,
        publisher=publisher,
        workflow_id=workflow_id,
        key_path=key_path,
    )


def _find_registered_artifact_for_hub_paths(
    state: ProvisionState,
    *,
    artifact_id: str,
    owner_domain: str,
    hub_paths: set[str],
):
    for candidate in state.list_registered_artifacts():
        if (
            candidate.artifact_id == artifact_id
            and candidate.owner_domain == owner_domain
            and candidate.hub_path in hub_paths
        ):
            return candidate
    return None
