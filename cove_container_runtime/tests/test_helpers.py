from __future__ import annotations

import hashlib
import json
import sys
import types

import pytest

from cove_container_runtime import common as common_module
from cove_container_runtime.attestation import (
    AttestationSettings,
    build_node_certificate_report_data,
    collect_attestation_bundle,
    verify_attestation_bundle,
)
from cove_container_runtime.certificates import generate_ed25519_keypair_material
from cove_container_runtime.common import (
    COVE_RUNTIME_USER_AGENT,
    RuntimeErrorBase,
    decode_key_b64,
    decrypt_ciphertext_file,
    encrypt_plaintext_bytes,
    http_get_bytes,
    http_get_to_file,
    http_put_bytes_resumable,
    load_inline_sidecar_context,
    sha256_file_literal,
)
from cove_container_runtime.jsonlogic import JsonLogicError, evaluate_jsonlogic
from cove_container_runtime.test_support import (
    MOCK_ATTESTATION_FORMAT,
    build_mock_certificate,
    verify_mock_certificate,
)


_COMPOSE_HASH = "sha256:" + ("b" * 64)
_RTMR_BYTES = 48
_DSTACK_EVENT_TYPE = 134217729


def _compose_event_log(compose_hash: str = _COMPOSE_HASH) -> tuple[list[dict[str, object]], str]:
    payload = bytes.fromhex(compose_hash.removeprefix("sha256:"))
    event_digest = _event_digest("compose-hash", payload)
    rtmr3 = _rtmr3_for_event_digests([event_digest])
    return [
        {
            "imr": 3,
            "event_type": _DSTACK_EVENT_TYPE,
            "event": "compose-hash",
            "event_payload": payload.hex(),
            "digest": event_digest.hex(),
        }
    ], rtmr3.hex()


def _event_digest(event_name: str, payload: bytes) -> bytes:
    hasher = hashlib.sha384()
    hasher.update(_DSTACK_EVENT_TYPE.to_bytes(4, "little"))
    hasher.update(b":")
    hasher.update(event_name.encode("utf-8"))
    hasher.update(b":")
    hasher.update(payload)
    return hasher.digest()


def _rtmr3_for_event_digests(event_digests: list[bytes]) -> bytes:
    rtmr = b"\0" * _RTMR_BYTES
    for event_digest in event_digests:
        rtmr = hashlib.sha384(rtmr + event_digest).digest()
    return rtmr


def _verified_quote_response(report_data_hex: str, compose_hash: str = _COMPOSE_HASH) -> dict[str, object]:
    _event_log, rtmr3 = _compose_event_log(compose_hash)
    report_data = bytes.fromhex(report_data_hex)
    if len(report_data) < 64:
        report_data_hex = report_data.ljust(64, b"\0").hex()
    return {
        "success": True,
        "quote": {
            "verified": True,
            "header": {"tee_type": "tdx"},
            "body": {
                "reportdata": report_data_hex,
                "mrtd": "00" * _RTMR_BYTES,
                "rtmr0": "00" * _RTMR_BYTES,
                "rtmr1": "00" * _RTMR_BYTES,
                "rtmr2": "00" * _RTMR_BYTES,
                "rtmr3": rtmr3,
            },
        },
    }


def test_build_mock_certificate_binds_quote_to_certificate_body_hash() -> None:
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name="node_one",
        generated_node_compose_hash="sha256:1234",
        inputs={"alice_secret_word": {"plaintext_hash": "sha256:abc"}},
        ephemeral_keypairs={"session_key": {"public_key_hash": "sha256:def"}},
        results={"worker": {"pass": True}},
    )

    assert certificate["attestation_bundle"]["format"] == MOCK_ATTESTATION_FORMAT
    assert (
        certificate["attestation_bundle"]["quoted_certificate_body_hash"]
        == certificate["certificate_body_hash"]
    )
    assert verify_mock_certificate(certificate)["certificate_body"]["node_id"] == "node_one"


