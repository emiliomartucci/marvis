"""Names, kill-switch and description of the tool gateway (no heavy imports).

``tool_profiles`` needs these at import time, and the root public gateway imports
``tool_profiles``; keeping them here stops that import from loading the tool
package and the database settings. The tools live in ``tools/tool_gateway.py``.
"""
from __future__ import annotations

import os
from collections.abc import Mapping

FIND_TOOL = "find_tool"
CALL_TOOL = "call_tool"
GATEWAY_TOOLS = frozenset({FIND_TOOL, CALL_TOOL})
_FALSEY = {"0", "false", "no", "off"}
_RESULT_DESCRIPTION_CHARS = 600
# Always loaded so a client with deferred loading (Claude Code) keeps the hidden
# names in context; the gateway is outside TOOL_TIERS because stdio lacks it.
_GATEWAY_META = {"anthropic/alwaysLoad": True, "marvis/tier": "gateway", "marvis/minRole": "viewer"}

FIND_TOOL_DESCRIPTION = (
    "Search the Marvis tools that are not listed here. Describe what you need in a "
    "few words or give a tool name, in any language. Returns up to 5 matching tools "
    "with name, description and input schema; run one with call_tool."
)
CALL_TOOL_DESCRIPTION = (
    "Run a Marvis tool that is not listed here, found with find_tool. "
    "name = the tool name, arguments = its input object."
)


def gateway_enabled(env: Mapping[str, str] | None = None) -> bool:
    """The gateway is ON unless ``MARVIS_TOOL_GATEWAY`` is explicitly falsey."""
    source = os.environ if env is None else env
    raw = source.get("MARVIS_TOOL_GATEWAY")
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def find_tool_description(hidden_names: list[str]) -> str:
    """``find_tool`` description naming the tools the diet hides for this caller."""
    if not hidden_names:
        return FIND_TOOL_DESCRIPTION
    return (
        f"More Marvis tools exist than are listed here: {', '.join(sorted(hidden_names))}. "
        f"{FIND_TOOL_DESCRIPTION}"
    )

