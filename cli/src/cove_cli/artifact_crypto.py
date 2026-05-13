from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


ARTIFACT_KEY_BYTES = 32
ARTIFACT_NONCE_BYTES = 12
ENCRYPTION_ALGORITHM = "aes-256-gcm"


class ArtifactCryptoError(RuntimeError):
    """Raised when local artifact encryption material is invalid."""


def sha256_literal(payload: bytes) -> str:
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def key_file_path(keys_dir: Path, artifact_id: str) -> Path:
    return keys_dir / artifact_id


def ensure_artifact_key(*, keys_dir: Path, artifact_id: str) -> tuple[Path, bytes]:
    keys_dir.mkdir(parents=True, exist_ok=True)
    path = key_file_path(keys_dir, artifact_id)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists():
        if not path.is_file():
            raise ArtifactCryptoError(f"artifact key path is not a file: {path}")
        key_bytes = path.read_bytes()
        if len(key_bytes) != ARTIFACT_KEY_BYTES:
            raise ArtifactCryptoError(
                f"artifact key at {path} must be {ARTIFACT_KEY_BYTES} bytes"
            )
        return path, key_bytes

    key_bytes = os.urandom(ARTIFACT_KEY_BYTES)
    file_descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(key_bytes)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        if path.exists():
            path.unlink()
        raise
    return path, key_bytes


def load_artifact_key(*, keys_dir: Path, artifact_id: str) -> bytes:
    path = key_file_path(keys_dir, artifact_id)
    if not path.is_file():
        raise ArtifactCryptoError(f"artifact key file not found: {path}")
    key_bytes = path.read_bytes()
    if len(key_bytes) != ARTIFACT_KEY_BYTES:
        raise ArtifactCryptoError(
            f"artifact key at {path} must be {ARTIFACT_KEY_BYTES} bytes"
        )
    return key_bytes


def encrypt_artifact_bytes(*, plaintext: bytes, key_bytes: bytes) -> bytes:
    if len(key_bytes) != ARTIFACT_KEY_BYTES:
        raise ArtifactCryptoError(
            f"artifact key must be {ARTIFACT_KEY_BYTES} bytes"
        )
    nonce = os.urandom(ARTIFACT_NONCE_BYTES)
    ciphertext_and_tag = AESGCM(key_bytes).encrypt(nonce, plaintext, None)
    return nonce + ciphertext_and_tag
