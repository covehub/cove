from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from cove_container_runtime.attestation import (
    build_key_release_report_data,
    build_runtime_artifact_report_data,
    collect_attestation_bundle,
    load_attestation_settings,
)
from cove_container_runtime.common import (
    COVE_RUNTIME_USER_AGENT,
    RuntimeErrorBase,
    RuntimeMetricsRecorder,
    SidecarConfigError,
    decode_key_b64,
    decrypt_ciphertext_bytes,
    decrypt_ciphertext_file,
    encrypt_plaintext_bytes,
    http_get_bytes,
    http_get_to_file,
    http_put_bytes,
    http_put_bytes_upload_session,
    join_url,
    load_inline_sidecar_context,
    log,
    optional_string,
    read_json_file,
    required_mapping,
    required_string,
    runtime_metrics_recorder_from_env,
    runtime_phase,
    sha256_file_literal,
    sha256_literal,
    url_with_query,
    wait_for_file,
    write_bytes_file,
    write_json_file,
)
from cove_container_runtime.owner_identity import (
    OwnerIdentityError,
    verify_owner_identity_document,
    verify_owner_signed_response_payload,
)

_DIGEST_PINNED_IMAGE_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_STATIC_HUB_PATH_RE = re.compile(
    r"^v1/artifacts/(?P<owner>[^/]+)/(?P<artifact>[^/]+)/"
    r"(?P<reference>sha256:[0-9a-f]{64})$"
)


class _ResolvedOwnerConfig:
    __slots__ = ("name", "owner_url", "owner_domain", "owner_identity")

    def __init__(
        self,
        *,
        name: str,
        owner_url: str,
        owner_domain: str,
        owner_identity: dict[str, Any],
    ) -> None:
        self.name = name
        self.owner_url = owner_url
        self.owner_domain = owner_domain
        self.owner_identity = owner_identity


def _artifact_provisioner_digest(image_reference: str) -> str:
    _name, separator, digest = image_reference.rpartition("@")
    if not separator or not digest:
        raise RuntimeErrorBase("artifact_provisioner_image must be digest-pinned")
    return digest


def run(
    config: dict[str, object],
    *,
    compose_hash: str | None = None,
    artifact_provisioner_image: str | None = None,
    metrics: RuntimeMetricsRecorder | None = None,
) -> None:
    mode = optional_string(config.get("mode"), "mode") or "static_input"
    if mode == "static_input":
        _run_static_input(
            config,
            compose_hash=compose_hash,
            artifact_provisioner_image=artifact_provisioner_image,
            metrics=metrics,
        )
        return
    if mode == "dynamic_input":
        _run_dynamic_input(
            config,
            compose_hash=compose_hash,
            artifact_provisioner_image=artifact_provisioner_image,
            metrics=metrics,
        )
        return
    if mode == "dynamic_output":
        _run_dynamic_output(
            config,
            compose_hash=compose_hash,
            artifact_provisioner_image=artifact_provisioner_image,
            metrics=metrics,
        )
        return
    raise SidecarConfigError(f"unsupported artifact provisioner mode: {mode}")


