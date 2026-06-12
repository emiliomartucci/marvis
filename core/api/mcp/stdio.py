"""Console entrypoint for the Marvis MCP stdio server."""
from __future__ import annotations

import argparse
import sys
from importlib.metadata import PackageNotFoundError, version
from typing import Sequence


def _version() -> str:
    try:
        return f"marvis-mcp {version('marvisx-cli')}"
    except PackageNotFoundError:
        return "marvis-mcp (dev)"


def main(argv: Sequence[str] | None = None) -> None:
    """Run the Marvis MCP server over stdio.

    The Claude Code plugin invokes this binary with ``--stdio``. ``--help`` and
    ``--version`` stay lightweight so hooks can verify PATH without starting the
    long-running MCP process.
    """
    parser = argparse.ArgumentParser(
        prog="marvis-mcp",
        description="Run the Marvis MCP server over stdio.",
    )
    parser.add_argument(
        "--stdio",
        action="store_true",
        help="Run the MCP server over stdio (default when no mode is supplied).",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="Print the installed Marvis MCP entrypoint version and exit.",
    )
    args = parser.parse_args(argv)

    if args.version:
        print(_version())
        return

    from core.api.mcp.server import main as run_server

    run_server(transport="stdio" if args.stdio else None)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
