import pytest

from cove_server.validation import (
    ValidationError,
    ensure_hash_segment,
    ensure_owner_domain,
    ensure_sha256_digest,
    normalize_relative_path,
)


def test_ensure_hash_segment_roundtrips() -> None:
    assert ensure_hash_segment(f"sha256:{'a' * 64}", "compose hash") == f"sha256:{'a' * 64}"


def test_ensure_sha256_digest_roundtrips() -> None:
    assert ensure_sha256_digest("a" * 64, "object digest") == "a" * 64


@pytest.mark.parametrize("value", ["sha256:" + ("a" * 64), "A" * 64, "a" * 63, "g" * 64])
def test_ensure_sha256_digest_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValidationError):
        ensure_sha256_digest(value)


@pytest.mark.parametrize("value", ["alice.example.test", "a1-b2.example.test"])
def test_ensure_owner_domain_accepts_dns_hostnames(value: str) -> None:
    assert ensure_owner_domain(value) == value


@pytest.mark.parametrize(
    "value",
    ["alice", "Alice.example.test", "alice_bob.example.test", "-alice.example.test", "alice-.example.test"],
)
def test_ensure_owner_domain_rejects_invalid_hostnames(value: str) -> None:
    with pytest.raises(ValidationError):
        ensure_owner_domain(value)


@pytest.mark.parametrize("value", ["../secret.txt", "/abs/path", "nodes/../bundle.json"])
def test_normalize_relative_path_rejects_traversal(value: str) -> None:
    with pytest.raises(ValidationError):
        normalize_relative_path(value)
