from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from cove_cli.artifact_crypto import sha256_literal
from cove_cli.common import RuntimeErrorBase
from cove_cli.config import config_path_for_home
from cove_cli.compile import reviewed_compose_hash
from cove_cli.deploy import (
    DeployCommandError,
    PhalaDeployOptions,
    _deployment_name,
    _wait_for_dependency_certificates,
    deploy_workflow,
    translated_node_deployment_compose_text,
)
from cove_cli.publish import MaterializedNode, MaterializedWorkflowBundle, push_workflow
from cove_cli.provisioning_identity import build_owner_identity_document

from .support import MockCovehubServer, build_test_owner_identity, write_test_certificate


ALICE_DOMAIN = "alice.cove-demo-parties.covehub.io"
ALICE_OWNER_URL = f"https://{ALICE_DOMAIN}"
BOB_DOMAIN = "bob.cove-demo-parties.covehub.io"
BOB_OWNER_URL = f"https://{BOB_DOMAIN}"
CAROL_DOMAIN = "carol.cove-demo-parties.covehub.io"
CAROL_OWNER_URL = f"https://{CAROL_DOMAIN}"
PUBLISHER_DOMAIN = CAROL_DOMAIN
PUBLISHER_OWNER_URL = CAROL_OWNER_URL


@pytest.fixture(autouse=True)
def _stub_owner_identity_resolution(monkeypatch):
    def fake_fetch_owner_identity_document(
        *,
        owner_url: str | None = None,
        expected_owner_domain: str | None = None,
        timeout: float = 5.0,
        **_legacy_kwargs,
    ):
        assert owner_url is not None
        owner_domain = expected_owner_domain or "owner.example.test"
        return build_test_owner_identity(owner_domain, owner_url)

    monkeypatch.setattr(
        "cove_cli.compile.fetch_owner_identity_document",
        fake_fetch_owner_identity_document,
    )

    def fake_static_resolution(*, owner, artifact_id: str, plaintext_hash: str):
        ciphertext_hash = sha256_literal(
            f"{owner.owner_domain}:{artifact_id}:{plaintext_hash}".encode("utf-8")
        )
        return {
            "hub_path": f"v1/artifacts/{owner.owner_domain}/{artifact_id}/{ciphertext_hash}",
            "artifact_id": artifact_id,
            "owner_domain": owner.owner_domain,
            "owner_url": owner.owner_url,
            "plaintext_hash": plaintext_hash,
            "ciphertext_hash": ciphertext_hash,
        }

    monkeypatch.setattr(
        "cove_cli.compile._fetch_owner_static_artifact_resolution",
        fake_static_resolution,
    )

    def fake_fetch_owner_identity_for_write(
        *,
        owner_url: str,
        owner_private_key_path: Path,
        expected_owner_domain: str | None = None,
        timeout: float = 5.0,
    ):
        assert expected_owner_domain is not None
        return build_owner_identity_document(
            owner_url=owner_url,
            owner_private_key_path=owner_private_key_path,
            owner_public_key_path=owner_private_key_path.parent / "owner-signing-public.pem",
        )

    monkeypatch.setattr(
        "cove_cli.publish.fetch_owner_identity_for_write",
        fake_fetch_owner_identity_for_write,
    )


