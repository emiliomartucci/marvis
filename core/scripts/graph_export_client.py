#!/usr/bin/env python3
# v1.0.0 - 2026-08-05 - Plan 2 U2: client-side graph export (grafo senza codice sul tenant)
"""Parse a repository locally and emit / POST its code graph, never the source.

Plan 2, Phase 1 (client-attested, zero setup): the user's agent already sits in
the repo. This reuses the existing AST/KG parser to produce the SAME (nodes,
edges) dicts the hosted indexer produces, attaches a provenance block, and
sends the batch to the tenant's `POST /api/v1/graph/ingest`. The source code
never leaves the machine; only the graph does.

The batch this builds is exactly a `GraphIngestRequest` (the U1 contract), so
client and server share one schema with no translation layer. A test asserts
that round-trip so the two can never drift apart.

Parsing runs IN-PROCESS (no ProcessPool): `parse_python_file` reads
`ast_parser.REPO_ROOT` to compute repo-relative paths, and that module global
does not propagate to subprocess workers (same reason
`populate_graph_chunked(repo_root=...)` pins `python_workers=0`).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.scripts import ast_parser as ap

# Identifies the parser that produced the batch, so the tenant can reason about
# compatibility. Bump when the emitted node/edge shape changes.
PARSER_VERSION = "ast_parser/1.4.0"

# Cross-language discovery: tracked Python + the TypeScript/JavaScript family
# across an arbitrary repo layout (not the marvisx-specific default). The
# tree-sitter TS grammar parses ES-module / CommonJS JavaScript too, so a
# JS-only repo (e.g. the marvis-cloud funnel) is graphed, not skipped. Git
# ls-files globs match recursively, so `*.py` covers both the repo root and
# nested directories (`**/*.py` would MISS root-level files because `**/`
# requires a directory).
_SCAN_PATTERNS = (
    "*.py",
    "*.ts",
    "*.tsx",
    "*.mts",
    "*.cts",
    "*.mjs",
    "*.cjs",
    "*.js",
    "*.jsx",
)


def _git(repo_root: Path, *args: str) -> str | None:
    """Run a read-only git command in repo_root; return stripped stdout or None."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(repo_root), *args],
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None
    return out.strip()


def build_provenance(repo_root: Path, source: str = "client-attested") -> dict[str, Any]:
    """Provenance for a batch parsed from repo_root's current working tree."""
    commit_sha = _git(repo_root, "rev-parse", "HEAD")
    # A non-empty porcelain status means uncommitted changes: the graph was
    # parsed against a dirty working tree. Honest label, never hidden.
    porcelain = _git(repo_root, "status", "--porcelain")
    dirty = bool(porcelain) if porcelain is not None else False
    return {
        "source": source,
        "commit_sha": commit_sha,
        "dirty": dirty,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "parser_version": PARSER_VERSION,
    }


def build_graph_batch(
    repo_root: str | Path,
    project: str,
    *,
    source: str = "client-attested",
) -> dict[str, Any]:
    """Parse repo_root and return a GraphIngestRequest-shaped batch (no source).

    Reuses the hosted parser's pure functions. Overrides the parser's module
    globals for the duration so node ids and paths are computed relative to the
    target repo, then restores them (concurrent callers / the next CLI run are
    unaffected).
    """
    root = Path(repo_root).resolve()

    saved_repo_root, saved_project_id = ap.REPO_ROOT, ap.PROJECT_ID
    ap.REPO_ROOT = root
    ap.PROJECT_ID = project
    try:
        py_files, ts_files = ap.discover_files(root, patterns=_SCAN_PATTERNS)
        results: list[tuple[list[dict], list[dict]]] = []
        for path in py_files:
            results.append(ap.parse_python_file(str(path)))
        for path in ts_files:
            try:
                results.append(ap.parse_typescript_file(str(path)))
            except Exception:  # noqa: BLE001 - one bad file must not sink the batch
                results.append(([], []))
        nodes = ap._dedupe_nodes([n for r in results for n in r[0]])
        edges = [e for r in results for e in r[1]]
    finally:
        ap.REPO_ROOT, ap.PROJECT_ID = saved_repo_root, saved_project_id

    return {
        "project": project,
        "provenance": build_provenance(root, source),
        "nodes": nodes,
        "edges": edges,
    }


def post_graph_batch(
    batch: dict[str, Any],
    tenant_url: str,
    bearer: str,
    *,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """POST the batch to the tenant's graph ingest endpoint. Returns the JSON body."""
    import httpx

    url = tenant_url.rstrip("/") + "/api/v1/graph/ingest"
    response = httpx.post(
        url,
        json=batch,
        headers={"Authorization": f"Bearer {bearer}"},
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse a repo locally and emit or POST its code graph "
        "(never the source) to a Marvis tenant."
    )
    parser.add_argument("--repo", default=".", help="Repository root (default: cwd).")
    parser.add_argument("--project", required=True, help="Project slug on the tenant.")
    parser.add_argument(
        "--source",
        default="client-attested",
        choices=("client-attested", "ci-signed"),
        help="Provenance source. 'ci-signed' when run from CI (marvis-index-action); "
        "'client-attested' (default) from a local session.",
    )
    parser.add_argument(
        "--tenant-url",
        default=None,
        help="Tenant base URL. If set (with --bearer), POST the batch; else emit JSON.",
    )
    parser.add_argument(
        "--bearer",
        default=None,
        help="Tenant bearer token for the POST. Falls back to the "
        "MARVIS_TENANT_BEARER env var (preferred in CI: keeps the secret off argv).",
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help="Print the batch JSON to stdout instead of POSTing.",
    )
    args = parser.parse_args(argv)

    batch = build_graph_batch(args.repo, args.project, source=args.source)

    if args.tenant_url and not args.emit:
        import os

        bearer = args.bearer or os.environ.get("MARVIS_TENANT_BEARER")
        if not bearer:
            print(
                "a bearer is required to POST: pass --bearer or set "
                "MARVIS_TENANT_BEARER (or use --emit)",
                file=sys.stderr,
            )
            return 2
        result = post_graph_batch(batch, args.tenant_url, bearer)
        print(json.dumps(result))
        return 0

    # Default: emit the batch (the client can pipe / inspect it).
    print(json.dumps(batch))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
