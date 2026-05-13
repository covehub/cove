from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Settings:
    data_root: Path
    host: str
    port: int
    quote_verifier_mode: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            data_root=_resolve_path(
                os.getenv("COVE_SERVER_DATA_ROOT"),
                PROJECT_ROOT / "data",
            ),
            host=os.getenv("COVE_SERVER_HOST", "127.0.0.1"),
            port=int(os.getenv("COVE_SERVER_PORT", "8000")),
            quote_verifier_mode=_resolve_quote_verifier_mode(
                os.getenv(
                    "COVE_SERVER_QUOTE_VERIFIER",
                    "phala_dstack",
                )
            ),
        )


def _resolve_path(raw_value: str | None, default: Path) -> Path:
    path = Path(raw_value).expanduser() if raw_value else default
    return path.resolve()


def _resolve_quote_verifier_mode(raw_value: str) -> str:
    normalized = raw_value.strip()
    if normalized != "phala_dstack":
        raise ValueError(
            "COVE_SERVER_QUOTE_VERIFIER must be 'phala_dstack'; mock verifier modes are not supported"
        )
    return normalized
