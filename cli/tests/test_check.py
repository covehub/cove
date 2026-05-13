from __future__ import annotations

import socket
from pathlib import Path

from cove_cli.check import check_workflow
from cove_cli.cli import run

from .support import MockCovehubServer, write_test_certificate


ALICE_DOMAIN = "cove-demo-hello-world-alice-provisioning.covehub.io"
ALICE_OWNER_URL = f"https://{ALICE_DOMAIN}"


def test_check_uses_default_workflow_path(tmp_path, monkeypatch, capsys) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    cove_home = _write_local_config(tmp_path, "http://127.0.0.1:1")

    monkeypatch.chdir(tmp_path)
    exit_code = run(["--cove-home", str(cove_home), "check"])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert str(workflow_path) in captured.out


def test_check_rejects_malformed_yaml(tmp_path) -> None:
    workflow_path = tmp_path / "workflow.cove.yaml"
    workflow_path.write_text("workflow:\n  id: [\n", encoding="utf-8")

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("failed to parse workflow YAML" in error for error in report.errors)


def test_check_rejects_missing_compose_files(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    workflow_text = workflow_path.read_text(encoding="utf-8").replace(
        "compose: nodes/node_one.compose.yaml",
        "compose: nodes/missing.compose.yaml",
    )
    workflow_path.write_text(workflow_text, encoding="utf-8")

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("nodes.node_one.compose file not found" in error for error in report.errors)


def test_check_rejects_missing_compose_service_keys(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    (tmp_path / "nodes" / "node_one.compose.yaml").write_text(
        "services:\n  different:\n    image: demo\n",
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any(
        "nodes.node_one.services.worker is not defined" in error
        for error in report.errors
    )


def test_check_rejects_unresolved_references(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8")
        .replace("owner: alice", "owner: missing_owner")
        .replace("input_a", "missing_input", 1),
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("owner must reference a declared owner" in error for error in report.errors)


def test_check_rejects_workflow_output_artifacts(tmp_path) -> None:
    workflow_path = tmp_path / "workflow.cove.yaml"
    _write_compose_file(tmp_path / "nodes" / "node_one.compose.yaml", ["worker"])
    _write_owner_cert(tmp_path, "alice")
    _write_schema_file(tmp_path)
    workflow_path.write_text(
        """
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners:
  alice: https://cove-demo-hello-world-alice-provisioning.covehub.io
artifacts:
  output_a:
    type: workflow_output
    owner: alice
nodes:
  node_one:
    compose: nodes/node_one.compose.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("workflow_output" in error for error in report.errors)


def test_check_accepts_dynamic_outputs(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            '    plaintext_hash: "{digest}"\n'.format(digest=_digest_a()),
            '    plaintext_hash: "{digest}"\n'
            "  output_a:\n"
            "    type: dynamic\n"
            "    owner: alice\n"
            "    hub_path: runtime/demo/artifacts/output_a/latest\n".format(digest=_digest_a()),
        ).replace(
            "        custom_certificate_field:\n          schema: schemas/result.json\n",
            "        outputs:\n          /workspace/output/out.txt: output_a\n"
            "        custom_certificate_field:\n          schema: schemas/result.json\n",
        ),
        encoding="utf-8",
    )

    with MockCovehubServer() as server:
        server.seed_artifact(f"v1/artifacts/{ALICE_DOMAIN}/input_a/latest")
        cove_home = _write_local_config(tmp_path, server.url)
        report = check_workflow(workflow_path, cove_home=cove_home)

    assert report.ok


def test_check_rejects_static_artifacts_in_outputs(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            "custom_certificate_field:\n          schema: schemas/result.json\n"
            '        preconditions:\n          "==":\n'
            "            - { var: inputs.input_a.plaintext_hash }\n"
            f'            - "{_digest_a()}"\n',
            "outputs:\n          /workspace/output/out.txt: input_a\n"
            "        custom_certificate_field:\n          schema: schemas/result.json\n"
            '        preconditions:\n          "==":\n'
            "            - { var: inputs.input_a.plaintext_hash }\n"
            f'            - "{_digest_a()}"\n',
        ),
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("must reference a dynamic artifact" in error for error in report.errors)


def test_check_rejects_dynamic_artifact_with_bad_hub_path(tmp_path) -> None:
    workflow_path = tmp_path / "workflow.cove.yaml"
    _write_compose_file(tmp_path / "nodes" / "node_one.compose.yaml", ["worker"])
    _write_owner_cert(tmp_path, "alice")
    _write_schema_file(tmp_path)
    workflow_path.write_text(
        """
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners:
  alice: https://cove-demo-hello-world-alice-provisioning.covehub.io
artifacts:
  output_a:
    type: dynamic
    owner: alice
    hub_path: runtime/demo/artifacts/wrong_name/latest
nodes:
  node_one:
    compose: nodes/node_one.compose.yaml
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("hub_path must match 'runtime/demo/artifacts/output_a/latest'" in error for error in report.errors)


def test_check_rejects_dynamic_artifact_duplicate_producers(tmp_path) -> None:
    workflow_path = tmp_path / "workflow.cove.yaml"
    _write_compose_file(tmp_path / "nodes" / "node_one.compose.yaml", ["worker"])
    _write_compose_file(tmp_path / "nodes" / "node_two.compose.yaml", ["worker"])
    _write_owner_cert(tmp_path, "alice")
    _write_schema_file(tmp_path)
    workflow_path.write_text(
        """
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners:
  alice: https://cove-demo-hello-world-alice-provisioning.covehub.io
artifacts:
  output_a:
    type: dynamic
    owner: alice
    hub_path: runtime/demo/artifacts/output_a/latest
nodes:
  node_one:
    compose: nodes/node_one.compose.yaml
    services:
      worker:
        outputs:
          /workspace/output/output_a.txt: output_a
        custom_certificate_field:
          schema: schemas/result.json
  node_two:
    compose: nodes/node_two.compose.yaml
    services:
      worker:
        outputs:
          /workspace/output/output_a.txt: output_a
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("dynamic artifact 'output_a' is produced by multiple services" in error for error in report.errors)


def test_check_rejects_dynamic_consumer_without_producer_dependency(tmp_path) -> None:
    workflow_path = tmp_path / "workflow.cove.yaml"
    _write_compose_file(tmp_path / "nodes" / "producer.compose.yaml", ["worker"])
    _write_compose_file(tmp_path / "nodes" / "consumer.compose.yaml", ["worker"])
    _write_owner_cert(tmp_path, "alice")
    _write_schema_file(tmp_path)
    workflow_path.write_text(
        """
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners:
  alice: https://cove-demo-hello-world-alice-provisioning.covehub.io
artifacts:
  input_a:
    type: static
    owner: alice
    hub_path: v1/artifacts/alice/input_a/latest
    plaintext_hash: "sha256:099bb9889d54300508225dafa9e26ed3454352408d818d2b4712f16446242896"
  output_a:
    type: dynamic
    owner: alice
    hub_path: runtime/demo/artifacts/output_a/latest
nodes:
  producer:
    compose: nodes/producer.compose.yaml
    services:
      worker:
        inputs:
          /workspace/input/input_a.txt: input_a
        outputs:
          /workspace/output/output_a.txt: output_a
        custom_certificate_field:
          schema: schemas/result.json
        preconditions:
          "==":
            - { var: inputs.input_a.plaintext_hash }
            - "sha256:099bb9889d54300508225dafa9e26ed3454352408d818d2b4712f16446242896"
  consumer:
    compose: nodes/consumer.compose.yaml
    services:
      worker:
        inputs:
          /workspace/input/output_a.txt: output_a
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any(
        "node 'consumer' consumes dynamic artifact 'output_a' but does not declare producer node 'producer' in dependencies"
        in error
        for error in report.errors
    )


def test_check_detects_dependency_cycles(tmp_path) -> None:
    workflow_path = tmp_path / "workflow.cove.yaml"
    _write_compose_file(tmp_path / "nodes" / "node_one.compose.yaml", ["worker"])
    _write_compose_file(tmp_path / "nodes" / "node_two.compose.yaml", ["worker"])
    _write_schema_file(tmp_path)
    workflow_path.write_text(
        """
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
    compose: nodes/node_one.compose.yaml
    dependencies: [node_two]
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
  node_two:
    compose: nodes/node_two.compose.yaml
    dependencies: [node_one]
    services:
      worker:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("dependency cycle detected" in error for error in report.errors)


def test_check_does_not_require_local_config_for_static_artifacts(tmp_path, monkeypatch) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    report = check_workflow(workflow_path)

    assert report.ok


def test_check_verifies_static_artifact_existence(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())

    with MockCovehubServer() as server:
        server.seed_artifact(f"v1/artifacts/{ALICE_DOMAIN}/input_a/latest")
        cove_home = _write_local_config(tmp_path, server.url)
        report = check_workflow(workflow_path, cove_home=cove_home)

    assert report.ok
    assert report.warnings == []


def test_check_does_not_fetch_missing_static_artifacts(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())

    with MockCovehubServer() as server:
        cove_home = _write_local_config(tmp_path, server.url)
        report = check_workflow(workflow_path, cove_home=cove_home)

    assert report.ok


def test_check_does_not_require_reachable_server(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    cove_home = _write_local_config(tmp_path, f"http://127.0.0.1:{_unused_port()}")

    report = check_workflow(workflow_path, cove_home=cove_home)

    assert report.ok


def test_check_rejects_legacy_owner_provisioning_fields(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    cert_path = tmp_path / "certs" / "alice.pem"
    write_test_certificate(cert_path, dns_names=["wrong.example.test"])
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            "  alice: https://cove-demo-hello-world-alice-provisioning.covehub.io\n",
            "  alice:\n"
            "    provisioning_url: https://example.test\n"
            "    provisioning_tls_certificate: certs/alice.pem\n",
        ),
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("owners.alice.provisioning_url is no longer supported" in error for error in report.errors)
    assert any("owners.alice.provisioning_tls_certificate is no longer supported" in error for error in report.errors)
    assert any("owners.alice must be an HTTPS owner URL string" in error for error in report.errors)


def test_check_requires_plaintext_hash_on_static_artifacts(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a())
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8").replace(
            f'    plaintext_hash: "{_digest_a()}"\n',
            "",
        ),
        encoding="utf-8",
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any(".plaintext_hash must be a sha256:<hex> string" in error for error in report.errors)


def test_check_warns_when_node_has_static_inputs_without_preconditions(
    tmp_path,
) -> None:
    workflow_path = _write_minimal_workflow(tmp_path, digest=_digest_a(), include_preconditions=False)

    with MockCovehubServer() as server:
        server.seed_artifact(f"v1/artifacts/{ALICE_DOMAIN}/input_a/latest")
        cove_home = _write_local_config(tmp_path, server.url)
        report = check_workflow(workflow_path, cove_home=cove_home)

    assert not report.ok
    assert any(
        "consumes static inputs but none of its services declare preconditions"
        in warning
        for warning in report.warnings
    )
    assert any("must be hash-anchored" in error for error in report.errors)


def test_check_errors_when_static_input_is_not_hash_anchored(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(
        tmp_path,
        digest=_digest_a(),
        preconditions_text='        preconditions:\n          "==":\n            - { var: node.id }\n            - "node_one"\n',
    )

    with MockCovehubServer() as server:
        server.seed_artifact(f"v1/artifacts/{ALICE_DOMAIN}/input_a/latest")
        cove_home = _write_local_config(tmp_path, server.url)
        report = check_workflow(workflow_path, cove_home=cove_home)

    assert not report.ok
    assert any("must be hash-anchored" in error for error in report.errors)


def test_check_errors_when_hash_anchor_literal_mismatches_artifact(tmp_path) -> None:
    workflow_path = _write_minimal_workflow(
        tmp_path,
        digest=_digest_a(),
        preconditions_text='        preconditions:\n          "==":\n            - { var: inputs.input_a.plaintext_hash }\n            - "sha256:ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"\n',
    )

    with MockCovehubServer() as server:
        server.seed_artifact(f"v1/artifacts/{ALICE_DOMAIN}/input_a/latest")
        cove_home = _write_local_config(tmp_path, server.url)
        report = check_workflow(workflow_path, cove_home=cove_home)

    assert not report.ok
    assert any("must be hash-anchored" in error for error in report.errors)


def test_check_rejects_reserved_compiler_surfaces(tmp_path) -> None:
    workflow_path = _write_policy_workflow(
        tmp_path,
        service_name="cove_worker",
        compose_service_block="""
  cove_worker:
    image: registry.example/cove-artifact-provisioner@sha256:1111111111111111111111111111111111111111111111111111111111111111
    environment:
      COVE_CONFIG_JSON: bad
    network_mode: host
    volumes:
      - ./runtime:/workspace/runtime
      - ./data:/cove
""".strip("\n"),
    )

    report = check_workflow(workflow_path)

    assert not report.ok
    assert any("reserved compiler-managed prefix" in error for error in report.errors)
    assert any("reserved environment variable 'COVE_CONFIG_JSON'" in error for error in report.errors)
    assert any("blocked Compose field 'network_mode'" in error for error in report.errors)
    assert any("compiler-managed source './runtime'" in error for error in report.errors)
    assert any("compiler-managed target '/cove'" in error for error in report.errors)
    assert any("reserved first-party sidecar image" in error for error in report.errors)


def test_check_warns_on_quote_channel_hints(tmp_path) -> None:
    workflow_path = _write_policy_workflow(
        tmp_path,
        service_name="worker",
        compose_service_block="""
  worker:
    image: example/demo@sha256:1111111111111111111111111111111111111111111111111111111111111111
    environment:
      QUOTE_HINT: /var/run/dstack.sock
""".strip("\n"),
    )

    report = check_workflow(workflow_path)

    assert report.ok
    assert any("/var/run/dstack.sock" in warning for warning in report.warnings)


def _write_minimal_workflow(
    root: Path,
    *,
    digest: str,
    include_preconditions: bool = True,
    preconditions_text: str | None = None,
) -> Path:
    workflow_path = root / "workflow.cove.yaml"
    _write_compose_file(root / "nodes" / "node_one.compose.yaml", ["worker"])
    _write_owner_cert(root, "alice")
    _write_schema_file(root)
    if preconditions_text is None:
        if include_preconditions:
            preconditions_text = (
                '        preconditions:\n'
                '          "==":\n'
                "            - { var: inputs.input_a.plaintext_hash }\n"
                f'            - "{digest}"\n'
            )
        else:
            preconditions_text = ""
    workflow_path.write_text(
        (
            """
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners:
  alice: https://cove-demo-hello-world-alice-provisioning.covehub.io
artifacts:
  input_a:
    type: static
    owner: alice
    hub_path: v1/artifacts/alice/input_a/latest
    plaintext_hash: "{digest}"
nodes:
  node_one:
    compose: nodes/node_one.compose.yaml
    services:
      worker:
        inputs:
          /workspace/input/input_a.txt: input_a
        custom_certificate_field:
          schema: schemas/result.json
{preconditions_text}"""
        ).format(digest=digest, preconditions_text=preconditions_text).strip()
        + "\n",
        encoding="utf-8",
    )
    return workflow_path


def _write_policy_workflow(
    root: Path,
    *,
    service_name: str,
    compose_service_block: str,
) -> Path:
    workflow_path = root / "workflow.cove.yaml"
    compose_path = root / "nodes" / "node_one.compose.yaml"
    compose_path.parent.mkdir(parents=True, exist_ok=True)
    compose_path.write_text(f"services:\n{compose_service_block}\n", encoding="utf-8")
    _write_schema_file(root)
    workflow_path.write_text(
        f"""
cove_version: 1
workflow:
  id: demo
platform:
  provider: phala
  runtime: dstack
owners: {{}}
artifacts: {{}}
nodes:
  node_one:
    compose: nodes/node_one.compose.yaml
    services:
      {service_name}:
        custom_certificate_field:
          schema: schemas/result.json
""".strip()
        + "\n",
        encoding="utf-8",
    )
    return workflow_path


def _write_compose_file(path: Path, services: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    service_block = "\n".join(
        f"  {service_name}:\n    image: demo" for service_name in services
    )
    path.write_text(f"services:\n{service_block}\n", encoding="utf-8")


def _write_owner_cert(root: Path, owner_name: str) -> None:
    cert_path = root / "certs" / f"{owner_name}.pem"
    write_test_certificate(
        cert_path,
        dns_names=["example.test"],
    )


def _write_schema_file(root: Path) -> None:
    schema_path = root / "schemas" / "result.json"
    schema_path.parent.mkdir(parents=True, exist_ok=True)
    schema_path.write_text(
        '{\n  "$schema": "https://json-schema.org/draft/2020-12/schema",\n'
        '  "type": "object"\n}\n',
        encoding="utf-8",
    )


def _write_local_config(root: Path, server_url: str) -> Path:
    cove_home = root / ".cove"
    config_path = cove_home / "config.yaml"
    cove_home.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        f"covehub_server_url: {server_url}\n",
        encoding="utf-8",
    )
    return cove_home


def _unused_port() -> int:
    with socket.socket() as handle:
        handle.bind(("127.0.0.1", 0))
        return int(handle.getsockname()[1])


def _digest_a() -> str:
    return "sha256:099bb9889d54300508225dafa9e26ed3454352408d818d2b4712f16446242896"
