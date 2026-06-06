from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlparse

import aiosqlite

from core.api.models import (
    InboxItemDetail,
    InboxItemSummary,
    InboxStatsResponse,
    InboxStatusUpdateRequest,
    InboxTaxonomyUpdateRequest,
    InboxTaxonomyUpdateResponse,
    InboxTriageDecisionRequest,
    InboxTriageDecisionResponse,
    UserInfo,
)
from core.api.use_cases._errors import (
    ConflictError,
    NotFoundError,
    ServiceUnavailableError,
)

logger = logging.getLogger(__name__)

# Differentiated scoring weights per status (PR A of inbox redesign, 2026-04-11).
# Immutable via MappingProxyType to prevent accidental runtime mutation.
# - newsletter +3: "gold public" signal (Emilio sends it to subscribers)
# - preferred  +2: "gold private" signal (Emilio wants to see more like this)
# - saved/idea +1: "useful" signal
# - read    +0.1: "attention" signal (weak but non-zero: reading is information)
# - auto_ignored 0: LLM auto-decision, zero weight (no write)
# - ignored   -1: "noise" signal
SCORE_WEIGHTS: Mapping[str, float] = MappingProxyType(
    {
        "newsletter": 3.0,
        "preferred": 2.0,
        "saved": 1.0,
        "idea": 1.0,
        "read": 0.1,
        "auto_ignored": 0.0,
        "ignored": -1.0,
    }
)


