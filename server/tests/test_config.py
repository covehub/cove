from __future__ import annotations

import pytest

from cove_server.config import Settings


def test_settings_from_env_defaults_to_phala_dstack(monkeypatch) -> None:
    monkeypatch.delenv("COVE_SERVER_QUOTE_VERIFIER", raising=False)
    monkeypatch.delenv("COVE_SERVER_HOST", raising=False)
    monkeypatch.delenv("COVE_SERVER_PORT", raising=False)

    settings = Settings.from_env()

    assert settings.quote_verifier_mode == "phala_dstack"
    assert settings.host == "127.0.0.1"
    assert settings.port == 8000


def test_settings_from_env_reads_host_and_port(monkeypatch) -> None:
    monkeypatch.setenv("COVE_SERVER_HOST", "0.0.0.0")
    monkeypatch.setenv("COVE_SERVER_PORT", "3518")

    settings = Settings.from_env()

    assert settings.host == "0.0.0.0"
    assert settings.port == 3518


def test_settings_from_env_rejects_mock_quote_verifier(monkeypatch) -> None:
    monkeypatch.setenv("COVE_SERVER_QUOTE_VERIFIER", "mock")

    with pytest.raises(ValueError, match="phala_dstack"):
        Settings.from_env()