def test_deploy_pulls_bundle_before_submitting_and_orders_nodes_topologically(
    tmp_path,
    monkeypatch,
) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".carol_cove"
    events: list[str] = []

    class FakePhalaClient:
        def __init__(self) -> None:
            self.provision_calls: list[dict[str, object]] = []

        def provision_cvm(self, payload: dict[str, object]):
            name = payload["name"]
            assert isinstance(name, str)
            assert payload["instance_type"] == "tdx.small"
            compose_file = payload["compose_file"]
            assert isinstance(compose_file, dict)
            assert compose_file["runner"] == "docker-compose"
            assert compose_file["name"] == name
            events.append(f"provision:{name}")
            self.provision_calls.append(payload)
            suffix = len(self.provision_calls)
            return {"app_id": f"app-{suffix}", "compose_hash": f"compose-{suffix}"}

        def commit_cvm_provision(self, payload: dict[str, object]):
            app_id = payload["app_id"]
            return {"id": f"cvm-{app_id}", "status": "pending"}

        def get_cvm_list(self, request: dict[str, object]):
            return _empty_cvm_list()

        def delete_cvm(self, payload: dict[str, object]) -> None:
            raise AssertionError(f"unexpected delete_cvm call: {payload}")

        def close(self) -> None:
            return None

    fake_client = FakePhalaClient()

    with MockCovehubServer() as server:
        _write_config(
            cove_home,
            {
                "covehub_server_url": server.url,
                "owner_server_url": PUBLISHER_OWNER_URL,
                "phala_cloud_api_key": "phala-api-key",
            },
        )
        push_workflow(workflow_dir / "workflow.cove.yaml", cove_home=cove_home)

        original_pull = __import__("cove_cli.deploy", fromlist=["pull_workflow_bundle"]).pull_workflow_bundle

        def wrapped_pull_workflow_bundle(**kwargs):
            events.append("pull")
            return original_pull(**kwargs)

        monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", wrapped_pull_workflow_bundle)
        monkeypatch.setattr(
            "cove_cli.deploy._create_phala_client",
            lambda api_key: _assert_api_key(api_key, fake_client),
        )

        output = deploy_workflow(
            f"{PUBLISHER_DOMAIN}/hello_world",
            cove_home=cove_home,
            phala_options=PhalaDeployOptions(instance_type="tdx.small", staged_launch=False),
        )

    assert events[0] == "pull"
    expected_names = [
        _deployment_name(
            publisher=PUBLISHER_DOMAIN,
            workflow_id="hello_world",
            node_id=node_id,
        )
        for node_id in [
            "alice_word_length_checker",
            "bob_word_length_checker",
            "character_set_checker",
            "final_server",
        ]
    ]
    assert [event for event in events[1:]] == [
        f"provision:{name}"
        for name in expected_names
    ]
    payload = json.loads(output)
    assert payload["publisher"] == PUBLISHER_DOMAIN
    assert payload["workflow_id"] == "hello_world"
    assert [deployment["deployment_name"] for deployment in payload["deployments"]] == expected_names
    assert payload["deployments"][-1]["cvm_id"] == "cvm-app-4"
    assert "node_deploy_wall_seconds" in payload["deployments"][-1]["timings_seconds"]

    final_payload = fake_client.provision_calls[-1]
    assert final_payload["name"] == expected_names[-1]
    assert final_payload["instance_type"] == "tdx.small"
    compose_file = final_payload["compose_file"]
    assert isinstance(compose_file, dict)
    assert compose_file["runner"] == "docker-compose"
    assert compose_file["name"] == final_payload["name"]
    translated_compose = compose_file["docker_compose_file"]
    assert isinstance(translated_compose, str)
    translated_payload = yaml.safe_load(translated_compose)
    assert translated_payload["volumes"]["cove_runtime"] == {}
    assert "cove_generated" not in translated_payload["volumes"]
    assert "cove_seed_generated" not in translated_payload["services"]
    assert all(
        "cove_seed_generated" not in service.get("depends_on", {})
        for service in translated_payload["services"].values()
        if isinstance(service, dict)
    )
    final_service_env = translated_payload["services"]["cove_node_certificate_writer"]["environment"]
    assert "COVE_COMPOSE_HASH" in final_service_env
    assert "COVE_COMPOSE_PATH" not in final_service_env
    assert any(
        service_name.startswith("cove_copy_")
        for service_name in translated_payload["services"]
    )
    for service_name, service in translated_payload["services"].items():
        if not service_name.startswith("cove_copy_"):
            continue
        source_path = service["environment"]["COVE_INPUT_SOURCE"]
        assert source_path.startswith("/cove/inputs/")
        assert not source_path.startswith("/runtime/cove/")
    assert _has_bind_mount(
        translated_payload["services"]["cove_node_certificate_writer"],
        "/var/run/dstack.sock",
    )
    assert not _has_bind_mount(
        translated_payload["services"]["cove_dependency_certificate_fetcher"],
        "/var/run/dstack.sock",
    )


def test_wait_for_dependency_certificates_retries_until_runtime_certificate_matches(
    monkeypatch,
) -> None:
    calls: list[str] = []

    def fake_http_get_json(*, url: str, timeout: float = 5.0):
        del timeout
        calls.append(url)
        if len(calls) == 1:
            raise RuntimeErrorBase("HTTP 404 for dependency")
        return {
            "certificate_body": {
                "workflow_id": "demo",
                "node_id": "upstream",
                "generated_node_compose_hash": "sha256:upstream-compose",
            }
        }

    monkeypatch.setattr("cove_cli.deploy.http_get_json", fake_http_get_json)
    monkeypatch.setattr("cove_cli.deploy.time.sleep", lambda _seconds: None)

    _wait_for_dependency_certificates(
        covehub_server_url="https://api.example.test",
        workflow_publisher_domain="alice.example.test",
        workflow_id="demo",
        node_id="downstream",
        dependency_names=("upstream",),
        dependency_compose_hashes={"upstream": "sha256:upstream-compose"},
        timeout_seconds=5.0,
        poll_interval_seconds=0.01,
    )

    assert calls == [
        "https://api.example.test/v1/runtime/alice.example.test/demo/certificates/upstream/latest",
        "https://api.example.test/v1/runtime/alice.example.test/demo/certificates/upstream/latest",
    ]


def test_deploy_returns_structured_json(tmp_path, monkeypatch) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )
    bundle = _write_minimal_bundle(tmp_path / "bundle")
    client = _CapturingPhalaClient()

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, client),
    )

    output = deploy_workflow(
        f"{PUBLISHER_DOMAIN}/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(instance_type="tdx.small"),
    )

    payload = json.loads(output)
    assert payload["publisher"] == PUBLISHER_DOMAIN
    assert payload["workflow_id"] == "demo"
    assert payload["reference"] == "latest"
    assert payload["published_ref"] == f"{PUBLISHER_DOMAIN}/demo/latest"
    assert payload["bundle_path"] == str(bundle.root_path)
    assert payload["manifest_hash"] == bundle.manifest_hash
    assert payload["deleted_finished_cvms"] == []
    assert len(payload["deployments"]) == 1
    deployment = payload["deployments"][0]
    assert deployment == {
        "node_id": "node_one",
        "deployment_name": _deployment_name(
            publisher=PUBLISHER_DOMAIN,
            workflow_id="demo",
            node_id="node_one",
        ),
        "cvm_id": "cvm-app-1",
        "status": "pending",
        "app_id": "app-1",
        "compose_hash": "compose-1",
        "timings_seconds": deployment["timings_seconds"],
    }
    assert "provision_cvm_seconds" in deployment["timings_seconds"]
    assert "commit_cvm_provision_seconds" in deployment["timings_seconds"]