def _pick(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping and mapping[name] is not None:
            return mapping[name]
    return None


async def _get_table_columns(db: aiosqlite.Connection, table_name: str) -> set[str]:
    rows = await (await db.execute(f"PRAGMA table_info({table_name})")).fetchall()
    return {
        row[1] if not isinstance(row, aiosqlite.Row) else row["name"] for row in rows
    }


async def ensure_inbox_core_available(db: aiosqlite.Connection) -> set[str]:
    columns = await _get_table_columns(db, "inbox_items")
    if not columns:
        raise ServiceUnavailableError(
            code="inbox_core_unavailable",
            message=(
                "Inbox core table 'inbox_items' not found. "
                "This contract assumes the parallel inbox core branch provides it."
            ),
        )
    return columns


def _normalize_payload(raw_value: Any) -> Any:
    if raw_value is None:
        return None
    if isinstance(raw_value, (dict, list)):
        return raw_value
    if isinstance(raw_value, str):
        text = raw_value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return raw_value
    return raw_value


def _decision_from_row(row: aiosqlite.Row | None) -> InboxTriageDecisionResponse | None:
    if row is None or row["decision"] is None:
        return None
    tags_raw = row["triage_tags_json"] or "[]"
    try:
        tags = json.loads(tags_raw)
    except (json.JSONDecodeError, TypeError):
        tags = []
    # Use triage_decided_by alias to avoid clash with inbox_items.decided_by
    triage_decided_by = None
    mapping = dict(row)
    if "triage_decided_by" in mapping:
        triage_decided_by = mapping["triage_decided_by"]
    elif "decided_by" in mapping:
        triage_decided_by = mapping["decided_by"]
    return InboxTriageDecisionResponse(
        inbox_item_id=row["id"],
        decision=row["decision"],
        confidence=row["confidence"],
        reason=row["reason"],
        target_program=row["target_program"],
        target_project=row["target_project"],
        task_kind=row["task_kind"],
        task_title=row["task_title"],
        task_description=row["task_description"],
        linked_task_id=row["linked_task_id"],
        tags=tags,
        decided_by=triage_decided_by or "",
        created_at=row["decision_created_at"],
        updated_at=row["decision_updated_at"],
    )


def _coerce_sender_str(sender: Any) -> str | None:
    """Normalize a sender value to a string.

    Email ingestion can store the `From` header as a structured object
    (e.g. {"name": "a16z", "address": "...", "html": "..."}). InboxItemDetail.sender
    is typed `str | None`, so a dict reaches Pydantic and raises a 500 on item
    fetch. Prefer the display name, fall back to address/email, else None.
    """
    if sender is None or isinstance(sender, str):
        return sender
    if isinstance(sender, dict):
        return (
            sender.get("name")
            or sender.get("address")
            or sender.get("email")
            or None
        )
    if isinstance(sender, (list, tuple)) and sender:
        return _coerce_sender_str(sender[0])
    return str(sender)


def _normalize_item(
    row: aiosqlite.Row, include_raw: bool = False
) -> InboxItemSummary | InboxItemDetail:
    mapping = dict(row)
    payload = _normalize_payload(
        _pick(
            mapping,
            "payload_json",
            "metadata_json",
            "raw_payload",
            "payload",
            "raw_content",
        )
    )
    program = _pick(
        mapping, "program", "program_slug", "program_hint", "default_program"
    )
    if program is None:
        candidate_programs_raw = _pick(mapping, "candidate_programs")
        try:
            candidate_programs = json.loads(candidate_programs_raw or "[]")
        except (TypeError, json.JSONDecodeError):
            candidate_programs = []
        if candidate_programs:
            program = candidate_programs[0]
    content = _pick(mapping, "content_text", "body_text", "plain_text", "content")
    triage = _decision_from_row(row)
    source_label = None
    if isinstance(payload, dict):
        source_label = payload.get("feedName") or payload.get("sourceLabel")
    if source_label is None:
        source_label = _pick(
            mapping, "source_label", "feed_title", "mailbox", "source_name", "source"
        )
    sender = _pick(mapping, "sender", "from_email", "author", "source_author")
    if sender is None and isinstance(payload, dict):
        sender = payload.get("sender") or payload.get("author")
    sender = _coerce_sender_str(sender)
    item_status = _pick(mapping, "status") or "unread"
    ignore_reason = _pick(mapping, "ignore_reason")
    base = dict(
        id=str(mapping["id"]),
        source_type=_pick(mapping, "source_type", "source_kind", "channel", "source"),
        source_label=source_label,
        external_id=_pick(
            mapping, "external_id", "message_id", "source_item_id", "item_key"
        ),
        title=_pick(mapping, "title", "subject", "name"),
        snippet=_pick(mapping, "summary", "snippet", "body_preview", "excerpt")
        or (content[:160] if isinstance(content, str) else None),
        sender=sender,
        url=_pick(mapping, "url", "source_url", "link"),
        program=program,
        project=_pick(mapping, "project", "project_slug", "project_hint"),
        topic=_pick(mapping, "topic") or "general",
        treatment=_pick(mapping, "treatment") or "read",
        status=item_status,
        ignore_reason=ignore_reason,
        received_at=_pick(
            mapping, "received_at", "created_at", "ingested_at", "updated_at"
        ),
        needs_triage=item_status == "unread",
        triage=triage,
    )
    if include_raw:
        return InboxItemDetail(
            **base,
            content=content,
            raw_payload=payload,
            tldr=_pick(mapping, "tldr"),
            deep_research=_pick(mapping, "deep_research"),
        )
    return InboxItemSummary(**base)


async def list_inbox_items(
    db: aiosqlite.Connection,
    user: UserInfo,
    *,
    limit: int,
    needs_triage: bool,
    classified: bool | None = None,
    source: str | None = None,
    program: str | None = None,
    topic: str | None = None,
    treatment: str | None = None,
    status: str | None = None,
) -> list[InboxItemSummary]:
    columns = await ensure_inbox_core_available(db)
    order_col = next(
        (
            name
            for name in ("received_at", "created_at", "ingested_at", "updated_at", "id")
            if name in columns
        ),
        "id",
    )
    workspace_id = user.workspace_id or "ws_default"
    params: list[Any] = [workspace_id]
    where: list[str] = []
    if "workspace_id" in columns:
        where.append("COALESCE(i.workspace_id, 'ws_default') = ?")
    else:
        params = []
    if needs_triage and not status:
        where.append("i.status = 'unread'")
    if status:
        where.append("i.status = ?")
        params.append(status)
    if source:
        where.append("i.source = ?")
        params.append(source)
    if program:
        where.append("i.default_program = ?")
        params.append(program)
    if topic:
        where.append("i.topic = ?")
        params.append(topic)
    if treatment:
        where.append("i.treatment = ?")
        params.append(treatment)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    query = (
        "SELECT i.*, "
        "d.decision, d.confidence, d.reason, d.target_program, d.target_project, d.task_kind, "
        "d.task_title, d.task_description, d.linked_task_id, d.tags_json AS triage_tags_json, "
        "d.decided_by AS triage_decided_by, d.created_at AS decision_created_at, d.updated_at AS decision_updated_at "
        "FROM inbox_items i "
        "LEFT JOIN inbox_triage_decisions d "
        "ON d.inbox_item_id = i.id AND COALESCE(d.workspace_id, 'ws_default') = ? "
        f"{where_sql} ORDER BY i.{order_col} DESC LIMIT ?"
    )
    params = (
        [workspace_id, *params, limit]
        if "workspace_id" in columns
        else [workspace_id, limit]
    )
    rows = await (await db.execute(query, params)).fetchall()
    items: list[InboxItemSummary] = []
    for row in rows:
        payload = _normalize_payload(row["metadata_json"]) or {}
        is_classified = isinstance(payload, dict) and bool(payload.get("classifiedAt"))
        if classified is not None and is_classified != classified:
            continue
        items.append(_normalize_item(row))
    return items


async def update_inbox_taxonomy(
    db: aiosqlite.Connection,
    user: UserInfo,
    inbox_item_id: str,
    body: InboxTaxonomyUpdateRequest,
) -> InboxTaxonomyUpdateResponse:
    columns = await ensure_inbox_core_available(db)
    workspace_id = user.workspace_id or "ws_default"
    where = ["id = ?"]
    params: list[Any] = [inbox_item_id]
    if "workspace_id" in columns:
        where.append("COALESCE(workspace_id, 'ws_default') = ?")
        params.append(workspace_id)
    row = await (
        await db.execute(
            f"SELECT id, metadata_json FROM inbox_items WHERE {' AND '.join(where)}",
            params,
        )
    ).fetchone()
    if row is None:
        raise NotFoundError(code="inbox_item_not_found", message="Inbox item not found")

    metadata = _normalize_payload(row["metadata_json"]) or {}
    if not isinstance(metadata, dict):
        metadata = {}
    if body.confidence is not None:
        metadata["confidence"] = body.confidence
    if body.note:
        metadata["note"] = body.note
    metadata["classifiedAt"] = datetime.now(timezone.utc).isoformat()

    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE inbox_items SET topic = ?, treatment = ?, metadata_json = ?, updated_at = ? WHERE id = ?"
        + (
            " AND COALESCE(workspace_id, 'ws_default') = ?"
            if "workspace_id" in columns
            else ""
        ),
        [
            body.topic,
            body.treatment,
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            now,
            inbox_item_id,
            *([workspace_id] if "workspace_id" in columns else []),
        ],
    )
    await db.commit()
    return InboxTaxonomyUpdateResponse(
        inbox_item_id=inbox_item_id,
        topic=body.topic,
        treatment=body.treatment,
        updated_at=now,
    )