def test_mock_certificate_includes_runtime_metrics_in_body() -> None:
    metrics = {
        "worker": {
            "schema_version": "cove_runtime_metrics_v1",
            "service_name": "worker",
            "role": "workload",
            "wall_seconds": 1.25,
        }
    }
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name="node_one",
        generated_node_compose_hash="sha256:1234",
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
        runtime_metrics=metrics,
    )

    verified = verify_mock_certificate(certificate, expected_node_name="node_one")

    assert verified["certificate_body"]["runtime_metrics"] == metrics


def test_verify_mock_certificate_rejects_mismatched_quote() -> None:
    certificate = build_mock_certificate(
        workflow_id="hello_world",
        node_name="node_one",
        generated_node_compose_hash="sha256:1234",
        inputs={"alice_secret_word": {"plaintext_hash": "sha256:abc"}},
        ephemeral_keypairs={"session_key": {"public_key_hash": "sha256:def"}},
        results={"worker": {"pass": True}},
    )
    certificate["attestation_bundle"]["quote"] = "mock-tdx-quote:wrong"

    try:
        verify_mock_certificate(certificate, expected_node_name="node_one")
    except RuntimeErrorBase:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected mock certificate verification failure")


def test_verify_mock_certificate_accepts_dependency_closure() -> None:
    upstream = build_mock_certificate(
        workflow_id="hello_world",
        node_name="upstream",
        generated_node_compose_hash="sha256:" + ("1" * 64),
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
    )
    downstream = build_mock_certificate(
        workflow_id="hello_world",
        node_name="downstream",
        generated_node_compose_hash="sha256:" + ("2" * 64),
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
        dependencies={"upstream": upstream},
    )

    verified = verify_mock_certificate(
        downstream,
        expected_workflow_id="hello_world",
        expected_node_name="downstream",
        expected_generated_node_compose_hash="sha256:" + ("2" * 64),
        expected_dependency_compose_hashes={
            "upstream": "sha256:" + ("1" * 64),
            "downstream": "sha256:" + ("2" * 64),
        },
        expected_dependency_edges={
            "upstream": [],
            "downstream": ["upstream"],
        },
    )

    assert verified["certificate_body"]["dependencies"]["upstream"] == upstream


def test_verify_mock_certificate_rejects_missing_dependency_closure() -> None:
    downstream = build_mock_certificate(
        workflow_id="hello_world",
        node_name="downstream",
        generated_node_compose_hash="sha256:" + ("2" * 64),
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
    )

    with pytest.raises(RuntimeErrorBase, match="missing dependency certificates"):
        verify_mock_certificate(
            downstream,
            expected_dependency_compose_hashes={
                "upstream": "sha256:" + ("1" * 64),
                "downstream": "sha256:" + ("2" * 64),
            },
            expected_dependency_edges={"downstream": ["upstream"]},
        )


def test_verify_mock_certificate_rejects_unexpected_dependency_closure() -> None:
    upstream = build_mock_certificate(
        workflow_id="hello_world",
        node_name="upstream",
        generated_node_compose_hash="sha256:" + ("1" * 64),
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
    )
    downstream = build_mock_certificate(
        workflow_id="hello_world",
        node_name="downstream",
        generated_node_compose_hash="sha256:" + ("2" * 64),
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
        dependencies={"upstream": upstream},
    )

    with pytest.raises(RuntimeErrorBase, match="unexpected dependency certificates"):
        verify_mock_certificate(
            downstream,
            expected_dependency_compose_hashes={
                "upstream": "sha256:" + ("1" * 64),
                "downstream": "sha256:" + ("2" * 64),
            },
            expected_dependency_edges={"downstream": []},
        )


def test_verify_mock_certificate_rejects_stale_dependency_compose() -> None:
    upstream = build_mock_certificate(
        workflow_id="hello_world",
        node_name="upstream",
        generated_node_compose_hash="sha256:" + ("9" * 64),
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
    )
    downstream = build_mock_certificate(
        workflow_id="hello_world",
        node_name="downstream",
        generated_node_compose_hash="sha256:" + ("2" * 64),
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
        dependencies={"upstream": upstream},
    )

    with pytest.raises(RuntimeErrorBase, match="stale or mismatched compose hash"):
        verify_mock_certificate(
            downstream,
            expected_dependency_compose_hashes={
                "upstream": "sha256:" + ("1" * 64),
                "downstream": "sha256:" + ("2" * 64),
            },
            expected_dependency_edges={"downstream": ["upstream"], "upstream": []},
        )