def test_deploy_deletes_finished_phala_cvms_for_workflow_before_provision(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )
    bundle = _write_minimal_bundle(tmp_path / "bundle")
    deployment_name = _deployment_name(
        publisher=PUBLISHER_DOMAIN,
        workflow_id="demo",
        node_id="node_one",
    )
    events: list[str] = []

    class FakePhalaClient:
        def get_cvm_list(self, request: dict[str, object]):
            events.append(f"list:{request['page']}")
            return {
                "items": [
                    {
                        "id": "old-cvm",
                        "name": deployment_name,
                        "status": "stopped",
                    },
                    {
                        "id": "running-cvm",
                        "name": deployment_name,
                        "status": "running",
                    },
                    {
                        "id": "other-cvm",
                        "name": "unrelated",
                        "status": "stopped",
                    },
                ],
                "total": 3,
                "page": 1,
                "page_size": 100,
                "pages": 1,
            }

        def delete_cvm(self, payload: dict[str, object]) -> None:
            events.append(f"delete:{payload['id']}")

        def provision_cvm(self, payload: dict[str, object]):
            events.append("provision")
            return {"app_id": "app-1", "compose_hash": "compose-1"}

        def commit_cvm_provision(self, payload: dict[str, object]):
            events.append("commit")
            return {"id": "cvm-app-1", "status": "pending"}

        def close(self) -> None:
            return None

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, FakePhalaClient()),
    )

    output = deploy_workflow(
        f"{PUBLISHER_DOMAIN}/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(instance_type="tdx.small"),
    )

    assert events == ["list:1", "delete:old-cvm", "provision", "commit"]
    payload = json.loads(output)
    assert payload["deleted_finished_cvms"] == [
        {
            "cvm_id": "old-cvm",
            "name": deployment_name,
            "status": "stopped",
            "finish_reason": "cvm_status",
        }
    ]


def test_deploy_deletes_running_phala_cvms_when_all_containers_exited(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )
    bundle = _write_minimal_bundle(tmp_path / "bundle")
    deployment_name = _deployment_name(
        publisher=PUBLISHER_DOMAIN,
        workflow_id="demo",
        node_id="node_one",
    )
    events: list[str] = []

    class FakePhalaClient:
        def get_cvm_list(self, request: dict[str, object]):
            events.append(f"list:{request['page']}")
            return {
                "items": [
                    {
                        "id": "finished-workload",
                        "name": deployment_name,
                        "status": "running",
                    },
                    {
                        "id": "live-service",
                        "name": deployment_name,
                        "status": "running",
                    },
                ],
                "total": 2,
                "page": 1,
                "page_size": 100,
                "pages": 1,
            }

        def get_cvm_containers_stats(self, payload: dict[str, object]):
            events.append(f"containers:{payload['id']}")
            if payload["id"] == "finished-workload":
                return {
                    "containers": [
                        {"name": "worker", "state": "exited", "status": "Exited (0)"},
                        {"name": "writer", "state": "exited", "status": "Exited (0)"},
                    ]
                }
            return {
                "containers": [
                    {"name": "server", "state": "running", "status": "Up 1 minute"}
                ]
            }

        def delete_cvm(self, payload: dict[str, object]) -> None:
            events.append(f"delete:{payload['id']}")

        def provision_cvm(self, payload: dict[str, object]):
            events.append("provision")
            return {"app_id": "app-1", "compose_hash": "compose-1"}

        def commit_cvm_provision(self, payload: dict[str, object]):
            events.append("commit")
            return {"id": "cvm-app-1", "status": "pending"}

        def close(self) -> None:
            return None

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, FakePhalaClient()),
    )

    output = deploy_workflow(
        f"{PUBLISHER_DOMAIN}/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(instance_type="tdx.small"),
    )

    assert events == [
        "list:1",
        "containers:finished-workload",
        "delete:finished-workload",
        "containers:live-service",
        "provision",
        "commit",
    ]
    payload = json.loads(output)
    assert payload["deleted_finished_cvms"] == [
        {
            "cvm_id": "finished-workload",
            "name": deployment_name,
            "status": "running",
            "finish_reason": "containers_finished",
        }
    ]


def test_deploy_deletes_legacy_nested_finished_phala_cvms(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )
    bundle = _write_minimal_bundle(tmp_path / "bundle")
    deployment_name = _deployment_name(
        publisher=PUBLISHER_DOMAIN,
        workflow_id="demo",
        node_id="node_one",
    )
    events: list[str] = []

    class FakePhalaClient:
        def get_cvm_list(self, request: dict[str, object]):
            events.append(f"list:{request['page']}")
            return {
                "items": [
                    {
                        "hosted": {
                            "id": "legacy-cvm",
                            "name": deployment_name,
                            "status": "stopped",
                        }
                    }
                ],
                "total": 1,
                "page": 1,
                "page_size": 100,
                "pages": 1,
            }

        def delete_cvm(self, payload: dict[str, object]) -> None:
            events.append(f"delete:{payload['id']}")

        def provision_cvm(self, payload: dict[str, object]):
            events.append("provision")
            return {"app_id": "app-1", "compose_hash": "compose-1"}

        def commit_cvm_provision(self, payload: dict[str, object]):
            events.append("commit")
            return {"id": "cvm-app-1", "status": "pending"}

        def close(self) -> None:
            return None

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, FakePhalaClient()),
    )

    output = deploy_workflow(
        f"{PUBLISHER_DOMAIN}/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(instance_type="tdx.small"),
    )

    assert events == ["list:1", "delete:legacy-cvm", "provision", "commit"]
    payload = json.loads(output)
    assert payload["deleted_finished_cvms"] == [
        {
            "cvm_id": "legacy-cvm",
            "name": deployment_name,
            "status": "stopped",
            "finish_reason": "cvm_status",
        }
    ]


