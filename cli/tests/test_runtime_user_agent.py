from __future__ import annotations

import json
from urllib import request as urllib_request

from cove_cli import common as cli_common
from cove_cli.covehub import COVEHUB_USER_AGENT

from .support import load_container_main_module


COVE_RUNTIME_USER_AGENT = "cove-runtime/0.0.1"


def test_cli_json_post_requests_send_cli_user_agent(monkeypatch) -> None:
    observed_headers: dict[str, str] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return json.dumps({"ok": True}).encode("utf-8")

    def fake_urlopen(request: urllib_request.Request, *, timeout: float) -> FakeResponse:
        observed_headers.update(dict(request.header_items()))
        assert timeout == 12.5
        return FakeResponse()

    monkeypatch.setattr(cli_common.urllib_request, "urlopen", fake_urlopen)

    assert cli_common.http_post_json(
        url="https://cloud-api.phala.network/api/v1/attestations/verify",
        payload={"hex": "00"},
        timeout=12.5,
    ) == {"ok": True}

    assert observed_headers["User-agent"] == COVEHUB_USER_AGENT
    assert not observed_headers["User-agent"].startswith("Python-urllib/")


def test_artifact_provisioner_owner_identity_requests_send_runtime_user_agent(monkeypatch) -> None:
    artifact_provisioner = load_container_main_module("artifact_provisioner")
    observed_headers: dict[str, str] = {}

    class FakeResponse:
        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b"{}"

    def fake_urlopen(request: urllib_request.Request, *, timeout: float) -> FakeResponse:
        observed_headers.update(dict(request.header_items()))
        assert timeout == 12.5
        return FakeResponse()

    monkeypatch.setattr(artifact_provisioner.urllib_request, "urlopen", fake_urlopen)

    artifact_provisioner._http_bytes_with_owner_identity(
        request=urllib_request.Request(
            "https://alice.cove-demo-parties.covehub.io/v1/artifacts/key-release",
            method="POST",
        ),
        owner_identity={},
        timeout=12.5,
    )

    assert observed_headers["User-agent"] == COVE_RUNTIME_USER_AGENT
    assert not observed_headers["User-agent"].startswith("Python-urllib/")