def test_verify_mock_certificate_rejects_mismatched_dependency_identity() -> None:
    wrong_upstream = build_mock_certificate(
        workflow_id="hello_world",
        node_name="wrong_upstream",
        generated_node_compose_hash="sha256:" + ("1" * 64),
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
    )
    downstream = build_mock_certificate(
        workflow_id="hello_world",
        node_name="downstream",
        generated_node_compose_hash="sha256:" + ("2" * 64),
        inputs={},
        ephemeral_keypairs={},
        results={"worker": {"pass": True}},
        dependencies={"upstream": wrong_upstream},
    )

    with pytest.raises(RuntimeErrorBase, match="does not match expected node"):
        verify_mock_certificate(
            downstream,
            expected_dependency_compose_hashes={
                "upstream": "sha256:" + ("1" * 64),
                "downstream": "sha256:" + ("2" * 64),
            },
            expected_dependency_edges={"downstream": ["upstream"], "upstream": []},
        )


def test_generate_ed25519_keypair_material_returns_hashes() -> None:
    material = generate_ed25519_keypair_material(
        keypair_name="session_key",
        certificate_common_name="hello_world.node.session_key",
    )

    assert "BEGIN PRIVATE KEY" in material.private_key_pem
    assert "BEGIN PUBLIC KEY" in material.public_key_pem
    assert material.metadata["name"] == "session_key"
    assert material.metadata["public_key_hash"].startswith("sha256:")


def test_jsonlogic_and_base64_validation() -> None:
    assert (
        evaluate_jsonlogic(
            {"==": [{"var": "inputs.input_a.value"}, "hello"]},
            {"inputs": {"input_a": {"value": "hello"}}},
        )
        is True
    )

    try:
        evaluate_jsonlogic({"var": ""}, {})
    except JsonLogicError:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected JsonLogic validation failure")

    try:
        decode_key_b64("not-base64")
    except RuntimeErrorBase:
        pass
    else:  # pragma: no cover - defensive
        raise AssertionError("expected base64 validation failure")


def test_runtime_http_helpers_send_default_user_agent(monkeypatch) -> None:
    observed_user_agents: list[str | None] = []

    class FakeResponse:
        status = 200

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return b"ok"

    def fake_urlopen(request, **_kwargs):
        observed_user_agents.append(request.get_header("User-agent"))
        return FakeResponse()

    monkeypatch.setattr(common_module.urllib_request, "urlopen", fake_urlopen)

    assert http_get_bytes(url="https://api.covehub.io/healthz") == b"ok"
    assert observed_user_agents == [COVE_RUNTIME_USER_AGENT]


def test_runtime_streaming_download_helper_sends_default_user_agent(
    monkeypatch,
    tmp_path,
) -> None:
    observed_user_agents: list[str | None] = []

    class FakeResponse:
        status = 200
        reason = "OK"
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self._reads = 0

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self, _size: int = -1) -> bytes:
            self._reads += 1
            return b"ok" if self._reads == 1 else b""

    def fake_urlopen(request, **_kwargs):
        observed_user_agents.append(request.get_header("User-agent"))
        return FakeResponse()

    monkeypatch.setattr(common_module.urllib_request, "urlopen", fake_urlopen)

    output_path = tmp_path / "artifact.bin"
    http_get_to_file(url="https://api.covehub.io/healthz", output_path=output_path)

    assert output_path.read_bytes() == b"ok"
    assert observed_user_agents == [COVE_RUNTIME_USER_AGENT]


