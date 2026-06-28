#!/usr/bin/env python3
"""Collect no-TEE GPU benchmark records into JSON and CSV reports."""

from __future__ import annotations

import sys
from pathlib import Path


DEMO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEMO_ROOT / "runner"))

from benchmark import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["collect-report", *sys.argv[1:]]))
