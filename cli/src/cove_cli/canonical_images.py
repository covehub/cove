from __future__ import annotations

from importlib import resources
import json
import re


_DIGEST_PINNED_IMAGE_RE = re.compile(r"^(?P<name>.+)@(?P<digest>sha256:[0-9a-f]{64})$")

ARTIFACT_PROVISIONER_IMAGE_NAME = "cove-artifact-provisioner"


class CanonicalImageError(RuntimeError):
    """Raised when the checked-in canonical container policy is invalid."""


def canonical_image_digest_pattern() -> re.Pattern[str]:
    return _DIGEST_PINNED_IMAGE_RE


def parse_digest_from_image_reference(image_reference: str) -> str | None:
    match = _DIGEST_PINNED_IMAGE_RE.fullmatch(image_reference)
    if match is None:
        return None
    return match.group("digest")


def load_canonical_container_refs() -> dict[str, str]:
    refs: dict[str, str] = {}
    canonical_source = "packaged canonical container digest policy"
    canonical_text = _packaged_canonical_text()
    try:
        payload = json.loads(canonical_text)
    except json.JSONDecodeError as exc:
        raise CanonicalImageError(
            f"canonical container digest file is not valid JSON: {canonical_source}"
        ) from exc
    containers = payload.get("containers") if isinstance(payload, dict) else None
    if not isinstance(containers, list):
        raise CanonicalImageError(
            f"canonical container digest file must contain a 'containers' list: {canonical_source}"
        )
    for entry in containers:
        if not isinstance(entry, dict):
            raise CanonicalImageError("canonical container entries must be objects")
        image_name = entry.get("image_name")
        canonical_ref = entry.get("canonical_ref")
        if not isinstance(image_name, str) or not image_name:
            raise CanonicalImageError("canonical container entries must define image_name")
        if not isinstance(canonical_ref, str) or not canonical_ref:
            raise CanonicalImageError(
                f"canonical container entry for {image_name!r} must define canonical_ref"
            )
        if parse_digest_from_image_reference(canonical_ref) is None:
            raise CanonicalImageError(
                f"canonical container entry for {image_name!r} has an invalid canonical_ref"
            )
        refs[image_name] = canonical_ref
    return refs


def load_canonical_container_digests() -> dict[str, str]:
    digests: dict[str, str] = {}
    for image_name, canonical_ref in load_canonical_container_refs().items():
        digest = parse_digest_from_image_reference(canonical_ref)
        if digest is None:  # pragma: no cover - defensive
            raise CanonicalImageError(
                f"canonical container entry for {image_name!r} has an invalid canonical_ref"
            )
        digests[image_name] = digest
    return digests


def canonical_container_ref(image_name: str) -> str | None:
    return load_canonical_container_refs().get(image_name)


def canonical_container_digest(image_name: str) -> str | None:
    return load_canonical_container_digests().get(image_name)


def is_canonical_artifact_provisioner_digest(digest: str) -> bool:
    return canonical_container_digest(ARTIFACT_PROVISIONER_IMAGE_NAME) == digest


def _packaged_canonical_text() -> str:
    try:
        return (
            resources.files("cove_cli")
            .joinpath("canonical_container_digests.json")
            .read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise CanonicalImageError(
            "canonical container digest file not found in installed CLI package"
        ) from exc
