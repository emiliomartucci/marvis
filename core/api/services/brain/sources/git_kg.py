# Git + Knowledge Graph source collector
# (sub-01 §3 — `commit_changed` / `kg_changed` / `doc_changed`).
#
# Three streams collapsed into a single collector because they share the
# same substrate (filesystem + graph nodes) and the same cycle math. Strict
# anti-patterns enforced (regression-guarded by the no-neighbors test):
#   * Per-project neighbor expansion → N+1 disaster, banned.
#     Use graph_service.get_hotspots instead.
#   * Direct SELECT against the graph node table in this module → banned.
#     Read paths live in api.services.graph_service.
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

from core.api.db import acquire_db
from core.api.paths import repo_root
from core.api.services import graph_service
from core.api.services.brain.digest_collector import SourceCollector
from core.api.services.brain.models import EventDraft, SourceCollectorContext
from core.api.services.brain.sources.base import (
    normalize_iso,
    resolve_source_project,
)

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Tunables (kept inline rather than promoted to settings — the plan §3
# fixes them explicitly and lifting them to app_settings would create
# another knob that drifts).
# ----------------------------------------------------------------------

COMMIT_CAP_PER_CYCLE: int = 100
KG_HOTSPOT_LIMIT: int = 20
KG_HOTSPOT_WINDOW = "30d"
DOC_NODE_KINDS: frozenset[str] = frozenset(
    {
        "handoff",
        "solution",
        "guide",
        "plan",
        "brainstorm",
        "audit",
        "spike",
        "analysis",
        "research",
        "rubric",
        "report",
        "policy",
        "contract",
    }
)

def _resolve_repo_root() -> Path:
    """Resolve the git repository root for commit_changed scans.

    Order of precedence:
    1. ``BRAIN_GIT_REPO_ROOT`` env var (explicit override, used in prod where
       pir-api is deployed under ``/data/pir/api/`` and the source repo lives
       elsewhere — typically ``~/workspace``).
    2. ``parents[3]`` of this file (development default — works when pir-api
       runs from the monorepo checkout, e.g. CI or dev workstation).

    The function returns a Path; callers still gate ``git log`` execution on
    ``(repo_root / ".git").exists()`` so a wrong value degrades to an empty
    event stream (collector continues, no crash).
    """
    env_override = os.getenv("BRAIN_GIT_REPO_ROOT")
    if env_override:
        return Path(env_override).expanduser().resolve()
    return repo_root(__file__)


_REPO_ROOT = _resolve_repo_root()


# ----------------------------------------------------------------------
# commit_changed
# ----------------------------------------------------------------------


_GIT_LOG_FORMAT = "%H%x1f%aI%x1f%an%x1f%s"
_GIT_LOG_SEP = "\x1f"


async def _git_log_window(
    *,
    since: datetime,
    until: datetime,
    cwd: Path,
    cap: int,
) -> list[dict[str, str]]:
    """Run `git log` for the cycle window.

    Returns at most `cap` commits ordered ASC by commit time so the watermark
    advances monotonically. Failure to invoke git (binary missing, non-repo)
    surfaces as an empty list — the collector continues and the partial
    failure tracking happens at the cycle level via try/except.
    """
    if not (cwd / ".git").exists():
        return []
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "log",
            f"--since={since.astimezone(timezone.utc).isoformat()}",
            f"--until={until.astimezone(timezone.utc).isoformat()}",
            f"--format={_GIT_LOG_FORMAT}",
            "--reverse",
            f"--max-count={cap}",
            "--no-merges",
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return []
    try:
        stdout_b, _stderr_b = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return []

    rows: list[dict[str, str]] = []
    for line in stdout_b.decode("utf-8", errors="replace").splitlines():
        parts = line.split(_GIT_LOG_SEP, 3)
        if len(parts) != 4:
            continue
        sha, author_iso, author_name, subject = parts
        rows.append(
            {
                "sha": sha,
                "author_iso": author_iso,
                "author_name": author_name,
                "subject": subject,
            }
        )
    return rows


def _commit_event(
    *, sha: str, author_iso: str, author_name: str, subject: str, now: datetime
) -> EventDraft:
    observed_at = normalize_iso(author_iso) or now
    evidence = {
        "sha": sha,
        "subject": subject[:200],
        "author": author_name,
        "author_iso": observed_at.isoformat(),
    }
    return EventDraft(
        event_type="commit_changed",
        source_system="git",
        source_ref=f"commit:{sha}",
        title=subject[:200] or f"commit {sha[:7]}",
        summary=f"{author_name}: {subject[:160]}",
        observed_at=observed_at,
        derived_from_state_at=observed_at,
        evidence=evidence,
    )


# ----------------------------------------------------------------------
# kg_changed (via graph_service.get_hotspots — NEVER per-project neighbor scan)
# ----------------------------------------------------------------------


def _node_id_for(node_type: str, qualified_name: str) -> str:
    """Mirror KG id convention so source_ref ↔ KG node_id round-trips."""
    prefix_map = {
        "function": "py",
        "file": "py",
        "module": "py",
    }
    prefix = prefix_map.get(node_type, node_type)
    return f"{prefix}:{node_type}:{qualified_name}"


def _kg_event(*, row: dict, now: datetime) -> EventDraft | None:
    touched_at = normalize_iso(row.get("touch_last_at"))
    if touched_at is None:
        return None
    node_id = row.get("id") or _node_id_for(row["type"], row.get("qualified_name", ""))
    project, program = resolve_source_project(row.get("project_id"))
    evidence = {
        "node_id": node_id,
        "type": row.get("type"),
        "touch_count_30d": row.get("touch_count_30d", 0),
        "touch_count_7d": row.get("touch_count_7d", 0),
        "touch_last_at": touched_at.isoformat(),
    }
    return EventDraft(
        event_type="kg_changed",
        source_system="kg",
        source_ref=node_id,
        title=str(row.get("name") or node_id)[:200],
        summary=f"KG hotspot {node_id}: {row.get('touch_count_30d', 0)}/30d touches",
        observed_at=touched_at,
        derived_from_state_at=touched_at,
        evidence=evidence,
        source_project=project,
        program_key=program,
    )


# ----------------------------------------------------------------------
# doc_changed (graph_nodes via graph_service helper — no direct SQL)
# ----------------------------------------------------------------------


async def _list_doc_nodes_changed_since(
    *, since: datetime, limit: int = 200
) -> list[dict]:
    """Read doc-kind nodes whose touch_last_at advanced past the watermark.

    Routed through graph_service.find_recently_touched_doc_nodes so this
    collector stays free of direct SQL against the graph node table
    (anti-pattern enforced via grep on the file).
    """
    fetcher = getattr(graph_service, "find_recently_touched_doc_nodes", None)
    if fetcher is None:
        # Fall back to no-op rather than the banned direct query — tests
        # cover this branch explicitly so the absence is visible in CI.
        return []
    return await fetcher(
        since_iso=since.astimezone(timezone.utc).isoformat(),
        kinds=tuple(sorted(DOC_NODE_KINDS)),
        limit=limit,
    )


def _doc_event(*, row: dict, now: datetime) -> EventDraft | None:
    touched_at = normalize_iso(row.get("touch_last_at"))
    if touched_at is None:
        return None
    node_id = row.get("id")
    path = row.get("file_path") or row.get("name") or node_id
    last_sha = row.get("last_modified_git_sha") or row.get("first_seen_git_sha") or ""
    project, program = resolve_source_project(row.get("project_id"))
    evidence = {
        "node_id": node_id,
        "kind": row.get("type"),
        "path": path,
        "sha": last_sha,
        "touch_last_at": touched_at.isoformat(),
    }
    return EventDraft(
        event_type="doc_changed",
        source_system="kg",
        source_ref=f"doc:{path}@{last_sha[:12] if last_sha else 'untracked'}",
        title=str(row.get("name") or path)[:200],
        summary=f"Doc {row.get('type')} updated: {path}",
        observed_at=touched_at,
        derived_from_state_at=touched_at,
        evidence=evidence,
        source_project=project,
        program_key=program,
    )


# ----------------------------------------------------------------------
# Collector entry point
# ----------------------------------------------------------------------


def _resolve_git_window(ctx: SourceCollectorContext) -> tuple[datetime, datetime]:
    """Return (since, until) for ``git log`` — clamped to the cycle window.

    Bug fix 2026-05-18: collectors must honor ``cycle_window_*`` instead of
    blindly reading from the watermark up to ``cutoff_at``. For backward-
    compat (periodic path with no window bounds) this collapses to the
    legacy ``(watermark, cutoff_at]`` slice.
    """
    if (
        ctx.cycle_window_start is not None
        and ctx.cycle_window_start > ctx.since_watermark
    ):
        since = ctx.cycle_window_start
    else:
        since = ctx.since_watermark
    if (
        ctx.cycle_window_end is not None
        and ctx.cycle_window_end < ctx.cutoff_at
    ):
        until = ctx.cycle_window_end
    else:
        until = ctx.cutoff_at
    return since, until


async def collect_git(ctx: SourceCollectorContext) -> AsyncIterator[EventDraft]:
    """Git commit_changed collector — source_system='git'.

    Repo root resolution order:
    1. ``BRAIN_GIT_REPO_ROOT`` env var (production: pir-api deployed under
       /data/pir/api/ → repo lives at ~/workspace).
    2. Module-level ``_REPO_ROOT`` (dev default + monkeypatchable in tests).

    Cycle window: bounded via ``ctx.cycle_window_start`` / ``cycle_window_end``
    when present, otherwise falls back to ``(since_watermark, cutoff_at]``.
    """
    env_override = os.getenv("BRAIN_GIT_REPO_ROOT")
    repo_root = (
        Path(env_override).expanduser().resolve() if env_override else _REPO_ROOT
    )
    since, until = _resolve_git_window(ctx)
    commits = await _git_log_window(
        since=since,
        until=until,
        cwd=repo_root,
        cap=COMMIT_CAP_PER_CYCLE,
    )
    for commit in commits:
        observed_at = normalize_iso(commit["author_iso"])
        if observed_at is not None and not ctx.in_window(observed_at):
            continue
        yield _commit_event(
            sha=commit["sha"],
            author_iso=commit["author_iso"],
            author_name=commit["author_name"],
            subject=commit["subject"],
            now=ctx.now,
        )


async def collect_kg(ctx: SourceCollectorContext) -> AsyncIterator[EventDraft]:
    """KG kg_changed (hotspots) + doc_changed (doc nodes) — source_system='kg'.

    Cycle window enforcement via ``ctx.in_window`` (bug fix 2026-05-18).
    """
    # kg_changed (hotspot delta projection)
    async with acquire_db() as db:
        hotspots = await graph_service.get_hotspots(
            db,
            window=KG_HOTSPOT_WINDOW,
            limit=KG_HOTSPOT_LIMIT,
            type_filter="file",
        )

    for row in hotspots:
        touched_at = normalize_iso(row.get("touch_last_at"))
        if touched_at is None or not ctx.in_window(touched_at):
            continue
        evt = _kg_event(row=row, now=ctx.now)
        if evt is not None:
            yield evt

    # doc_changed — fetch since the lower bound (max(watermark, window_start))
    since_doc, _until_doc = _resolve_git_window(ctx)
    try:
        doc_rows = await _list_doc_nodes_changed_since(since=since_doc)
    except Exception:  # pragma: no cover — helper missing or KG offline
        logger.exception("brain.sources.git_kg: doc_changed fetch failed")
        doc_rows = []

    for row in doc_rows:
        touched_at = normalize_iso(row.get("touch_last_at"))
        if touched_at is None or not ctx.in_window(touched_at):
            continue
        evt = _doc_event(row=row, now=ctx.now)
        if evt is not None:
            yield evt


# Backward-compat alias for tests/code che ancora referenziano collect()
async def collect(ctx: SourceCollectorContext) -> AsyncIterator[EventDraft]:
    async for evt in collect_git(ctx):
        yield evt
    async for evt in collect_kg(ctx):
        yield evt


git_collector = SourceCollector(source_system="git", collect=collect_git)
kg_collector = SourceCollector(source_system="kg", collect=collect_kg)
# Legacy alias (NON registrare — single source_system='git_kg' viola CHECK constraint)
git_kg_collector = git_collector


__all__ = [
    "COMMIT_CAP_PER_CYCLE",
    "DOC_NODE_KINDS",
    "KG_HOTSPOT_LIMIT",
    "KG_HOTSPOT_WINDOW",
    "collect",
    "git_kg_collector",
]
