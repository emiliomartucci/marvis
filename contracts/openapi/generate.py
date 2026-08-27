#!/usr/bin/env python3
"""Deterministic OpenAPI baseline generator for the governed marvisx engine contract.

Part of U2 of the product-surface separation (docs/goals/2026-07-24-separate-surfaces.md).
The generated spec is the CONTRACT baseline: the drift gate
(tests/contracts/test_openapi_baseline.py + .github/workflows/openapi-diff.yml)
regenerates in-process and fails on any unintended change to the HTTP surface.

Determinism (verified): the route set on origin/main has NO env-conditional
`include_router`, so `app.openapi()` is byte-stable given fixed dependencies —
two clean-env runs are byte-identical. The spec IS coupled to the resolved
FastAPI/pydantic versions, so regenerate under the same dep set the CI gate
installs: requirements-tenant.txt (the manifest the production API image
installs — requirements.txt cannot even import the app: it misses
email-validator and pygit2).

Machine-independence: BenchRequest.cwd bakes MARVIS_WORKSPACE_ROOT (or
$HOME/workspace) into its schema default, so an unpinned run embeds the
generating machine's filesystem layout in the contract. This module pins the
variable to a canonical value before the app is imported; regenerate through
this script only, never by calling app.openapi() directly.

Profile (ratified by the owner 2026-07-27 — KTD3/R6/R7): the governed contract
covers all public, discoverable routes, optional routers included (profile:
all-surfaces in contracts/openapi/VERSION). Historical browser file-mutation
routes are intentionally agent-only and registered with ``include_in_schema=False``;
they remain runtime-compatible but are not a public discovery contract. If a
conditional public router is introduced, enable it in the generation profile
(env pins in this module) so the baseline keeps covering it — never let a public
surface silently drop out of the contract.

Usage:
  python contracts/openapi/generate.py           # (re)write the committed baseline
  python contracts/openapi/generate.py --check    # exit 1 if regen != committed
"""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE = REPO_ROOT / "contracts" / "openapi" / "marvisx.json"

# Must be set before core.api is imported: bench.py reads it at module import
# time into a Pydantic schema default (see docstring, machine-independence).
CANONICAL_WORKSPACE_ROOT = "/workspace"
os.environ["MARVIS_WORKSPACE_ROOT"] = CANONICAL_WORKSPACE_ROOT


def build_spec() -> dict:
    # Import lazily and with the repo root on sys.path so the generator runs from
    # any cwd (CI, worktree, pytest).
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from core.api.main import app  # noqa: PLC0415 — deferred by design

    # Pytest and other in-process consumers may have imported ``bench.py``
    # before this generator pinned MARVIS_WORKSPACE_ROOT.  FastAPI then keeps
    # that machine-specific default in its cached OpenAPI document.  Normalize
    # a copy of the generated contract so import order cannot leak a developer
    # home directory into committed bytes (and do not mutate the live cache).
    spec = deepcopy(app.openapi())
    bench_cwd = (
        spec.get("components", {})
        .get("schemas", {})
        .get("BenchRequest", {})
        .get("properties", {})
        .get("cwd")
    )
    if not isinstance(bench_cwd, dict):
        raise RuntimeError("OpenAPI contract is missing BenchRequest.cwd")
    bench_cwd["default"] = CANONICAL_WORKSPACE_ROOT
    return spec


def serialize(spec: dict) -> str:
    return json.dumps(spec, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    text = serialize(build_spec())

    if "--check" in argv:
        committed = BASELINE.read_text(encoding="utf-8") if BASELINE.exists() else ""
        if text != committed:
            sys.stderr.write(
                "openapi baseline DRIFT: the regenerated spec differs from the "
                "committed contracts/openapi/marvisx.json.\n"
                "Run `python contracts/openapi/generate.py` and commit the result "
                "(and confirm the change is intended — this is the contract gate).\n"
            )
            return 1
        print("openapi baseline: in sync with contracts/openapi/marvisx.json")
        return 0

    BASELINE.write_text(text, encoding="utf-8")
    spec = json.loads(text)
    paths = len(spec.get("paths", {}))
    ops = sum(
        1
        for methods in spec.get("paths", {}).values()
        for m in methods
        if m in ("get", "post", "put", "patch", "delete", "options", "head")
    )
    schemas = len(spec.get("components", {}).get("schemas", {}))
    print(
        f"wrote {BASELINE.relative_to(REPO_ROOT)} — openapi {spec.get('openapi')} "
        f"| paths {paths} | ops {ops} | schemas {schemas}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