def test_decrypt_ciphertext_file_streams_to_disk(tmp_path) -> None:
    plaintext = (b"stream-me-please-" * 65536) + b"done"
    key_bytes = bytes(range(32))
    ciphertext_path = tmp_path / "artifact.bin"
    plaintext_path = tmp_path / "artifact.txt"

    ciphertext_path.write_bytes(
        encrypt_plaintext_bytes(plaintext=plaintext, key_bytes=key_bytes)
    )

    observed_plaintext_hash = decrypt_ciphertext_file(
        ciphertext_path=ciphertext_path,
        plaintext_path=plaintext_path,
        key_bytes=key_bytes,
    )

    assert plaintext_path.read_bytes() == plaintext
    assert observed_plaintext_hash == sha256_file_literal(plaintext_path)


def test_runtime_chunked_upload_helper_uses_upload_sessions(monkeypatch) -> None:
    observed: list[tuple[str, str | None, bytes]] = []

    class FakeResponse:
        def __init__(self, status: int, payload: bytes) -> None:
            self.status = status
            self._payload = payload

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

        def read(self) -> bytes:
            return self._payload

    def fake_urlopen(request, **_kwargs):
        payload = request.data if isinstance(request.data, bytes) else b""
        observed.append((request.full_url, request.get_method(), payload))
        if request.full_url.endswith('/upload-session'):
            return FakeResponse(201, json.dumps({"session_id": "sess1", "offset": 0}).encode('utf-8'))
        if request.full_url.endswith('/v1/uploads/sessions/sess1'):
            offset_value = request.get_header('X-Cove-Upload-Offset') or request.headers.get('X-cove-upload-offset')
            offset = int(offset_value)
            return FakeResponse(200, json.dumps({"session_id": "sess1", "offset": offset + len(payload)}).encode('utf-8'))
        if request.full_url.endswith('/v1/uploads/sessions/sess1/complete'):
            return FakeResponse(201, b"")
        raise AssertionError(f"unexpected request: {request.full_url}")

    monkeypatch.setattr(common_module.urllib_request, 'urlopen', fake_urlopen)
    monkeypatch.setattr(common_module, '_CHUNKED_UPLOAD_THRESHOLD_BYTES', 8)
    monkeypatch.setattr(common_module, '_CHUNKED_UPLOAD_CHUNK_SIZE_BYTES', 4)

    payload = b'abcdefghij'
    http_put_bytes_resumable(
        url='https://api.covehub.io/v1/runtime/alice.example.test/demo/artifacts/model/sha256:abc',
        payload=payload,
        headers={'Content-Type': 'application/octet-stream', 'X-TDX-Quote': 'quote'},
        session_create_headers={'Content-Type': 'application/octet-stream', 'X-TDX-Quote': 'quote'},
        session_create_payload={'upload_length': len(payload)},
    )

    assert [item[0] for item in observed] == [
        'https://api.covehub.io/v1/runtime/alice.example.test/demo/artifacts/model/sha256:abc/upload-session',
        'https://api.covehub.io/v1/uploads/sessions/sess1',
        'https://api.covehub.io/v1/uploads/sessions/sess1',
        'https://api.covehub.io/v1/uploads/sessions/sess1',
        'https://api.covehub.io/v1/uploads/sessions/sess1/complete',
    ]
    assert observed[1][2] == b'abcd'
    assert observed[2][2] == b'efgh'
    assert observed[3][2] == b'ij'


def test_load_inline_sidecar_context_reads_config_from_environment(
    monkeypatch,
) -> None:
    config_payload = {
        "service_name": "worker",
        "preconditions": {"==": [{"var": "inputs.input_a.value"}, "hello"]},
    }
    compose_hash = "sha256:" + ("1" * 64)
    monkeypatch.setenv("COVE_SERVICE_NAME", "cove_preconditions_worker")
    monkeypatch.setenv("COVE_COMPOSE_HASH", compose_hash)
    monkeypatch.setenv("COVE_CONFIG_JSON", json.dumps(config_payload, sort_keys=True))

    context = load_inline_sidecar_context()

    assert context.service_name == "cove_preconditions_worker"
    assert context.config == config_payload
    assert context.compose_hash == compose_hash


