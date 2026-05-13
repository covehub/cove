from __future__ import annotations

from jsonschema import Draft202012Validator

from cove_container_runtime.common import (
    RuntimeErrorBase,
    SidecarConfigError,
    load_inline_sidecar_context,
    log,
    optional_string,
    read_json_file,
    required_mapping,
    required_string,
    write_json_file,
)


def run(config: dict[str, object]) -> None:
    node_name = required_string(config, "node_name")
    service_name = required_string(config, "service_name")
    result_output_path = required_string(config, "result_output_path")
    result_source_path = optional_string(config.get("result_source_path"), "result_source_path")
    schema = config.get("schema")

    if result_source_path is None:
        payload: dict[str, object] = {}
    else:
        payload = read_json_file(result_source_path)

    if schema is not None:
        schema = required_mapping(schema, "schema")
        validator = Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
        if errors:
            raise RuntimeErrorBase(
                "result payload for "
                f"{node_name}.{service_name} failed schema validation: {errors[0].message}"
            )

    write_json_file(result_output_path, payload)
    log("service_certificate_writer", f"wrote result for {node_name}.{service_name}")


def main() -> int:
    try:
        run(load_inline_sidecar_context().config)
        return 0
    except (RuntimeErrorBase, SidecarConfigError) as exc:
        print(f"ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
