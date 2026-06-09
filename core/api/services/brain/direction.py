# Brain v1.2 — Direction service (hybrid storage: filesystem + DB cache + changelog).
#
# Hybrid storage model:
#   * Source of truth: /data/projects/<slug>/context.md frontmatter (direction.* keys)
#   * DB cache:        project_directions table (fast SQL query)
#   * Audit log:       direction_changelog table (append-only history)
#   * KG node update:  caller responsibility (router-level)
#
# Layering invariants:
#   * Atomic write to filesystem with .bak backup before overwriting.
#   * DB cache sync MUST follow filesystem write (eventual consistency).
#   * Changelog append-only — never UPDATE/DELETE rows.
#   * No LLM imports here (parent §9.3) — direction service is plumbing only.
from __future__ import annotations

import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite
import yaml

from core.api.db import acquire_db

logger = logging.getLogger(__name__)

_DATA_PROJECTS_DIR = Path("/data/projects")
_MAX_SUMMARY_LEN = 4000
_MAX_OUT_OF_SCOPE_LEN = 2000


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectionRecord:
    """In-memory view of a project direction."""

    project_slug: str
    summary: str
    out_of_scope: str
    last_updated_at: str  # ISO8601 with Z
    last_updated_by: str | None = None
    source_drift_signal: str | None = None
    source_finding_id: str | None = None
    schema_version: int = 1


@dataclass(frozen=True)
class ChangelogEntry:
    """Append-only changelog row."""

    project_slug: str
    change_type: str  # bootstrap | direction_update | manual_edit
    applied_at: str
    applied_by: str
    new_summary: str
    new_out_of_scope: str
    old_summary: str | None = None
    old_out_of_scope: str | None = None
    source_finding_id: str | None = None
    source_drift_signal_id: str | None = None
    rationale: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _context_md_path(slug: str) -> Path:
    """Return the canonical context.md path for a project slug.

    Raises ValueError if slug contains path-traversal characters.
    """
    if "/" in slug or ".." in slug or slug.startswith("."):
        raise ValueError(f"invalid project slug: {slug!r}")
    return _DATA_PROJECTS_DIR / slug / "context.md"


# ---------------------------------------------------------------------------
# Frontmatter validation
# ---------------------------------------------------------------------------


def validate_direction_yaml(data: dict[str, Any]) -> list[str]:
    """Validate a direction dict; return list of error strings (empty if OK).

    Expected schema:
        direction:
          summary: str (non-empty, <=4000)
          out_of_scope: str (non-empty, <=2000)
          last_updated: ISO8601 string (optional, will be filled on write)
          last_updated_by: str (optional)
    """
    errors: list[str] = []

    if not isinstance(data, dict):
        return ["direction payload must be a dict"]

    direction = data.get("direction") if "direction" in data else data
    if not isinstance(direction, dict):
        return ["direction.* keys missing or not a dict"]

    summary = direction.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        errors.append("direction.summary must be a non-empty string")
    elif len(summary) > _MAX_SUMMARY_LEN:
        errors.append(f"direction.summary too long ({len(summary)} > {_MAX_SUMMARY_LEN})")

    oos = direction.get("out_of_scope")
    if not isinstance(oos, str) or not oos.strip():
        errors.append("direction.out_of_scope must be a non-empty string")
    elif len(oos) > _MAX_OUT_OF_SCOPE_LEN:
        errors.append(
            f"direction.out_of_scope too long ({len(oos)} > {_MAX_OUT_OF_SCOPE_LEN})"
        )

    return errors


# ---------------------------------------------------------------------------
# Filesystem read/write
# ---------------------------------------------------------------------------


def _read_frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    """Read YAML frontmatter from a markdown file.

    Returns (frontmatter_dict, body_md). Returns ({}, full_content) when
    no frontmatter is present.
    """
    if not path.exists():
        return {}, ""
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---", 3)
    if end == -1:
        return {}, content
    raw = content[3:end].strip()
    body = content[end + 4 :].lstrip("\n")
    try:
        fm = yaml.safe_load(raw) or {}
        if not isinstance(fm, dict):
            return {}, content
        return fm, body
    except yaml.YAMLError as exc:
        logger.warning("frontmatter parse failed for %s: %s", path, exc)
        return {}, content


