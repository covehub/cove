from __future__ import annotations

from jsonschema import Draft202012Validator

from cove_container_runtime.common import (
    RuntimeErrorBase,
    RuntimeMetricsRecorder,
    SidecarConfigError,
    load_inline_sidecar_context,
    log,
    optional_string,
    read_json_file,
    required_mapping,
    required_string,
    runtime_metrics_recorder_from_env,
    runtime_phase,
    write_json_file,
)


def run(config: dict[str, object], *, metrics: RuntimeMetricsRecorder | None = None) -> None:
    node_name = required_string(config, "node_name")
    service_name = required_string(config, "service_name")
    result_output_path = required_string(config, "result_output_path")
    result_source_path = optional_string(config.get("result_source_path"), "result_source_path")
    schema = config.get("schema")

    with runtime_phase(metrics, "load_result_seconds"):
        if result_source_path is None:
            payload: dict[str, object] = {}
        else:
            payload = read_json_file(result_source_path)

    if schema is not None:
        with runtime_phase(metrics, "validate_schema_seconds"):
            schema = required_mapping(schema, "schema")
            validator = Draft202012Validator(schema)
            errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
            if errors:
                raise RuntimeErrorBase(
                    "result payload for "
                    f"{node_name}.{service_name} failed schema validation: {errors[0].message}"
                )

    with runtime_phase(metrics, "write_result_seconds"):
        write_json_file(result_output_path, payload)
    log("service_certificate_writer", f"wrote result for {node_name}.{service_name}")


def main() -> int:
    metrics: RuntimeMetricsRecorder | None = None
    try:
        context = load_inline_sidecar_context()
        metrics = runtime_metrics_recorder_from_env(
            service_name=context.service_name,
            role="service_certificate_writer",
        )
        run(context.config, metrics=metrics)
        metrics.write(exit_code=0)
        return 0
    except (RuntimeErrorBase, SidecarConfigError) as exc:
        if metrics is not None:
            metrics.write(exit_code=1, error=str(exc))
        print(f"ERROR: {exc}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