def test_deploy_staged_launch_waits_before_submitting_dependent_nodes(
    tmp_path,
    monkeypatch,
) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".carol_cove"
    events: list[str] = []

    class FakePhalaClient:
        def __init__(self) -> None:
            self.provision_calls: list[dict[str, object]] = []

        def provision_cvm(self, payload: dict[str, object]):
            name = payload["name"]
            assert isinstance(name, str)
            events.append(f"provision:{name}")
            self.provision_calls.append(payload)
            suffix = len(self.provision_calls)
            return {"app_id": f"app-{suffix}", "compose_hash": f"compose-{suffix}"}

        def commit_cvm_provision(self, payload: dict[str, object]):
            app_id = payload["app_id"]
            return {"id": f"cvm-{app_id}", "status": "pending"}

        def get_cvm_list(self, request: dict[str, object]):
            return _empty_cvm_list()

        def delete_cvm(self, payload: dict[str, object]) -> None:
            raise AssertionError(f"unexpected delete_cvm call: {payload}")

        def close(self) -> None:
            return None

    fake_client = FakePhalaClient()

    with MockCovehubServer() as server:
        _write_config(
            cove_home,
            {
                "covehub_server_url": server.url,
                "owner_server_url": PUBLISHER_OWNER_URL,
                "phala_cloud_api_key": "phala-api-key",
            },
        )
        push_workflow(workflow_dir / "workflow.cove.yaml", cove_home=cove_home)

        original_pull = __import__("cove_cli.deploy", fromlist=["pull_workflow_bundle"]).pull_workflow_bundle

        def wrapped_pull_workflow_bundle(**kwargs):
            events.append("pull")
            return original_pull(**kwargs)

        def fake_wait_for_dependency_certificates(*, node_id: str, dependency_names: tuple[str, ...], **_kwargs):
            if dependency_names:
                events.append(f"wait:{node_id}:{','.join(dependency_names)}")

        monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", wrapped_pull_workflow_bundle)
        monkeypatch.setattr(
            "cove_cli.deploy._create_phala_client",
            lambda api_key: _assert_api_key(api_key, fake_client),
        )
        monkeypatch.setattr(
            "cove_cli.deploy._wait_for_dependency_certificates",
            fake_wait_for_dependency_certificates,
        )

        deploy_workflow(
            f"{PUBLISHER_DOMAIN}/hello_world",
            cove_home=cove_home,
            phala_options=PhalaDeployOptions(instance_type="tdx.small"),
        )

    expected_names = [
        _deployment_name(
            publisher=PUBLISHER_DOMAIN,
            workflow_id="hello_world",
            node_id=node_id,
        )
        for node_id in [
            "alice_word_length_checker",
            "bob_word_length_checker",
            "character_set_checker",
            "final_server",
        ]
    ]
    assert events == [
        "pull",
        f"provision:{expected_names[0]}",
        f"provision:{expected_names[1]}",
        "wait:character_set_checker:alice_word_length_checker,bob_word_length_checker",
        f"provision:{expected_names[2]}",
        "wait:final_server:alice_word_length_checker,bob_word_length_checker,character_set_checker",
        f"provision:{expected_names[3]}",
    ]


def test_deploy_workflow_node_launches_only_requested_node(
    tmp_path,
    monkeypatch,
) -> None:
    workflow_dir = _copy_hello_world_workflow(tmp_path)
    cove_home = tmp_path / ".carol_cove"
    wait_calls: list[tuple[str, tuple[str, ...]]] = []

    class FakePhalaClient:
        def __init__(self) -> None:
            self.provision_calls: list[dict[str, object]] = []

        def provision_cvm(self, payload: dict[str, object]):
            self.provision_calls.append(payload)
            return {"app_id": "app-1", "compose_hash": "compose-1"}

        def commit_cvm_provision(self, payload: dict[str, object]):
            return {"id": "cvm-app-1", "status": "pending"}

        def get_cvm_list(self, request: dict[str, object]):
            return _empty_cvm_list()

        def delete_cvm(self, payload: dict[str, object]) -> None:
            raise AssertionError(f"unexpected delete_cvm call: {payload}")

        def close(self) -> None:
            return None

    fake_client = FakePhalaClient()

    with MockCovehubServer() as server:
        _write_config(
            cove_home,
            {
                "covehub_server_url": server.url,
                "owner_server_url": PUBLISHER_OWNER_URL,
                "phala_cloud_api_key": "phala-api-key",
            },
        )
        push_workflow(workflow_dir / "workflow.cove.yaml", cove_home=cove_home)

        monkeypatch.setattr(
            "cove_cli.deploy._create_phala_client",
            lambda api_key: _assert_api_key(api_key, fake_client),
        )
        monkeypatch.setattr(
            "cove_cli.deploy._wait_for_dependency_certificates",
            lambda *, node_id, dependency_names, **_kwargs: wait_calls.append(
                (node_id, dependency_names)
            ),
        )

        output = deploy_workflow(
            f"{PUBLISHER_DOMAIN}/hello_world",
            cove_home=cove_home,
            phala_options=PhalaDeployOptions(
                instance_type="tdx.small",
                workflow_node_id="final_server",
            ),
        )

    expected_name = _deployment_name(
        publisher=PUBLISHER_DOMAIN,
        workflow_id="hello_world",
        node_id="final_server",
    )
    assert wait_calls == [
        (
            "final_server",
            (
                "alice_word_length_checker",
                "bob_word_length_checker",
                "character_set_checker",
            ),
        )
    ]
    assert len(fake_client.provision_calls) == 1
    assert fake_client.provision_calls[0]["name"] == expected_name
    payload = json.loads(output)
    assert [deployment["node_id"] for deployment in payload["deployments"]] == ["final_server"]
    assert payload["deployments"][0]["deployment_name"] == expected_name