async def get_inbox_stats(
    db: aiosqlite.Connection,
    user: UserInfo,
    *,
    needs_triage: bool,
    classified: bool | None = None,
    source: str | None = None,
    program: str | None = None,
    topic: str | None = None,
    treatment: str | None = None,
    status: str | None = None,
) -> InboxStatsResponse:
    columns = await ensure_inbox_core_available(db)
    workspace_id = user.workspace_id or "ws_default"
    where: list[str] = []
    params: list[Any] = [workspace_id]
    if "workspace_id" in columns:
        where.append("COALESCE(i.workspace_id, 'ws_default') = ?")
    else:
        params = []
    if needs_triage and not status:
        where.append("i.status = 'unread'")
    if status:
        where.append("i.status = ?")
        params.append(status)
    if source:
        where.append("i.source = ?")
        params.append(source)
    if program:
        where.append("i.default_program = ?")
        params.append(program)
    if topic:
        where.append("i.topic = ?")
        params.append(topic)
    if treatment:
        where.append("i.treatment = ?")
        params.append(treatment)
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    query = (
        "SELECT "
        "COUNT(*) AS total, "
        "SUM(CASE WHEN d.task_kind = 'idea' THEN 1 ELSE 0 END) AS ideas, "
        "SUM(CASE WHEN d.task_kind = 'normal' THEN 1 ELSE 0 END) AS tasks, "
        "SUM(CASE WHEN d.decision = 'needs_human_review' THEN 1 ELSE 0 END) AS review, "
        "SUM(CASE WHEN i.status = 'unread' THEN 1 ELSE 0 END) AS unread, "
        "SUM(CASE WHEN i.status = 'read' THEN 1 ELSE 0 END) AS status_read, "
        "SUM(CASE WHEN i.status = 'saved' THEN 1 ELSE 0 END) AS status_saved, "
        "SUM(CASE WHEN i.status IN ('ignored', 'auto_ignored') THEN 1 ELSE 0 END) AS status_ignored "
        "FROM inbox_items i "
        "LEFT JOIN inbox_triage_decisions d "
        "ON d.inbox_item_id = i.id AND COALESCE(d.workspace_id, 'ws_default') = ? "
        f"{where_sql}"
    )
    final_params = (
        [workspace_id, *params] if "workspace_id" in columns else [workspace_id]
    )
    row = await (await db.execute(query, final_params)).fetchone()
    stats = InboxStatsResponse(
        total=int(row["total"] or 0),
        ideas=int(row["ideas"] or 0),
        tasks=int(row["tasks"] or 0),
        review=int(row["review"] or 0),
        unread=int(row["unread"] or 0),
        read=int(row["status_read"] or 0),
        saved=int(row["status_saved"] or 0),
        ignored=int(row["status_ignored"] or 0),
    )
    if classified is None:
        return stats

    classified_items = await list_inbox_items(
        db,
        user,
        limit=500,
        needs_triage=needs_triage,
        classified=classified,
        source=source,
        program=program,
        topic=topic,
        treatment=treatment,
        status=status,
    )
    return InboxStatsResponse(
        total=len(classified_items),
        ideas=stats.ideas if not needs_triage else 0,
        tasks=stats.tasks if not needs_triage else 0,
        review=stats.review if not needs_triage else 0,
        unread=stats.unread,
        read=stats.read,
        saved=stats.saved,
        ignored=stats.ignored,
    )


