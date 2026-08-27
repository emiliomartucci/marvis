# v1.0.0 - 2026-05-27 - S1 F3.1d: check_safety MCP tool (use_cases-direct, no HTTP, no subprocess)
"""Safety-preflight MCP tool — port of the Node ``check_safety`` tool, IMPORT-direct.

The Node ``check_safety`` SHELLS ``python3 scripts/safety_bridge.py check
--action-type <t> [--file-path ...] [--command ...] [--cwd ...]`` and parses the
``{allowed, reason, rule}`` JSON it prints. ``core/scripts/safety_bridge.py`` is
pure stdlib (``argparse`` / ``json`` / ``re`` / ``shlex`` / ``subprocess`` for the
git/npm checks) — it has ZERO fastapi and ZERO Marvis-app dependency. So the in-process
port imports the bridge's ``evaluate_action`` (the exact function ``_run_check``
calls) DIRECTLY: no subprocess, no ``python3`` re-exec, no HTTP. The returned
``Decision`` is mapped to the SAME ``{allowed, reason, rule}`` dict shape the Node
``checkSafety`` returns (``safety_bridge._run_check``'s output), so the behaviour is
byte-identical — minus the subprocess fork.

There is no DB and no use_case for this tool (the safety bridge is a Constitution
preflight, not a domain entity), so the tool body neither acquires a connection nor
calls ``use_cases`` — it computes the decision in-process. ``evaluate_action`` is
fail-CLOSED by contract: ``_run_check`` catches any exception and denies with
``rule='bridge-error'``; that envelope is reproduced here so a config/parse failure
returns ``{allowed: False, ...}`` rather than raising.

The bridge module is imported FUNCTION-LOCAL inside the tool. It lives under
``core/scripts/`` (a scripts dir, not a package on the app import path); resolving it
at import time would couple the MCP module-import to ``scripts`` being importable.
Importing it lazily inside the call keeps the tool registration cheap and the import
graph clean (the smoke test's tool-registration assertions don't need the bridge).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any, Literal

# Resolve core/scripts/safety_bridge.py relative to this file
# (core/api/mcp/tools/safety.py -> parents[3] == core/).
_SAFETY_BRIDGE_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "safety_bridge.py"
)

SafetyActionType = Literal["file_write", "bash_command"]


def _load_safety_bridge():
    """Load ``core/scripts/safety_bridge.py`` as a module (it is not on sys.path).

    The bridge is a standalone CLI script under ``core/scripts/`` (not a package
    module), so it is loaded by file path. fastapi-free + stdlib-only — importing it
    pulls nothing into the MCP runtime beyond the stdlib.
    """
    name = "_marvis_safety_bridge"
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, _SAFETY_BRIDGE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"Cannot load safety bridge at {_SAFETY_BRIDGE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register BEFORE exec: the bridge defines an `@dataclass` (Decision) whose
    # processing looks up `cls.__module__` in sys.modules — exec_module would
    # crash with AttributeError if the module isn't registered first. Caching also
    # avoids re-exec on every tool call.
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(name, None)
        raise
    return module


def register(mcp) -> None:
    """Register the safety-preflight tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def check_safety(
        action_type: SafetyActionType,
        file_path: str | None = None,
        command: str | None = None,
        cwd: str | None = None,
    ) -> dict[str, Any]:
        """Validate file-write / bash-command against MarvisX Constitution BEFORE executing it.

        QUANDO USARLO: su provider senza native PreToolUse hook (Codex, Gemini, OpenClaw container) — mirror dell'enforcement che Claude Code ha gratis via hook.
        QUANDO NON USARLO: NOT su Claude Code con hook attivi — e' ridondante. NOT come sostituto della Constitution (e' un preflight, non un substitute).
        RESTITUISCE: {allowed:bool, reason, rule}."""
        # In-process port of `safety_bridge check`: import the stdlib bridge and call
        # the SAME `evaluate_action` the Node subprocess invokes. Map the Decision to
        # the {allowed, reason, rule} dict shape Node's checkSafety returns. Fail
        # CLOSED on any error (parity with _run_check).
        try:
            bridge = _load_safety_bridge()
            decision = bridge.evaluate_action(
                action_type,
                file_path=file_path,
                command=command,
                cwd=cwd,
            )
            return {
                "allowed": decision.allowed,
                "reason": decision.reason,
                "rule": decision.rule,
            }
        except Exception as exc:  # noqa: BLE001 — fail-closed, mirror _run_check
            return {
                "allowed": False,
                "reason": f"Safety bridge failure: {exc}",
                "rule": "bridge-error",
            }