def test_collect_attestation_bundle_uses_dstack_client_for_phala_dstack(
    monkeypatch,
) -> None:
    report_data = b"phala-report-data"

    class FakeInfo:
        def model_dump(self) -> dict[str, object]:
            return {"app_id": "demo"}

    class FakeQuote:
        def __init__(self) -> None:
            self.quote = "phala-quote"
            self.event_log = [{"event": "phala"}]
            self.vm_config = {"tee": "tdx"}
            self.report_data = report_data.hex()

    class FakeClient:
        def __init__(self, timeout: int | None = None) -> None:
            assert timeout == 30

        def get_quote(self, raw_report_data: bytes) -> FakeQuote:
            assert raw_report_data == report_data
            return FakeQuote()

        def info(self) -> FakeInfo:
            return FakeInfo()

    monkeypatch.setitem(
        sys.modules,
        "dstack_sdk",
        types.SimpleNamespace(DstackClient=FakeClient),
    )

    bundle = collect_attestation_bundle(
        AttestationSettings(mode="phala_dstack", provider="phala", runtime="dstack"),
        report_data=report_data,
    )

    assert bundle["format"] == "phala_dstack_v1"
    assert bundle["quote"] == "phala-quote"
    assert bundle["event_log"] == [{"event": "phala"}]
    assert bundle["report_data"] == report_data.hex()
    assert bundle["info"] == {"app_id": "demo"}


def test_verify_attestation_bundle_accepts_verified_phala_dstack_quote(
    monkeypatch,
) -> None:
    expected_report_data = build_node_certificate_report_data(
        certificate_body_hash="sha256:" + ("a" * 64),
        compose_hash="sha256:" + ("b" * 64),
    )
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: _verified_quote_response(expected_report_data.hex()),
    )

    verify_attestation_bundle(
        {
            "format": "phala_dstack_v1",
            "quote": "phala-quote",
            "event_log": _compose_event_log()[0],
            "report_data": expected_report_data.hex(),
        },
        expected_report_data=expected_report_data,
        expected_compose_hash=_COMPOSE_HASH,
    )


def test_verify_attestation_bundle_accepts_tdx_zero_padded_report_data(
    monkeypatch,
) -> None:
    expected_report_data = b"cove-report-data"
    padded_report_data = expected_report_data.ljust(64, b"\0").hex()
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: _verified_quote_response(padded_report_data),
    )

    verify_attestation_bundle(
        {
            "format": "phala_dstack_v1",
            "quote": "phala-quote",
            "event_log": _compose_event_log()[0],
            "report_data": padded_report_data,
        },
        expected_report_data=expected_report_data,
        expected_compose_hash=_COMPOSE_HASH,
    )


def test_verify_attestation_bundle_rejects_network_failure(monkeypatch) -> None:
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: (_raise_runtime_error("network unreachable")),
    )

    with pytest.raises(RuntimeErrorBase, match="network unreachable"):
        verify_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "phala-quote",
                "event_log": _compose_event_log()[0],
                "report_data": (b"\0" * 64).hex(),
            },
            expected_report_data=b"\0" * 64,
            expected_compose_hash=_COMPOSE_HASH,
        )


def test_verify_attestation_bundle_rejects_malformed_verifier_response(
    monkeypatch,
) -> None:
    expected_report_data = b"verified-report-data"
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: {
            "success": True,
            "quote": {
                "verified": True,
                "header": {"tee_type": "tdx"},
                "body": {},
            },
        },
    )

    with pytest.raises(RuntimeErrorBase, match="body.reportdata"):
        verify_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "phala-quote",
                "event_log": _compose_event_log()[0],
                "report_data": expected_report_data.hex(),
            },
            expected_report_data=expected_report_data,
            expected_compose_hash=_COMPOSE_HASH,
        )


def test_verify_attestation_bundle_rejects_unverified_quote(monkeypatch) -> None:
    expected_report_data = b"verified-report-data"
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: {
            "success": True,
            "quote": {
                "verified": False,
                "body": {"reportdata": expected_report_data.hex()},
            },
        },
    )

    with pytest.raises(RuntimeErrorBase, match="did not verify successfully"):
        verify_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "phala-quote",
                "event_log": _compose_event_log()[0],
                "report_data": expected_report_data.hex(),
            },
            expected_report_data=expected_report_data,
            expected_compose_hash=_COMPOSE_HASH,
        )


