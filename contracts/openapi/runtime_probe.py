#!/usr/bin/env python3
"""Print the serialized OpenAPI spec of the app rooted at the current cwd.

Fresh-process probe for the contract gates: run it with cwd at a marvisx
checkout or at a public/shared payload root. It pins the machine-dependent
default the app bakes into its schema (see contracts/openapi/generate.py),
imports the app from cwd, and prints the spec serialized exactly like the
committed baselines, so callers can compare byte-for-byte.
"""
from __future__ import annotations

import json
import os
import sys

# Must be set before core.api is imported: bench.py reads it at module import
# time into a Pydantic schema default (machine-independence contract).
os.environ["MARVIS_WORKSPACE_ROOT"] = "/workspace"
sys.path.insert(0, os.getcwd())

from core.api.main import app  # noqa: E402


def main() -> int:
    spec = app.openapi()
    sys.stdout.write(
        json.dumps(spec, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
