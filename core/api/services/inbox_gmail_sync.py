from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from core.api.models import InboxGmailSyncCandidate, InboxGmailSyncCompleteRequest, UserInfo
from core.api.use_cases._errors import NotFoundError

GMAIL_SOURCE = "gmail-marvisx"
LABEL_NEWS = "marvisx-news"
LABEL_IGNORE = "marvisx-ignore"


def _loads_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _classified_at(metadata: dict[str, Any]) -> str | None:
    direct = metadata.get("classifiedAt")
    if isinstance(direct, str) and direct:
        return direct
    classifier = metadata.get("classifier")
    if isinstance(classifier, dict):
        nested = classifier.get("classified_at")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _already_synced(metadata: dict[str, Any]) -> bool:
    sync = metadata.get("gmail_sync")
    return isinstance(sync, dict) and bool(sync.get("synced_at"))


def _confidence(metadata: dict[str, Any]) -> float | None:
    classifier = metadata.get("classifier")
    raw = classifier.get("confidence") if isinstance(classifier, dict) else None
    if raw is None:
        raw = metadata.get("confidence")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _labels_for(treatment: str, status: str) -> tuple[list[str], bool]:
    if treatment == "ignore":
        return [LABEL_IGNORE], True
    if treatment == "save":
        return [LABEL_NEWS], True
    if treatment in {"read", "read_save"}:
        return [LABEL_NEWS], False
    if status in {"auto_ignored", "ignored"}:
        return [LABEL_IGNORE], True
    if status == "saved":
        return [LABEL_NEWS], True
    return [LABEL_NEWS], False


def _candidate_from_row(row: aiosqlite.Row) -> InboxGmailSyncCandidate | None:
    metadata = _loads_metadata(row["metadata_json"])
    if not _classified_at(metadata) or _already_synced(metadata):
        return None
    gmail_message_id = row["source_item_id"]
    if not gmail_message_id:
        return None

    treatment = row["treatment"] or "read"
    status = row["status"] or "unread"
    add_labels, remove_unread = _labels_for(treatment, status)
    return InboxGmailSyncCandidate(
        inbox_item_id=row["id"],
        gmail_message_id=gmail_message_id,
        topic=row["topic"] or "general",
        treatment=treatment,
        status=status,
        confidence=_confidence(metadata),
        add_labels=add_labels,
        remove_unread=remove_unread,
    )


async def list_gmail_sync_candidates(
    db: aiosqlite.Connection,
    workspace_id: str,
    *,
    limit: int = 50,
) -> list[InboxGmailSyncCandidate]:
    """Return classified Gmail items whose backend decision has not been mirrored."""
    scan_limit = max(1, min(limit * 5, 1000))
    rows = await (
        await db.execute(
            """
            SELECT id, source_item_id, status, topic, treatment, metadata_json
            FROM inbox_items
            WHERE source = ?
              AND COALESCE(workspace_id, 'ws_default') = ?
              AND source_item_id IS NOT NULL
              AND source_item_id != ''
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (GMAIL_SOURCE, workspace_id, scan_limit),
        )
    ).fetchall()

    candidates: list[InboxGmailSyncCandidate] = []
    for row in rows:
        candidate = _candidate_from_row(row)
        if candidate is None:
            continue
        candidates.append(candidate)
        if len(candidates) >= limit:
            break
    return candidates


async def mark_gmail_sync_complete(
    db: aiosqlite.Connection,
    user: UserInfo,
    inbox_item_id: str,
    body: InboxGmailSyncCompleteRequest,
) -> dict[str, Any]:
    workspace_id = user.workspace_id or "ws_default"
    row = await (
        await db.execute(
            """
            SELECT metadata_json
            FROM inbox_items
            WHERE id = ?
              AND source = ?
              AND COALESCE(workspace_id, 'ws_default') = ?
            """,
            (inbox_item_id, GMAIL_SOURCE, workspace_id),
        )
    ).fetchone()
    if row is None:
        raise NotFoundError(
            code="gmail_inbox_item_not_found", message="Gmail inbox item not found"
        )

    metadata = _loads_metadata(row["metadata_json"])
    now = datetime.now(timezone.utc).isoformat()
    metadata["gmail_sync"] = {
        "synced_at": now,
        "labels_applied": body.labels_applied,
        "removed_unread": body.removed_unread,
        "synced_by": user.username,
    }

    await db.execute(
        "UPDATE inbox_items SET metadata_json = ?, updated_at = ? WHERE id = ?",
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), now, inbox_item_id),
    )
    await db.commit()
    return {"inbox_item_id": inbox_item_id, "gmail_synced_at": now}