def test_verify_attestation_bundle_rejects_report_data_mismatch(monkeypatch) -> None:
    expected_report_data = b"verified-report-data"
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: {
            "success": True,
            "quote": {
                "header": {"tee_type": "tdx"},
                "verified": True,
                "body": {"reportdata": ("ff" * 64)},
            },
        },
    )

    with pytest.raises(RuntimeErrorBase, match="verified quote report data"):
        verify_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "phala-quote",
                "event_log": _compose_event_log()[0],
                "report_data": expected_report_data.hex(),
            },
            expected_report_data=expected_report_data,
            expected_compose_hash=_COMPOSE_HASH,
        )


def test_verify_attestation_bundle_rejects_non_tdx_quote(monkeypatch) -> None:
    expected_report_data = b"verified-report-data"
    padded_report_data = expected_report_data.ljust(64, b"\0").hex()
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: {
            **_verified_quote_response(padded_report_data),
            "quote": {
                **_verified_quote_response(padded_report_data)["quote"],
                "header": {"tee_type": "sgx"},
            },
        },
    )

    with pytest.raises(RuntimeErrorBase, match="not a TDX quote"):
        verify_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "phala-quote",
                "event_log": _compose_event_log()[0],
                "report_data": expected_report_data.hex(),
            },
            expected_report_data=expected_report_data,
            expected_compose_hash=_COMPOSE_HASH,
        )


def test_verify_attestation_bundle_rejects_missing_rtmr_fields(monkeypatch) -> None:
    expected_report_data = b"verified-report-data"
    padded_report_data = expected_report_data.ljust(64, b"\0").hex()
    _event_log, rtmr3 = _compose_event_log()
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: {
            "success": True,
            "quote": {
                "verified": True,
                "header": {"tee_type": "tdx"},
                "body": {
                    "reportdata": padded_report_data,
                    "rtmr3": rtmr3,
                },
            },
        },
    )

    with pytest.raises(RuntimeErrorBase, match="quote.body.mrtd"):
        verify_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "phala-quote",
                "event_log": _compose_event_log()[0],
                "report_data": expected_report_data.hex(),
            },
            expected_report_data=expected_report_data,
            expected_compose_hash=_COMPOSE_HASH,
        )


def test_verify_attestation_bundle_rejects_duplicate_compose_events(monkeypatch) -> None:
    expected_report_data = b"verified-report-data"
    padded_report_data = expected_report_data.ljust(64, b"\0").hex()
    event_log, _rtmr3 = _compose_event_log()
    event_digest = bytes.fromhex(str(event_log[0]["digest"]))
    duplicate_event_log = [event_log[0], dict(event_log[0])]
    duplicate_rtmr3 = _rtmr3_for_event_digests([event_digest, event_digest]).hex()
    quote_response = _verified_quote_response(padded_report_data)
    quote_response["quote"]["body"]["rtmr3"] = duplicate_rtmr3
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: quote_response,
    )

    with pytest.raises(RuntimeErrorBase, match="exactly one RTMR3 compose-hash event"):
        verify_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "phala-quote",
                "event_log": duplicate_event_log,
                "report_data": expected_report_data.hex(),
            },
            expected_report_data=expected_report_data,
            expected_compose_hash=_COMPOSE_HASH,
        )


def test_verify_attestation_bundle_rejects_compose_mismatch(monkeypatch) -> None:
    expected_report_data = b"verified-report-data"
    padded_report_data = expected_report_data.ljust(64, b"\0").hex()
    monkeypatch.setattr(
        "cove_container_runtime.attestation.http_post_json",
        lambda **_kwargs: _verified_quote_response(padded_report_data),
    )

    with pytest.raises(RuntimeErrorBase, match="compose-hash event does not match expected compose hash"):
        verify_attestation_bundle(
            {
                "format": "phala_dstack_v1",
                "quote": "phala-quote",
                "event_log": _compose_event_log()[0],
                "report_data": expected_report_data.hex(),
            },
            expected_report_data=expected_report_data,
            expected_compose_hash="sha256:" + ("c" * 64),
        )


def _raise_runtime_error(message: str):
    raise RuntimeErrorBase(message)
