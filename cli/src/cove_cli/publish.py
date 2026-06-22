from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
import base64
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

from .canonical_images import (
    ARTIFACT_PROVISIONER_IMAGE_NAME,
    CanonicalImageError,
    load_canonical_container_digests as load_canonical_container_digests_from_policy,
    is_canonical_artifact_provisioner_digest,
)
from .compile import (
    CompileArtifact,
    CompileCommandError,
    compile_workflow_artifact,
    reviewed_compose_hash,
)
from .config import ensure_local_config, provision_paths_for_home
from .covehub import (
    CovehubError,
    download_workflow_object,
    upload_workflow_object,
)
from .workflow import (
    WorkflowDefinition,
    load_workflow_definition,
    stable_topological_nodes,
)
from .provisioning_identity import (
    ensure_owner_signing_key_material,
    fetch_owner_identity_for_write,
    owner_domain_from_url,
)
from .owner_identity import OwnerIdentityError, verify_owner_identity_document

MANIFEST_FILENAME = "bundle.manifest.json"
GENERATED_COMPOSE_FILENAME = "compose.generated.yaml"
GENERATED_COMPOSE_HASH_FILENAME = "compose.generated.sha256"
WORKFLOW_BUNDLE_SIGNATURE_PURPOSE = "cove_workflow_bundle_v1"
_DIGEST_PINNED_IMAGE_RE = re.compile(r"^(?P<name>.+)@(?P<digest>sha256:[0-9a-f]{64})$")
_STATIC_HUB_PATH_RE = re.compile(
    r"^v1/artifacts/(?P<owner>[^/]+)/(?P<artifact>[^/]+)/"
    r"(?P<reference>sha256:[0-9a-f]{64})$"
)


class PublishCommandError(RuntimeError):
    """Raised when publishing or pulling a workflow bundle fails."""


@dataclass(frozen=True, slots=True)
class BundleFile:
    path: str
    sha256: str


@dataclass(frozen=True, slots=True)
class RuntimeSkeletonEntry:
    path: str
    kind: str


@dataclass(frozen=True, slots=True)
class MaterializedArtifact:
    name: str
    type: str
    direction: str
    hub_path: str
    owner: str


@dataclass(frozen=True, slots=True)
class MaterializedNode:
    node_id: str
    compose_path: str
    compose_hash: str
    artifact_provisioner_image: str | None
    artifact_provisioner_digest: str | None
    artifacts: list[MaterializedArtifact]
    runtime_skeleton: list[RuntimeSkeletonEntry]


@dataclass(frozen=True, slots=True)
class MaterializedWorkflowBundle:
    publisher: str
    workflow_id: str
    manifest_hash: str
    root_path: Path
    owners: dict[str, str]
    files: list[BundleFile]
    nodes: list[MaterializedNode]


@dataclass(frozen=True, slots=True)
class PublishedWorkflowRef:
    publisher: str
    workflow_id: str
    reference: str


def push_workflow(
    workflow_path: str | Path | None = None,
    *,
    cove_home: str | Path | None = None,
    overwrite: bool = False,
) -> str:
    config = ensure_local_config(cove_home)
    if not config.covehub_server_url:
        raise PublishCommandError(
            f"'covehub_server_url' must be configured in {config.path} for cove push"
        )
    if not config.owner_server_url:
        raise PublishCommandError(
            f"'owner_server_url' must be configured in {config.path} for cove push"
        )
    provision_paths = provision_paths_for_home(config.cove_home)
    ensure_owner_signing_key_material(
        private_key_path=provision_paths.owner_private_key_path,
        public_key_path=provision_paths.owner_public_key_path,
    )
    publisher_domain = owner_domain_from_url(config.owner_server_url)
    try:
        owner_identity = fetch_owner_identity_for_write(
            owner_url=config.owner_server_url,
            expected_owner_domain=publisher_domain,
            owner_private_key_path=provision_paths.owner_private_key_path,
        )
    except Exception as exc:
        raise PublishCommandError(
            f"failed to resolve served owner identity at {config.owner_server_url}: {exc}"
        ) from exc

    compile_artifact = compile_workflow_artifact(workflow_path, cove_home=cove_home)
    workflow = compile_artifact.workflow
    with tempfile.TemporaryDirectory(prefix="cove-publish-") as temp_dir_name:
        bundle_root = Path(temp_dir_name) / "bundle"
        bundle = bundle_compiled_workflow(
            workflow=workflow,
            compile_artifact=compile_artifact,
            publisher=publisher_domain,
            bundle_root=bundle_root,
        )
        workflow_object = _workflow_object_bytes(
            bundle,
            publisher_identity=owner_identity,
            publisher_private_key_path=provision_paths.owner_private_key_path,
        )
        upload_result = upload_workflow_object(
            server_url=config.covehub_server_url,
            publisher=publisher_domain,
            workflow_id=workflow.workflow_id,
            owner_identity=owner_identity,
            owner_private_key_path=provision_paths.owner_private_key_path,
            payload=workflow_object,
            overwrite=overwrite,
        )

    return "\n".join(
        [
            f"Published workflow bundle '{publisher_domain}/{workflow.workflow_id}'",
            f"Manifest hash: {bundle.manifest_hash}",
            f"Workflow object: {upload_result.hub_path}",
            f"Latest workflow: {upload_result.latest_hub_path}",
            "Uploaded bundle files:",
            *[f"- {bundle_file.path}" for bundle_file in bundle.files],
            f"- {MANIFEST_FILENAME}",
        ]
    )


