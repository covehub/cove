from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required environment variable: {name}")
    return value


def optional_env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def ensure_parent(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def read_secret_word(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def write_json(path: str, payload: dict[str, Any]) -> None:
    ensure_parent(path)
    Path(path).write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_text(path: str, payload: str) -> None:
    ensure_parent(path)
    Path(path).write_text(payload, encoding="utf-8")


def log(service: str, message: str) -> None:
    print(f"[{service}] {message}", flush=True)
