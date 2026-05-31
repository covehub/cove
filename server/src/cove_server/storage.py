from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


class StorageError(RuntimeError):
    """Base storage error."""


class PathConflictError(StorageError):
    """Raised when a bundle file path is reused with different bytes."""


class UnsafePathError(StorageError):
    """Raised when a resolved filesystem path escapes the storage root."""


class UploadSessionNotFoundError(StorageError):
    """Raised when an upload session does not exist."""


class UploadSessionConflictError(StorageError):
    """Raised when an upload session is in an incompatible state."""


class UploadSessionValidationError(StorageError):
    """Raised when an upload session fails integrity validation."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    created: bool


@dataclass(frozen=True, slots=True)
class UploadSession:
    session_id: str
    namespace_parts: tuple[str, ...]
    digest_segment: str
    expected_size: int | None
    current_size: int
    completed: bool
    created: bool | None = None


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def object_size(self, path_parts: Sequence[str]) -> int:
        path = self._typed_path(path_parts)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path.stat().st_size

    def read_object(self, path_parts: Sequence[str]) -> bytes:
        path = self._typed_path(path_parts)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path.read_bytes()

    def write_object(
        self,
        namespace_parts: Sequence[str],
        digest_segment: str,
        payload: bytes,
        *,
        update_latest: bool = True,
    ) -> WriteResult:
        path = self._typed_path([*namespace_parts, digest_segment])
        created = not path.exists()
        if not created:
            existing_payload = path.read_bytes()
            if existing_payload != payload:
                raise PathConflictError(str(path))
        else:
            self._atomic_write(path, payload)

        if update_latest:
            self._atomic_write(self._typed_path([*namespace_parts, "latest"]), payload)
        return WriteResult(created=created)

    def write_object_from_path(
        self,
        namespace_parts: Sequence[str],
        digest_segment: str,
        source_path: Path,
        *,
        update_latest: bool = True,
    ) -> WriteResult:
        path = self._typed_path([*namespace_parts, digest_segment])
        created = not path.exists()
        if not created:
            if not self._files_equal(path, source_path):
                raise PathConflictError(str(path))
        else:
            self._atomic_copy(path, source_path)

        if update_latest:
            self._atomic_copy(self._typed_path([*namespace_parts, "latest"]), source_path)
        return WriteResult(created=created)

    def create_upload_session(
        self,
        namespace_parts: Sequence[str],
        digest_segment: str,
        *,
        expected_size: int | None = None,
    ) -> UploadSession:
        if expected_size is not None and expected_size < 0:
            raise UploadSessionValidationError("upload_length must be non-negative")

        while True:
            session_id = secrets.token_hex(16)
            session_dir = self._upload_session_dir(session_id)
            try:
                session_dir.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                continue
            break

        session = UploadSession(
            session_id=session_id,
            namespace_parts=tuple(namespace_parts),
            digest_segment=digest_segment,
            expected_size=expected_size,
            current_size=0,
            completed=False,
        )
        self._write_upload_session(session)
        self._session_data_path(session_id).touch()
        return session

    def get_upload_session(self, session_id: str) -> UploadSession:
        return self._read_upload_session(session_id)

    def append_upload_chunk(
        self,
        session_id: str,
        *,
        offset: int,
        payload: bytes,
    ) -> UploadSession:
        if offset < 0:
            raise UploadSessionValidationError("upload offset must be non-negative")

        session = self._read_upload_session(session_id)
        if session.completed:
            raise UploadSessionConflictError("upload session is already completed")

        data_path = self._session_data_path(session_id)
        current_size = data_path.stat().st_size if data_path.exists() else 0
        if offset != current_size:
            raise UploadSessionConflictError(
                f"upload offset {offset} does not match current size {current_size}"
            )

        next_size = current_size + len(payload)
        if session.expected_size is not None and next_size > session.expected_size:
            raise UploadSessionConflictError(
                f"upload would exceed declared upload_length {session.expected_size}"
            )

        with data_path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        updated = UploadSession(
            session_id=session.session_id,
            namespace_parts=session.namespace_parts,
            digest_segment=session.digest_segment,
            expected_size=session.expected_size,
            current_size=next_size,
            completed=False,
        )
        self._write_upload_session(updated)
        return updated

    def complete_upload_session(self, session_id: str) -> tuple[UploadSession, WriteResult]:
        session = self._read_upload_session(session_id)
        if session.completed:
            raise UploadSessionConflictError("upload session is already completed")

        data_path = self._session_data_path(session_id)
        if not data_path.is_file():
            raise UploadSessionNotFoundError(session_id)
        current_size = data_path.stat().st_size
        if session.expected_size is not None and current_size != session.expected_size:
            raise UploadSessionConflictError(
                f"upload size {current_size} does not match declared upload_length {session.expected_size}"
            )

        observed_digest = self._sha256_file(data_path)
        if observed_digest != session.digest_segment:
            raise UploadSessionValidationError(
                "uploaded bytes sha256 does not match digest path"
            )

        result = self.write_object_from_path(
            session.namespace_parts,
            session.digest_segment,
            data_path,
        )
        completed = UploadSession(
            session_id=session.session_id,
            namespace_parts=session.namespace_parts,
            digest_segment=session.digest_segment,
            expected_size=session.expected_size,
            current_size=current_size,
            completed=True,
            created=result.created,
        )
        self._write_upload_session(completed)
        data_path.unlink(missing_ok=True)
        return completed, result

    def delete_prefix(self, path_parts: Sequence[str]) -> bool:
        path = self._typed_path(path_parts)
        if not path.exists():
            return False
        if path.is_file():
            path.unlink()
            return True
        for child in sorted(path.rglob("*"), reverse=True):
            if child.is_file() or child.is_symlink():
                child.unlink()
            elif child.is_dir():
                child.rmdir()
        path.rmdir()
        return True

    def is_ready(self) -> bool:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root.is_dir() and os.access(self.root, os.R_OK | os.W_OK)

    def _atomic_write(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.tmp.",
        )
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _atomic_copy(self, path: Path, source_path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temp_path = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.tmp.",
        )
        try:
            with source_path.open("rb") as source_handle, os.fdopen(file_descriptor, "wb") as target_handle:
                shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                target_handle.flush()
                os.fsync(target_handle.fileno())
            os.replace(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.unlink(temp_path)

    def _files_equal(self, left: Path, right: Path) -> bool:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as left_handle, right.open("rb") as right_handle:
            while True:
                left_chunk = left_handle.read(1024 * 1024)
                right_chunk = right_handle.read(1024 * 1024)
                if left_chunk != right_chunk:
                    return False
                if not left_chunk:
                    return True

    def _sha256_file(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    def _resolve(self, path_parts: Sequence[str]) -> Path:
        candidate = self.root.joinpath(*path_parts)
        resolved = candidate.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise UnsafePathError(str(candidate))
        return resolved

    def _typed_path(self, path_parts: Sequence[str]) -> Path:
        return self._resolve(path_parts)

    def _upload_session_dir(self, session_id: str) -> Path:
        return self._resolve([".upload_sessions", session_id])

    def _session_metadata_path(self, session_id: str) -> Path:
        return self._upload_session_dir(session_id) / "session.json"

    def _session_data_path(self, session_id: str) -> Path:
        return self._upload_session_dir(session_id) / "payload.bin"

    def _read_upload_session(self, session_id: str) -> UploadSession:
        metadata_path = self._session_metadata_path(session_id)
        if not metadata_path.is_file():
            raise UploadSessionNotFoundError(session_id)
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise StorageError(f"invalid upload session metadata at {metadata_path}") from exc
        if not isinstance(payload, dict):
            raise StorageError(f"upload session metadata at {metadata_path} must be an object")

        namespace_parts = payload.get("namespace_parts")
        if not isinstance(namespace_parts, list) or not all(
            isinstance(part, str) and part for part in namespace_parts
        ):
            raise StorageError(f"upload session metadata at {metadata_path} is malformed")

        current_size = payload.get("current_size")
        if not isinstance(current_size, int) or current_size < 0:
            raise StorageError(f"upload session metadata at {metadata_path} is malformed")

        expected_size = payload.get("expected_size")
        if expected_size is not None and (not isinstance(expected_size, int) or expected_size < 0):
            raise StorageError(f"upload session metadata at {metadata_path} is malformed")

        completed = payload.get("completed")
        if not isinstance(completed, bool):
            raise StorageError(f"upload session metadata at {metadata_path} is malformed")

        created = payload.get("created")
        if created is not None and not isinstance(created, bool):
            raise StorageError(f"upload session metadata at {metadata_path} is malformed")

        return UploadSession(
            session_id=session_id,
            namespace_parts=tuple(namespace_parts),
            digest_segment=self._required_non_empty_string(payload.get("digest_segment")),
            expected_size=expected_size,
            current_size=current_size,
            completed=completed,
            created=created,
        )

    def _write_upload_session(self, session: UploadSession) -> None:
        payload = {
            "namespace_parts": list(session.namespace_parts),
            "digest_segment": session.digest_segment,
            "expected_size": session.expected_size,
            "current_size": session.current_size,
            "completed": session.completed,
            "created": session.created,
        }
        self._atomic_write(
            self._session_metadata_path(session.session_id),
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )

    def _required_non_empty_string(self, value: object) -> str:
        if not isinstance(value, str) or not value:
            raise StorageError("expected non-empty string in upload session metadata")
        return value
