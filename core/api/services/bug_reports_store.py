"""Operator-side store for the fleet ``report_bug`` tool (transport C).

Framework-agnostic async ops on a single-writer ``tasks`` connection, so the same
logic backs the FastMCP custom routes (the real hosted surface — tenants run
``core.api.mcp.server``, not the REST app) and is unit-testable against a plain
aiosqlite connection.

Attribution is derived from the verified HMAC ingest token (``authenticate``),
never from the payload. A report is a ``pending`` task on the operator's
``bug-reports`` project scoped by ``workspace_id = <reporter tenant>`` so reads are
tenant-isolated. Dedup: ``(source='bug_report', source_ref=<sha256 key>)`` unique.
Redaction + caps run here too (defense in depth)."""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone

import aiosqlite

from core.api.config import settings
from core.api.services import bug_reports_core as brc

BUG_PROJECT = "bug-reports"
BUG_SOURCE = "bug_report"
_ENV_FIELDS = ("tool_or_area", "error_code", "repro", "client")


class BugIngestError(Exception):
    """Transport-level failure with an HTTP status + machine-readable code."""

    def __init__(self, status_code: int, code: str, detail: str, *, retry_after_ms: int | None = None) -> None:
        self.status_code = status_code
        self.code = code
        self.detail = detail
        self.retry_after_ms = retry_after_ms
        super().__init__(detail)


# One choke point for the whole fleet (every reporter funnels through the
# operator process) → a per-reporter-tenant in-memory sliding window is the
# authoritative rate limit. Fail-open (memory-only, restart resets).
_LIMIT = int(getattr(settings, "bugreport_rate_limit_per_hour", 10) or 10)
_limiter = brc.SlidingWindowRateLimiter(limit=_LIMIT, window_seconds=3600.0)


def authenticate(tenant: str | None, token: str | None) -> str:
    """Verify the HMAC ingest headers and return the reporter tenant, or raise.

    The secret lives only in the operator env; each tenant holds only its own
    derived token → a tenant can only ever authenticate AS ITSELF."""
    secret = getattr(settings, "bugreport_ingest_secret", "") or ""
    if not secret:
        raise BugIngestError(503, "not_configured", "bug ingest not configured")
    if not tenant or not token:
        raise BugIngestError(401, "unauthorized", "missing ingest credentials")
    if not brc.verify_ingest_token(secret, tenant, token):
        raise BugIngestError(401, "unauthorized", "invalid ingest credentials")
    return tenant


def _redact_payload(title: str, description: str, environment: dict | None) -> tuple[str, str, dict[str, str], int]:
    red_title, n_title = brc.redact(brc.cap(title or "", brc.TITLE_CAP))
    red_desc, n_desc = brc.redact(brc.cap(description or "", brc.DESCRIPTION_CAP))
    redactions = n_title + n_desc
    env_clean: dict[str, str] = {}
    if isinstance(environment, dict):
        for field in _ENV_FIELDS:
            val = environment.get(field)
            if val:
                r, n = brc.redact(brc.cap(str(val), brc.ENV_FIELD_CAP))
                env_clean[field] = r
                redactions += n
    return red_title, red_desc, env_clean, redactions


def _compose_description(desc: str, env: dict[str, str], kind_hint: str | None, severity_hint: str | None, redactions: int) -> str:
    lines = [desc, "", "---"]
    for label, key in (("Tool/area", "tool_or_area"), ("Error code", "error_code"), ("Repro", "repro"), ("Client", "client")):
        if env.get(key):
            lines.append(f"{label}: {env[key]}")
    if kind_hint:
        lines.append(f"Kind hint: {kind_hint}")
    if severity_hint:
        lines.append(f"Severity hint: {severity_hint}")
    lines.append(f"Redactions: {redactions}")
    return "\n".join(lines)