def pull_workflow(
    published_ref: str,
    destination: str | Path | None = None,
    *,
    cove_home: str | Path | None = None,
) -> str:
    config = ensure_local_config(cove_home)
    if not config.covehub_server_url:
        raise PublishCommandError(
            f"'covehub_server_url' must be configured in {config.path} for cove pull"
        )

    parsed_ref = parse_published_ref(published_ref)
    bundle = pull_workflow_bundle(
        server_url=config.covehub_server_url,
        publisher=parsed_ref.publisher,
        workflow_id=parsed_ref.workflow_id,
        reference=parsed_ref.reference,
        destination=destination,
        cove_home=cove_home,
    )

    next_steps = []
    local_owner_domain = (
        owner_domain_from_url(config.owner_server_url)
        if config.owner_server_url
        else None
    )
    for node in bundle.nodes:
        for artifact in node.artifacts:
            if materialized_artifact_owner_domain(bundle, artifact) != local_owner_domain:
                continue
            compose_file = bundle.root_path / node.compose_path
            next_steps.append(f"cove provision allow {artifact.name} {compose_file}")

    return "\n".join(
        [
            f"Pulled workflow bundle '{parsed_ref.publisher}/{parsed_ref.workflow_id}'",
            f"Workflow reference: {parsed_ref.reference}",
            f"Local bundle path: {bundle.root_path}",
            f"Manifest hash: {bundle.manifest_hash}",
            "Next-step allow commands:",
            *[f"- {line}" for line in next_steps],
        ]
    )


def pull_workflow_bundle(
    *,
    server_url: str,
    publisher: str,
    workflow_id: str,
    reference: str = "latest",
    destination: str | Path | None = None,
    cove_home: str | Path | None = None,
    require_publisher_signature: bool = False,
) -> MaterializedWorkflowBundle:
    if destination is None:
        paths = provision_paths_for_home(cove_home)
        bundle_root = paths.materialized_workflows_dir / publisher / workflow_id
    else:
        bundle_root = Path(destination).expanduser().resolve()

    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True, exist_ok=True)

    workflow_object_bytes = download_workflow_object(
        server_url=server_url,
        publisher=publisher,
        workflow_id=workflow_id,
        reference=reference,
    )
    manifest_bytes, file_payloads = _parse_workflow_object_bytes(
        workflow_object_bytes,
        expected_publisher=publisher,
        require_publisher_signature=require_publisher_signature,
    )
    manifest_payload = _parse_manifest_bytes(
        manifest_bytes,
        bundle_root=bundle_root,
    )
    _verify_manifest_hash(manifest_payload)

    for bundle_file in manifest_payload.files:
        try:
            file_bytes = file_payloads[bundle_file.path]
        except KeyError as exc:
            raise PublishCommandError(
                f"workflow object is missing bundle file payload: {bundle_file.path}"
            ) from exc
        observed_sha = _sha256_literal(file_bytes)
        if observed_sha != bundle_file.sha256:
            raise PublishCommandError(
                f"downloaded file hash mismatch for {bundle_file.path}: {observed_sha} != {bundle_file.sha256}"
            )
        target_path = bundle_root / bundle_file.path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(file_bytes)

    (bundle_root / MANIFEST_FILENAME).write_bytes(manifest_bytes)
    _recreate_runtime_skeleton(bundle_root, manifest_payload)
    return load_workflow_bundle(bundle_root)