async def get_inbox_item_detail(
    db: aiosqlite.Connection, user: UserInfo, inbox_item_id: str
) -> InboxItemDetail:
    columns = await ensure_inbox_core_available(db)
    workspace_id = user.workspace_id or "ws_default"
    where = ["i.id = ?"]
    params: list[Any] = [workspace_id, inbox_item_id]
    if "workspace_id" in columns:
        where.append("COALESCE(i.workspace_id, 'ws_default') = ?")
        params.append(workspace_id)
    query = (
        "SELECT i.*, "
        "d.decision, d.confidence, d.reason, d.target_program, d.target_project, d.task_kind, "
        "d.task_title, d.task_description, d.linked_task_id, d.tags_json AS triage_tags_json, "
        "d.decided_by AS triage_decided_by, d.created_at AS decision_created_at, d.updated_at AS decision_updated_at "
        "FROM inbox_items i "
        "LEFT JOIN inbox_triage_decisions d "
        "ON d.inbox_item_id = i.id AND COALESCE(d.workspace_id, 'ws_default') = ? "
        f"WHERE {' AND '.join(where)}"
    )
    row = await (await db.execute(query, params)).fetchone()
    if row is None:
        raise NotFoundError(code="inbox_item_not_found", message="Inbox item not found")
    return _normalize_item(row, include_raw=True)


async def _tasks_has_kind_column(db: aiosqlite.Connection) -> bool:
    return "kind" in await _get_table_columns(db, "tasks")


async def _task_exists(
    db: aiosqlite.Connection, task_id: str, workspace_id: str
) -> bool:
    row = await (
        await db.execute(
            "SELECT id FROM tasks WHERE id = ? AND deleted_at IS NULL AND COALESCE(workspace_id, 'ws_default') = ?",
            (task_id, workspace_id),
        )
    ).fetchone()
    return row is not None


