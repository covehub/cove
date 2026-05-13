from __future__ import annotations

import os
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


@dataclass(frozen=True, slots=True)
class WriteResult:
    created: bool


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

    def _resolve(self, path_parts: Sequence[str]) -> Path:
        candidate = self.root.joinpath(*path_parts)
        resolved = candidate.resolve(strict=False)
        if resolved != self.root and self.root not in resolved.parents:
            raise UnsafePathError(str(candidate))
        return resolved

    def _typed_path(self, path_parts: Sequence[str]) -> Path:
        return self._resolve(path_parts)
