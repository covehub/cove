from __future__ import annotations

import json
from pathlib import Path

from cove_container_runtime.certificates import build_node_certificate
from cove_container_runtime.common import (
    RuntimeErrorBase,
    SidecarConfigError,
    http_put_bytes,
    join_url,
    load_inline_sidecar_context,
    load_named_json_entries,
    log,
    required_string,
    sha256_literal,
    write_json_file,
)


def _header_event_log(event_log: object) -> str:
    if isinstance(event_log, str):
        return event_log
    return json.dumps(event_log, sort_keys=True)


def run(config: dict[str, object], *, compose_hash: str | None = None) -> None:
    covehub_server_url = required_string(config, "covehub_server_url")
    workflow_publisher_domain = required_string(config, "workflow_publisher_domain")
    workflow_id = required_string(config, "workflow_id")
    node_name = required_string(config, "node_name")
    certificate_path = required_string(config, "certificate_path")
    if compose_hash is None:
        compose_hash = required_string(config, "generated_node_compose_hash")

    inputs = load_named_json_entries(config.get("inputs"), "inputs")
    keypairs = load_named_json_entries(config.get("ephemeral_keypairs"), "ephemeral_keypairs")
    results = load_named_json_entries(config.get("results"), "results")
    outputs = load_named_json_entries(config.get("outputs"), "outputs")
    dependencies = load_named_json_entries(config.get("dependencies"), "dependencies")

    certificate = build_node_certificate(
        workflow_id=workflow_id,
        node_name=node_name,
        generated_node_compose_hash=compose_hash,
        inputs=inputs,
        ephemeral_keypairs=keypairs,
        results=results,
        outputs=outputs,
        dependencies=dependencies,
        attestation_config=config.get("attestation")
        if isinstance(config.get("attestation"), dict)
        else None,
    )
    write_json_file(certificate_path, certificate)

    certificate_payload = Path(certificate_path).read_bytes()
    certificate_hash = sha256_literal(certificate_payload)
    upload_relative_path = (
        f"v1/runtime/{workflow_publisher_domain}/{workflow_id}/"
        f"certificates/{node_name}/{certificate_hash}"
    )
    http_put_bytes(
        url=join_url(covehub_server_url, upload_relative_path),
        payload=certificate_payload,
        headers={
            "Content-Type": "application/json",
            "X-TDX-Quote": certificate["attestation_bundle"]["quote"],
            "X-TDX-Event-Log": _header_event_log(
                certificate["attestation_bundle"].get("event_log")
            ),
            "X-Cove-Node-Id": node_name,
            "X-Cove-Compose-Hash": compose_hash,
        },
    )
    log("node_certificate_writer", f"wrote and uploaded certificate for {node_name}")


def main() -> int:
    try:
        context = load_inline_sidecar_context()
        run(context.config, compose_hash=context.compose_hash)
        return 0
    except (RuntimeErrorBase, SidecarConfigError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
