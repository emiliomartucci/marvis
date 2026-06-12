# v1.0.0 - 2026-05-16 - KG PR-Impact sub-01 D2: git diff + dual-tree mapping
"""Git diff parsing + hunk → function attribution.

Two execution modes:

- **CLI mode**: run `git` subprocess against a real repo to get file lists,
  contents, hunks, and blame. Used by `scripts/populate_pr_impact.py`.
- **In-memory mode**: callers pass pre-computed `bytes` for old/new content
  and pre-parsed hunks. Used by unit tests to avoid spinning a git fixture.

The hunk parsing accepts the canonical `git diff --unified=0` format. Empty
files, pure deletions, pure additions, and renames are all handled.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.api.services.pr_impact_pipeline.languages import (
    LanguageSpec,
    language_for_path,
)
from core.api.services.pr_impact_pipeline.parser import (
    FunctionSpan,
    find_enclosing_function,
    line_to_byte_offset,
    parse_tree,
)


TouchKind = Literal["add", "modify", "delete"]

FileStatus = Literal["A", "M", "D", "R", "C", "T"]


@dataclass(frozen=True)
class Hunk:
    """One contiguous chunk of changes from `git diff --unified=0`."""

    old_start: int
    old_lines: int
    new_start: int
    new_lines: int
    added_lines: int
    removed_lines: int


@dataclass(frozen=True)
class FileChange:
    """A single file's status in the PR diff."""

    path: str
    status: FileStatus
    old_path: str | None = None  # set on rename/copy
    similarity: int | None = None  # 0-100 for rename/copy


@dataclass(frozen=True)
class TouchedFunction:
    """One PR-impact populator output row.

    Pairs naturally with a `pr_function_touches` INSERT and a `modifies`
    graph edge. `function` is `None` when the diff touches top-level code
    (imports, module-level statements) — caller decides whether to skip.
    """

    file_path: str
    function: FunctionSpan | None
    touch_kind: TouchKind
    lines_added: int
    lines_removed: int
    hunks: tuple[Hunk, ...]


# --------------------------------------------------------------------------
# git CLI wrappers (skipped by tests via the in-memory APIs below)
# --------------------------------------------------------------------------


class GitInvocationError(RuntimeError):
    """Wrap non-zero exits from the underlying git subprocess."""


def _run_git(repo: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise GitInvocationError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:500]}"
        )
    return proc.stdout


def list_changed_files(repo: Path, base_sha: str, head_sha: str) -> list[FileChange]:
    """Run `git diff --name-status -M50% base..head` and parse the result."""
    raw = _run_git(
        repo,
        ["diff", "--name-status", "-M50%", f"{base_sha}..{head_sha}"],
    )
    changes: list[FileChange] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        status_raw = parts[0]
        # Renames look like `R85<TAB>old<TAB>new`
        if status_raw.startswith("R") or status_raw.startswith("C"):
            similarity = int(status_raw[1:]) if status_raw[1:].isdigit() else None
            changes.append(
                FileChange(
                    path=parts[2],
                    status=status_raw[0],  # "R" or "C"
                    old_path=parts[1],
                    similarity=similarity,
                )
            )
        else:
            changes.append(FileChange(path=parts[1], status=status_raw[0]))
    return changes