async def _create_triage_task(
    db: aiosqlite.Connection,
    user: UserInfo,
    inbox_item_id: str,
    body: InboxTriageDecisionRequest,
) -> str:
    task_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    tags = ["inboxx"]
    tags.extend(tag for tag in body.tags if tag not in tags)
    tags_json = json.dumps(tags)
    has_kind = await _tasks_has_kind_column(db)
    task_kind = body.task_kind or (
        "idea" if body.decision == "create_idea" else "normal"
    )
    columns = [
        "id",
        "title",
        "description",
        "status",
        "project",
        "priority",
        "created_by",
        "owner_id",
        "source",
        "source_ref",
        "tags",
        "workspace_id",
        "created_at",
        "updated_at",
    ]
    values = [
        task_id,
        body.task_title,
        body.task_description or body.reason,
        "pending",
        body.target_project,
        "medium",
        user.username,
        None,
        "console",
        f"inbox:{inbox_item_id}",
        tags_json,
        user.workspace_id or "ws_default",
        now,
        now,
    ]
    if has_kind:
        columns.insert(5, "kind")
        values.insert(5, task_kind)
    placeholders = ", ".join("?" for _ in columns)
    await db.execute(
        f"INSERT INTO tasks ({', '.join(columns)}) VALUES ({placeholders})",
        values,
    )
    return task_id


async def apply_triage_decision(
    db: aiosqlite.Connection,
    user: UserInfo,
    inbox_item_id: str,
    body: InboxTriageDecisionRequest,
) -> InboxTriageDecisionResponse:
    columns = await ensure_inbox_core_available(db)
    workspace_id = user.workspace_id or "ws_default"
    item_query = "SELECT id FROM inbox_items WHERE id = ?"
    item_params: list[Any] = [inbox_item_id]
    if "workspace_id" in columns:
        item_query += " AND COALESCE(workspace_id, 'ws_default') = ?"
        item_params.append(workspace_id)
    row = await (await db.execute(item_query, item_params)).fetchone()
    if row is None:
        raise NotFoundError(code="inbox_item_not_found", message="Inbox item not found")

    existing = await (
        await db.execute(
            "SELECT linked_task_id, decision FROM inbox_triage_decisions WHERE inbox_item_id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
            (inbox_item_id, workspace_id),
        )
    ).fetchone()

    linked_task_id = body.linked_task_id
    if body.decision in {"create_idea", "create_task"}:
        if (
            existing
            and existing["linked_task_id"]
            and existing["linked_task_id"] != linked_task_id
        ):
            raise ConflictError(
                code="inbox_item_already_linked",
                message=(
                    "This inbox item is already linked to a task. "
                    "Reuse linked_task_id instead of creating a duplicate."
                ),
            )
        if linked_task_id:
            if not await _task_exists(db, linked_task_id, workspace_id):
                raise NotFoundError(
                    code="linked_task_not_found", message="Linked task not found"
                )
        else:
            linked_task_id = await _create_triage_task(db, user, inbox_item_id, body)

    now = datetime.now(timezone.utc).isoformat()
    task_kind = body.task_kind or (
        "idea"
        if body.decision == "create_idea"
        else "normal"
        if body.decision == "create_task"
        else None
    )
    tags_json = json.dumps(body.tags)
    decision_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO inbox_triage_decisions (id, inbox_item_id, decision, reason, confidence, target_program, "
        "target_project, task_kind, task_title, task_description, linked_task_id, tags_json, decided_by, workspace_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(inbox_item_id, workspace_id) DO UPDATE SET "
        "decision=excluded.decision, reason=excluded.reason, confidence=excluded.confidence, "
        "target_program=excluded.target_program, target_project=excluded.target_project, task_kind=excluded.task_kind, "
        "task_title=excluded.task_title, task_description=excluded.task_description, linked_task_id=excluded.linked_task_id, "
        "tags_json=excluded.tags_json, decided_by=excluded.decided_by, updated_at=excluded.updated_at",
        (
            decision_id,
            inbox_item_id,
            body.decision,
            body.reason,
            body.confidence,
            body.target_program,
            body.target_project,
            task_kind,
            body.task_title,
            body.task_description,
            linked_task_id,
            tags_json,
            user.username,
            workspace_id,
            now,
            now,
        ),
    )
    await db.commit()

    saved = await (
        await db.execute(
            "SELECT ? AS inbox_item_id, decision, confidence, reason, target_program, target_project, task_kind, task_title, "
            "task_description, linked_task_id, tags_json, decided_by, created_at, updated_at "
            "FROM inbox_triage_decisions WHERE inbox_item_id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
            (inbox_item_id, inbox_item_id, workspace_id),
        )
    ).fetchone()
    return InboxTriageDecisionResponse(
        inbox_item_id=inbox_item_id,
        decision=saved["decision"],
        confidence=saved["confidence"],
        reason=saved["reason"],
        target_program=saved["target_program"],
        target_project=saved["target_project"],
        task_kind=saved["task_kind"],
        task_title=saved["task_title"],
        task_description=saved["task_description"],
        linked_task_id=saved["linked_task_id"],
        tags=json.loads(saved["tags_json"] or "[]"),
        decided_by=saved["decided_by"],
        created_at=saved["created_at"],
        updated_at=saved["updated_at"],
    )


