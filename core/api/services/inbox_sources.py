# v1.0.0 - 2026-04-11 - PR B: sources CRUD + metrics + SSRF guard
"""Service for inbox sources catalog (CRUD + metrics).

Tables used: inbox_sources (from migration 061), inbox_items, source_scores.

The service returns plain dicts so that FastAPI can serialize them directly
without an extra Pydantic layer. Keep the shape stable: the frontend (PR D)
consumes these fields verbatim.
"""
from __future__ import annotations

import ipaddress
import logging
import socket
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

import aiosqlite
from fastapi import HTTPException

logger = logging.getLogger(__name__)

MetricRange = Literal["24h", "7d", "30d", "total"]
_VALID_RANGES: frozenset[str] = frozenset({"24h", "7d", "30d", "total"})
_VALID_SOURCE_TYPES: frozenset[str] = frozenset({"rss", "email", "manual", "api", "legacy"})


# ---------------------------------------------------------------------------
# SSRF prevention
# ---------------------------------------------------------------------------


def validate_public_url(url: str) -> None:
    """Raise HTTPException 422 if URL points to a private/loopback/internal address.

    Blocks: non-http(s) schemes, missing hostname, unresolvable hostnames, and
    hostnames that resolve to private / loopback / link-local / reserved /
    multicast IP ranges (including the AWS metadata 169.254.169.254).
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail="Invalid URL") from exc
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="Only http(s) URLs allowed")
    hostname = parsed.hostname
    if not hostname:
        raise HTTPException(status_code=422, detail="Missing hostname")
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise HTTPException(status_code=422, detail="Cannot resolve hostname") from exc
    for info in infos:
        ip_str = info[4][0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise HTTPException(
                status_code=422,
                detail=f"URL resolves to non-public address: {ip_str}",
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalize_source_key(raw: str) -> str:
    """Normalize source_key the same way inbox_triage._update_source_score does.

    Strip whitespace, lowercase, drop 'www.' prefix. This ensures joins with
    source_scores work across sources that were registered with/without 'www.'.
    """
    key = (raw or "").strip().lower()
    # If it looks like a URL, extract the netloc
    try:
        parsed = urlparse(key)
        if parsed.netloc:
            key = parsed.netloc
    except Exception:
        pass
    if key.startswith("www."):
        key = key.removeprefix("www.")
    return key


def _row_to_dict(row: aiosqlite.Row) -> dict:
    """Convert aiosqlite.Row (or any Mapping) to a plain dict."""
    return {k: row[k] for k in row.keys()}


def _range_to_cutoff(range_value: str) -> str | None:
    """Return an ISO timestamp cutoff for a given range, or None for 'total'."""
    if range_value == "total":
        return None
    now = datetime.now(timezone.utc)
    if range_value == "24h":
        cutoff = now - timedelta(hours=24)
    elif range_value == "7d":
        cutoff = now - timedelta(days=7)
    elif range_value == "30d":
        cutoff = now - timedelta(days=30)
    else:
        return None
    return cutoff.isoformat()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def list_sources(
    db: aiosqlite.Connection,
    workspace_id: str,
    *,
    active_only: bool = False,
) -> list[dict]:
    """Return all sources with inline aggregate metrics.

    One SQL query with LEFT JOINs to avoid N+1. Each row contains:
      - base source columns
      - total_items, unread_count, auto_ignored_count (from inbox_items)
      - score, upvotes, downvotes, reads (from source_scores)
    """
    where_active = "AND s.active = 1" if active_only else ""

    query = f"""
        SELECT
            s.id,
            s.name,
            s.source_key,
            s.feed_url,
            s.source_type,
            s.active,
            s.last_fetch_at,
            s.last_fetch_error,
            s.workspace_id,
            s.created_at,
            s.updated_at,
            COALESCE(items.total_items, 0)          AS total_items,
            COALESCE(items.unread_count, 0)         AS unread_count,
            COALESCE(items.auto_ignored_count, 0)   AS auto_ignored_count,
            COALESCE(scores.score, 0)               AS score,
            COALESCE(scores.upvotes, 0)             AS upvotes,
            COALESCE(scores.downvotes, 0)           AS downvotes,
            COALESCE(scores.reads, 0)               AS reads
        FROM inbox_sources s
        LEFT JOIN (
            SELECT
                REPLACE(
                    LOWER(
                        SUBSTR(
                            REPLACE(REPLACE(url, 'https://', ''), 'http://', ''),
                            1,
                            CASE
                                WHEN INSTR(REPLACE(REPLACE(url, 'https://', ''), 'http://', ''), '/') > 0
                                THEN INSTR(REPLACE(REPLACE(url, 'https://', ''), 'http://', ''), '/') - 1
                                ELSE LENGTH(REPLACE(REPLACE(url, 'https://', ''), 'http://', ''))
                            END
                        )
                    ),
                    'www.', ''
                ) AS domain_key,
                COUNT(*) AS total_items,
                SUM(CASE WHEN status = 'unread' THEN 1 ELSE 0 END) AS unread_count,
                SUM(CASE WHEN status = 'auto_ignored' THEN 1 ELSE 0 END) AS auto_ignored_count,
                workspace_id AS ws
            FROM inbox_items
            WHERE url IS NOT NULL AND url != ''
            GROUP BY domain_key, workspace_id
        ) items ON items.domain_key = s.source_key AND items.ws = s.workspace_id
        LEFT JOIN source_scores scores
            ON scores.source_key = s.source_key
           AND scores.workspace_id = s.workspace_id
        WHERE s.workspace_id = ?
        {where_active}
        ORDER BY s.name ASC
    """

    cursor = await db.execute(query, (workspace_id,))
    rows = await cursor.fetchall()
    return [_row_to_dict(r) for r in rows]


async def get_source(
    db: aiosqlite.Connection,
    workspace_id: str,
    source_id: str,
) -> dict | None:
    """Return a single source row with the same aggregate shape as list_sources."""
    query = """
        SELECT
            s.id, s.name, s.source_key, s.feed_url, s.source_type,
            s.active, s.last_fetch_at, s.last_fetch_error,
            s.workspace_id, s.created_at, s.updated_at,
            COALESCE(items.total_items, 0)        AS total_items,
            COALESCE(items.unread_count, 0)       AS unread_count,
            COALESCE(items.auto_ignored_count, 0) AS auto_ignored_count,
            COALESCE(scores.score, 0)             AS score,
            COALESCE(scores.upvotes, 0)           AS upvotes,
            COALESCE(scores.downvotes, 0)         AS downvotes,
            COALESCE(scores.reads, 0)             AS reads
        FROM inbox_sources s
        LEFT JOIN (
            SELECT
                REPLACE(
                    LOWER(
                        SUBSTR(
                            REPLACE(REPLACE(url, 'https://', ''), 'http://', ''),
                            1,
                            CASE
                                WHEN INSTR(REPLACE(REPLACE(url, 'https://', ''), 'http://', ''), '/') > 0
                                THEN INSTR(REPLACE(REPLACE(url, 'https://', ''), 'http://', ''), '/') - 1
                                ELSE LENGTH(REPLACE(REPLACE(url, 'https://', ''), 'http://', ''))
                            END
                        )
                    ),
                    'www.', ''
                ) AS domain_key,
                COUNT(*) AS total_items,
                SUM(CASE WHEN status = 'unread' THEN 1 ELSE 0 END) AS unread_count,
                SUM(CASE WHEN status = 'auto_ignored' THEN 1 ELSE 0 END) AS auto_ignored_count,
                workspace_id AS ws
            FROM inbox_items
            WHERE url IS NOT NULL AND url != ''
            GROUP BY domain_key, workspace_id
        ) items ON items.domain_key = s.source_key AND items.ws = s.workspace_id
        LEFT JOIN source_scores scores
            ON scores.source_key = s.source_key
           AND scores.workspace_id = s.workspace_id
        WHERE s.id = ? AND s.workspace_id = ?
    """
    cursor = await db.execute(query, (source_id, workspace_id))
    row = await cursor.fetchone()
    return _row_to_dict(row) if row else None


async def create_source(
    db: aiosqlite.Connection,
    workspace_id: str,
    *,
    name: str,
    source_key: str,
    feed_url: str | None = None,
    source_type: str = "rss",
) -> dict:
    """Insert a new source. 409 on duplicate (workspace_id, source_key)."""
    if source_type not in _VALID_SOURCE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid source_type: {source_type}",
        )
    normalized_key = _normalize_source_key(source_key)
    if not normalized_key:
        raise HTTPException(status_code=422, detail="source_key cannot be empty")

    source_id = f"src_{uuid.uuid4().hex[:24]}"
    now = datetime.now(timezone.utc).isoformat()

    try:
        await db.execute(
            "INSERT INTO inbox_sources "
            "(id, name, source_key, feed_url, source_type, active, "
            "workspace_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (
                source_id,
                name.strip(),
                normalized_key,
                feed_url,
                source_type,
                workspace_id,
                now,
                now,
            ),
        )
        await db.commit()
    except aiosqlite.IntegrityError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Source already exists with source_key={normalized_key!r}",
        ) from exc

    result = await get_source(db, workspace_id, source_id)
    if result is None:
        raise HTTPException(status_code=500, detail="Source created but not found")
    return result


async def update_source(
    db: aiosqlite.Connection,
    workspace_id: str,
    source_id: str,
    *,
    name: str | None = None,
    feed_url: str | None = None,
    active: bool | None = None,
) -> dict:
    """Selectively update a source. Returns the updated row."""
    existing = await get_source(db, workspace_id, source_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Source not found")

    updates: list[str] = []
    params: list[object] = []
    if name is not None:
        updates.append("name = ?")
        params.append(name.strip())
    if feed_url is not None:
        updates.append("feed_url = ?")
        params.append(feed_url)
    if active is not None:
        updates.append("active = ?")
        params.append(1 if active else 0)

    if not updates:
        return existing

    updates.append("updated_at = ?")
    params.append(datetime.now(timezone.utc).isoformat())
    params.append(source_id)
    params.append(workspace_id)

    await db.execute(
        f"UPDATE inbox_sources SET {', '.join(updates)} "
        "WHERE id = ? AND workspace_id = ?",
        tuple(params),
    )
    await db.commit()

    result = await get_source(db, workspace_id, source_id)
    if result is None:  # pragma: no cover - defensive
        raise HTTPException(status_code=500, detail="Source disappeared after update")
    return result


async def delete_source(
    db: aiosqlite.Connection,
    workspace_id: str,
    source_id: str,
) -> None:
    """Soft delete: set active = 0. Keeps historical data intact."""
    existing = await get_source(db, workspace_id, source_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Source not found")

    await db.execute(
        "UPDATE inbox_sources SET active = 0, updated_at = ? "
        "WHERE id = ? AND workspace_id = ?",
        (datetime.now(timezone.utc).isoformat(), source_id, workspace_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


async def get_source_metrics(
    db: aiosqlite.Connection,
    workspace_id: str,
    source_id: str,
    *,
    range: MetricRange = "total",  # noqa: A002 - deliberate shadow of builtin for API ergonomics
) -> dict:
    """Return detailed metrics for a source, filtered by a time range.

    Range options:
      - "24h"  -> items created in last 24h
      - "7d"   -> items created in last 7d
      - "30d"  -> items created in last 30d
      - "total"-> all-time (default)
    """
    if range not in _VALID_RANGES:
        raise HTTPException(status_code=422, detail=f"Invalid range: {range}")

    source_row = await (
        await db.execute(
            "SELECT source_key FROM inbox_sources "
            "WHERE id = ? AND workspace_id = ?",
            (source_id, workspace_id),
        )
    ).fetchone()
    if source_row is None:
        raise HTTPException(status_code=404, detail="Source not found")
    source_key = source_row["source_key"]

    cutoff = _range_to_cutoff(range)
    where_items = "source = ? AND workspace_id = ?"
    params: list[object] = [source_key, workspace_id]
    if cutoff is not None:
        where_items += " AND created_at >= ?"
        params.append(cutoff)

    row = await (
        await db.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'unread'       THEN 1 ELSE 0 END) AS unread,
                SUM(CASE WHEN status = 'read'         THEN 1 ELSE 0 END) AS read_items,
                SUM(CASE WHEN status = 'saved'        THEN 1 ELSE 0 END) AS saved,
                SUM(CASE WHEN status = 'newsletter'   THEN 1 ELSE 0 END) AS newsletter,
                SUM(CASE WHEN status = 'preferred'    THEN 1 ELSE 0 END) AS preferred,
                SUM(CASE WHEN status = 'idea'         THEN 1 ELSE 0 END) AS idea,
                SUM(CASE WHEN status = 'ignored'      THEN 1 ELSE 0 END) AS ignored,
                SUM(CASE WHEN status = 'auto_ignored' THEN 1 ELSE 0 END) AS auto_ignored
            FROM inbox_items
            WHERE {where_items}
            """,
            tuple(params),
        )
    ).fetchone()

    # Pull the score row (cumulative, range-independent — score is an aggregate)
    score_row = await (
        await db.execute(
            "SELECT score, upvotes, downvotes, reads FROM source_scores "
            "WHERE workspace_id = ? AND source_key = ?",
            (workspace_id, source_key),
        )
    ).fetchone()

    return {
        "source_id": source_id,
        "source_key": source_key,
        "range": range,
        "cutoff": cutoff,
        "total": int(row["total"] or 0),
        "unread": int(row["unread"] or 0),
        "read": int(row["read_items"] or 0),
        "saved": int(row["saved"] or 0),
        "newsletter": int(row["newsletter"] or 0),
        "preferred": int(row["preferred"] or 0),
        "idea": int(row["idea"] or 0),
        "ignored": int(row["ignored"] or 0),
        "auto_ignored": int(row["auto_ignored"] or 0),
        "score": float(score_row["score"]) if score_row else 0.0,
        "upvotes": int(score_row["upvotes"]) if score_row else 0,
        "downvotes": int(score_row["downvotes"]) if score_row else 0,
        "reads": int(score_row["reads"]) if score_row else 0,
    }