def test_deploy_reuse_cvm_requires_single_workflow_node(tmp_path, monkeypatch) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )

    monkeypatch.setattr(
        "cove_cli.deploy.pull_workflow_bundle",
        lambda **_kwargs: pytest.fail("unexpected pull"),
    )
    monkeypatch.setattr("cove_cli.deploy._create_phala_client", _unused_fake_client)

    with pytest.raises(
        DeployCommandError,
        match="--phala-reuse-cvm-id requires --workflow-node",
    ):
        deploy_workflow(
            "alice/demo",
            cove_home=cove_home,
            phala_options=PhalaDeployOptions(reuse_cvm_id="cvm-existing"),
        )


def test_deploy_workflow_node_reuses_existing_phala_cvm(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
            "phala_docker_username": "alice-docker",
            "phala_docker_access_token": "docker-read-token",
        },
    )
    bundle = _write_minimal_bundle(tmp_path / "bundle")

    class FakePhalaClient:
        def __init__(self) -> None:
            self.provision_update_payloads: list[dict[str, object]] = []
            self.commit_update_payloads: list[dict[str, object]] = []

        def provision_cvm_compose_file_update(self, payload: dict[str, object]):
            self.provision_update_payloads.append(payload)
            return {
                "app_id": "existing-app",
                "compose_hash": "compose-update-1",
            }

        def commit_cvm_compose_file_update(self, payload: dict[str, object]):
            self.commit_update_payloads.append(payload)
            return {"status": "updating"}

        def provision_cvm(self, payload: dict[str, object]):
            raise AssertionError(f"unexpected provision_cvm call: {payload}")

        def commit_cvm_provision(self, payload: dict[str, object]):
            raise AssertionError(f"unexpected commit_cvm_provision call: {payload}")

        def get_cvm_list(self, request: dict[str, object]):
            raise AssertionError(f"unexpected get_cvm_list call: {request}")

        def delete_cvm(self, payload: dict[str, object]) -> None:
            raise AssertionError(f"unexpected delete_cvm call: {payload}")

        def close(self) -> None:
            return None

    fake_client = FakePhalaClient()

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, fake_client),
    )
    monkeypatch.setattr(
        "cove_cli.deploy._encrypt_phala_env_vars",
        lambda *_args, **_kwargs: pytest.fail("unexpected registry env rotation"),
    )

    output = deploy_workflow(
        f"{PUBLISHER_DOMAIN}/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(
            workflow_node_id="node_one",
            reuse_cvm_id="cvm-existing",
        ),
    )

    expected_name = _deployment_name(
        publisher=PUBLISHER_DOMAIN,
        workflow_id="demo",
        node_id="node_one",
    )
    assert len(fake_client.provision_update_payloads) == 1
    provision_payload = fake_client.provision_update_payloads[0]
    assert provision_payload["id"] == "cvm-existing"
    assert "update_env_vars" not in provision_payload
    compose_file = provision_payload["app_compose"]
    assert isinstance(compose_file, dict)
    assert compose_file["runner"] == "docker-compose"
    assert compose_file["name"] == expected_name
    assert compose_file["allowed_envs"] == [
        "DSTACK_DOCKER_USERNAME",
        "DSTACK_DOCKER_PASSWORD",
    ]
    translated_compose = compose_file["docker_compose_file"]
    assert isinstance(translated_compose, str)
    assert "docker-read-token" not in str(fake_client.provision_update_payloads)
    assert "docker-read-token" not in translated_compose

    assert fake_client.commit_update_payloads == [
        {
            "id": "cvm-existing",
            "compose_hash": "compose-update-1",
        }
    ]
    payload = json.loads(output)
    assert payload["deleted_finished_cvms"] == []
    assert len(payload["deployments"]) == 1
    deployment = payload["deployments"][0]
    assert deployment == {
        "node_id": "node_one",
        "deployment_name": expected_name,
        "cvm_id": "cvm-existing",
        "status": "updating",
        "app_id": "existing-app",
        "compose_hash": "compose-update-1",
        "timings_seconds": deployment["timings_seconds"],
    }
    assert "provision_cvm_compose_update_seconds" in deployment["timings_seconds"]
    assert "commit_cvm_compose_update_seconds" in deployment["timings_seconds"]
    assert "docker-read-token" not in output


