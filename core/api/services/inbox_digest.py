from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Literal

import aiosqlite

from core.api.models.inbox import VALID_DIGEST_SELECTION_STATES

DigestSelectionState = Literal["visible", "overflow", "expired"]


def _validate_state(state: str) -> DigestSelectionState:
    if state not in VALID_DIGEST_SELECTION_STATES:
        raise ValueError(f"invalid digest selection state: {state!r}")
    return state  # type: ignore[return-value]


def _row_to_dict(row: aiosqlite.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


async def get_active_digest_selection(
    db: aiosqlite.Connection,
    workspace_id: str,
    inbox_item_id: str,
) -> dict[str, Any] | None:
    row = await (
        await db.execute(
            "SELECT * FROM inbox_digest_selections "
            "WHERE workspace_id = ? AND inbox_item_id = ? AND state IN ('visible', 'overflow') "
            "ORDER BY updated_at DESC LIMIT 1",
            (workspace_id, inbox_item_id),
        )
    ).fetchone()
    return _row_to_dict(row)


async def upsert_digest_selection(
    db: aiosqlite.Connection,
    *,
    inbox_item_id: str,
    workspace_id: str,
    digest_cycle_key: str,
    state: DigestSelectionState,
    domain_key: str,
    score: float,
    rank_in_domain: int | None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    state = _validate_state(state)
    now = datetime.now(timezone.utc).isoformat()
    existing = await get_active_digest_selection(db, workspace_id, inbox_item_id)
    if existing is not None:
        await db.execute(
            "UPDATE inbox_digest_selections SET digest_cycle_key = ?, state = ?, domain_key = ?, "
            "score = ?, rank_in_domain = ?, expires_at = ?, updated_at = ? "
            "WHERE id = ?",
            (
                digest_cycle_key,
                state,
                domain_key,
                float(score),
                rank_in_domain,
                expires_at,
                now,
                existing["id"],
            ),
        )
        row = await (
            await db.execute(
                "SELECT * FROM inbox_digest_selections WHERE id = ?",
                (existing["id"],),
            )
        ).fetchone()
        return _row_to_dict(row) or {}

    selection_id = f"digsel_{uuid.uuid4().hex[:24]}"
    await db.execute(
        "INSERT INTO inbox_digest_selections ("
        "id, inbox_item_id, digest_cycle_key, state, domain_key, score, rank_in_domain, expires_at, workspace_id, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            selection_id,
            inbox_item_id,
            digest_cycle_key,
            state,
            domain_key,
            float(score),
            rank_in_domain,
            expires_at,
            workspace_id,
            now,
            now,
        ),
    )
    row = await (
        await db.execute(
            "SELECT * FROM inbox_digest_selections WHERE id = ?",
            (selection_id,),
        )
    ).fetchone()
    return _row_to_dict(row) or {}


async def remove_item_from_digest_selection(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    inbox_item_id: str,
) -> int:
    cursor = await db.execute(
        "DELETE FROM inbox_digest_selections "
        "WHERE workspace_id = ? AND inbox_item_id = ? AND state IN ('visible', 'overflow')",
        (workspace_id, inbox_item_id),
    )
    return int(cursor.rowcount or 0)


async def get_current_digest_cycle_key(
    db: aiosqlite.Connection,
    workspace_id: str,
) -> str:
    row = await (
        await db.execute(
            "SELECT value FROM app_settings WHERE key = 'inbox_daily_digest_last_cycle_key'"
        )
    ).fetchone()
    if row is None:
        return ""
    return (row[0] if not hasattr(row, "keys") else row["value"]) or ""


async def list_digest_items(
    db: aiosqlite.Connection,
    workspace_id: str,
    *,
    state: DigestSelectionState,
    digest_cycle_key: str | None = None,
    domain_key: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    state = _validate_state(state)
    effective_cycle_key = digest_cycle_key
    if effective_cycle_key is None and state in ("visible", "overflow"):
        effective_cycle_key = await get_current_digest_cycle_key(db, workspace_id)

    where = ["s.workspace_id = ?", "s.state = ?"]
    params: list[Any] = [workspace_id, state]
    if effective_cycle_key:
        where.append("s.digest_cycle_key = ?")
        params.append(effective_cycle_key)
    if domain_key:
        where.append("s.domain_key = ?")
        params.append(domain_key)
    params.append(limit)

    rows = await (
        await db.execute(
            "SELECT s.id, s.inbox_item_id, s.digest_cycle_key, s.state, s.domain_key, s.score, s.rank_in_domain, s.expires_at, "
            "i.status, i.title, i.url, i.topic, i.treatment, i.created_at, i.updated_at "
            "FROM inbox_digest_selections s "
            "JOIN inbox_items i ON i.id = s.inbox_item_id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY s.domain_key ASC, s.rank_in_domain ASC, s.updated_at DESC LIMIT ?",
            tuple(params),
        )
    ).fetchall()
    return [{key: row[key] for key in row.keys()} for row in rows]


async def get_digest_stats(
    db: aiosqlite.Connection,
    workspace_id: str,
) -> dict[str, Any]:
    cycle_key = await get_current_digest_cycle_key(db, workspace_id)
    visible = await (
        await db.execute(
            "SELECT COUNT(*) FROM inbox_digest_selections WHERE workspace_id = ? AND state = 'visible'"
            + (" AND digest_cycle_key = ?" if cycle_key else ""),
            (workspace_id, cycle_key) if cycle_key else (workspace_id,),
        )
    ).fetchone()
    overflow = await (
        await db.execute(
            "SELECT COUNT(*) FROM inbox_digest_selections WHERE workspace_id = ? AND state = 'overflow'"
            + (" AND digest_cycle_key = ?" if cycle_key else ""),
            (workspace_id, cycle_key) if cycle_key else (workspace_id,),
        )
    ).fetchone()
    expired = await (
        await db.execute(
            "SELECT COUNT(*) FROM inbox_digest_selections WHERE workspace_id = ? AND state = 'expired'",
            (workspace_id,),
        )
    ).fetchone()
    columns = await (await db.execute("PRAGMA table_info(inbox_items)")).fetchall()
    has_deep_research = any(row[1] == "deep_research" for row in columns)
    visible_deep_research_count = 0
    if has_deep_research:
        visible_deep_research = await (
            await db.execute(
                "SELECT COUNT(*) FROM inbox_digest_selections s "
                "JOIN inbox_items i ON i.id = s.inbox_item_id "
                "WHERE s.workspace_id = ? AND s.state = 'visible' "
                + ("AND s.digest_cycle_key = ? " if cycle_key else "")
                + "AND i.deep_research IS NOT NULL AND i.deep_research != ''",
                (workspace_id, cycle_key) if cycle_key else (workspace_id,),
            )
        ).fetchone()
        visible_deep_research_count = int(visible_deep_research[0] or 0)
    return {
        "cycle_key": cycle_key,
        "visible": int(visible[0] or 0),
        "overflow": int(overflow[0] or 0),
        "expired": int(expired[0] or 0),
        "visible_deep_research": visible_deep_research_count,
    }
