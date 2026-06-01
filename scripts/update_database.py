#!/usr/bin/env python3
"""Incrementally update the local Compreg database with only new dates."""

from __future__ import annotations

import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from build_spb_database import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main(["--incremental", *sys.argv[1:]]))