def test_deploy_requires_phala_cloud_api_key_in_local_config(tmp_path) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(cove_home, {"covehub_server_url": "http://127.0.0.1:8000"})

    with pytest.raises(DeployCommandError, match="phala_cloud_api_key"):
        deploy_workflow("alice/demo", cove_home=cove_home)


def test_deploy_requires_explicit_phala_instance_type(tmp_path) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )

    with pytest.raises(DeployCommandError, match="--phala-instance-type"):
        deploy_workflow("alice/demo", cove_home=cove_home)


def test_deploy_passes_common_phala_options_to_provision_payload(tmp_path, monkeypatch) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )
    bundle = _write_bundle_root(
        tmp_path / "bundle",
        workflow_text="""
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners: {}
artifacts: {}
nodes:
  node_one:
    compose: nodes/node_one/compose.generated.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        compose_text="""
services:
  worker:
    image: example/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111
""".strip()
        + "\n",
    )
    captured_payloads: list[dict[str, object]] = []

    class FakePhalaClient:
        def provision_cvm(self, payload: dict[str, object]):
            captured_payloads.append(payload)
            return {"app_id": "app-1", "compose_hash": "compose-1"}

        def commit_cvm_provision(self, payload: dict[str, object]):
            return {"id": "cvm-app-1", "status": "pending"}

        def get_cvm_list(self, request: dict[str, object]):
            return _empty_cvm_list()

        def delete_cvm(self, payload: dict[str, object]) -> None:
            raise AssertionError(f"unexpected delete_cvm call: {payload}")

        def close(self) -> None:
            return None

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, FakePhalaClient()),
    )

    deploy_workflow(
        f"{PUBLISHER_DOMAIN}/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(
            instance_type="h200.small",
            region="us-west",
            os_image="dstack-0.5.9",
            node_id=18,
            disk_size_gb=200,
            public_logs=False,
            public_sysinfo=True,
            listed=False,
        ),
    )

    assert len(captured_payloads) == 1
    payload = captured_payloads[0]
    assert payload["name"] == _deployment_name(
        publisher=PUBLISHER_DOMAIN,
        workflow_id="demo",
        node_id="node_one",
    )
    assert payload["instance_type"] == "h200.small"
    assert payload["region"] == "us-west"
    assert payload["image"] == "dstack-0.5.9"
    assert payload["node_id"] == 18
    assert payload["disk_size"] == 200
    assert payload["listed"] is False
    compose_file = payload["compose_file"]
    assert isinstance(compose_file, dict)
    assert compose_file["runner"] == "docker-compose"
    assert compose_file["name"] == payload["name"]
    assert compose_file["public_logs"] is False
    assert compose_file["public_sysinfo"] is True


def test_deploy_encrypts_configured_docker_hub_credentials_for_phala(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
            "phala_docker_username": "alice-docker",
            "phala_docker_access_token": "docker-read-token",
        },
    )
    bundle = _write_minimal_bundle(tmp_path / "bundle")
    client = _CapturingPhalaClient()
    encrypted_inputs: list[tuple[list[tuple[str, str]], str]] = []

    def fake_encrypt(env_vars: list[tuple[str, str]], public_key_hex: str) -> str:
        encrypted_inputs.append((env_vars, public_key_hex))
        return "encrypted-env"

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, client),
    )
    monkeypatch.setattr("cove_cli.deploy._encrypt_phala_env_vars", fake_encrypt)

    output = deploy_workflow(
        "alice/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(instance_type="tdx.medium"),
    )

    assert client.provision_payloads[0]["env_keys"] == [
        "DSTACK_DOCKER_USERNAME",
        "DSTACK_DOCKER_PASSWORD",
    ]
    compose_file = client.provision_payloads[0]["compose_file"]
    assert isinstance(compose_file, dict)
    assert compose_file["allowed_envs"] == [
        "DSTACK_DOCKER_USERNAME",
        "DSTACK_DOCKER_PASSWORD",
    ]
    assert encrypted_inputs == [
        (
            [
                ("DSTACK_DOCKER_USERNAME", "alice-docker"),
                ("DSTACK_DOCKER_PASSWORD", "docker-read-token"),
            ],
            "app-env-pubkey-1",
        )
    ]
    assert client.commit_payloads[0]["encrypted_env"] == "encrypted-env"
    assert client.commit_payloads[0]["env_keys"] == [
        "DSTACK_DOCKER_USERNAME",
        "DSTACK_DOCKER_PASSWORD",
    ]
    translated_compose = compose_file["docker_compose_file"]
    assert isinstance(translated_compose, str)
    assert "docker-read-token" not in str(client.provision_payloads)
    assert "docker-read-token" not in str(client.commit_payloads)
    assert "docker-read-token" not in translated_compose
    assert "docker-read-token" not in output


def test_deploy_adds_custom_registry_to_encrypted_phala_env(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
            "phala_docker_username": "alice-docker",
            "phala_docker_access_token": "docker-read-token",
            "phala_docker_registry": "ghcr.io",
        },
    )
    bundle = _write_minimal_bundle(tmp_path / "bundle")
    client = _CapturingPhalaClient()
    encrypted_inputs: list[list[tuple[str, str]]] = []

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, client),
    )
    monkeypatch.setattr(
        "cove_cli.deploy._encrypt_phala_env_vars",
        lambda env_vars, _public_key: encrypted_inputs.append(env_vars) or "encrypted-env",
    )

    deploy_workflow(
        "alice/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(instance_type="tdx.medium"),
    )

    assert client.provision_payloads[0]["env_keys"] == [
        "DSTACK_DOCKER_USERNAME",
        "DSTACK_DOCKER_PASSWORD",
        "DSTACK_DOCKER_REGISTRY",
    ]
    compose_file = client.provision_payloads[0]["compose_file"]
    assert isinstance(compose_file, dict)
    assert compose_file["allowed_envs"] == [
        "DSTACK_DOCKER_USERNAME",
        "DSTACK_DOCKER_PASSWORD",
        "DSTACK_DOCKER_REGISTRY",
    ]
    assert encrypted_inputs == [
        [
            ("DSTACK_DOCKER_USERNAME", "alice-docker"),
            ("DSTACK_DOCKER_PASSWORD", "docker-read-token"),
            ("DSTACK_DOCKER_REGISTRY", "ghcr.io"),
        ]
    ]


def test_deploy_docker_registry_flags_override_config(tmp_path, monkeypatch) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
            "phala_docker_username": "config-user",
            "phala_docker_access_token": "config-token",
            "phala_docker_registry": "ghcr.io",
        },
    )
    bundle = _write_minimal_bundle(tmp_path / "bundle")
    client = _CapturingPhalaClient()
    encrypted_inputs: list[list[tuple[str, str]]] = []

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, client),
    )
    monkeypatch.setattr(
        "cove_cli.deploy._encrypt_phala_env_vars",
        lambda env_vars, _public_key: encrypted_inputs.append(env_vars) or "encrypted-env",
    )

    deploy_workflow(
        "alice/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(
            instance_type="tdx.medium",
            docker_username="flag-user",
            docker_access_token="flag-token",
            docker_registry="registry.example.com",
        ),
    )

    assert encrypted_inputs == [
        [
            ("DSTACK_DOCKER_USERNAME", "flag-user"),
            ("DSTACK_DOCKER_PASSWORD", "flag-token"),
            ("DSTACK_DOCKER_REGISTRY", "registry.example.com"),
        ]
    ]


def test_deploy_rejects_partial_docker_registry_credentials_before_phala(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
            "phala_docker_username": "alice-docker",
        },
    )

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: pytest.fail("unexpected pull"))
    monkeypatch.setattr("cove_cli.deploy._create_phala_client", _unused_fake_client)

    with pytest.raises(DeployCommandError, match="username and access token"):
        deploy_workflow(
            "alice/demo",
            cove_home=cove_home,
            phala_options=PhalaDeployOptions(instance_type="tdx.medium"),
        )


def test_deploy_without_registry_credentials_preserves_payload_shape(
    tmp_path,
    monkeypatch,
) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )
    bundle = _write_minimal_bundle(tmp_path / "bundle")
    client = _CapturingPhalaClient()

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr(
        "cove_cli.deploy._create_phala_client",
        lambda api_key: _assert_api_key(api_key, client),
    )

    deploy_workflow(
        "alice/demo",
        cove_home=cove_home,
        phala_options=PhalaDeployOptions(instance_type="tdx.medium"),
    )

    assert "env_keys" not in client.provision_payloads[0]
    compose_file = client.provision_payloads[0]["compose_file"]
    assert isinstance(compose_file, dict)
    assert "allowed_envs" not in compose_file
    assert "env_keys" not in client.commit_payloads[0]
    assert "encrypted_env" not in client.commit_payloads[0]


def test_deploy_rejects_non_phala_platform(tmp_path, monkeypatch) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )
    bundle = _write_bundle_root(
        tmp_path / "bundle",
        workflow_text="""
cove_version: 1
workflow:
  id: demo
platform:
  provider: local
  runtime: docker
owners: {}
artifacts: {}
nodes:
  node_one:
    compose: nodes/node_one/compose.generated.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        compose_text="""
services:
  worker:
    image: example/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111
""".strip()
        + "\n",
    )

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr("cove_cli.deploy._create_phala_client", _unused_fake_client)

    with pytest.raises(DeployCommandError, match="workflow.platform.provider='phala'"):
        deploy_workflow(
            "alice/demo",
            cove_home=cove_home,
            phala_options=PhalaDeployOptions(instance_type="tdx.small"),
        )


