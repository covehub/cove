from __future__ import annotations

from typing import Any


class JsonLogicError(ValueError):
    """Raised when a JsonLogic expression is unsupported or invalid."""


def evaluate_jsonlogic(expression: Any, context: dict[str, Any]) -> Any:
    if isinstance(expression, dict):
        if set(expression.keys()) == {"var"}:
            raw_path = expression["var"]
            if not isinstance(raw_path, str) or not raw_path:
                raise JsonLogicError("'var' must be a non-empty string")
            return _lookup_var(context, raw_path)
        if set(expression.keys()) == {"=="}:
            operands = expression["=="]
            if not isinstance(operands, list) or len(operands) != 2:
                raise JsonLogicError("'==' must be a 2-item list")
            return evaluate_jsonlogic(operands[0], context) == evaluate_jsonlogic(
                operands[1],
                context,
            )
        if set(expression.keys()) == {"and"}:
            operands = expression["and"]
            if not isinstance(operands, list):
                raise JsonLogicError("'and' must be a list")
            return all(bool(evaluate_jsonlogic(operand, context)) for operand in operands)
        raise JsonLogicError(
            f"unsupported JsonLogic operator(s): {', '.join(sorted(expression.keys()))}"
        )

    if isinstance(expression, list):
        return [evaluate_jsonlogic(item, context) for item in expression]

    return expression


def _lookup_var(context: dict[str, Any], raw_path: str) -> Any:
    current: Any = context
    for part in raw_path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise JsonLogicError(f"missing variable path: {raw_path}")
        current = current[part]
    return current
