#!/usr/bin/env python3
"""Run the no-TEE GPU benchmark node sequence."""

from __future__ import annotations

import sys
from pathlib import Path


DEMO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_ROOT / "runner"))

from benchmark import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["run-sequence", *sys.argv[1:]]))