def test_deploy_rejects_unsupported_compose_features(tmp_path, monkeypatch) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )
    bundle = _write_bundle_root(
        tmp_path / "bundle",
        workflow_text="""
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners: {}
artifacts: {}
nodes:
  node_one:
    compose: nodes/node_one/compose.generated.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        compose_text="""
services:
  worker:
    image: example/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111
    build:
      context: .
""".strip()
        + "\n",
    )

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr("cove_cli.deploy._create_phala_client", _unused_fake_client)

    with pytest.raises(DeployCommandError, match="unsupported keys: build"):
        deploy_workflow(
            "alice/demo",
            cove_home=cove_home,
            phala_options=PhalaDeployOptions(instance_type="tdx.small"),
        )


def test_deploy_allows_gpu_device_reservation_deploy_key(tmp_path) -> None:
    bundle = _write_bundle_root(
        tmp_path / "bundle",
        workflow_text="""
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners: {}
artifacts: {}
nodes:
  node_one:
    compose: nodes/node_one/compose.generated.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        compose_text="""
services:
  worker:
    image: example/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
""".strip()
        + "\n",
    )

    translated = yaml.safe_load(
        translated_node_deployment_compose_text(bundle, bundle.nodes[0])
    )

    assert translated["services"]["worker"]["deploy"] == {
        "resources": {
            "reservations": {
                "devices": [
                    {
                        "driver": "nvidia",
                        "count": "all",
                        "capabilities": ["gpu"],
                    }
                ]
            }
        }
    }


