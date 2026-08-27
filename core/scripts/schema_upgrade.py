#!/usr/bin/env python3
"""Managed CLI wrapper for the receipt-backed schema transaction."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.api.runtime_settings import apply_marvis_settings
from core.api.services.schema_upgrade import (
    receipt_as_json,
    restore_controlled_upgrade,
    run_controlled_upgrade,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marvis-schema-upgrade")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("upgrade", "restore"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument("--release-id", required=True)
        subparser.add_argument("--proof-kind", required=True)
        subparser.add_argument("--receipt", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    apply_marvis_settings(force=True)
    kwargs = {
        "proof_kind": args.proof_kind,
        "receipt_path": args.receipt,
    }
    if args.command == "upgrade":
        receipt = run_controlled_upgrade(args.release_id, **kwargs)
    else:
        receipt = restore_controlled_upgrade(args.release_id, **kwargs)
    print(receipt_as_json(receipt), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"schema transaction refused: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