def file_content_at_revision(repo: Path, revision: str, path: str) -> bytes:
    """Return the file bytes at the given revision (or b'' if missing)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "show", f"{revision}:{path}"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise GitInvocationError(f"git show failed: {exc}") from exc
    if proc.returncode != 0:
        # `path does not exist in revision` -> treat as empty.
        return b""
    return proc.stdout


def file_hunks(
    repo: Path,
    base_sha: str,
    head_sha: str,
    path: str,
    old_path: str | None = None,
) -> list[Hunk]:
    """Parse unified=0 hunks for a single file."""
    args = ["diff", "--unified=0", f"{base_sha}..{head_sha}", "--"]
    if old_path and old_path != path:
        args.extend([old_path, path])
    else:
        args.append(path)
    raw = _run_git(repo, args)
    return parse_unified_hunks(raw)


def blame_email_for_range(
    repo: Path,
    revision: str,
    path: str,
    line_start: int,
    line_end: int,
) -> str | None:
    """Best-effort `git blame --line-porcelain` over a function range.

    Returns the most-common author email across the lines (or None when
    blame fails). The populator stores this in `pr_function_touches.blame_author`
    — PII tracked in docs/privacy/pii-inventory.md §5.
    """
    if line_end < line_start:
        return None
    try:
        raw = _run_git(
            repo,
            [
                "blame",
                "--line-porcelain",
                "-L",
                f"{line_start},{line_end}",
                revision,
                "--",
                path,
            ],
        )
    except GitInvocationError:
        return None
    counts: dict[str, int] = {}
    for line in raw.splitlines():
        if line.startswith("author-mail "):
            email = line[len("author-mail "):].strip().strip("<>")
            if email:
                counts[email] = counts.get(email, 0) + 1
    if not counts:
        return None
    # Return the most-attributed email; ties broken alphabetically for determinism.
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


# --------------------------------------------------------------------------
# Hunk parsing (pure — unit-test friendly)
# --------------------------------------------------------------------------

_HUNK_HEADER_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_lines>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_lines>\d+))? @@"
)


def parse_unified_hunks(diff_text: str) -> list[Hunk]:
    """Convert raw unified-diff text into a list of Hunk dataclasses.

    Only the @@-headers and the immediately following +/- line counts are
    used. We deliberately ignore the file-header (`diff --git`, `+++ a/`,
    `--- b/`) because callers already know which path they asked about.
    """
    hunks: list[Hunk] = []
    current_added = 0
    current_removed = 0
    current_header: Hunk | None = None
    for line in diff_text.splitlines():
        m = _HUNK_HEADER_RE.match(line)
        if m:
            if current_header is not None:
                hunks.append(
                    _finalize_hunk(current_header, current_added, current_removed)
                )
            old_start = int(m.group("old_start"))
            old_lines = int(m.group("old_lines") or 1)
            new_start = int(m.group("new_start"))
            new_lines = int(m.group("new_lines") or 1)
            current_header = Hunk(
                old_start=old_start,
                old_lines=old_lines,
                new_start=new_start,
                new_lines=new_lines,
                added_lines=0,
                removed_lines=0,
            )
            current_added = 0
            current_removed = 0
            continue
        if current_header is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            current_added += 1
        elif line.startswith("-") and not line.startswith("---"):
            current_removed += 1
    if current_header is not None:
        hunks.append(_finalize_hunk(current_header, current_added, current_removed))
    return hunks


def _finalize_hunk(header: Hunk, added: int, removed: int) -> Hunk:
    return Hunk(
        old_start=header.old_start,
        old_lines=header.old_lines,
        new_start=header.new_start,
        new_lines=header.new_lines,
        added_lines=added,
        removed_lines=removed,
    )


# --------------------------------------------------------------------------
# Hunk → function attribution
# --------------------------------------------------------------------------


def attribute_hunks_to_functions(
    *,
    path: str,
    status: FileStatus,
    new_content: bytes,
    old_content: bytes,
    hunks: list[Hunk],
    spec: LanguageSpec | None = None,
) -> list[TouchedFunction]:
    """Map each hunk to its enclosing function in NEW (or OLD for pure deletes).

    `spec` is resolved from the file extension when not provided. Returns
    one TouchedFunction per (function, status) — multiple hunks within the
    same function are collapsed.
    """
    if spec is None:
        spec = language_for_path(path)
    if spec is None:
        return []  # unsupported language

    new_tree = parse_tree(spec, new_content) if new_content else None
    old_tree = parse_tree(spec, old_content) if old_content else None

    # Aggregate hunks by function qualified_name + touch_kind.
    aggregated: dict[tuple[str, TouchKind], _Aggregate] = {}
    for hunk in hunks:
        kind, fn = _resolve_function_for_hunk(
            hunk=hunk,
            status=status,
            spec=spec,
            new_tree=new_tree,
            old_tree=old_tree,
            new_content=new_content,
            old_content=old_content,
        )
        key = (fn.qualified_name if fn else "<top-level>", kind)
        agg = aggregated.get(key)
        if agg is None:
            agg = _Aggregate(function=fn, kind=kind, hunks=[], added=0, removed=0)
            aggregated[key] = agg
        agg.hunks.append(hunk)
        agg.added += hunk.added_lines
        agg.removed += hunk.removed_lines

    out: list[TouchedFunction] = []
    for agg in aggregated.values():
        out.append(
            TouchedFunction(
                file_path=path,
                function=agg.function,
                touch_kind=agg.kind,
                lines_added=agg.added,
                lines_removed=agg.removed,
                hunks=tuple(agg.hunks),
            )
        )
    return out


@dataclass
class _Aggregate:
    function: FunctionSpan | None
    kind: TouchKind
    hunks: list[Hunk]
    added: int
    removed: int


def _resolve_function_for_hunk(
    *,
    hunk: Hunk,
    status: FileStatus,
    spec: LanguageSpec,
    new_tree,
    old_tree,
    new_content: bytes,
    old_content: bytes,
) -> tuple[TouchKind, FunctionSpan | None]:
    has_add = hunk.added_lines > 0
    has_del = hunk.removed_lines > 0

    # File-level status dominates when unambiguous.
    if status == "A":
        kind: TouchKind = "add"
    elif status == "D":
        kind = "delete"
    elif has_add and not has_del:
        kind = "add"
    elif has_del and not has_add:
        kind = "delete"
    else:
        kind = "modify"

    # Pure deletes attribute to OLD tree (function existed there). Adds
    # and modifies attribute to NEW. Renames are treated like modifies.
    if kind == "delete" and old_tree is not None:
        fn = _function_at_line(spec, old_tree, old_content, hunk.old_start, hunk.old_lines)
    elif new_tree is not None:
        line_start = hunk.new_start if hunk.new_lines > 0 else hunk.new_start + 1
        fn = _function_at_line(spec, new_tree, new_content, line_start, max(hunk.new_lines, 1))
    else:
        fn = None
    return kind, fn


def _function_at_line(
    spec: LanguageSpec,
    tree,
    content: bytes,
    line_start: int,
    line_count: int,  # noqa: ARG001 — kept for caller symmetry; probe is single-byte
) -> FunctionSpan | None:
    """Locate the enclosing function for a hunk that starts at `line_start`.

    We probe a single byte at the start of the line rather than spanning the
    whole hunk because `descendant_for_byte_range` returns the *smallest
    node covering the entire range*. When that range extends past a
    function boundary (a trailing newline or a blank line after the body),
    the result balloons up to the module node and we lose the enclosing fn.
    The probe + ancestor-walk in `find_enclosing_function` recovers it.
    """
    byte_start = line_to_byte_offset(content, line_start)
    # Bias the probe forward by a couple of bytes to skip leading indentation
    # whitespace — that whitespace can belong to the parent block rather
    # than the function body in some tree-sitter parses.
    probe = byte_start
    while probe < len(content) and content[probe] in (0x20, 0x09):  # space, tab
        probe += 1
    if probe >= len(content):
        probe = byte_start
    return find_enclosing_function(spec, tree, probe, probe + 1)


__all__ = [
    "Hunk",
    "FileChange",
    "FileStatus",
    "TouchKind",
    "TouchedFunction",
    "GitInvocationError",
    "list_changed_files",
    "file_content_at_revision",
    "file_hunks",
    "blame_email_for_range",
    "parse_unified_hunks",
    "attribute_hunks_to_functions",
]