def test_deploy_rejects_relative_bind_sources(tmp_path, monkeypatch) -> None:
    cove_home = tmp_path / ".cove"
    _write_config(
        cove_home,
        {
            "covehub_server_url": "http://127.0.0.1:8000",
            "phala_cloud_api_key": "phala-api-key",
        },
    )
    bundle = _write_bundle_root(
        tmp_path / "bundle",
        workflow_text="""
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners: {}
artifacts: {}
nodes:
  node_one:
    compose: nodes/node_one/compose.generated.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        compose_text="""
services:
  worker:
    image: example/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111
    volumes:
    - type: bind
      source: ./runtime/cove
      target: /cove
      read_only: true
""".strip()
        + "\n",
    )

    monkeypatch.setattr("cove_cli.deploy.pull_workflow_bundle", lambda **_kwargs: bundle)
    monkeypatch.setattr("cove_cli.deploy._create_phala_client", _unused_fake_client)

    with pytest.raises(DeployCommandError, match="unsupported relative bind source"):
        deploy_workflow(
            "alice/demo",
            cove_home=cove_home,
            phala_options=PhalaDeployOptions(instance_type="tdx.small"),
        )


def _write_minimal_bundle(bundle_root: Path) -> MaterializedWorkflowBundle:
    return _write_bundle_root(
        bundle_root,
        workflow_text="""
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners: {}
artifacts: {}
nodes:
  node_one:
    compose: nodes/node_one/compose.generated.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        compose_text="""
services:
  worker:
    image: example/worker@sha256:1111111111111111111111111111111111111111111111111111111111111111
""".strip()
        + "\n",
    )


class _CapturingPhalaClient:
    def __init__(self) -> None:
        self.provision_payloads: list[dict[str, object]] = []
        self.commit_payloads: list[dict[str, object]] = []

    def provision_cvm(self, payload: dict[str, object]):
        self.provision_payloads.append(payload)
        suffix = len(self.provision_payloads)
        return {
            "app_id": f"app-{suffix}",
            "compose_hash": f"compose-{suffix}",
            "app_env_encrypt_pubkey": f"app-env-pubkey-{suffix}",
        }

    def commit_cvm_provision(self, payload: dict[str, object]):
        self.commit_payloads.append(payload)
        app_id = payload["app_id"]
        return {"id": f"cvm-{app_id}", "status": "pending"}

    def get_cvm_list(self, request: dict[str, object]):
        return _empty_cvm_list()

    def delete_cvm(self, payload: dict[str, object]) -> None:
        raise AssertionError(f"unexpected delete_cvm call: {payload}")

    def close(self) -> None:
        return None


def _copy_hello_world_workflow(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[2] / "demos" / "hello_world" / "workflow"
    target = tmp_path / "workflow"
    shutil.copytree(source, target)
    certs_dir = target / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    write_test_certificate(
        certs_dir / "alice.pem",
        dns_names=["localhost"],
        ip_addresses=["127.0.0.1"],
    )
    write_test_certificate(
        certs_dir / "bob.pem",
        dns_names=["localhost"],
        ip_addresses=["127.0.0.1"],
    )
    return target


def _write_config(cove_home: Path, payload: dict[str, object]) -> None:
    config_path = config_path_for_home(cove_home)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_bundle_root(
    bundle_root: Path,
    *,
    workflow_text: str,
    compose_text: str,
) -> MaterializedWorkflowBundle:
    compose_path = bundle_root / "nodes" / "node_one" / "compose.generated.yaml"
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text(compose_text, encoding="utf-8")
    (bundle_root / "workflow.normalized.cove.yaml").write_text(workflow_text, encoding="utf-8")
    return MaterializedWorkflowBundle(
        publisher=PUBLISHER_DOMAIN,
        workflow_id="demo",
        manifest_hash="sha256:" + ("0" * 64),
        root_path=bundle_root,
        owners={"carol": CAROL_OWNER_URL},
        files=[],
        nodes=[
            MaterializedNode(
                node_id="node_one",
                compose_path="nodes/node_one/compose.generated.yaml",
                compose_hash=reviewed_compose_hash(yaml.safe_load(compose_text)),
                artifact_provisioner_image=None,
                artifact_provisioner_digest=None,
                artifacts=[],
                runtime_skeleton=[],
            )
        ],
    )


def _has_bind_mount(service: dict[str, object], target: str) -> bool:
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        return False
    return any(
        isinstance(volume, dict)
        and volume.get("type") == "bind"
        and volume.get("target") == target
        for volume in volumes
    )


def _sha256_literal(payload: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _unused_fake_client(_api_key: str):
    class Client:
        def provision_cvm(self, payload):
            raise AssertionError(f"unexpected provision_cvm call: {payload}")

        def commit_cvm_provision(self, payload):
            raise AssertionError(f"unexpected commit_cvm_provision call: {payload}")

        def close(self) -> None:
            return None

    return Client()


def _empty_cvm_list() -> dict[str, object]:
    return {
        "items": [],
        "total": 0,
        "page": 1,
        "page_size": 100,
        "pages": 1,
    }


def _assert_api_key(api_key: str, client):
    assert api_key == "phala-api-key"
    return client
