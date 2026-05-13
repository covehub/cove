from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .workflow import (
    WorkflowDefinition,
    anchored_input_literals,
    load_workflow_definition,
)
from .security_policy import evaluate_workflow_security_policy


@dataclass(frozen=True, slots=True)
class CheckReport:
    workflow_path: Path
    errors: list[str]
    warnings: list[str]

    @property
    def ok(self) -> bool:
        return not self.errors


def check_workflow(
    workflow_path: str | Path | None = None,
    *,
    cove_home: str | Path | None = None,
) -> CheckReport:
    load_result = load_workflow_definition(workflow_path)
    errors = list(load_result.errors)
    warnings: list[str] = []
    if load_result.workflow is None:
        return CheckReport(
            workflow_path=load_result.workflow_path,
            errors=errors,
            warnings=warnings,
        )

    workflow = load_result.workflow
    policy_report = evaluate_workflow_security_policy(workflow)
    errors.extend(policy_report.errors)
    warnings.extend(policy_report.warnings)
    _validate_input_hash_anchors(workflow, errors, warnings)

    return CheckReport(
        workflow_path=workflow.path,
        errors=errors,
        warnings=warnings,
    )


def format_report(report: CheckReport) -> str:
    lines: list[str] = []
    for warning in report.warnings:
        lines.append(f"WARNING: {warning}")
    for error in report.errors:
        lines.append(f"ERROR: {error}")

    if report.ok:
        lines.append(
            f"cove check passed for {report.workflow_path}"
            f" with {len(report.warnings)} warning(s)"
        )
    else:
        lines.append(
            f"cove check failed for {report.workflow_path}"
            f" with {len(report.errors)} error(s)"
            f" and {len(report.warnings)} warning(s)"
        )

    return "\n".join(lines)


def _validate_input_hash_anchors(
    workflow: WorkflowDefinition,
    errors: list[str],
    warnings: list[str],
) -> None:
    for node in workflow.nodes.values():
        node_has_static_inputs = any(
            any(workflow.artifacts[artifact_name].is_static for artifact_name in service.inputs.values())
            for service in node.services.values()
        )
        node_has_preconditions = any(
            service.preconditions is not None for service in node.services.values()
        )
        if node_has_static_inputs and not node_has_preconditions:
            warnings.append(
                f"node '{node.name}' consumes static inputs but none of its services declare preconditions"
            )

        for service in node.services.values():
            anchored_inputs = anchored_input_literals(service.preconditions)
            for artifact_name in service.inputs.values():
                artifact = workflow.artifacts[artifact_name]
                if not artifact.is_static:
                    continue
                anchored_literals = anchored_inputs.get(artifact_name, set())
                if artifact.plaintext_hash not in anchored_literals:
                    errors.append(
                        "service "
                        f"'{node.name}.{service.name}' input '{artifact_name}' "
                        "must be hash-anchored via "
                        f"inputs.{artifact_name}.plaintext_hash == "
                        f"\"{artifact.plaintext_hash}\" in preconditions"
                    )
