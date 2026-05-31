from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Annotated, Any

from cove_container_runtime.attestation import (
    build_node_certificate_report_data,
    build_runtime_artifact_report_data,
)
from cove_container_runtime.common import canonical_json_bytes, sha256_literal
from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from .config import Settings
from .quote_verifier import (
    QuoteVerificationError,
    RuntimeAttestation,
    build_quote_verifier,
)
from .storage import (
    LocalStorage,
    PathConflictError,
    StorageError,
    UploadSession,
    UploadSessionConflictError,
    UploadSessionNotFoundError,
    UploadSessionValidationError,
)
from .validation import (
    ValidationError,
    ensure_hash_segment,
    ensure_identifier,
    ensure_owner_domain,
)
from .write_auth import WriteAuthError, verify_domain_write


@dataclass(slots=True)
class Services:
    settings: Settings
    storage: LocalStorage
    quote_verifier: object


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    services = Services(
        settings=settings,
        storage=LocalStorage(settings.data_root),
        quote_verifier=build_quote_verifier(settings.quote_verifier_mode),
    )

    app = FastAPI(title="Covehub Server", version="0.2.0")
    app.state.services = services

    @app.exception_handler(ValidationError)
    async def handle_validation_error(
        _request: Request,
        exc: ValidationError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    @app.exception_handler(StorageError)
    async def handle_storage_error(
        _request: Request,
        exc: StorageError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": str(exc)},
        )

    @app.get("/healthz")
    def healthz(current_services: Services = Depends(_get_services)) -> dict[str, object]:
        data_root_ok = current_services.storage.is_ready()
        return {
            "ok": data_root_ok,
            "app": "ok",
            "data_root": "ok" if data_root_ok else "error",
        }

    @app.put("/v1/artifacts/{owner}/{artifact_name}/{hash_segment}")
    async def put_static_artifact(
        owner: str,
        artifact_name: str,
        hash_segment: str,
        request: Request,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        owner = ensure_owner_domain(owner, "artifact owner")
        artifact_name = ensure_identifier(artifact_name, "artifact name")
        payload = await request.body()
        _verify_domain_write_request(request, owner_domain=owner, payload=payload)
        return _write_typed_object(
            current_services,
            namespace_parts=["artifacts", owner, artifact_name],
            hash_segment=hash_segment,
            payload=payload,
        )

    @app.post("/v1/artifacts/{owner}/{artifact_name}/{hash_segment}/upload-session")
    async def create_static_artifact_upload_session(
        owner: str,
        artifact_name: str,
        hash_segment: str,
        request: Request,
        current_services: Services = Depends(_get_services),
    ) -> JSONResponse:
        owner = ensure_owner_domain(owner, "artifact owner")
        artifact_name = ensure_identifier(artifact_name, "artifact name")
        payload = await request.body()
        _verify_domain_write_request(request, owner_domain=owner, payload=payload)
        expected_size = _parse_upload_session_request_payload(payload)
        session = _create_upload_session(
            current_services,
            namespace_parts=["artifacts", owner, artifact_name],
            hash_segment=hash_segment,
            expected_size=expected_size,
        )
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=_upload_session_payload(session))

    @app.get("/v1/artifacts/{owner}/{artifact_name}/{reference}")
    def get_static_artifact(
        owner: str,
        artifact_name: str,
        reference: str,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        owner = ensure_owner_domain(owner, "artifact owner")
        artifact_name = ensure_identifier(artifact_name, "artifact name")
        return _read_object_response(
            current_services,
            ["artifacts", owner, artifact_name, _ensure_reference(reference)],
        )

    @app.head("/v1/artifacts/{owner}/{artifact_name}/{reference}")
    def head_static_artifact(
        owner: str,
        artifact_name: str,
        reference: str,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        owner = ensure_owner_domain(owner, "artifact owner")
        artifact_name = ensure_identifier(artifact_name, "artifact name")
        return _head_object_response(
            current_services,
            ["artifacts", owner, artifact_name, _ensure_reference(reference)],
        )

    @app.put("/v1/workflows/{publisher}/{workflow_id}/{hash_segment}")
    async def put_workflow_object(
        publisher: str,
        workflow_id: str,
        hash_segment: str,
        request: Request,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        publisher = ensure_owner_domain(publisher, "workflow publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        payload = await request.body()
        _verify_domain_write_request(request, owner_domain=publisher, payload=payload)
        return _write_typed_object(
            current_services,
            namespace_parts=["workflows", publisher, workflow_id],
            hash_segment=hash_segment,
            payload=payload,
        )

    @app.post("/v1/workflows/{publisher}/{workflow_id}/{hash_segment}/upload-session")
    async def create_workflow_upload_session(
        publisher: str,
        workflow_id: str,
        hash_segment: str,
        request: Request,
        current_services: Services = Depends(_get_services),
    ) -> JSONResponse:
        publisher = ensure_owner_domain(publisher, "workflow publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        payload = await request.body()
        _verify_domain_write_request(request, owner_domain=publisher, payload=payload)
        expected_size = _parse_upload_session_request_payload(payload)
        session = _create_upload_session(
            current_services,
            namespace_parts=["workflows", publisher, workflow_id],
            hash_segment=hash_segment,
            expected_size=expected_size,
        )
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=_upload_session_payload(session))

    @app.get("/v1/workflows/{publisher}/{workflow_id}/{reference}")
    def get_workflow_object(
        publisher: str,
        workflow_id: str,
        reference: str,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        publisher = ensure_owner_domain(publisher, "workflow publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        return _read_object_response(
            current_services,
            ["workflows", publisher, workflow_id, _ensure_reference(reference)],
        )

    @app.head("/v1/workflows/{publisher}/{workflow_id}/{reference}")
    def head_workflow_object(
        publisher: str,
        workflow_id: str,
        reference: str,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        publisher = ensure_owner_domain(publisher, "workflow publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        return _head_object_response(
            current_services,
            ["workflows", publisher, workflow_id, _ensure_reference(reference)],
        )

    @app.put("/v1/runtime/{publisher}/{workflow_id}/certificates/{node_id}/{hash_segment}")
    async def put_runtime_node_certificate(
        publisher: str,
        workflow_id: str,
        node_id: str,
        hash_segment: str,
        request: Request,
        x_tdx_quote: Annotated[str | None, Header(alias="X-TDX-Quote")] = None,
        x_tdx_event_log: Annotated[str | None, Header(alias="X-TDX-Event-Log")] = None,
        x_cove_node_id: Annotated[str | None, Header(alias="X-Cove-Node-Id")] = None,
        x_cove_compose_hash: Annotated[str | None, Header(alias="X-Cove-Compose-Hash")] = None,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        publisher = ensure_owner_domain(publisher, "runtime publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        node_id = ensure_identifier(node_id, "node id")
        payload = await request.body()
        certificate = _parse_runtime_certificate_payload(payload)
        _verify_runtime_certificate_attestation(
            current_services,
            certificate=certificate,
            quote=x_tdx_quote,
            event_log=x_tdx_event_log,
            node_id=x_cove_node_id,
            path_workflow_id=workflow_id,
            path_node_id=node_id,
            compose_hash=x_cove_compose_hash,
        )
        return _write_typed_object(
            current_services,
            namespace_parts=["runtime", publisher, workflow_id, "certificates", node_id],
            hash_segment=hash_segment,
            payload=payload,
        )

    @app.get("/v1/runtime/{publisher}/{workflow_id}/certificates/{node_id}/{reference}")
    def get_runtime_node_certificate(
        publisher: str,
        workflow_id: str,
        node_id: str,
        reference: str,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        publisher = ensure_owner_domain(publisher, "runtime publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        node_id = ensure_identifier(node_id, "node id")
        return _read_object_response(
            current_services,
            ["runtime", publisher, workflow_id, "certificates", node_id, _ensure_reference(reference)],
        )

    @app.head("/v1/runtime/{publisher}/{workflow_id}/certificates/{node_id}/{reference}")
    def head_runtime_node_certificate(
        publisher: str,
        workflow_id: str,
        node_id: str,
        reference: str,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        publisher = ensure_owner_domain(publisher, "runtime publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        node_id = ensure_identifier(node_id, "node id")
        return _head_object_response(
            current_services,
            ["runtime", publisher, workflow_id, "certificates", node_id, _ensure_reference(reference)],
        )

    @app.put("/v1/runtime/{publisher}/{workflow_id}/artifacts/{artifact_name}/{hash_segment}")
    async def put_runtime_artifact(
        publisher: str,
        workflow_id: str,
        artifact_name: str,
        hash_segment: str,
        request: Request,
        x_tdx_quote: Annotated[str | None, Header(alias="X-TDX-Quote")] = None,
        x_tdx_event_log: Annotated[str | None, Header(alias="X-TDX-Event-Log")] = None,
        x_cove_workflow_id: Annotated[str | None, Header(alias="X-Cove-Workflow-Id")] = None,
        x_cove_artifact_name: Annotated[str | None, Header(alias="X-Cove-Artifact-Name")] = None,
        x_cove_node_id: Annotated[str | None, Header(alias="X-Cove-Node-Id")] = None,
        x_cove_compose_hash: Annotated[str | None, Header(alias="X-Cove-Compose-Hash")] = None,
        x_cove_attestation_format: Annotated[str | None, Header(alias="X-Cove-Attestation-Format")] = None,
        x_cove_report_data: Annotated[str | None, Header(alias="X-Cove-Report-Data")] = None,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        publisher = ensure_owner_domain(publisher, "runtime publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        artifact_name = ensure_identifier(artifact_name, "artifact name")
        payload = await request.body()
        _verify_runtime_artifact_attestation(
            current_services,
            quote=x_tdx_quote,
            event_log=x_tdx_event_log,
            workflow_id=x_cove_workflow_id,
            artifact_name=x_cove_artifact_name,
            path_workflow_id=workflow_id,
            path_artifact_name=artifact_name,
            node_id=x_cove_node_id,
            compose_hash=x_cove_compose_hash,
            attestation_format=x_cove_attestation_format,
            report_data=x_cove_report_data,
        )
        return _write_typed_object(
            current_services,
            namespace_parts=["runtime", publisher, workflow_id, "artifacts", artifact_name],
            hash_segment=hash_segment,
            payload=payload,
        )

    @app.post("/v1/runtime/{publisher}/{workflow_id}/artifacts/{artifact_name}/{hash_segment}/upload-session")
    async def create_runtime_artifact_upload_session(
        publisher: str,
        workflow_id: str,
        artifact_name: str,
        hash_segment: str,
        request: Request,
        x_tdx_quote: Annotated[str | None, Header(alias="X-TDX-Quote")] = None,
        x_tdx_event_log: Annotated[str | None, Header(alias="X-TDX-Event-Log")] = None,
        x_cove_workflow_id: Annotated[str | None, Header(alias="X-Cove-Workflow-Id")] = None,
        x_cove_artifact_name: Annotated[str | None, Header(alias="X-Cove-Artifact-Name")] = None,
        x_cove_node_id: Annotated[str | None, Header(alias="X-Cove-Node-Id")] = None,
        x_cove_compose_hash: Annotated[str | None, Header(alias="X-Cove-Compose-Hash")] = None,
        x_cove_attestation_format: Annotated[str | None, Header(alias="X-Cove-Attestation-Format")] = None,
        x_cove_report_data: Annotated[str | None, Header(alias="X-Cove-Report-Data")] = None,
        current_services: Services = Depends(_get_services),
    ) -> JSONResponse:
        publisher = ensure_owner_domain(publisher, "runtime publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        artifact_name = ensure_identifier(artifact_name, "artifact name")
        payload = await request.body()
        _verify_runtime_artifact_attestation(
            current_services,
            quote=x_tdx_quote,
            event_log=x_tdx_event_log,
            workflow_id=x_cove_workflow_id,
            artifact_name=x_cove_artifact_name,
            path_workflow_id=workflow_id,
            path_artifact_name=artifact_name,
            node_id=x_cove_node_id,
            compose_hash=x_cove_compose_hash,
            attestation_format=x_cove_attestation_format,
            report_data=x_cove_report_data,
        )
        expected_size = _parse_upload_session_request_payload(payload)
        session = _create_upload_session(
            current_services,
            namespace_parts=["runtime", publisher, workflow_id, "artifacts", artifact_name],
            hash_segment=hash_segment,
            expected_size=expected_size,
        )
        return JSONResponse(status_code=status.HTTP_201_CREATED, content=_upload_session_payload(session))

    @app.get("/v1/runtime/{publisher}/{workflow_id}/artifacts/{artifact_name}/{reference}")
    def get_runtime_artifact(
        publisher: str,
        workflow_id: str,
        artifact_name: str,
        reference: str,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        publisher = ensure_owner_domain(publisher, "runtime publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        artifact_name = ensure_identifier(artifact_name, "artifact name")
        return _read_object_response(
            current_services,
            ["runtime", publisher, workflow_id, "artifacts", artifact_name, _ensure_reference(reference)],
        )

    @app.head("/v1/runtime/{publisher}/{workflow_id}/artifacts/{artifact_name}/{reference}")
    def head_runtime_artifact(
        publisher: str,
        workflow_id: str,
        artifact_name: str,
        reference: str,
        current_services: Services = Depends(_get_services),
    ) -> Response:
        publisher = ensure_owner_domain(publisher, "runtime publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        artifact_name = ensure_identifier(artifact_name, "artifact name")
        return _head_object_response(
            current_services,
            ["runtime", publisher, workflow_id, "artifacts", artifact_name, _ensure_reference(reference)],
        )

    @app.get("/v1/uploads/sessions/{session_id}")
    def get_upload_session(
        session_id: str,
        current_services: Services = Depends(_get_services),
    ) -> dict[str, object]:
        return _read_upload_session(current_services, session_id)

    @app.put("/v1/uploads/sessions/{session_id}")
    async def append_upload_session_chunk(
        session_id: str,
        request: Request,
        x_cove_upload_offset: Annotated[str | None, Header(alias="X-Cove-Upload-Offset")] = None,
        current_services: Services = Depends(_get_services),
    ) -> dict[str, object]:
        if x_cove_upload_offset is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="missing X-Cove-Upload-Offset header",
            )
        payload = await request.body()
        try:
            session = current_services.storage.append_upload_chunk(
                session_id,
                offset=_parse_upload_offset(x_cove_upload_offset),
                payload=payload,
            )
        except UploadSessionNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="upload session not found",
            ) from exc
        except UploadSessionConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except UploadSessionValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        return _upload_session_payload(session)

    @app.post("/v1/uploads/sessions/{session_id}/complete")
    def complete_upload_session(
        session_id: str,
        current_services: Services = Depends(_get_services),
    ) -> JSONResponse:
        try:
            session, result = current_services.storage.complete_upload_session(session_id)
        except UploadSessionNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="upload session not found",
            ) from exc
        except UploadSessionConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(exc),
            ) from exc
        except UploadSessionValidationError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
        return JSONResponse(
            status_code=_write_status_code(result.created),
            content=_upload_session_payload(session),
        )

    @app.delete("/v1/runtime/{publisher}/{workflow_id}")
    def delete_runtime_state(
        publisher: str,
        workflow_id: str,
        request: Request,
        current_services: Services = Depends(_get_services),
    ) -> dict[str, object]:
        publisher = ensure_owner_domain(publisher, "runtime publisher")
        workflow_id = ensure_identifier(workflow_id, "workflow id")
        _verify_domain_write_request(request, owner_domain=publisher, payload=b"")
        deleted = current_services.storage.delete_prefix(["runtime", publisher, workflow_id])
        return {
            "publisher": publisher,
            "workflow_id": workflow_id,
            "deleted": deleted,
        }

    return app


def _get_services(request: Request) -> Services:
    return request.app.state.services


def _verify_domain_write_request(
    request: Request,
    *,
    owner_domain: str,
    payload: bytes,
) -> None:
    try:
        verify_domain_write(
            method=request.method,
            path=request.url.path,
            payload=payload,
            owner_domain=owner_domain,
            headers=request.headers,
        )
    except WriteAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc


def _create_upload_session(
    services: Services,
    *,
    namespace_parts: list[str],
    hash_segment: str,
    expected_size: int | None,
) -> UploadSession:
    try:
        return services.storage.create_upload_session(
            namespace_parts,
            ensure_hash_segment(hash_segment),
            expected_size=expected_size,
        )
    except UploadSessionValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


def _read_upload_session(services: Services, session_id: str) -> dict[str, object]:
    try:
        session = services.storage.get_upload_session(session_id)
    except UploadSessionNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="upload session not found",
        ) from exc
    return _upload_session_payload(session)


def _upload_session_payload(session: UploadSession) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_id": session.session_id,
        "path": "/".join([*session.namespace_parts, session.digest_segment]),
        "digest": session.digest_segment,
        "offset": session.current_size,
        "completed": session.completed,
    }
    if session.expected_size is not None:
        payload["upload_length"] = session.expected_size
    if session.created is not None:
        payload["created"] = session.created
    return payload


def _parse_upload_session_request_payload(payload: bytes) -> int | None:
    if not payload:
        return None
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upload session payload must be valid JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upload session payload must be a JSON object",
        )
    upload_length = parsed.get("upload_length")
    if upload_length is None:
        return None
    if not isinstance(upload_length, int) or upload_length < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="upload_length must be a non-negative integer",
        )
    return upload_length


def _parse_upload_offset(value: str) -> int:
    try:
        offset = int(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Cove-Upload-Offset must be an integer",
        ) from exc
    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Cove-Upload-Offset must be non-negative",
        )
    return offset


def _write_typed_object(
    services: Services,
    *,
    namespace_parts: list[str],
    hash_segment: str,
    payload: bytes,
) -> Response:
    _ensure_payload_hash_segment(hash_segment, payload)
    try:
        result = services.storage.write_object(namespace_parts, hash_segment, payload)
    except PathConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="stored object bytes do not match requested digest",
        ) from exc
    return Response(
        status_code=_write_status_code(result.created),
        media_type="application/octet-stream",
    )


def _read_object_response(services: Services, path_parts: list[str]) -> Response:
    try:
        payload = services.storage.read_object(path_parts)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="object not found",
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return Response(content=payload, media_type="application/octet-stream")


def _head_object_response(services: Services, path_parts: list[str]) -> Response:
    try:
        size = services.storage.object_size(path_parts)
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="object not found",
        ) from exc
    except StorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return Response(status_code=status.HTTP_200_OK, headers={"Content-Length": str(size)})


def _ensure_payload_hash_segment(hash_segment: str, payload: bytes) -> str:
    hash_segment = ensure_hash_segment(hash_segment)
    observed_digest = sha256_literal(payload)
    if observed_digest != hash_segment:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="payload sha256 does not match digest path",
        )
    return hash_segment


def _ensure_reference(reference: str) -> str:
    if reference == "latest":
        return reference
    return ensure_hash_segment(reference)


def _verify_runtime_certificate_attestation(
    services: Services,
    *,
    certificate: dict[str, object],
    quote: str | None,
    event_log: str | None,
    node_id: str | None,
    path_workflow_id: str,
    path_node_id: str,
    compose_hash: str | None,
) -> None:
    if quote is None or node_id is None or compose_hash is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing runtime attestation headers",
        )

    try:
        normalized_node_id = ensure_identifier(node_id, "node id")
        normalized_compose_hash = ensure_hash_segment(compose_hash, "compose hash")
        certificate_body = _required_object(
            certificate.get("certificate_body"),
            "certificate_body",
        )
        attestation_bundle = _required_object(
            certificate.get("attestation_bundle"),
            "attestation_bundle",
        )
        certificate_body_hash = ensure_hash_segment(
            _required_string(certificate.get("certificate_body_hash"), "certificate_body_hash"),
            "certificate_body_hash",
        )
        observed_body_hash = sha256_literal(canonical_json_bytes(certificate_body))
        if observed_body_hash != certificate_body_hash:
            raise ValidationError("certificate_body_hash does not match certificate body")

        body_workflow_id = ensure_identifier(
            _required_string(certificate_body.get("workflow_id"), "certificate_body.workflow_id"),
            "workflow id",
        )
        if body_workflow_id != path_workflow_id:
            raise ValidationError("runtime certificate workflow id does not match path workflow id")
        body_node_id = ensure_identifier(
            _required_string(certificate_body.get("node_id"), "certificate_body.node_id"),
            "node id",
        )
        if body_node_id != path_node_id:
            raise ValidationError("runtime certificate node id does not match path node id")
        if body_node_id != normalized_node_id:
            raise ValidationError("runtime attestation node id does not match certificate body node id")
        body_compose_hash = ensure_hash_segment(
            _required_string(
                certificate_body.get("generated_node_compose_hash"),
                "certificate_body.generated_node_compose_hash",
            ),
            "compose hash",
        )
        if body_compose_hash != normalized_compose_hash:
            raise ValidationError("runtime attestation compose hash does not match certificate body compose hash")

        attested_node_id = ensure_identifier(
            _required_string(attestation_bundle.get("node_id"), "attestation_bundle.node_id"),
            "node id",
        )
        if attested_node_id != body_node_id:
            raise ValidationError("attestation node id does not match certificate body node id")
        attested_compose_hash = ensure_hash_segment(
            _required_string(
                attestation_bundle.get("generated_node_compose_hash"),
                "attestation_bundle.generated_node_compose_hash",
            ),
            "compose hash",
        )
        if attested_compose_hash != body_compose_hash:
            raise ValidationError("attestation compose hash does not match certificate body compose hash")
        quoted_body_hash = ensure_hash_segment(
            _required_string(
                attestation_bundle.get("quoted_certificate_body_hash"),
                "attestation_bundle.quoted_certificate_body_hash",
            ),
            "certificate_body_hash",
        )
        if quoted_body_hash != certificate_body_hash:
            raise ValidationError("quoted certificate body hash does not match certificate_body_hash")
        bundle_quote = _required_string(attestation_bundle.get("quote"), "attestation_bundle.quote")
        if bundle_quote != quote:
            raise ValidationError("runtime attestation quote header does not match certificate attestation quote")
        bundle_event_log = _serialize_event_log(attestation_bundle.get("event_log"))
        if event_log is not None and bundle_event_log != event_log:
            raise ValidationError("runtime attestation event log header does not match certificate event log")
        report_data = _required_string(attestation_bundle.get("report_data"), "attestation_bundle.report_data")
        attestation = RuntimeAttestation(
            format=_required_string(attestation_bundle.get("format"), "attestation_bundle.format"),
            quote=bundle_quote,
            event_log=event_log,
            node_id=normalized_node_id,
            compose_hash=normalized_compose_hash,
            report_data=report_data,
            expected_report_data=build_node_certificate_report_data(
                certificate_body_hash=certificate_body_hash,
                compose_hash=body_compose_hash,
            ),
        )
        services.quote_verifier.verify(attestation)
    except (QuoteVerificationError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc


def _verify_runtime_artifact_attestation(
    services: Services,
    *,
    quote: str | None,
    event_log: str | None,
    workflow_id: str | None,
    artifact_name: str | None,
    path_workflow_id: str,
    path_artifact_name: str,
    node_id: str | None,
    compose_hash: str | None,
    attestation_format: str | None,
    report_data: str | None,
) -> None:
    if (
        quote is None
        or workflow_id is None
        or artifact_name is None
        or node_id is None
        or compose_hash is None
        or attestation_format is None
        or report_data is None
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing runtime attestation headers",
        )

    try:
        normalized_workflow_id = ensure_identifier(workflow_id, "workflow id")
        normalized_artifact_name = ensure_identifier(artifact_name, "artifact name")
        if normalized_workflow_id != path_workflow_id:
            raise ValidationError("runtime artifact workflow id does not match path workflow id")
        if normalized_artifact_name != path_artifact_name:
            raise ValidationError("runtime artifact name does not match path artifact name")
        normalized_node_id = ensure_identifier(node_id, "node id")
        normalized_compose_hash = ensure_hash_segment(compose_hash, "compose hash")
        attestation = RuntimeAttestation(
            format=_required_string(attestation_format, "attestation format"),
            quote=quote,
            event_log=event_log,
            node_id=normalized_node_id,
            compose_hash=normalized_compose_hash,
            report_data=_required_string(report_data, "report_data"),
            expected_report_data=build_runtime_artifact_report_data(
                workflow_id=normalized_workflow_id,
                node_id=normalized_node_id,
                compose_hash=normalized_compose_hash,
                artifact_name=normalized_artifact_name,
            ),
        )
        services.quote_verifier.verify(attestation)
    except (QuoteVerificationError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc


def _parse_runtime_certificate_payload(payload: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(payload.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="runtime certificate payload must be valid JSON",
        ) from exc
    if not isinstance(parsed, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="runtime certificate payload must be a JSON object",
        )
    return parsed


def _write_status_code(created: bool) -> int:
    if created:
        return status.HTTP_201_CREATED
    return status.HTTP_200_OK


def _required_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be an object")
    return value


def _required_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{label} must be a non-empty string")
    return value.strip()


def _serialize_event_log(event_log: object) -> str:
    if isinstance(event_log, str):
        return event_log
    return json.dumps(event_log, sort_keys=True)