async def create_report(
    db: aiosqlite.Connection,
    reporter: str,
    *,
    title: str,
    description: str,
    environment: dict | None = None,
    kind_hint: str | None = None,
    severity_hint: str | None = None,
    now: float | None = None,
) -> dict:
    """Create (or dedup) a pending bug-report task for ``reporter``."""
    now_ts = time.time() if now is None else now
    allowed, retry_after_ms = _limiter.check(reporter, now_ts)
    if not allowed:
        raise BugIngestError(429, "rate_limited", "bug report rate limit exceeded", retry_after_ms=retry_after_ms)
    if not title or not description:
        raise BugIngestError(400, "invalid", "title and description are required")

    red_title, red_desc, env_clean, redactions = _redact_payload(title, description, environment)
    dedup = brc.dedup_key(reporter, red_title, red_desc)

    db.row_factory = aiosqlite.Row
    existing = await (
        await db.execute(
            "SELECT id FROM tasks WHERE source=? AND source_ref=? AND workspace_id=? "
            "AND project=? AND deleted_at IS NULL LIMIT 1",
            (BUG_SOURCE, dedup, reporter, BUG_PROJECT),
        )
    ).fetchone()
    if existing is not None:
        return {"report_id": existing["id"], "status": "logged", "deduplicated": True}

    report_id = str(uuid.uuid4())
    iso = datetime.now(timezone.utc).isoformat()
    description_full = _compose_description(red_desc, env_clean, kind_hint, severity_hint, redactions)
    tags = ["bug-report", f"reporter:{reporter}"]
    if kind_hint:
        tags.append(f"kind:{kind_hint}")
    if severity_hint:
        tags.append(f"severity:{severity_hint}")

    try:
        await db.execute(
            "INSERT INTO tasks (id, title, description, status, project, priority, "
            "created_by, owner_id, source, source_ref, tags, kind, "
            "workspace_id, completion_mode, created_at, updated_at) "
            "VALUES (?, ?, ?, 'pending', ?, 'medium', ?, NULL, ?, ?, ?, 'normal', ?, 'none', ?, ?)",
            (
                report_id,
                red_title,
                description_full,
                BUG_PROJECT,
                f"bug-ingest:{reporter}",
                BUG_SOURCE,
                dedup,
                json.dumps(tags),
                reporter,
                iso,
                iso,
            ),
        )
        await db.commit()
    except aiosqlite.IntegrityError:
        row = await (
            await db.execute(
                "SELECT id FROM tasks WHERE source=? AND source_ref=? AND workspace_id=? "
                "AND project=? AND deleted_at IS NULL LIMIT 1",
                (BUG_SOURCE, dedup, reporter, BUG_PROJECT),
            )
        ).fetchone()
        if row is not None:
            return {"report_id": row["id"], "status": "logged", "deduplicated": True}
        raise

    return {"report_id": report_id, "status": "logged", "deduplicated": False}


async def get_report(db: aiosqlite.Connection, reporter: str, report_id: str) -> dict | None:
    """Return the report if it belongs to ``reporter``, else None (→ generic 404)."""
    db.row_factory = aiosqlite.Row
    row = await (
        await db.execute(
            "SELECT id, status, title, created_at, updated_at FROM tasks "
            "WHERE id=? AND workspace_id=? AND project=? AND source=? AND deleted_at IS NULL LIMIT 1",
            (report_id, reporter, BUG_PROJECT, BUG_SOURCE),
        )
    ).fetchone()
    if row is None:
        return None
    return {
        "report_id": row["id"],
        "status": row["status"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


async def list_reports(
    db: aiosqlite.Connection,
    reporter: str,
    *,
    status: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    limit = max(1, min(int(limit), 200))
    where = ["workspace_id=?", "project=?", "source=?", "deleted_at IS NULL"]
    params: list[object] = [reporter, BUG_PROJECT, BUG_SOURCE]
    if status:
        where.append("status=?")
        params.append(status)
    if since:
        where.append("created_at>=?")
        params.append(since)
    params.append(limit)
    db.row_factory = aiosqlite.Row
    rows = await (
        await db.execute(
            "SELECT id, status, title, created_at, updated_at FROM tasks WHERE "
            + " AND ".join(where)
            + " ORDER BY created_at DESC LIMIT ?",
            params,
        )
    ).fetchall()
    return [
        {
            "report_id": r["id"],
            "status": r["status"],
            "title": r["title"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
        }
        for r in rows
    ]


async def list_reports_admin(
    db: aiosqlite.Connection,
    *,
    workspace: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict]:
    """Operator-only: list bug reports across ALL reporter tenants (cross-workspace).

    Unlike ``list_reports`` (scoped to one reporter), this drops the ``workspace_id``
    filter so the operator can triage the whole fleet. ``workspace`` narrows to one
    reporter tenant. Titles are already redacted at ingest time (safe to return)."""
    limit = max(1, min(int(limit), 500))
    where = ['project=?', 'source=?', 'deleted_at IS NULL']
    params: list[object] = [BUG_PROJECT, BUG_SOURCE]
    if workspace:
        where.append('workspace_id=?')
        params.append(workspace)
    if status:
        where.append('status=?')
        params.append(status)
    if since:
        where.append('created_at>=?')
        params.append(since)
    params.append(limit)
    db.row_factory = aiosqlite.Row
    rows = await (
        await db.execute(
            'SELECT id, workspace_id, status, title, created_at, updated_at FROM tasks WHERE '
            + ' AND '.join(where)
            + ' ORDER BY created_at DESC LIMIT ?',
            params,
        )
    ).fetchall()
    return [
        {
            'report_id': r['id'],
            'workspace': r['workspace_id'],
            'status': r['status'],
            'title': r['title'],
            'created_at': r['created_at'],
            'updated_at': r['updated_at'],
        }
        for r in rows
    ]