async def _update_source_score(
    db: aiosqlite.Connection,
    inbox_item_id: str,
    workspace_id: str,
    new_status: str,
) -> None:
    """Update source_scores with differentiated weights.

    Uses the SCORE_WEIGHTS table (see module-level constant). Weights:
      newsletter=+3, preferred=+2, saved=+1, idea=+1, read=+0.1,
      auto_ignored=0 (no write), ignored=-1.

    Differentiated counters are also tracked:
      - upvotes: any "positive" signal (saved, newsletter, idea, preferred)
      - downvotes: ignored only
      - reads: read only (the new counter from migration 061)
    """
    if new_status not in SCORE_WEIGHTS:
        return

    score_delta = SCORE_WEIGHTS[new_status]
    if score_delta == 0:
        # auto_ignored (or any future neutral status): no-op, skip the write
        return

    # Get source_key from the item's URL domain (most useful for scoring)
    row = await (
        await db.execute(
            "SELECT source, url, domain_key, metadata_json FROM inbox_items WHERE id = ?",
            (inbox_item_id,),
        )
    ).fetchone()
    if row is None:
        return

    source_key = row["domain_key"] or row["source"]
    # Prefer URL domain as source_key (e.g. "notboring.co", "simonwillison.net").
    # Normalization MUST match _migration_062_backfill_from_urls so the JOIN
    # between source_scores and inbox_sources actually matches rows.
    url = row["url"]
    if url:
        try:
            netloc = urlparse(url).netloc
            if netloc:
                source_key = netloc.removeprefix("www.").lower()
        except Exception:  # noqa: BLE001 - defensive; source_key stays as fallback
            pass

    if not source_key:
        return

    is_read = new_status == "read"
    is_upvote = new_status in ("saved", "newsletter", "idea", "preferred")
    is_downvote = new_status == "ignored"

    upvote_delta = 1 if is_upvote else 0
    downvote_delta = 1 if is_downvote else 0
    reads_delta = 1 if is_read else 0
    now = datetime.now(timezone.utc).isoformat()

    await db.execute(
        "INSERT INTO source_scores "
        "(id, source_key, score, upvotes, downvotes, reads, workspace_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(workspace_id, source_key) DO UPDATE SET "
        "score = source_scores.score + ?, "
        "upvotes = source_scores.upvotes + ?, "
        "downvotes = source_scores.downvotes + ?, "
        "reads = source_scores.reads + ?, "
        "updated_at = ?",
        (
            str(uuid.uuid4()),
            source_key,
            score_delta,
            upvote_delta,
            downvote_delta,
            reads_delta,
            workspace_id,
            now,
            now,
            score_delta,
            upvote_delta,
            downvote_delta,
            reads_delta,
            now,
        ),
    )


