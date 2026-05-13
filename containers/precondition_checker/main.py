from __future__ import annotations

from cove_container_runtime.common import (
    RuntimeErrorBase,
    SidecarConfigError,
    load_inline_sidecar_context,
    log,
    read_json_file,
    required_mapping,
    required_string,
)
from cove_container_runtime.jsonlogic import JsonLogicError, evaluate_jsonlogic


def run(config: dict[str, object]) -> None:
    node_name = required_string(config, "node_name")
    service_name = required_string(config, "service_name")
    inputs = required_mapping(config.get("inputs"), "inputs")
    certificates = required_mapping(config.get("certificates") or {}, "certificates")
    preconditions = config.get("preconditions")

    if preconditions is None:
        log("precondition_checker", f"{node_name}.{service_name} has no preconditions")
        return

    input_context: dict[str, object] = {}
    for artifact_name, metadata_path in inputs.items():
        if not isinstance(metadata_path, str):
            raise SidecarConfigError(
                f"precondition input metadata path for {artifact_name} must be a string"
            )
        input_context[artifact_name] = read_json_file(metadata_path)

    certificate_context: dict[str, object] = {}
    for dependency_name, certificate_path in certificates.items():
        if not isinstance(certificate_path, str):
            raise SidecarConfigError(
                f"precondition certificate path for {dependency_name} must be a string"
            )
        certificate_context[dependency_name] = read_json_file(certificate_path)

    try:
        passed = bool(
            evaluate_jsonlogic(
                preconditions,
                {
                    "inputs": input_context,
                    "certificates": certificate_context,
                },
            )
        )
    except JsonLogicError as exc:
        raise RuntimeErrorBase(
            f"precondition evaluation failed for {node_name}.{service_name}: {exc}"
        ) from exc

    if not passed:
        raise RuntimeErrorBase(f"preconditions failed for {node_name}.{service_name}")

    log("precondition_checker", f"preconditions passed for {node_name}.{service_name}")


def main() -> int:
    try:
        run(load_inline_sidecar_context().config)
        return 0
    except (RuntimeErrorBase, SidecarConfigError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
