from __future__ import annotations

import re
from pathlib import PurePosixPath


SHA256_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
HASH_SEGMENT_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


class ValidationError(ValueError):
    """Raised when a public identifier or path is invalid."""


def ensure_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValidationError(f"invalid {label}")
    return value


def ensure_owner_domain(value: str, label: str = "owner domain") -> str:
    stripped = value.strip()
    normalized = stripped.lower()
    labels = normalized.split(".")
    if (
        stripped != normalized
        or len(labels) < 2
        or any(DNS_LABEL_RE.fullmatch(part) is None for part in labels)
    ):
        raise ValidationError(f"invalid {label}")
    return normalized


def ensure_hash_segment(value: str, label: str = "hash") -> str:
    if not HASH_SEGMENT_RE.fullmatch(value):
        raise ValidationError(f"invalid {label}")
    return value


def ensure_sha256_digest(value: str, label: str = "sha256 digest") -> str:
    if not SHA256_DIGEST_RE.fullmatch(value):
        raise ValidationError(f"invalid {label}")
    return value


def normalize_relative_path(value: str) -> list[str]:
    pure_path = PurePosixPath(value)
    if pure_path.is_absolute():
        raise ValidationError("relative path must not be absolute")

    parts: list[str] = []
    for part in pure_path.parts:
        if part in {"", ".", ".."}:
            raise ValidationError("relative path traversal is not allowed")
        if "\\" in part or "\x00" in part:
            raise ValidationError("relative path contains unsafe characters")
        parts.append(part)

    if not parts:
        raise ValidationError("relative path must not be empty")

    return parts