async def update_inbox_status(
    db: aiosqlite.Connection,
    user: UserInfo,
    inbox_item_id: str,
    body: InboxStatusUpdateRequest,
) -> dict[str, Any]:
    """Update the status of an inbox item. decided_by is set server-side."""
    columns = await ensure_inbox_core_available(db)
    workspace_id = user.workspace_id or "ws_default"
    where = ["id = ?"]
    params: list[Any] = [inbox_item_id]
    if "workspace_id" in columns:
        where.append("COALESCE(workspace_id, 'ws_default') = ?")
        params.append(workspace_id)
    row = await (
        await db.execute(
            f"SELECT id FROM inbox_items WHERE {' AND '.join(where)}",
            params,
        )
    ).fetchone()
    if row is None:
        raise NotFoundError(code="inbox_item_not_found", message="Inbox item not found")

    now = datetime.now(timezone.utc).isoformat()
    update_fields = [
        "status = ?",
        "decided_by = ?",
        "decided_at = ?",
        "updated_at = ?",
    ]
    update_params: list[Any] = [body.status, user.username, now, now]

    if body.ignore_reason:
        update_fields.append("ignore_reason = ?")
        update_params.append(body.ignore_reason)
    else:
        update_fields.append("ignore_reason = NULL")

    where_clause = ["id = ?"]
    update_params.append(inbox_item_id)
    if "workspace_id" in columns:
        where_clause.append("COALESCE(workspace_id, 'ws_default') = ?")
        update_params.append(workspace_id)

    await db.execute(
        f"UPDATE inbox_items SET {', '.join(update_fields)} WHERE {' AND '.join(where_clause)}",
        update_params,
    )

    if body.status != "unread":
        try:
            from core.api.services.inbox_digest import remove_item_from_digest_selection

            await remove_item_from_digest_selection(
                db,
                workspace_id=workspace_id,
                inbox_item_id=inbox_item_id,
            )
        except Exception:
            # Non-critical: do not block manual state changes on digest cleanup.
            pass

    # Update source score based on status decision
    try:
        await _update_source_score(db, inbox_item_id, workspace_id, body.status)
    except Exception:
        # Non-critical: don't fail the status update if score tracking fails
        pass

    await db.commit()

    # Auto-generate deep research for newsletter items (non-blocking)
    if body.status == "newsletter":
        import asyncio

        asyncio.create_task(_auto_generate_deep_research(inbox_item_id, workspace_id))

    return {
        "inbox_item_id": inbox_item_id,
        "status": body.status,
        "ignore_reason": body.ignore_reason,
        "decided_by": user.username,
        "decided_at": now,
    }


async def _auto_generate_deep_research(
    inbox_item_id: str,
    workspace_id: str,
) -> None:
    """Auto-generate deep research for newsletter items in background."""
    try:
        from core.api.services.inbox_tldr import get_or_generate_deep_research

        await get_or_generate_deep_research(inbox_item_id, workspace_id)
        logger.info(
            "Auto-generated deep research for newsletter item %s", inbox_item_id
        )
    except Exception as exc:
        logger.warning("Auto deep research failed for %s: %s", inbox_item_id, exc)


async def get_source_scores(
    db: aiosqlite.Connection,
    workspace_id: str,
) -> list[dict[str, Any]]:
    """Return all source scores for the workspace."""
    rows = await (
        await db.execute(
            "SELECT source_key, score, upvotes, downvotes "
            "FROM source_scores WHERE workspace_id = ? ORDER BY score DESC",
            (workspace_id,),
        )
    ).fetchall()
    return [
        {
            "source_key": row["source_key"],
            "score": row["score"],
            "upvotes": row["upvotes"],
            "downvotes": row["downvotes"],
        }
        for row in rows
    ]


async def get_unread_count(
    db: aiosqlite.Connection,
    user: UserInfo,
) -> int:
    """Lightweight unread count query for badge polling."""
    columns = await ensure_inbox_core_available(db)
    workspace_id = user.workspace_id or "ws_default"

    if "workspace_id" in columns:
        row = await (
            await db.execute(
                "SELECT COUNT(*) AS cnt FROM inbox_items "
                "WHERE status = 'unread' AND COALESCE(workspace_id, 'ws_default') = ?",
                (workspace_id,),
            )
        ).fetchone()
    else:
        row = await (
            await db.execute(
                "SELECT COUNT(*) AS cnt FROM inbox_items WHERE status = 'unread'"
            )
        ).fetchone()

    return int(row["cnt"] or 0)
