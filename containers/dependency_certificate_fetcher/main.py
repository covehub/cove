from __future__ import annotations

import time

from cove_container_runtime.certificates import verify_node_certificate
from cove_container_runtime.common import (
    RuntimeErrorBase,
    RuntimeMetricsRecorder,
    SidecarConfigError,
    http_get_json,
    join_url,
    load_inline_sidecar_context,
    log,
    required_mapping,
    required_string,
    runtime_metrics_recorder_from_env,
    runtime_phase,
    write_json_file,
)


def run(config: dict[str, object], *, metrics: RuntimeMetricsRecorder | None = None) -> None:
    node_name = required_string(config, "node_name")
    covehub_server_url = required_string(config, "covehub_server_url")
    workflow_publisher_domain = required_string(config, "workflow_publisher_domain")
    workflow_id = required_string(config, "workflow_id")
    timeout_seconds = _required_number(config.get("timeout_seconds"), "timeout_seconds")
    poll_interval_seconds = _required_number(
        config.get("poll_interval_seconds"),
        "poll_interval_seconds",
    )

    raw_dependencies = config.get("dependencies")
    if not isinstance(raw_dependencies, list):
        raise SidecarConfigError("dependencies must be a list")

    for raw_dependency in raw_dependencies:
        dependency = required_mapping(raw_dependency, "dependency")
        dependency_name = required_string(dependency, "node_name")
        certificate_path = required_string(dependency, "certificate_path")
        expected_workflow_id = required_string(dependency, "expected_workflow_id")
        expected_node_id = required_string(dependency, "expected_node_id")
        expected_generated_node_compose_hash = required_string(
            dependency,
            "expected_generated_node_compose_hash",
        )
        with runtime_phase(metrics, f"fetch_certificate_{dependency_name}_seconds"):
            certificate = _wait_for_runtime_certificate(
                covehub_server_url=covehub_server_url,
                workflow_publisher_domain=workflow_publisher_domain,
                workflow_id=workflow_id,
                dependency_name=dependency_name,
                expected_workflow_id=expected_workflow_id,
                expected_node_id=expected_node_id,
                expected_generated_node_compose_hash=expected_generated_node_compose_hash,
                timeout_seconds=timeout_seconds,
                poll_interval_seconds=poll_interval_seconds,
            )
        with runtime_phase(metrics, f"write_certificate_{dependency_name}_seconds"):
            write_json_file(certificate_path, certificate)
        log(
            "dependency_certificate_fetcher",
            f"{node_name} fetched certificate for dependency {dependency_name}",
        )


def _wait_for_runtime_certificate(
    *,
    covehub_server_url: str,
    workflow_publisher_domain: str,
    workflow_id: str,
    dependency_name: str,
    expected_workflow_id: str,
    expected_node_id: str,
    expected_generated_node_compose_hash: str,
    timeout_seconds: float,
    poll_interval_seconds: float,
) -> dict[str, object]:
    deadline = time.time() + timeout_seconds
    relative_path = (
        f"v1/runtime/{workflow_publisher_domain}/{workflow_id}/"
        f"certificates/{dependency_name}/latest"
    )
    while time.time() < deadline:
        try:
            certificate = http_get_json(
                url=join_url(covehub_server_url, relative_path),
                timeout=5.0,
            )
        except RuntimeErrorBase as exc:
            log(
                "dependency_certificate_fetcher",
                f"{dependency_name} certificate fetch not ready yet: {exc}",
            )
            time.sleep(poll_interval_seconds)
            continue

        try:
            return verify_node_certificate(
                certificate,
                expected_workflow_id=expected_workflow_id,
                expected_node_name=expected_node_id,
                expected_generated_node_compose_hash=expected_generated_node_compose_hash,
            )
        except RuntimeErrorBase as exc:
            log(
                "dependency_certificate_fetcher",
                f"{dependency_name} latest certificate not ready yet: {exc}",
            )
            time.sleep(poll_interval_seconds)
            continue

    raise RuntimeErrorBase(
        "timed out waiting for runtime certificate "
        f"for dependency {dependency_name}"
    )


def _required_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SidecarConfigError(f"{label} must be a number")
    return float(value)


def main() -> int:
    metrics: RuntimeMetricsRecorder | None = None
    try:
        context = load_inline_sidecar_context()
        metrics = runtime_metrics_recorder_from_env(
            service_name=context.service_name,
            role="dependency_certificate_fetcher",
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
