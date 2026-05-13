from __future__ import annotations

import json
from pathlib import Path
from urllib import request as urllib_request

import pytest
import yaml

from cove_cli.cli import run
from cove_cli.config import (
    ConfigError,
    config_path_for_home,
    ensure_local_config,
    update_local_config,
)
from cove_cli.owner_identity import OwnerIdentityError
from cove_cli.provisioning_identity import (
    build_owner_identity_document,
    ensure_owner_signing_key_material,
    fetch_owner_identity_document,
    fetch_owner_identity_for_write,
    owner_domain_from_url,
)

from .support import MockCovehubServer, build_test_owner_identity


ALICE_OWNER_URL = "https://cove-demo-hello-world-alice-provisioning.covehub.io"
ALICE_DOMAIN = "cove-demo-hello-world-alice-provisioning.covehub.io"


def test_owner_domain_is_derived_from_https_owner_url() -> None:
    assert owner_domain_from_url(f"{ALICE_OWNER_URL}:9443") == ALICE_DOMAIN


def test_fetch_owner_identity_document_sends_cli_user_agent(monkeypatch) -> None:
    identity_document = build_test_owner_identity("alice", ALICE_OWNER_URL)
    observed: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self) -> bytes:
            return json.dumps(identity_document).encode("utf-8")

    def fake_urlopen(request: urllib_request.Request, timeout: float):
        observed["url"] = request.full_url
        observed["user_agent"] = request.get_header("User-agent")
        return FakeResponse()

    monkeypatch.setattr(urllib_request, "urlopen", fake_urlopen)

    resolved = fetch_owner_identity_document(owner_url=ALICE_OWNER_URL)

    assert resolved["owner_domain"] == ALICE_DOMAIN
    assert observed["url"] == f"{ALICE_OWNER_URL}/identity"
    assert observed["user_agent"] == "cove-cli/0.0.1"


def test_fetch_owner_identity_for_write_accepts_served_local_key(tmp_path, monkeypatch) -> None:
    private_key_path = tmp_path / "owner-signing-private.pem"
    public_key_path = tmp_path / "owner-signing-public.pem"
    ensure_owner_signing_key_material(
        private_key_path=private_key_path,
        public_key_path=public_key_path,
    )
    identity_document = build_owner_identity_document(
        owner_url=ALICE_OWNER_URL,
        owner_private_key_path=private_key_path,
        owner_public_key_path=public_key_path,
    )
    _stub_owner_identity_response(monkeypatch, identity_document)

    resolved = fetch_owner_identity_for_write(
        owner_url=ALICE_OWNER_URL,
        owner_private_key_path=private_key_path,
    )

    assert resolved == identity_document


def test_fetch_owner_identity_for_write_rejects_served_key_mismatch(tmp_path, monkeypatch) -> None:
    private_key_path = tmp_path / "owner-signing-private.pem"
    public_key_path = tmp_path / "owner-signing-public.pem"
    ensure_owner_signing_key_material(
        private_key_path=private_key_path,
        public_key_path=public_key_path,
    )
    _stub_owner_identity_response(
        monkeypatch,
        build_test_owner_identity(ALICE_DOMAIN, ALICE_OWNER_URL),
    )

    with pytest.raises(OwnerIdentityError, match="does not match the local owner signing key"):
        fetch_owner_identity_for_write(
            owner_url=ALICE_OWNER_URL,
            owner_private_key_path=private_key_path,
        )


def test_update_local_config_writes_owner_url_without_user_credentials(tmp_path) -> None:
    cove_home = tmp_path / "home"

    updated = update_local_config(
        cove_home=cove_home,
        covehub_server_url="https://api.covehub.io",
        owner_server_url=ALICE_OWNER_URL,
        phala_docker_username="alice-docker",
        phala_docker_access_token="docker-token",
    )

    assert updated.owner_server_url == ALICE_OWNER_URL
    payload = yaml.safe_load(config_path_for_home(cove_home).read_text(encoding="utf-8"))
    assert payload == {
        "covehub_server_url": "https://api.covehub.io",
        "phala_docker_username": "alice-docker",
        "phala_docker_access_token": "docker-token",
        "owner_server_url": ALICE_OWNER_URL,
    }


def test_local_config_rejects_legacy_user_credentials(tmp_path) -> None:
    cove_home = tmp_path / "home"
    config_path = config_path_for_home(cove_home)
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "covehub_server_url": "https://api.covehub.io",
                "username": "alice",
                "access_token": "token",
                "owner_server_url": ALICE_OWNER_URL,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="no longer supported"):
        ensure_local_config(cove_home)


def test_init_creates_owner_domain_config_and_keys(tmp_path, monkeypatch, capsys) -> None:
    home = tmp_path / "home"
    answers = iter(
        [
            str(home),
            "https://api.covehub.io",
            ALICE_OWNER_URL,
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda prompt: "")

    assert run(["init"]) == 0

    output = capsys.readouterr().out
    assert f"Initialized owner domain '{ALICE_DOMAIN}'" in output
    config = ensure_local_config(home)
    assert config.owner_server_url == ALICE_OWNER_URL
    assert "username" not in yaml.safe_load(config.path.read_text(encoding="utf-8"))
    assert (home / "owner-signing-private.pem").is_file()
    assert (home / "owner-signing-public.pem").is_file()


def test_provision_upload_uses_domain_path_and_signed_headers(tmp_path, monkeypatch, capsys) -> None:
    cove_home = tmp_path / "home"
    update_local_config(
        cove_home=cove_home,
        covehub_server_url="http://127.0.0.1:1",
        owner_server_url=ALICE_OWNER_URL,
    )
    plaintext = tmp_path / "secret.txt"
    plaintext.write_text("hello", encoding="utf-8")
    observed: dict[str, object] = {}

    def fake_fetch_owner_identity_for_write(
        *,
        owner_url: str,
        owner_private_key_path: Path,
        expected_owner_domain: str | None = None,
        timeout: float = 5.0,
    ):
        observed["owner_url"] = owner_url
        observed["expected_owner_domain"] = expected_owner_domain
        return build_owner_identity_document(
            owner_url=owner_url,
            owner_private_key_path=owner_private_key_path,
            owner_public_key_path=owner_private_key_path.parent / "owner-signing-public.pem",
        )

    monkeypatch.setattr(
        "cove_cli.provision.fetch_owner_identity_for_write",
        fake_fetch_owner_identity_for_write,
    )

    with MockCovehubServer() as server:
        update_local_config(cove_home=cove_home, covehub_server_url=server.url)

        assert run(["--cove-home", str(cove_home), "provision", "secret", str(plaintext)]) == 0

    output = capsys.readouterr().out
    assert observed == {
        "owner_url": ALICE_OWNER_URL,
        "expected_owner_domain": ALICE_DOMAIN,
    }
    assert f"v1/artifacts/{ALICE_DOMAIN}/secret/" in output


def _stub_owner_identity_response(monkeypatch, identity_document: dict[str, object]) -> None:
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

        def read(self) -> bytes:
            return json.dumps(identity_document).encode("utf-8")

    monkeypatch.setattr(urllib_request, "urlopen", lambda request, timeout: FakeResponse())