def _write_frontmatter(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    """Atomic write of (frontmatter + body) back to a markdown file.

    Creates a .bak sibling before overwriting. Uses a temp file + rename
    so concurrent readers never see a half-written file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    # Backup existing file if present
    if path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, bak)

    # Dump frontmatter with stable key order
    yaml_dump = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).rstrip("\n")

    full = f"---\n{yaml_dump}\n---\n\n{body.lstrip()}"

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(full, encoding="utf-8")
    os.replace(tmp, path)


def read_direction_frontmatter(slug: str) -> DirectionRecord | None:
    """Read direction frontmatter from /data/projects/<slug>/context.md.

    Returns None when:
      * context.md missing
      * no direction.* block present
      * direction block fails validation (logged warning)
    """
    path = _context_md_path(slug)
    fm, _ = _read_frontmatter(path)
    direction = fm.get("direction")
    if not isinstance(direction, dict):
        return None
    errors = validate_direction_yaml({"direction": direction})
    if errors:
        logger.warning("direction validation failed for %s: %s", slug, errors)
        return None
    return DirectionRecord(
        project_slug=slug,
        summary=direction["summary"].strip(),
        out_of_scope=direction["out_of_scope"].strip(),
        last_updated_at=direction.get("last_updated", _utc_iso()),
        last_updated_by=direction.get("last_updated_by"),
        source_finding_id=direction.get("source_finding_id"),
        source_drift_signal=direction.get("source_drift_signal_id"),
    )


def write_direction_frontmatter(
    slug: str,
    summary: str,
    out_of_scope: str,
    applied_by: str,
    source_finding: str | None = None,
    source_drift_signal: str | None = None,
) -> DirectionRecord:
    """Atomically write direction frontmatter to /data/projects/<slug>/context.md.

    Creates a .bak before overwriting. Returns the resulting DirectionRecord.
    Raises ValueError if summary/out_of_scope fail validation.
    """
    payload = {
        "direction": {
            "summary": summary.strip(),
            "out_of_scope": out_of_scope.strip(),
        }
    }
    errors = validate_direction_yaml(payload)
    if errors:
        raise ValueError(f"direction validation failed: {errors}")

    now = _utc_iso()
    path = _context_md_path(slug)
    fm, body = _read_frontmatter(path)

    direction_fm: dict[str, Any] = {
        "summary": summary.strip(),
        "out_of_scope": out_of_scope.strip(),
        "last_updated": now,
        "last_updated_by": applied_by,
    }
    if source_finding:
        direction_fm["source_finding_id"] = source_finding
    if source_drift_signal:
        direction_fm["source_drift_signal_id"] = source_drift_signal

    # Preserve other frontmatter keys
    new_fm = dict(fm) if fm else {}
    new_fm["direction"] = direction_fm
    new_fm.setdefault("schema_version", 1)

    _write_frontmatter(path, new_fm, body)

    return DirectionRecord(
        project_slug=slug,
        summary=direction_fm["summary"],
        out_of_scope=direction_fm["out_of_scope"],
        last_updated_at=now,
        last_updated_by=applied_by,
        source_finding_id=source_finding,
        source_drift_signal=source_drift_signal,
    )


# ---------------------------------------------------------------------------
# DB cache: project_directions
# ---------------------------------------------------------------------------


async def sync_db_cache(
    record: DirectionRecord,
    *,
    db: aiosqlite.Connection,
) -> None:
    """UPSERT a DirectionRecord into project_directions (cache table)."""
    await db.execute(
        """
        INSERT INTO project_directions (
            project_slug, summary, out_of_scope, last_updated_at,
            last_updated_by, source_drift_signal, source_finding_id,
            schema_version
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(project_slug) DO UPDATE SET
            summary = excluded.summary,
            out_of_scope = excluded.out_of_scope,
            last_updated_at = excluded.last_updated_at,
            last_updated_by = excluded.last_updated_by,
            source_drift_signal = excluded.source_drift_signal,
            source_finding_id = excluded.source_finding_id,
            schema_version = excluded.schema_version
        """,
        (
            record.project_slug,
            record.summary,
            record.out_of_scope,
            record.last_updated_at,
            record.last_updated_by,
            record.source_drift_signal,
            record.source_finding_id,
            record.schema_version,
        ),
    )


async def read_direction_db_cache(slug: str) -> DirectionRecord | None:
    """Fetch the cached direction for a slug. Returns None if absent."""
    async with acquire_db() as db:
        cur = await db.execute(
            """
            SELECT project_slug, summary, out_of_scope, last_updated_at,
                   last_updated_by, source_drift_signal, source_finding_id,
                   schema_version
              FROM project_directions
             WHERE project_slug = ?
            """,
            (slug,),
        )
        row = await cur.fetchone()
        await cur.close()
    if not row:
        return None
    return DirectionRecord(
        project_slug=row[0],
        summary=row[1],
        out_of_scope=row[2],
        last_updated_at=row[3],
        last_updated_by=row[4],
        source_drift_signal=row[5],
        source_finding_id=row[6],
        schema_version=row[7],
    )


# ---------------------------------------------------------------------------
# Changelog: append-only history
# ---------------------------------------------------------------------------


async def append_changelog(
    entry: ChangelogEntry,
    *,
    db: aiosqlite.Connection,
) -> str:
    """Append a ChangelogEntry row. Returns the new changelog_id."""
    changelog_id = f"chg_{uuid.uuid4().hex[:24]}"
    await db.execute(
        """
        INSERT INTO direction_changelog (
            changelog_id, project_slug, applied_at, applied_by, change_type,
            old_summary, new_summary, old_out_of_scope, new_out_of_scope,
            source_finding_id, source_drift_signal_id, rationale
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            changelog_id,
            entry.project_slug,
            entry.applied_at,
            entry.applied_by,
            entry.change_type,
            entry.old_summary,
            entry.new_summary,
            entry.old_out_of_scope,
            entry.new_out_of_scope,
            entry.source_finding_id,
            entry.source_drift_signal_id,
            entry.rationale,
        ),
    )
    return changelog_id


async def list_changelog(
    slug: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Read changelog history for a slug ordered by applied_at DESC."""
    async with acquire_db() as db:
        cur = await db.execute(
            """
            SELECT changelog_id, project_slug, applied_at, applied_by,
                   change_type, old_summary, new_summary, old_out_of_scope,
                   new_out_of_scope, source_finding_id, source_drift_signal_id,
                   rationale
              FROM direction_changelog
             WHERE project_slug = ?
             ORDER BY applied_at DESC
             LIMIT ?
            """,
            (slug, limit),
        )
        rows = await cur.fetchall()
        await cur.close()
    cols = [
        "changelog_id",
        "project_slug",
        "applied_at",
        "applied_by",
        "change_type",
        "old_summary",
        "new_summary",
        "old_out_of_scope",
        "new_out_of_scope",
        "source_finding_id",
        "source_drift_signal_id",
        "rationale",
    ]
    return [dict(zip(cols, row)) for row in rows]