def bundle_compiled_workflow(
    *,
    workflow: WorkflowDefinition,
    compile_artifact: CompileArtifact,
    publisher: str,
    bundle_root: Path,
) -> MaterializedWorkflowBundle:
    if bundle_root.exists():
        shutil.rmtree(bundle_root)
    bundle_root.mkdir(parents=True, exist_ok=True)

    normalized_source = compile_artifact.build_dir / "workflow.normalized.cove.yaml"
    normalized_target = bundle_root / "workflow.normalized.cove.yaml"
    normalized_target.write_bytes(normalized_source.read_bytes())

    node_manifests: list[MaterializedNode] = []
    for node in stable_topological_nodes(workflow.nodes):
        source_node_dir = compile_artifact.build_dir / "nodes" / node.name
        target_node_dir = bundle_root / "nodes" / node.name

        compose_source = source_node_dir / GENERATED_COMPOSE_FILENAME
        compose_hash_source = source_node_dir / GENERATED_COMPOSE_HASH_FILENAME
        compose_target = target_node_dir / GENERATED_COMPOSE_FILENAME
        compose_hash_target = target_node_dir / GENERATED_COMPOSE_HASH_FILENAME
        compose_target.parent.mkdir(parents=True, exist_ok=True)
        compose_target.write_bytes(compose_source.read_bytes())
        compose_hash_target.write_bytes(compose_hash_source.read_bytes())

        generated_compose = _inspect_generated_compose(
            compose_path=compose_source,
        )
        compose_hash = _load_generated_compose_hash(
            compose_path=compose_source,
            compose_hash_path=compose_hash_source,
        )

        node_manifests.append(
            MaterializedNode(
                node_id=node.name,
                compose_path=f"nodes/{node.name}/{GENERATED_COMPOSE_FILENAME}",
                compose_hash=compose_hash,
                artifact_provisioner_image=generated_compose.artifact_provisioner_image,
                artifact_provisioner_digest=generated_compose.artifact_provisioner_digest,
                artifacts=[
                    MaterializedArtifact(
                        name=artifact_name,
                        type=workflow.artifacts[artifact_name].type,
                        direction="input",
                        hub_path=workflow.artifacts[artifact_name].hub_path,
                        owner=workflow.artifacts[artifact_name].owner,
                    )
                    for artifact_name in node.used_artifacts
                ]
                + [
                    MaterializedArtifact(
                        name=artifact_name,
                        type=workflow.artifacts[artifact_name].type,
                        direction="output",
                        hub_path=workflow.artifacts[artifact_name].hub_path,
                        owner=workflow.artifacts[artifact_name].owner,
                    )
                    for artifact_name in node.produced_artifacts
                ],
                runtime_skeleton=generated_compose.runtime_skeleton,
            )
        )

    for node in node_manifests:
        if node.artifact_provisioner_digest is not None and not is_canonical_artifact_provisioner_digest(
            node.artifact_provisioner_digest
        ):
            raise PublishCommandError(
                "artifact provisioner digest is not canonical: "
                f"{node.artifact_provisioner_digest}"
            )

    files = [
        BundleFile(
            path=str(path.relative_to(bundle_root).as_posix()),
            sha256=_sha256_literal(path.read_bytes()),
        )
        for path in sorted(bundle_root.rglob("*"))
        if path.is_file()
    ]
    manifest_without_hash = {
        "publisher": publisher,
        "workflow_id": workflow.workflow_id,
        "owners": {
            owner.name: owner.owner_url
            for owner in workflow.owners.values()
        },
        "files": [{"path": entry.path, "sha256": entry.sha256} for entry in files],
        "nodes": [_node_manifest_payload(node) for node in node_manifests],
    }
    manifest_hash = _sha256_literal(_canonical_json_bytes(manifest_without_hash))
    manifest_payload = {
        **manifest_without_hash,
        "manifest_hash": manifest_hash,
    }
    (bundle_root / MANIFEST_FILENAME).write_text(
        json.dumps(manifest_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return MaterializedWorkflowBundle(
        publisher=publisher,
        workflow_id=workflow.workflow_id,
        manifest_hash=manifest_hash,
        root_path=bundle_root,
        owners={
            owner.name: owner.owner_url
            for owner in workflow.owners.values()
        },
        files=files,
        nodes=node_manifests,
    )


def load_workflow_bundle(bundle_root: str | Path) -> MaterializedWorkflowBundle:
    resolved_root = Path(bundle_root).expanduser().resolve()
    return _parse_manifest_bytes(
        (resolved_root / MANIFEST_FILENAME).read_bytes(),
        bundle_root=resolved_root,
    )


def load_workflow_bundle_for_compose(
    compose_file_path: str | Path,
) -> tuple[MaterializedWorkflowBundle, MaterializedNode]:
    compose_path = Path(compose_file_path).expanduser().resolve()
    bundle_root = _find_bundle_root(compose_path)
    bundle = load_workflow_bundle(bundle_root)
    relative_compose_path = compose_path.relative_to(bundle_root).as_posix()
    for node in bundle.nodes:
        if node.compose_path == relative_compose_path:
            return bundle, node
    raise PublishCommandError(
        f"compose file is not listed in bundle.manifest.json: {compose_path}"
    )


def materialize_compiled_bundle(
    *,
    workflow: WorkflowDefinition,
    compile_artifact: CompileArtifact,
    publisher: str,
    bundle_root: Path,
) -> MaterializedWorkflowBundle:
    return bundle_compiled_workflow(
        workflow=workflow,
        compile_artifact=compile_artifact,
        publisher=publisher,
        bundle_root=bundle_root,
    )


def load_materialized_bundle(bundle_root: str | Path) -> MaterializedWorkflowBundle:
    return load_workflow_bundle(bundle_root)


def load_materialized_bundle_for_compose(
    compose_file_path: str | Path,
) -> tuple[MaterializedWorkflowBundle, MaterializedNode]:
    return load_workflow_bundle_for_compose(compose_file_path)


def materialized_artifact_owner_domain(
    bundle: MaterializedWorkflowBundle,
    artifact: MaterializedArtifact,
) -> str:
    owner_url = bundle.owners.get(artifact.owner)
    if owner_url is None:
        return artifact.owner
    return owner_domain_from_url(owner_url)


def materialized_artifact_hub_path(
    bundle: MaterializedWorkflowBundle,
    artifact: MaterializedArtifact,
) -> str:
    if artifact.hub_path.startswith("v1/runtime/"):
        return artifact.hub_path
    if artifact.hub_path.startswith("runtime/"):
        return f"v1/runtime/{bundle.publisher}/{artifact.hub_path.removeprefix('runtime/')}"

    match = _STATIC_HUB_PATH_RE.fullmatch(artifact.hub_path)
    if match is None:
        return artifact.hub_path

    owner_domain = materialized_artifact_owner_domain(bundle, artifact)
    path_owner = match.group("owner")
    if path_owner != owner_domain:
        raise PublishCommandError(
            f"artifact {artifact.name!r} hub_path owner {path_owner!r} "
            f"does not match owner domain {owner_domain!r}"
        )
    return artifact.hub_path


def parse_published_ref(published_ref: str) -> PublishedWorkflowRef:
    parts = published_ref.split("/")
    if len(parts) not in {2, 3} or not all(parts):
        raise PublishCommandError(
            "published workflow ref must use <publisher>/<workflow_id> or "
            "<publisher>/<workflow_id>/sha256:<digest>"
        )
    reference = parts[2] if len(parts) == 3 else "latest"
    if reference != "latest" and not re.fullmatch(r"sha256:[0-9a-f]{64}", reference):
        raise PublishCommandError("published workflow digest ref must use sha256:<hex>")
    return PublishedWorkflowRef(
        publisher=parts[0],
        workflow_id=parts[1],
        reference=reference,
    )


def load_canonical_container_digests() -> dict[str, str]:
    try:
        return load_canonical_container_digests_from_policy()
    except CanonicalImageError as exc:
        raise PublishCommandError(str(exc)) from exc


def _load_workflow_for_publish(workflow_path: Path) -> WorkflowDefinition:
    load_result = load_workflow_definition(workflow_path)
    if load_result.workflow is None:
        raise CompileCommandError("\n".join(load_result.errors))
    return load_result.workflow


@dataclass(frozen=True, slots=True)
class _GeneratedComposeInspection:
    runtime_skeleton: list[RuntimeSkeletonEntry]
    artifact_provisioner_image: str | None
    artifact_provisioner_digest: str | None


def _inspect_generated_compose(
    *,
    compose_path: Path,
) -> _GeneratedComposeInspection:
    try:
        compose_payload = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PublishCommandError(f"failed to parse generated compose at {compose_path}: {exc}") from exc
    if not isinstance(compose_payload, dict):
        raise PublishCommandError(f"generated compose at {compose_path} must be a YAML mapping")
    services = compose_payload.get("services")
    if not isinstance(services, dict):
        raise PublishCommandError(f"generated compose at {compose_path} must define services")
    declared_volumes = _declared_named_volumes(compose_payload, compose_path=compose_path)

    artifact_provisioner_image: str | None = None
    artifact_provisioner_digest: str | None = None

    for service_name, raw_service in services.items():
        if not isinstance(raw_service, dict):
            raise PublishCommandError(
                f"generated compose service {service_name!r} in {compose_path} must be a mapping"
            )
        service = json.loads(json.dumps(raw_service))
        image_value = service.get("image")
        if not isinstance(image_value, str) or not image_value.strip():
            raise PublishCommandError(
                f"generated compose service {service_name!r} in {compose_path} must define an image"
            )
        digest_match = _DIGEST_PINNED_IMAGE_RE.fullmatch(image_value)
        if digest_match is None:
            raise PublishCommandError(
                f"generated compose service {service_name!r} in {compose_path} is not digest pinned: {image_value}"
            )
        if image_value.startswith(f"{ARTIFACT_PROVISIONER_IMAGE_NAME}@") or (
            "/" in image_value and image_value.rsplit("/", 1)[-1].startswith(f"{ARTIFACT_PROVISIONER_IMAGE_NAME}@")
        ):
            if artifact_provisioner_image is None:
                artifact_provisioner_image = image_value
                artifact_provisioner_digest = digest_match.group("digest")
            elif artifact_provisioner_image != image_value:
                raise PublishCommandError(
                    f"node compose {compose_path} contains multiple artifact provisioner images"
                )

        raw_volumes = service.get("volumes")
        if raw_volumes is not None:
            if not isinstance(raw_volumes, list):
                raise PublishCommandError(
                    f"generated compose service {service_name!r} volumes must be a list"
                )
            for raw_volume in raw_volumes:
                if not isinstance(raw_volume, dict):
                    raise PublishCommandError(
                        f"generated compose service {service_name!r} volume entries must be mappings"
                    )
                volume = json.loads(json.dumps(raw_volume))
                volume_type = volume.get("type")
                source_value = volume.get("source")
                if not isinstance(source_value, str) or not source_value:
                    raise PublishCommandError(
                        f"generated compose service {service_name!r} volume source must be a string"
                    )
                target_value = volume.get("target")
                if not isinstance(target_value, str) or not target_value:
                    raise PublishCommandError(
                        f"generated compose service {service_name!r} volume target must be a string"
                    )
                if volume_type == "volume":
                    if source_value not in declared_volumes:
                        raise PublishCommandError(
                            f"generated compose service {service_name!r} uses undeclared named volume {source_value!r}"
                        )
                    continue
                if volume_type == "bind":
                    if source_value not in {"/var/run/dstack.sock", "/run/dstack.sock"}:
                        raise PublishCommandError(
                            f"generated compose service {service_name!r} has non-portable bind source {source_value!r}"
                        )
                    continue
                raise PublishCommandError(
                    f"generated compose service {service_name!r} uses unsupported volume type {volume_type!r}"
                )
    return _GeneratedComposeInspection(
        runtime_skeleton=[],
        artifact_provisioner_image=artifact_provisioner_image,
        artifact_provisioner_digest=artifact_provisioner_digest,
    )


def _load_generated_compose_hash(*, compose_path: Path, compose_hash_path: Path) -> str:
    if not compose_hash_path.is_file():
        raise PublishCommandError(f"generated compose hash file not found: {compose_hash_path}")
    compose_hash = compose_hash_path.read_text(encoding="utf-8").strip()
    if not compose_hash:
        raise PublishCommandError(f"generated compose hash file is empty: {compose_hash_path}")
    try:
        compose_payload = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise PublishCommandError(f"failed to parse generated compose at {compose_path}: {exc}") from exc
    if not isinstance(compose_payload, dict):
        raise PublishCommandError(f"generated compose at {compose_path} must be a YAML mapping")
    observed_hash = reviewed_compose_hash(compose_payload)
    if compose_hash != observed_hash:
        raise PublishCommandError(
            f"generated compose hash mismatch for {compose_path}: {compose_hash} != {observed_hash}"
        )
    return compose_hash


def _declared_named_volumes(
    compose_payload: dict[str, Any],
    *,
    compose_path: Path,
) -> set[str]:
    raw_volumes = compose_payload.get("volumes")
    if raw_volumes is None:
        return set()
    if not isinstance(raw_volumes, dict):
        raise PublishCommandError(f"generated compose at {compose_path} volumes must be a mapping")
    declared_volumes: set[str] = set()
    for volume_name, raw_config in raw_volumes.items():
        if not isinstance(volume_name, str) or not volume_name:
            raise PublishCommandError(
                f"generated compose at {compose_path} has an invalid named volume"
            )
        if raw_config is not None and not isinstance(raw_config, dict):
            raise PublishCommandError(
                f"generated compose volume {volume_name!r} at {compose_path} must be a mapping"
            )
        declared_volumes.add(volume_name)
    return declared_volumes


def _canonical_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _workflow_object_bytes(
    bundle: MaterializedWorkflowBundle,
    *,
    publisher_identity: dict[str, object] | None = None,
    publisher_private_key_path: Path | None = None,
) -> bytes:
    manifest_payload = json.loads((bundle.root_path / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    if not isinstance(manifest_payload, dict):
        raise PublishCommandError("bundle manifest must be a JSON object")
    workflow_object = {
        "format": "cove.workflow.bundle.v1",
        "manifest": manifest_payload,
        "files": [
            {
                "path": bundle_file.path,
                "sha256": bundle_file.sha256,
                "content_b64": base64.b64encode(
                    (bundle.root_path / bundle_file.path).read_bytes()
                ).decode("ascii"),
            }
            for bundle_file in bundle.files
        ],
    }
    if publisher_identity is not None or publisher_private_key_path is not None:
        if publisher_identity is None or publisher_private_key_path is None:
            raise PublishCommandError(
                "publisher identity and private key path are both required for workflow signing"
            )
        workflow_object["publisher_signature"] = _publisher_signature_payload(
            manifest_payload,
            publisher_identity=publisher_identity,
            publisher_private_key_path=publisher_private_key_path,
        )
    return _canonical_json_bytes(workflow_object)


def _parse_workflow_object_bytes(
    payload: bytes,
    *,
    expected_publisher: str | None = None,
    require_publisher_signature: bool = False,
) -> tuple[bytes, dict[str, bytes]]:
    try:
        workflow_object = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise PublishCommandError("workflow object is not valid JSON") from exc
    if not isinstance(workflow_object, dict):
        raise PublishCommandError("workflow object must be a JSON object")
    if workflow_object.get("format") != "cove.workflow.bundle.v1":
        raise PublishCommandError("workflow object has an unsupported format")
    manifest = workflow_object.get("manifest")
    raw_files = workflow_object.get("files")
    if not isinstance(manifest, dict) or not isinstance(raw_files, list):
        raise PublishCommandError("workflow object is missing manifest or files")
    raw_publisher_signature = workflow_object.get("publisher_signature")
    if raw_publisher_signature is None:
        if require_publisher_signature:
            raise PublishCommandError("workflow object is missing publisher_signature")
    elif isinstance(raw_publisher_signature, dict):
        _verify_publisher_signature(
            raw_publisher_signature,
            manifest=manifest,
            expected_publisher=expected_publisher,
        )
    else:
        raise PublishCommandError("workflow object publisher_signature must be an object")

    manifest_bytes = (
        json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    files: dict[str, bytes] = {}
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise PublishCommandError("workflow object file entries must be objects")
        path = raw_file.get("path")
        sha256 = raw_file.get("sha256")
        content_b64 = raw_file.get("content_b64")
        if (
            not isinstance(path, str)
            or not isinstance(sha256, str)
            or not isinstance(content_b64, str)
        ):
            raise PublishCommandError("workflow object file entry is invalid")
        _validate_relative_bundle_path(path, label="workflow object file path")
        try:
            file_bytes = base64.b64decode(content_b64.encode("ascii"), validate=True)
        except Exception as exc:
            raise PublishCommandError(
                f"workflow object file payload is not valid base64: {path}"
            ) from exc
        observed_sha = _sha256_literal(file_bytes)
        if observed_sha != sha256:
            raise PublishCommandError(
                f"workflow object file hash mismatch for {path}: {observed_sha} != {sha256}"
            )
        files[path] = file_bytes
    return manifest_bytes, files


def _publisher_signature_payload(
    manifest: dict[str, Any],
    *,
    publisher_identity: dict[str, object],
    publisher_private_key_path: Path,
) -> dict[str, Any]:
    publisher = _required_string(manifest, "publisher")
    try:
        verified_identity = verify_owner_identity_document(
            publisher_identity,
            expected_owner_domain=publisher,
        )
    except OwnerIdentityError as exc:
        raise PublishCommandError(f"publisher identity is invalid: {exc}") from exc
    private_key = load_pem_private_key(publisher_private_key_path.read_bytes(), password=None)
    if not isinstance(private_key, ed25519.Ed25519PrivateKey):
        raise PublishCommandError(f"publisher private key at {publisher_private_key_path} must be Ed25519")
    signature = private_key.sign(
        _canonical_json_bytes(_workflow_signature_payload(manifest))
    )
    return {
        "purpose": WORKFLOW_BUNDLE_SIGNATURE_PURPOSE,
        "signature_algorithm": "ed25519",
        "owner_identity": verified_identity,
        "signature": base64.b64encode(signature).decode("ascii"),
    }


def _verify_publisher_signature(
    publisher_signature: dict[str, Any],
    *,
    manifest: dict[str, Any],
    expected_publisher: str | None,
) -> None:
    purpose = _required_string(publisher_signature, "purpose")
    if purpose != WORKFLOW_BUNDLE_SIGNATURE_PURPOSE:
        raise PublishCommandError("workflow object publisher_signature purpose is unsupported")
    signature_algorithm = _required_string(publisher_signature, "signature_algorithm")
    if signature_algorithm != "ed25519":
        raise PublishCommandError("workflow object publisher_signature algorithm must be ed25519")
    publisher = _required_string(manifest, "publisher")
    if expected_publisher is not None and publisher != expected_publisher:
        raise PublishCommandError(
            f"workflow object publisher {publisher!r} does not match expected {expected_publisher!r}"
        )
    owner_identity = publisher_signature.get("owner_identity")
    if not isinstance(owner_identity, dict):
        raise PublishCommandError("workflow object publisher_signature.owner_identity must be an object")
    try:
        verified_identity = verify_owner_identity_document(
            owner_identity,
            expected_owner_domain=publisher,
        )
    except OwnerIdentityError as exc:
        raise PublishCommandError(f"workflow object publisher identity is invalid: {exc}") from exc
    public_key = load_pem_public_key(
        _required_string(verified_identity, "owner_public_key_pem").encode("utf-8")
    )
    if not isinstance(public_key, ed25519.Ed25519PublicKey):
        raise PublishCommandError("workflow object publisher public key must be Ed25519")
    try:
        signature = base64.b64decode(
            _required_string(publisher_signature, "signature").encode("ascii"),
            validate=True,
        )
    except Exception as exc:
        raise PublishCommandError("workflow object publisher_signature.signature must be valid base64") from exc
    try:
        public_key.verify(signature, _canonical_json_bytes(_workflow_signature_payload(manifest)))
    except Exception as exc:
        raise PublishCommandError("workflow object publisher signature verification failed") from exc


def _workflow_signature_payload(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "purpose": WORKFLOW_BUNDLE_SIGNATURE_PURPOSE,
        "manifest": manifest,
    }


def _node_manifest_payload(node: MaterializedNode) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "compose_path": node.compose_path,
        "compose_hash": node.compose_hash,
        "artifact_provisioner_image": node.artifact_provisioner_image,
        "artifact_provisioner_digest": node.artifact_provisioner_digest,
        "artifacts": [
            {
                "name": artifact.name,
                "type": artifact.type,
                "direction": artifact.direction,
                "hub_path": artifact.hub_path,
                "owner": artifact.owner,
            }
            for artifact in node.artifacts
        ],
        "runtime_skeleton": [
            {
                "path": entry.path,
                "kind": entry.kind,
            }
            for entry in node.runtime_skeleton
        ],
    }


def _parse_manifest_bytes(
    payload: bytes,
    *,
    bundle_root: Path,
) -> MaterializedWorkflowBundle:
    try:
        manifest = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise PublishCommandError("bundle manifest is not valid JSON") from exc
    if not isinstance(manifest, dict):
        raise PublishCommandError("bundle manifest must be a JSON object")

    publisher = manifest.get("publisher")
    workflow_id = manifest.get("workflow_id")
    manifest_hash = manifest.get("manifest_hash")
    raw_owners = manifest.get("owners")
    raw_files = manifest.get("files")
    raw_nodes = manifest.get("nodes")
    if (
        not isinstance(publisher, str)
        or not isinstance(workflow_id, str)
        or not isinstance(manifest_hash, str)
        or not isinstance(raw_owners, dict)
        or not isinstance(raw_files, list)
        or not isinstance(raw_nodes, list)
    ):
        raise PublishCommandError("bundle manifest is missing required fields")

    owners: dict[str, str] = {}
    for owner_name, owner_url in raw_owners.items():
        if not isinstance(owner_name, str) or not isinstance(owner_url, str):
            raise PublishCommandError("bundle manifest owners entries are invalid")
        owners[owner_name] = owner_url

    files: list[BundleFile] = []
    for raw_file in raw_files:
        if not isinstance(raw_file, dict):
            raise PublishCommandError("bundle manifest files entries must be objects")
        path = raw_file.get("path")
        sha256 = raw_file.get("sha256")
        if not isinstance(path, str) or not isinstance(sha256, str):
            raise PublishCommandError("bundle manifest files entries are invalid")
        _validate_relative_bundle_path(path, label="bundle file path")
        files.append(BundleFile(path=path, sha256=sha256))

    nodes: list[MaterializedNode] = []
    for raw_node in raw_nodes:
        if not isinstance(raw_node, dict):
            raise PublishCommandError("bundle manifest node entries must be objects")
        raw_artifacts = raw_node.get("artifacts")
        raw_runtime_skeleton = raw_node.get("runtime_skeleton")
        if not isinstance(raw_artifacts, list) or not isinstance(raw_runtime_skeleton, list):
            raise PublishCommandError("bundle manifest node entries are invalid")
        artifacts: list[MaterializedArtifact] = []
        for raw_artifact in raw_artifacts:
            if not isinstance(raw_artifact, dict):
                raise PublishCommandError("bundle manifest artifact entries must be objects")
            artifacts.append(
                MaterializedArtifact(
                    name=_required_string(raw_artifact, "name"),
                    type=_required_string(raw_artifact, "type"),
                    direction=_required_string(raw_artifact, "direction"),
                    hub_path=_required_string(raw_artifact, "hub_path"),
                    owner=_required_string(raw_artifact, "owner"),
                )
            )
        runtime_skeleton: list[RuntimeSkeletonEntry] = []
        for raw_entry in raw_runtime_skeleton:
            if not isinstance(raw_entry, dict):
                raise PublishCommandError(
                    "bundle manifest runtime_skeleton entries must be objects"
                )
            path = _required_string(raw_entry, "path")
            _validate_relative_bundle_path(path, label="runtime skeleton path")
            runtime_skeleton.append(
                RuntimeSkeletonEntry(
                    path=path,
                    kind=_required_string(raw_entry, "kind"),
                )
            )
        compose_path = _required_string(raw_node, "compose_path")
        _validate_relative_bundle_path(compose_path, label="node compose path")
        nodes.append(
            MaterializedNode(
                node_id=_required_string(raw_node, "node_id"),
                compose_path=compose_path,
                compose_hash=_required_string(raw_node, "compose_hash"),
                artifact_provisioner_image=_optional_string(raw_node, "artifact_provisioner_image"),
                artifact_provisioner_digest=_optional_string(raw_node, "artifact_provisioner_digest"),
                artifacts=artifacts,
                runtime_skeleton=runtime_skeleton,
            )
        )

    return MaterializedWorkflowBundle(
        publisher=publisher,
        workflow_id=workflow_id,
        manifest_hash=manifest_hash,
        root_path=bundle_root,
        owners=owners,
        files=files,
        nodes=nodes,
    )


def _verify_manifest_hash(bundle: MaterializedWorkflowBundle) -> None:
    payload = {
        "publisher": bundle.publisher,
        "workflow_id": bundle.workflow_id,
        "owners": dict(bundle.owners),
        "files": [{"path": file.path, "sha256": file.sha256} for file in bundle.files],
        "nodes": [_node_manifest_payload(node) for node in bundle.nodes],
    }
    observed_hash = _sha256_literal(_canonical_json_bytes(payload))
    if observed_hash != bundle.manifest_hash:
        raise PublishCommandError(
            f"bundle manifest hash mismatch: {observed_hash} != {bundle.manifest_hash}"
        )


def _recreate_runtime_skeleton(
    bundle_root: Path,
    bundle: MaterializedWorkflowBundle,
) -> None:
    for node in bundle.nodes:
        node_root = bundle_root / "nodes" / node.node_id
        for entry in node.runtime_skeleton:
            target_path = node_root / entry.path
            if entry.kind == "dir":
                target_path.mkdir(parents=True, exist_ok=True)
            elif entry.kind == "file":
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.touch(exist_ok=True)
            else:
                raise PublishCommandError(
                    f"unsupported runtime skeleton entry kind: {entry.kind}"
                )
def _find_bundle_root(compose_path: Path) -> Path:
    for parent in (compose_path.parent, *compose_path.parents):
        if (parent / MANIFEST_FILENAME).is_file():
            return parent
    raise PublishCommandError(
        f"could not find {MANIFEST_FILENAME} for compose file {compose_path}"
    )


def _sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _sha256_text(payload: str) -> str:
    return _sha256_literal(payload.encode("utf-8"))


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise PublishCommandError(f"bundle manifest field {key!r} must be a string")
    return value


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise PublishCommandError(f"bundle manifest field {key!r} must be a string")
    return value




def _validate_relative_bundle_path(path: str, *, label: str) -> None:
    pure_path = PurePosixPath(path)
    if pure_path.is_absolute():
        raise PublishCommandError(f"{label} must not be absolute: {path}")
    if any(part in {"", ".", ".."} for part in pure_path.parts):
        raise PublishCommandError(f"{label} contains unsafe traversal: {path}")