def _materialize_encrypted_artifact(
    *,
    artifact_name: str,
    download_url: str,
    staged_plaintext_path: str,
    expected_ciphertext_hash: str,
    expected_plaintext_hash: str,
    key_bytes: bytes,
    metrics: RuntimeMetricsRecorder | None = None,
) -> None:
    target_path = Path(staged_plaintext_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    ciphertext_path = target_path.parent / f".{target_path.name}.ciphertext.tmp"
    plaintext_path = target_path.parent / f".{target_path.name}.plaintext.tmp"
    for scratch_path in (ciphertext_path, plaintext_path):
        scratch_path.unlink(missing_ok=True)

    try:
        with runtime_phase(metrics, "download_ciphertext_seconds"):
            http_get_to_file(url=download_url, output_path=ciphertext_path)
        with runtime_phase(metrics, "verify_ciphertext_hash_seconds"):
            observed_ciphertext_hash = sha256_file_literal(ciphertext_path)
        if observed_ciphertext_hash != expected_ciphertext_hash:
            raise RuntimeErrorBase(
                "ciphertext hash mismatch for "
                f"{artifact_name}: {observed_ciphertext_hash} != {expected_ciphertext_hash}"
            )

        with runtime_phase(metrics, "decrypt_artifact_seconds"):
            observed_plaintext_hash = decrypt_ciphertext_file(
                ciphertext_path=ciphertext_path,
                plaintext_path=plaintext_path,
                key_bytes=key_bytes,
            )
        if observed_plaintext_hash != expected_plaintext_hash:
            raise RuntimeErrorBase(
                "decrypted plaintext hash mismatch for "
                f"{artifact_name}: {observed_plaintext_hash} != {expected_plaintext_hash}"
            )

        with runtime_phase(metrics, "stage_plaintext_seconds"):
            plaintext_path.replace(target_path)
    finally:
        ciphertext_path.unlink(missing_ok=True)
        plaintext_path.unlink(missing_ok=True)


def _run_static_input(
    config: dict[str, object],
    *,
    compose_hash: str | None,
    artifact_provisioner_image: str | None,
    metrics: RuntimeMetricsRecorder | None = None,
) -> None:
    with runtime_phase(metrics, "resolve_static_input_seconds"):
        artifact_name = required_string(config, "artifact_name")
        owner_config = _resolve_owner_config(config)
        hub_path = required_string(config, "hub_path")
        materialized_hub_path = _materialize_hub_path(
            config,
            hub_path=hub_path,
            owner_config=owner_config,
        )
        static_hub_match = _validate_static_hub_path(
            hub_path=materialized_hub_path,
            artifact_name=artifact_name,
            owner_config=owner_config,
        )
        expected_plaintext_hash = required_string(config, "expected_plaintext_hash")
        staged_plaintext_path = required_string(config, "staged_plaintext_path")
        metadata_path = required_string(config, "metadata_path")

    with runtime_phase(metrics, "key_release_seconds"):
        provision_response = _fetch_key_release_response(
            config,
            owner_config=owner_config,
            hub_path=materialized_hub_path,
            compose_hash=compose_hash,
            artifact_provisioner_image=artifact_provisioner_image,
            require_allow_rule=False,
        )

    with runtime_phase(metrics, "verify_key_release_seconds"):
        plaintext_hash = required_string(provision_response, "plaintext_hash")
        ciphertext_hash = required_string(provision_response, "ciphertext_hash")
        encryption_algorithm = required_string(provision_response, "encryption_algorithm")
        key_bytes = decode_key_b64(required_string(provision_response, "key_b64"))

        _verify_owner_response_binding(
            provision_response,
            owner_config=owner_config,
            artifact_name=artifact_name,
            hub_path=materialized_hub_path,
            require_owner_url=True,
        )
        if encryption_algorithm != "aes-256-gcm":
            raise RuntimeErrorBase(
                f"unsupported encryption algorithm for {artifact_name}: {encryption_algorithm}"
            )
        if plaintext_hash != expected_plaintext_hash:
            raise RuntimeErrorBase(
                f"plaintext hash mismatch for {artifact_name}: {plaintext_hash} != {expected_plaintext_hash}"
            )
        if static_hub_match.group("reference") != ciphertext_hash:
            raise RuntimeErrorBase(
                f"ciphertext hash path mismatch for {artifact_name}: {static_hub_match.group('reference')} != {ciphertext_hash}"
            )

    _materialize_encrypted_artifact(
        artifact_name=artifact_name,
        download_url=join_url(_server_url(config), materialized_hub_path),
        staged_plaintext_path=staged_plaintext_path,
        expected_ciphertext_hash=ciphertext_hash,
        expected_plaintext_hash=plaintext_hash,
        key_bytes=key_bytes,
        metrics=metrics,
    )
    with runtime_phase(metrics, "write_input_metadata_seconds"):
        write_json_file(
            metadata_path,
            {
                "artifact_name": artifact_name,
                "owner_domain": owner_config.owner_domain,
                "hub_path": materialized_hub_path,
                "plaintext_hash": plaintext_hash,
                "ciphertext_hash": ciphertext_hash,
                "content_type": provision_response.get("content_type", "application/octet-stream"),
                "transport_mode": provision_response.get("transport_mode", "encrypted"),
                "key_path": provision_response.get("key_path"),
                "encryption_algorithm": encryption_algorithm,
            },
        )
    log("artifact_provisioner", f"provisioned static input {artifact_name} from {materialized_hub_path}")


def _run_dynamic_input(
    config: dict[str, object],
    *,
    compose_hash: str | None,
    artifact_provisioner_image: str | None,
    metrics: RuntimeMetricsRecorder | None = None,
) -> None:
    with runtime_phase(metrics, "resolve_dynamic_input_seconds"):
        artifact_name = required_string(config, "artifact_name")
        owner_config = _resolve_owner_config(config)
        hub_path = required_string(config, "hub_path")
        materialized_hub_path = _materialize_hub_path(
            config,
            hub_path=hub_path,
            owner_config=owner_config,
        )
        producer_certificate_path = required_string(config, "producer_certificate_path")
        staged_plaintext_path = required_string(config, "staged_plaintext_path")
        metadata_path = required_string(config, "metadata_path")

    with runtime_phase(metrics, "verify_producer_certificate_seconds"):
        output_metadata = _load_dynamic_output_metadata(
            certificate_path=producer_certificate_path,
            artifact_name=artifact_name,
            expected_owner_domain=owner_config.owner_domain,
            expected_channel_hub_path=materialized_hub_path,
        )
        exact_hub_path = required_string(output_metadata, "hub_path")
        channel_hub_path = optional_string(
            output_metadata.get("channel_hub_path"),
            "channel_hub_path",
        ) or materialized_hub_path
    with runtime_phase(metrics, "key_release_seconds"):
        provision_response = _fetch_key_release_response(
            config,
            owner_config=owner_config,
            hub_path=channel_hub_path,
            compose_hash=compose_hash,
            artifact_provisioner_image=artifact_provisioner_image,
            require_allow_rule=True,
        )
    with runtime_phase(metrics, "verify_key_release_seconds"):
        _verify_owner_response_binding(
            provision_response,
            owner_config=owner_config,
            artifact_name=artifact_name,
            hub_path=channel_hub_path,
            require_owner_url=False,
        )
        encryption_algorithm = required_string(provision_response, "encryption_algorithm")
        key_bytes = decode_key_b64(required_string(provision_response, "key_b64"))
        if encryption_algorithm != "aes-256-gcm":
            raise RuntimeErrorBase(
                f"unsupported encryption algorithm for {artifact_name}: {encryption_algorithm}"
            )

    _materialize_encrypted_artifact(
        artifact_name=artifact_name,
        download_url=join_url(_server_url(config), exact_hub_path),
        staged_plaintext_path=staged_plaintext_path,
        expected_ciphertext_hash=required_string(output_metadata, "ciphertext_hash"),
        expected_plaintext_hash=required_string(output_metadata, "plaintext_hash"),
        key_bytes=key_bytes,
        metrics=metrics,
    )
    with runtime_phase(metrics, "write_input_metadata_seconds"):
        write_json_file(
            metadata_path,
            {
                **output_metadata,
                "key_path": provision_response.get("key_path"),
                "transport_mode": output_metadata.get("transport_mode", "encrypted"),
            },
        )
    log("artifact_provisioner", f"provisioned dynamic input {artifact_name} from {exact_hub_path}")


def _run_dynamic_output(
    config: dict[str, object],
    *,
    compose_hash: str | None,
    artifact_provisioner_image: str | None,
    metrics: RuntimeMetricsRecorder | None = None,
) -> None:
    with runtime_phase(metrics, "resolve_dynamic_output_seconds"):
        artifact_name = required_string(config, "artifact_name")
        owner_config = _resolve_owner_config(config)
        hub_path = required_string(config, "hub_path")
        materialized_hub_path = _materialize_hub_path(
            config,
            hub_path=hub_path,
            owner_config=owner_config,
        )
        workflow_id = required_string(config, "workflow_id")
        node_id = required_string(config, "node_id")
        output_source_path = required_string(config, "output_source_path")
        metadata_path = required_string(config, "metadata_path")
        if compose_hash is None:
            compose_hash = required_string(config, "compose_hash")

    with runtime_phase(metrics, "key_release_seconds"):
        provision_response = _fetch_key_release_response(
            config,
            owner_config=owner_config,
            hub_path=materialized_hub_path,
            compose_hash=compose_hash,
            artifact_provisioner_image=artifact_provisioner_image,
            require_allow_rule=True,
        )
    with runtime_phase(metrics, "verify_key_release_seconds"):
        _verify_owner_response_binding(
            provision_response,
            owner_config=owner_config,
            artifact_name=artifact_name,
            hub_path=materialized_hub_path,
            require_owner_url=False,
        )
        encryption_algorithm = required_string(provision_response, "encryption_algorithm")
        key_bytes = decode_key_b64(required_string(provision_response, "key_b64"))
        if encryption_algorithm != "aes-256-gcm":
            raise RuntimeErrorBase(
                f"unsupported encryption algorithm for {artifact_name}: {encryption_algorithm}"
            )

    with runtime_phase(metrics, "wait_for_output_seconds"):
        output_path = wait_for_file(output_source_path)
    with runtime_phase(metrics, "read_output_seconds"):
        plaintext = output_path.read_bytes()
        plaintext_hash = sha256_literal(plaintext)
    with runtime_phase(metrics, "encrypt_output_seconds"):
        ciphertext = encrypt_plaintext_bytes(plaintext=plaintext, key_bytes=key_bytes)
        ciphertext_hash = sha256_literal(ciphertext)
        exact_hub_path = _exact_hub_path(materialized_hub_path, ciphertext_hash)
        content_type = mimetypes.guess_type(output_path.name)[0] or "application/octet-stream"

    with runtime_phase(metrics, "collect_output_attestation_seconds"):
        attestation_bundle = collect_attestation_bundle(
            load_attestation_settings(config),
            report_data=build_runtime_artifact_report_data(
                workflow_id=workflow_id,
                node_id=node_id,
                compose_hash=compose_hash,
                artifact_name=artifact_name,
            ),
        )
        event_log = attestation_bundle.get("event_log")
        if event_log is None:
            raise RuntimeErrorBase("runtime artifact attestation bundle is missing event_log")
        attestation_payload: dict[str, object] = {
            "quote": required_string(attestation_bundle, "quote"),
            "event_log": event_log,
            "workflow_id": workflow_id,
            "artifact_name": artifact_name,
            "node_id": node_id,
            "compose_hash": compose_hash,
            "attestation_format": required_string(attestation_bundle, "format"),
            "report_data": required_string(attestation_bundle, "report_data"),
        }
        info = attestation_bundle.get("info")
        if isinstance(info, dict):
            attestation_payload["info"] = info

    with runtime_phase(metrics, "upload_output_seconds"):
        http_put_bytes_upload_session(
            url=join_url(_server_url(config), exact_hub_path),
            payload=ciphertext,
            headers={"Content-Type": "application/octet-stream"},
            session_create_payload={
                "upload_length": len(ciphertext),
                "attestation": attestation_payload,
            },
        )

    with runtime_phase(metrics, "write_output_metadata_seconds"):
        write_json_file(
            metadata_path,
            {
                "artifact_name": artifact_name,
                "owner_domain": owner_config.owner_domain,
                "hub_path": exact_hub_path,
                "channel_hub_path": materialized_hub_path,
                "plaintext_hash": plaintext_hash,
                "ciphertext_hash": ciphertext_hash,
                "content_type": content_type,
                "transport_mode": "encrypted",
                "key_path": provision_response.get("key_path"),
                "encryption_algorithm": encryption_algorithm,
            },
        )
    log("artifact_provisioner", f"published dynamic output {artifact_name} to {exact_hub_path}")


def _fetch_key_release_response(
    config: dict[str, object],
    *,
    owner_config: _ResolvedOwnerConfig,
    hub_path: str,
    compose_hash: str | None,
    artifact_provisioner_image: str | None,
    require_allow_rule: bool,
) -> dict[str, Any]:
    owner_url = owner_config.owner_url
    owner_identity = owner_config.owner_identity
    workflow_publisher_domain = optional_string(
        config.get("workflow_publisher_domain"),
        "workflow_publisher_domain",
    )
    workflow_id = optional_string(config.get("workflow_id"), "workflow_id")
    node_id = optional_string(config.get("node_id"), "node_id")
    if compose_hash is None:
        compose_hash = optional_string(config.get("compose_hash"), "compose_hash")
    if artifact_provisioner_image is None:
        artifact_provisioner_image = optional_string(
            config.get("artifact_provisioner_image"),
            "artifact_provisioner_image",
        )

    allow_gated_identity = (workflow_publisher_domain, workflow_id, node_id, compose_hash)
    has_allow_gated_identity = all(value is not None for value in allow_gated_identity)
    has_partial_allow_gated_identity = any(value is not None for value in allow_gated_identity)
    if has_partial_allow_gated_identity and not has_allow_gated_identity:
        raise SidecarConfigError(f"incomplete allow-gated provisioner workflow identity for {hub_path}")

    if (
        has_allow_gated_identity
        and artifact_provisioner_image is not None
        and _DIGEST_PINNED_IMAGE_RE.fullmatch(artifact_provisioner_image)
    ):
        artifact_provisioner_digest = _artifact_provisioner_digest(artifact_provisioner_image)
        attestation_bundle = collect_attestation_bundle(
            load_attestation_settings(config),
            report_data=build_key_release_report_data(
                workflow_publisher_domain=workflow_publisher_domain,
                workflow_id=workflow_id,
                node_id=node_id,
                compose_hash=compose_hash,
                artifact_provisioner_digest=artifact_provisioner_digest,
            ),
        )
        key_release_url = join_url(owner_url, "/v1/artifacts/key-release")
        payload = {
            "hub_path": hub_path,
            "workflow_publisher_domain": workflow_publisher_domain,
            "workflow_id": workflow_id,
            "node_id": node_id,
            "compose_hash": compose_hash,
            "artifact_provisioner_image": artifact_provisioner_image,
            "attestation": attestation_bundle,
        }
        return _http_post_json_with_owner_identity(
            url=key_release_url,
            payload=payload,
            owner_identity=owner_identity,
        )

    if require_allow_rule:
        raise SidecarConfigError(
            "allow-gated artifact key release requires workflow identity, compose hash, "
            "and a digest-pinned artifact provisioner image"
        )

    by_hub_path_url = url_with_query(
        owner_url,
        "/v1/artifacts/by-hub-path",
        {"hub_path": hub_path},
    )
    return _http_get_json_with_owner_identity(
        url=by_hub_path_url,
        owner_identity=owner_identity,
    )


def _verified_owner_identity(
    config: dict[str, object],
    *,
    owner_url: str,
    expected_owner_domain: str | None = None,
) -> dict[str, Any]:
    raw_identity = config.get("owner_identity")
    if raw_identity is None:
        raise SidecarConfigError("owner_identity is required")
    identity = required_mapping(raw_identity, "owner_identity")
    try:
        return verify_owner_identity_document(
            identity,
            expected_owner_url=owner_url,
            expected_owner_domain=expected_owner_domain,
        )
    except OwnerIdentityError as exc:
        raise SidecarConfigError(f"invalid owner_identity for {owner_url}: {exc}") from exc


def _resolve_owner_config(config: dict[str, object]) -> _ResolvedOwnerConfig:
    owner_name = optional_string(config.get("owner"), "owner")
    expected_owner_domain = optional_string(config.get("owner_domain"), "owner_domain")
    if owner_name is not None:
        owners = required_mapping(config.get("owners"), "owners")
        owner_url = required_string(owners, owner_name)
    else:
        owner_url = required_string(config, "owner_url")
        owner_name = expected_owner_domain or owner_url

    owner_identity = _verified_owner_identity(
        config,
        owner_url=owner_url,
        expected_owner_domain=expected_owner_domain,
    )
    return _ResolvedOwnerConfig(
        name=owner_name,
        owner_url=owner_url,
        owner_domain=required_string(owner_identity, "owner_domain"),
        owner_identity=owner_identity,
    )


def _materialize_hub_path(
    config: dict[str, object],
    *,
    hub_path: str,
    owner_config: _ResolvedOwnerConfig,
) -> str:
    if hub_path.startswith("runtime/"):
        workflow_publisher_domain = required_string(config, "workflow_publisher_domain")
        return f"v1/runtime/{workflow_publisher_domain}/{hub_path.removeprefix('runtime/')}"
    if hub_path.startswith("v1/runtime/"):
        return hub_path

    match = _STATIC_HUB_PATH_RE.fullmatch(hub_path)
    if match is None:
        return hub_path

    path_owner = match.group("owner")
    if path_owner == owner_config.owner_domain:
        return hub_path
    raise SidecarConfigError(
        f"hub_path owner {path_owner!r} does not match verified owner domain {owner_config.owner_domain!r}"
    )


def _validate_static_hub_path(
    *,
    hub_path: str,
    artifact_name: str,
    owner_config: _ResolvedOwnerConfig,
) -> re.Match[str]:
    match = _STATIC_HUB_PATH_RE.fullmatch(hub_path)
    if match is None:
        raise SidecarConfigError(
            f"static artifact hub_path must be an exact v1/artifacts path: {hub_path}"
        )
    if match.group("owner") != owner_config.owner_domain:
        raise SidecarConfigError(
            f"static artifact hub_path owner {match.group('owner')!r} does not match verified owner domain {owner_config.owner_domain!r}"
        )
    if match.group("artifact") != artifact_name:
        raise SidecarConfigError(
            f"static artifact hub_path artifact {match.group('artifact')!r} does not match {artifact_name!r}"
        )
    return match


def _verify_owner_response_binding(
    response: dict[str, Any],
    *,
    owner_config: _ResolvedOwnerConfig,
    artifact_name: str,
    hub_path: str,
    require_owner_url: bool,
) -> None:
    response_hub_path = required_string(response, "hub_path")
    if response_hub_path != hub_path:
        raise RuntimeErrorBase(
            f"owner response hub_path mismatch for {artifact_name}: {response_hub_path} != {hub_path}"
        )
    response_owner_domain = required_string(response, "owner_domain")
    if response_owner_domain != owner_config.owner_domain:
        raise RuntimeErrorBase(
            f"owner response owner_domain mismatch for {artifact_name}: "
            f"{response_owner_domain} != {owner_config.owner_domain}"
        )
    if require_owner_url or response.get("owner_url") is not None:
        response_owner_url = required_string(response, "owner_url").rstrip("/")
        if response_owner_url != owner_config.owner_url.rstrip("/"):
            raise RuntimeErrorBase(
                f"owner response owner_url mismatch for {artifact_name}: "
                f"{response_owner_url} != {owner_config.owner_url.rstrip('/')}"
            )


def _http_post_json_with_owner_identity(
    *,
    url: str,
    payload: dict[str, Any],
    owner_identity: dict[str, Any],
    timeout: float = 60.0,
) -> dict[str, Any]:
    request = urllib_request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    return _http_json_with_owner_identity(
        request=request,
        owner_identity=owner_identity,
        timeout=timeout,
    )


def _http_get_json_with_owner_identity(
    *,
    url: str,
    owner_identity: dict[str, Any],
    timeout: float = 60.0,
) -> dict[str, Any]:
    request = urllib_request.Request(url, method="GET")
    return _http_json_with_owner_identity(
        request=request,
        owner_identity=owner_identity,
        timeout=timeout,
    )


def _http_json_with_owner_identity(
    *,
    request: urllib_request.Request,
    owner_identity: dict[str, Any],
    timeout: float,
) -> dict[str, Any]:
    body = _http_bytes_with_owner_identity(
        request=request,
        owner_identity=owner_identity,
        timeout=timeout,
    )
    try:
        response_payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeErrorBase(
            f"server returned invalid JSON for {request.full_url}"
        ) from exc
    if not isinstance(response_payload, dict):
        raise RuntimeErrorBase(
            f"server returned a non-object JSON payload for {request.full_url}"
        )
    try:
        return verify_owner_signed_response_payload(
            response_payload,
            owner_identity=owner_identity,
        )
    except OwnerIdentityError as exc:
        raise RuntimeErrorBase(
            f"owner response signature verification failed for {request.full_url}: {exc}"
        ) from exc


def _http_bytes_with_owner_identity(
    *,
    request: urllib_request.Request,
    owner_identity: dict[str, Any],
    timeout: float,
) -> bytes:
    if not request.has_header("User-agent"):
        request.add_header("User-Agent", COVE_RUNTIME_USER_AGENT)
    try:
        with urllib_request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib_error.HTTPError as exc:
        detail = _extract_http_detail(exc)
        raise RuntimeErrorBase(f"HTTP {exc.code} for {request.full_url}: {detail}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeErrorBase(f"failed to reach {request.full_url}: {exc.reason}") from exc


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


def _load_dynamic_output_metadata(
    *,
    certificate_path: str,
    artifact_name: str,
    expected_owner_domain: str,
    expected_channel_hub_path: str,
) -> dict[str, Any]:
    certificate = read_json_file(wait_for_file(certificate_path))
    certificate_body = required_mapping(certificate.get("certificate_body"), "certificate_body")
    outputs = required_mapping(certificate_body.get("outputs"), "certificate_body.outputs")
    output_metadata = required_mapping(outputs.get(artifact_name), f"certificate_body.outputs.{artifact_name}")

    owner_domain = required_string(output_metadata, "owner_domain")
    hub_path = required_string(output_metadata, "hub_path")
    channel_hub_path = optional_string(
        output_metadata.get("channel_hub_path"),
        "channel_hub_path",
    )
    if owner_domain != expected_owner_domain:
        raise RuntimeErrorBase(
            f"producer certificate owner mismatch for {artifact_name}: {owner_domain} != {expected_owner_domain}"
        )
    if channel_hub_path is not None:
        observed_channel = channel_hub_path
    else:
        observed_channel = _latest_hub_path(hub_path)
    if observed_channel != expected_channel_hub_path:
        raise RuntimeErrorBase(
            "producer certificate channel hub path mismatch for "
            f"{artifact_name}: {observed_channel} != {expected_channel_hub_path}"
        )

    required_string(output_metadata, "plaintext_hash")
    required_string(output_metadata, "ciphertext_hash")
    required_string(output_metadata, "encryption_algorithm")
    return output_metadata


def _exact_hub_path(channel_hub_path: str, ciphertext_hash: str) -> str:
    if not channel_hub_path.endswith("/latest"):
        raise RuntimeErrorBase(
            f"dynamic artifact hub_path must end with /latest: {channel_hub_path}"
        )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", ciphertext_hash):
        raise RuntimeErrorBase(f"invalid ciphertext hash: {ciphertext_hash}")
    return f"{channel_hub_path[:-len('/latest')]}/{ciphertext_hash}"


def _latest_hub_path(exact_hub_path: str) -> str:
    match = re.fullmatch(r"(?P<prefix>.+)/sha256:[0-9a-f]{64}", exact_hub_path)
    if match is None:
        return exact_hub_path
    return f"{match.group('prefix')}/latest"


def _server_url(config: dict[str, object]) -> str:
    return required_string(config, "covehub_server_url")


def main() -> int:
    metrics: RuntimeMetricsRecorder | None = None
    try:
        context = load_inline_sidecar_context()
        mode = optional_string(context.config.get("mode"), "mode") or "static_input"
        metrics = runtime_metrics_recorder_from_env(
            service_name=context.service_name,
            role=f"artifact_provisioner:{mode}",
        )
        run(context.config, compose_hash=context.compose_hash, metrics=metrics)
        metrics.write(exit_code=0)
        return 0
    except (RuntimeErrorBase, SidecarConfigError) as exc:
        if metrics is not None:
            metrics.write(exit_code=1, error=str(exc))
        print(f"ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
