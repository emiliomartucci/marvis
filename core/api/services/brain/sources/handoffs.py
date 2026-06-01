# Handoffs source collector (sub-01 §3 — `handoff_changed`).
#
# Handoffs are filesystem-only: `{metadata_path}/memory/handoff-*.md`. There
# is no `handoff_index` table in console.db today, so the plan's "fallback
# to handoff_index.updated_at" is implemented as: `mtime` is authoritative;
# if a fallback table appears later we add a second branch here.
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import yaml

from core.api.services.brain.digest_collector import SourceCollector
from core.api.services.brain.models import EventDraft, SourceCollectorContext
from core.api.services.brain.sources.base import (
    resolve_source_project,
    stable_short_hash,
)

logger = logging.getLogger(__name__)


# Per-cycle hard cap protects against an accidental bulk-touch of every
# handoff file (e.g. `chmod -R`, restore-from-backup). Plan §6.D0 says 1000
# events/source — we leave the per-source cap to the orchestrator and just
# bound the directory walk for safety.
MAX_HANDOFFS_SCANNED: int = 2000


def _iter_project_roots() -> list[Path]:
    """Return project metadata roots without coupling to router globals.

    The Marvis convention (kb/projects.md): `/data/projects/<slug>/` plus
    `~/workspace/projects/<slug>/`. We import lazily and tolerate missing
    `api.routers.projects` (e.g. during unit tests that don't bootstrap the
    full API surface).
    """
    try:
        from core.api.routers.projects import PROJECT_DIRS

        return [p for p in PROJECT_DIRS if p.exists()]
    except Exception:  # pragma: no cover — defensive
        defaults = [
            Path("/data/projects"),
            Path.home() / "workspace" / "projects",
        ]
        return [p for p in defaults if p.exists()]


def _parse_handoff_frontmatter(path: Path) -> tuple[list[str], str | None, str | None]:
    """Return (tags, session, slug_hint) from frontmatter; best-effort."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], None, None
    if not text.startswith("---"):
        return [], None, None
    try:
        _, body = text.split("---", 1)
        meta_block, _ = body.split("---", 1)
        meta = yaml.safe_load(meta_block) or {}
    except (ValueError, yaml.YAMLError):
        return [], None, None
    if not isinstance(meta, dict):
        return [], None, None
    raw_tags = meta.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    tags = [str(t) for t in raw_tags if isinstance(t, (str, int))]
    session = (
        str(meta.get("session"))
        if meta.get("session") not in (None, "")
        else None
    )
    slug_hint = meta.get("project") or meta.get("slug")
    return tags, session, str(slug_hint) if slug_hint else None


def _project_slug_from_path(handoff_path: Path) -> str | None:
    """Recover the project slug from `<root>/<slug>/memory/<file>` shape."""
    parts = handoff_path.parts
    if "memory" not in parts:
        return None
    idx = parts.index("memory")
    return parts[idx - 1] if idx >= 1 else None


async def collect(ctx: SourceCollectorContext) -> AsyncIterator[EventDraft]:
    seen = 0
    for root in _iter_project_roots():
        try:
            project_dirs = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for project_dir in project_dirs:
            memory_dir = project_dir / "memory"
            if not memory_dir.is_dir():
                continue
            try:
                handoffs = list(memory_dir.glob("handoff-*.md"))
            except OSError:
                continue
            for handoff in handoffs:
                if seen >= MAX_HANDOFFS_SCANNED:
                    return
                seen += 1
                try:
                    stat = handoff.stat()
                except OSError:
                    continue
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                # Cycle window enforcement (bug fix 2026-05-18) — collapses to
                # the legacy (watermark, cutoff] filter when no window bounds.
                if not ctx.in_window(mtime):
                    continue

                tags, session, slug_hint = _parse_handoff_frontmatter(handoff)
                slug = slug_hint or _project_slug_from_path(handoff) or project_dir.name
                source_project, program_key = resolve_source_project(slug)
                filename_hash = stable_short_hash(handoff.name)
                source_ref = f"handoff:{slug}:{filename_hash}"
                evidence = {
                    "path": str(handoff),
                    "mtime": mtime.isoformat(),
                    "size": stat.st_size,
                    "frontmatter_tags": sorted(tags),
                }
                if session:
                    evidence["session"] = session

                title = handoff.name
                summary = (
                    f"handoff {handoff.name} updated"
                    + (f" (session {session})" if session else "")
                )

                yield EventDraft(
                    event_type="handoff_changed",
                    source_system="handoff",
                    source_ref=source_ref,
                    title=title[:200],
                    summary=summary[:2000],
                    observed_at=mtime,
                    derived_from_state_at=mtime,
                    evidence=evidence,
                    source_project=source_project,
                    program_key=program_key,
                )


handoffs_collector = SourceCollector(source_system="handoff", collect=collect)


__all__ = ["collect", "handoffs_collector"]
