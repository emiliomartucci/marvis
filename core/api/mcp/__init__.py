# v1.0.0 - 2026-05-27 - S1 F0: MCP package stub (Python MCP server lands in S1 F3)
"""Python MCP surface for the collapsed single-process runtime.

F0 ships only the error adapter (``_adapter.raise_mcp_error``). The full FastMCP
server that exposes the 91 tools (``server.py`` + ``tools/``) lands in S1 F3.
This package does NOT import ``fastapi``; the ``mcp`` SDK is needed only at tool
call time (``raise_mcp_error`` imports ``ToolError`` function-locally).
"""
