#!/usr/bin/env python3
"""Repository-local entrypoint for the shared public-claims gate."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys


_IMPL = (
    Path(__file__).resolve().parents[1]
    / "core/scripts/quality-gates/verify_public_claims.py"
)
_SPEC = importlib.util.spec_from_file_location("marvis_public_claims", _IMPL)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("shared public-claims gate is not importable")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)

PublicClaimError = _MODULE.PublicClaimError
_check_text = _MODULE._check_text
verify_artifact = _MODULE.verify_artifact
verify_source = _MODULE.verify_source


if __name__ == "__main__":
    raise SystemExit(_MODULE.main())
