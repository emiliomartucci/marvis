from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import aiosqlite
import yaml

from core.api.config import settings
from core.api.use_cases._errors import (
    AuthorizationError,
    ConflictError,
    ServiceError,
    ValidationError,
)
from core.api.models.auth import UserInfo
from core.api.models.inbox import (
    InboxIngestBatchItemError,
    InboxIngestBatchRequest,
    InboxIngestBatchResponse,
    InboxIngestRequest,
    InboxIngestResponse,
)
from core.api.services.inbox_taxonomy import (
    infer_topic_from_metadata,
    infer_treatment,
    normalize_inbox_topic,
    normalize_inbox_treatment,
)
from core.api.services.inbox_source_identity import (
    HTTP_URL_RE as _HTTP_URL_RE,
    is_gmail_hosted_url as _shared_is_gmail_hosted_url,
    is_low_signal_source_key,
    normalize_domain_key,
    unwrap_tracking_url,
)

logger = logging.getLogger(__name__)

_PROGRAM_SLUG_RE = r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"

_PUBLISHED_METADATA_KEYS = (
    "published_at",
    "publishedAt",
    "published",
    "pub_date",
    "pubDate",
    "updated_at",
    "updatedAt",
    "date",
)


@dataclass(slots=True)
class InboxSourceConfig:
    name: str
    enabled: bool = True
    candidate_programs: list[str] = field(default_factory=list)
    default_program: str | None = None
    allowed_roots: list[str] = field(default_factory=list)


def _sanitize_text(value: str | None, *, max_length: int) -> str | None:
    if value is None:
        return None
    sanitized = value.replace("\x00", "").replace("\r\n", "\n").strip()
    if not sanitized:
        return None
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length].rstrip()
    return sanitized or None


def _sanitize_metadata(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _sanitize_metadata(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_metadata(item) for item in value]
    if isinstance(value, str):
        return value.replace("\x00", "").strip()
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _load_source_configs() -> dict[str, InboxSourceConfig]:
    raw = settings.inbox_sources_json.strip() or "{}"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Invalid inbox_sources_json: %s", exc)
        return {}

    if not isinstance(payload, dict):
        return {}

    configs: dict[str, InboxSourceConfig] = {}
    for source_name, config in payload.items():
        if not isinstance(config, dict):
            continue
        configs[source_name] = InboxSourceConfig(
            name=source_name,
            enabled=bool(config.get("enabled", True)),
            candidate_programs=[
                str(item) for item in config.get("candidate_programs", [])
            ],
            default_program=str(config["default_program"])
            if config.get("default_program")
            else None,
            allowed_roots=[str(item) for item in config.get("allowed_roots", [])],
        )
    return configs


def get_source_config(source: str) -> InboxSourceConfig:
    config = _load_source_configs().get(source)
    if config is None or not config.enabled:
        raise AuthorizationError(
            code="inbox_source_disabled", message="Inbox source is not enabled"
        )
    return config


def _known_programs() -> set[str]:
    yaml_path = Path.home() / "workspace" / "programs.yaml"
    if not yaml_path.exists():
        return set()
    try:
        programs = yaml.safe_load(yaml_path.read_text()) or {}
    except Exception:
        logger.exception("Failed to load programs.yaml for inbox ingest")
        return set()
    if not isinstance(programs, dict):
        return set()
    return {str(name) for name in programs.keys()}


def _validate_program_slug(program: str) -> str:
    import re

    if not re.match(_PROGRAM_SLUG_RE, program):
        raise ValidationError(
            code="invalid_program_slug", message=f"Invalid program slug: {program}"
        )
    return program


def _merge_program_metadata(
    body: InboxIngestRequest,
    config: InboxSourceConfig,
) -> tuple[list[str], str | None]:
    known = _known_programs()
    requested = [_validate_program_slug(item) for item in body.candidate_programs]
    configured = [_validate_program_slug(item) for item in config.candidate_programs]

    for program in requested + configured:
        if known and program not in known:
            raise ValidationError(
                code="unknown_program", message=f"Unknown program: {program}"
            )

    merged: list[str] = []
    for program in requested + configured:
        if program not in merged:
            merged.append(program)

    default_program = body.default_program or config.default_program
    if default_program is not None:
        default_program = _validate_program_slug(default_program)
        if known and default_program not in known:
            raise ValidationError(
                code="unknown_program", message=f"Unknown program: {default_program}"
            )
        if default_program not in merged:
            merged.insert(0, default_program)
    elif merged:
        default_program = merged[0]

    return merged, default_program


def _normalize_url(url: str | None) -> str | None:
    if url is None:
        return None
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationError(code="invalid_url", message="Invalid url")
    return url


def _normalize_domain_key(raw: str | None) -> str | None:
    return normalize_domain_key(raw)


def _normalize_sender_identity(raw: str | None) -> str | None:
    if not raw:
        return None
    _, email = parseaddr(raw)
    candidate = (email or raw).strip().lower()
    if not candidate or "@" not in candidate:
        return None
    local, domain = candidate.rsplit("@", 1)
    local = local.strip()
    domain = domain.strip()
    if not local or not domain:
        return None
    return f"{local}@{domain}"


def _iter_sender_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        values: list[str] = []
        for entry in value:
            values.extend(_iter_sender_values(entry))
        return values
    if not isinstance(value, dict):
        return []

    values = []
    parsed_values = value.get("value")
    if isinstance(parsed_values, list):
        for entry in parsed_values:
            if isinstance(entry, dict):
                address = entry.get("address")
                name = entry.get("name")
                if isinstance(address, str) and address.strip():
                    if isinstance(name, str) and name.strip():
                        values.append(f"{name.strip()} <{address.strip()}>")
                    else:
                        values.append(address.strip())
            elif isinstance(entry, str) and entry.strip():
                values.append(entry.strip())

    for key in ("text", "address", "email", "html"):
        raw = value.get(key)
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    return values


def _extract_sender_domain(metadata: dict[str, Any]) -> str | None:
    for key in ("sender", "from_email", "author", "source_author"):
        for value in _iter_sender_values(metadata.get(key)):
            _, email = parseaddr(value)
            candidate = email or value.strip()
            if "@" not in candidate:
                continue
            _, domain = candidate.rsplit("@", 1)
            domain_key = _normalize_domain_key(domain)
            if domain_key:
                return domain_key
    return None


def _extract_sender_identity(metadata: dict[str, Any]) -> str | None:
    for key in ("sender", "from_email", "author", "source_author"):
        for value in _iter_sender_values(metadata.get(key)):
            sender_key = _normalize_sender_identity(value)
            if sender_key:
                return sender_key
    return None


def _is_gmail_hosted_url(url: str | None) -> bool:
    return _shared_is_gmail_hosted_url(url)


def _safe_normalize_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        return _normalize_url(url.strip())
    except ServiceError:
        return None


def _normalize_article_url_candidate(raw_url: str | None) -> str | None:
    normalized = _safe_normalize_url(raw_url)
    if not normalized or _is_gmail_hosted_url(normalized):
        return None
    unwrapped = _safe_normalize_url(unwrap_tracking_url(normalized))
    if unwrapped and not _is_gmail_hosted_url(unwrapped):
        return unwrapped
    return normalized


def _prefer_real_article_url(
    current: str | None,
    candidate: str | None,
) -> str | None:
    if not candidate:
        return current
    if not is_low_signal_source_key(_normalize_domain_key(candidate)):
        return candidate
    return current or candidate


def _extract_url_from_text(text: str | None, current: str | None) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return current
    resolved = current
    for match in _HTTP_URL_RE.finditer(text):
        candidate = _normalize_article_url_candidate(match.group(0).rstrip(".,;:"))
        resolved = _prefer_real_article_url(resolved, candidate)
        if resolved and not is_low_signal_source_key(_normalize_domain_key(resolved)):
            return resolved
    return resolved


def _extract_article_url(metadata: dict[str, Any], content: str | None) -> str | None:
    resolved: str | None = None
    for key in (
        "article_url",
        "articleUrl",
        "canonical_url",
        "canonicalUrl",
        "link",
        "href",
        "url",
    ):
        value = metadata.get(key)
        candidate = _normalize_article_url_candidate(
            value if isinstance(value, str) else None
        )
        resolved = _prefer_real_article_url(resolved, candidate)
        if resolved and not is_low_signal_source_key(_normalize_domain_key(resolved)):
            return resolved

    links = metadata.get("links")
    if isinstance(links, list):
        for entry in links:
            if isinstance(entry, str):
                candidate = _normalize_article_url_candidate(entry)
            elif isinstance(entry, dict):
                candidate = _normalize_article_url_candidate(
                    entry.get("href") or entry.get("url") or entry.get("link")
                )
            else:
                candidate = None
            resolved = _prefer_real_article_url(resolved, candidate)
            if resolved and not is_low_signal_source_key(
                _normalize_domain_key(resolved)
            ):
                return resolved

    for key in ("snippet", "summary", "description", "text", "body", "plain"):
        resolved = _extract_url_from_text(
            metadata.get(key) if isinstance(metadata.get(key), str) else None,
            resolved,
        )
        if resolved and not is_low_signal_source_key(_normalize_domain_key(resolved)):
            return resolved

    resolved = _extract_url_from_text(content, resolved)
    return resolved


def _resolve_item_url(
    source: str,
    normalized_url: str | None,
    metadata: dict[str, Any],
    content: str | None,
) -> str | None:
    if source != "gmail-marvisx":
        return normalized_url
    candidate = _normalize_article_url_candidate(normalized_url)
    if candidate and not is_low_signal_source_key(_normalize_domain_key(candidate)):
        return candidate

    article_url = _extract_article_url(metadata, content)
    if article_url and not is_low_signal_source_key(_normalize_domain_key(article_url)):
        return article_url
    return article_url or candidate


def _derive_domain_key(
    source: str,
    normalized_url: str | None,
    metadata: dict[str, Any],
) -> str:
    if source == "gmail-marvisx":
        article_domain_key = _normalize_domain_key(normalized_url)
        if article_domain_key and not is_low_signal_source_key(article_domain_key):
            return article_domain_key
        return (
            _extract_sender_identity(metadata)
            or _extract_sender_domain(metadata)
            or article_domain_key
            or _normalize_domain_key(source)
            or source.strip().lower()
        )
    return (
        _normalize_domain_key(normalized_url)
        or _extract_sender_domain(metadata)
        or _normalize_domain_key(source)
        or source.strip().lower()
    )


def _coerce_timestamp(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    candidate = value.strip()
    try:
        dt = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        try:
            dt = parsedate_to_datetime(candidate)
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def _derive_published_at(
    explicit_published_at: str | None,
    metadata: dict[str, Any],
) -> str | None:
    published_at = _coerce_timestamp(explicit_published_at)
    if published_at:
        return published_at
    for key in _PUBLISHED_METADATA_KEYS:
        raw = metadata.get(key)
        if not isinstance(raw, str):
            continue
        published_at = _coerce_timestamp(raw)
        if published_at:
            return published_at
    return None


def _resolve_source_path(raw_path: str | None, config: InboxSourceConfig) -> str | None:
    if raw_path is None:
        return None
    if not config.allowed_roots:
        raise ValidationError(
            code="source_path_not_enabled",
            message="Source paths are not enabled for this source",
        )

    candidate = Path(raw_path).expanduser()
    if not candidate.is_absolute():
        raise ValidationError(
            code="source_path_not_absolute", message="source_path must be absolute"
        )
    if candidate.is_symlink():
        raise AuthorizationError(
            code="source_path_symlink", message="Symlink source_path is not allowed"
        )

    resolved = candidate.resolve()
    if not resolved.exists() or not resolved.is_file():
        raise ValidationError(
            code="source_path_not_a_file",
            message="source_path must point to an existing file",
        )

    for root_str in config.allowed_roots:
        root = Path(root_str).expanduser().resolve()
        if resolved == root or resolved.is_relative_to(root):
            return str(resolved)

    raise AuthorizationError(
        code="source_path_outside_roots", message="source_path is outside allowed roots"
    )


def _build_dedup_key(
    source: str,
    source_item_id: str | None,
    title: str | None,
    content: str | None,
    url: str | None,
    source_path: str | None,
) -> str:
    if source_item_id:
        material = f"{source}|item|{source_item_id}"
    else:
        material = json.dumps(
            {
                "source": source,
                "title": title,
                "content": content,
                "url": url,
                "source_path": source_path,
            },
            sort_keys=True,
            ensure_ascii=True,
        )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _row_to_response(row: aiosqlite.Row, *, deduplicated: bool) -> InboxIngestResponse:
    candidate_programs = json.loads(row["candidate_programs"] or "[]")
    return InboxIngestResponse(
        id=row["id"],
        source=row["source"],
        deduplicated=deduplicated,
        status=row["status"],
        title=row["title"],
        source_item_id=row["source_item_id"],
        source_path=row["source_path"],
        candidate_programs=candidate_programs,
        default_program=row["default_program"],
        topic=row["topic"],
        treatment=row["treatment"],
        created_at=row["created_at"],
    )


# Auto-status from treatment (module-level: shared by single + batch ingest).
_TREATMENT_TO_STATUS = {
    "read": "unread",
    "read_save": "unread",
    "save": "saved",
    "ignore": "auto_ignored",
}


# Background task set (prevents GC of fire-and-forget embed coroutines) — the inbox
# twin of ``inbox_llm_classifier._pending_classifier_tasks``.
_bg_embed_inbox: set[asyncio.Task] = set()


def _schedule_embed_inbox(
    *,
    item_id: str,
    title: str | None,
    content: str | None,
    workspace_id: str,
) -> None:
    """Fire-and-forget: embed a fresh inbox item in the background. No-ops if the
    embedder is unavailable. The inbox twin of
    ``inbox_llm_classifier.schedule_classification``.

    Called right after the item is persisted + committed so it is immediately findable
    by meaning (keyword-only until a manual reindex otherwise). The embed body
    re-acquires the single-writer lock via ``write_db``, so we always defer past the
    current request with ``asyncio.create_task`` rather than awaiting inline (learning
    f83f5209). Never raises to the caller — ingest stays non-blocking; if there is no
    running loop (e.g. a synchronous test harness) it logs and returns.
    """
    from core.api.services import embedding_service

    if not embedding_service.is_available():
        return

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.debug("No running loop; skipping inbox embed for %s", item_id)
        return

    async def _embed() -> None:
        try:
            await embedding_service.embed_inbox_document(
                item_id=item_id,
                title=title or "",
                content=content,
                workspace_id=workspace_id,
            )
        except Exception:  # noqa: BLE001
            logger.debug(
                "Auto-embed inbox item %s failed (non-critical)",
                item_id,
                exc_info=True,
            )

    try:
        task = loop.create_task(_embed())
    except Exception:  # noqa: BLE001
        logger.exception("Failed to create inbox embed task for %s", item_id)
        return
    _bg_embed_inbox.add(task)
    task.add_done_callback(_bg_embed_inbox.discard)


_INSERT_INBOX_ITEM_SQL = (
    "INSERT INTO inbox_items ("
    "id, source, source_item_id, dedup_key, status, title, content, url, domain_key, published_at, freshness_at, source_path, "
    "metadata_json, candidate_programs, default_program, topic, treatment, created_by, workspace_id, created_at, updated_at"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def _prepare_insert(
    body: InboxIngestRequest, *, user: UserInfo
) -> tuple[str, tuple, str, str]:
    """Compute sanitized fields for an inbox INSERT.

    Returns (item_id, insert_args, dedup_key, workspace_id). Raises
    ``ValidationError`` (422) on metadata oversize. Shared by `ingest_item` and
    `ingest_items_batch`.
    """
    config = get_source_config(body.source)

    title = _sanitize_text(body.title, max_length=settings.inbox_max_title_chars)
    content = _sanitize_text(body.content, max_length=settings.inbox_max_content_chars)
    source_item_id = _sanitize_text(body.source_item_id, max_length=255)
    normalized_url = _normalize_url(_sanitize_text(body.url, max_length=2000))
    metadata = _sanitize_metadata(body.metadata)

    candidate_programs, default_program = _merge_program_metadata(body, config)
    source_path = _resolve_source_path(
        _sanitize_text(body.source_path, max_length=2000), config
    )
    metadata_dict = dict(metadata) if isinstance(metadata, dict) else {}
    if body.source == "gmail-marvisx" and _is_gmail_hosted_url(normalized_url):
        metadata_dict.setdefault("gmail_thread_url", normalized_url)
    canonical_url = _resolve_item_url(
        body.source, normalized_url, metadata_dict, content
    )
    domain_key = _derive_domain_key(body.source, canonical_url, metadata_dict)
    published_at = _derive_published_at(body.published_at, metadata_dict)
    metadata_json = json.dumps(metadata_dict, sort_keys=True, ensure_ascii=True)
    if len(metadata_json.encode("utf-8")) > settings.inbox_max_metadata_bytes:
        raise ValidationError(
            code="metadata_too_large", message="metadata is too large"
        )
    topic = (
        normalize_inbox_topic(body.topic)
        if body.topic
        else infer_topic_from_metadata(metadata_dict, title=title, content=content)
    )
    treatment = (
        normalize_inbox_treatment(body.treatment)
        if body.treatment
        else infer_treatment(topic, metadata_dict)
    )
    dedup_key = _build_dedup_key(
        body.source,
        source_item_id,
        title,
        content,
        canonical_url,
        source_path,
    )

    now = datetime.now(timezone.utc).isoformat()
    freshness_at = published_at or now
    item_id = f"inbox_{uuid.uuid4().hex[:24]}"
    candidate_programs_json = json.dumps(candidate_programs, ensure_ascii=True)
    auto_status = _TREATMENT_TO_STATUS.get(treatment, "unread")
    workspace_id = user.workspace_id or "ws_default"

    args = (
        item_id,
        body.source,
        source_item_id,
        dedup_key,
        auto_status,
        title,
        content,
        canonical_url,
        domain_key,
        published_at,
        freshness_at,
        source_path,
        metadata_json,
        candidate_programs_json,
        default_program,
        topic,
        treatment,
        user.username,
        workspace_id,
        now,
        now,
    )
    return item_id, args, dedup_key, workspace_id


async def ingest_item(
    body: InboxIngestRequest,
    *,
    user: UserInfo,
    db: aiosqlite.Connection,
) -> InboxIngestResponse:
    item_id, args, dedup_key, workspace_id = _prepare_insert(body, user=user)

    try:
        await db.execute(_INSERT_INBOX_ITEM_SQL, args)
        await db.commit()
    except aiosqlite.IntegrityError:
        cursor = await db.execute(
            "SELECT * FROM inbox_items WHERE source = ? AND dedup_key = ? AND COALESCE(workspace_id, 'ws_default') = ?",
            (body.source, dedup_key, workspace_id),
        )
        existing = await cursor.fetchone()
        if existing is None:
            raise ConflictError(
                code="duplicate_inbox_item", message="Duplicate inbox item"
            )
        return _row_to_response(existing, deduplicated=True)

    cursor = await db.execute("SELECT * FROM inbox_items WHERE id = ?", (item_id,))
    row = await cursor.fetchone()
    if row is None:
        err = ServiceError(
            code="inbox_item_lost",
            message="Inbox item created but not found",
        )
        err.http_status = 500  # preserve the original 500 status (was a 500 raise)
        raise err

    # PR B (2026-04-11): schedule async classification as a write-ahead task.
    # The item is already persisted with status='unread'. The background task
    # will update status/metadata when the LLM responds (or fall back). This
    # call MUST never raise to the caller — ingest stays non-blocking.
    try:
        from core.api.services.inbox_llm_classifier import schedule_classification

        schedule_classification(
            settings.db_path,
            workspace_id,
            item_id,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to schedule classifier for item %s", item_id)

    # Embed-on-write so the fresh item is immediately searchable by meaning. Uses the
    # sanitized stored title/content (matches what a later reindex reads back from DB).
    _schedule_embed_inbox(
        item_id=item_id,
        title=row["title"],
        content=row["content"],
        workspace_id=workspace_id,
    )

    return _row_to_response(row, deduplicated=False)


async def ingest_items_batch(
    body: InboxIngestBatchRequest,
    *,
    user: UserInfo,
    db: aiosqlite.Connection,
) -> InboxIngestBatchResponse:
    """Batch-ingest N items in a single write lock + single commit.

    Rationale: the retired RSS fan-out fired N separate POST /ingest requests
    per poll, each acquiring the process-wide `_write_lock` + doing an fsync.
    With N ~800 this saturated disk IO and stalled readers via busy_timeout
    (see session 2026-04-24 handoff). This endpoint folds that burst into one
    transaction: ONE lock acquisition, ONE fsync, per-item SAVEPOINT so a
    single constraint violation doesn't poison the rest of the batch.

    Per-item errors (validation, dedup collision) are collected into the
    response rather than aborting the whole batch — the caller keeps forward
    progress on good items.
    """
    inserted: list[InboxIngestResponse] = []
    deduplicated: list[InboxIngestResponse] = []
    errors: list[InboxIngestBatchItemError] = []
    fresh_item_ids: list[tuple[str, str]] = []  # (item_id, workspace_id)

    for idx, item in enumerate(body.items):
        try:
            item_id, args, dedup_key, workspace_id = _prepare_insert(item, user=user)
        except ServiceError as exc:
            errors.append(
                InboxIngestBatchItemError(
                    index=idx,
                    source=item.source,
                    source_item_id=item.source_item_id,
                    error=str(exc.message),
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to prepare inbox batch item %d", idx)
            errors.append(
                InboxIngestBatchItemError(
                    index=idx,
                    source=item.source,
                    source_item_id=item.source_item_id,
                    error=f"prepare_error: {exc}",
                )
            )
            continue

        savepoint = f"sp_ingest_{idx}"
        await db.execute(f"SAVEPOINT {savepoint}")
        try:
            await db.execute(_INSERT_INBOX_ITEM_SQL, args)
        except aiosqlite.IntegrityError:
            await db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            await db.execute(f"RELEASE SAVEPOINT {savepoint}")
            cursor = await db.execute(
                "SELECT * FROM inbox_items WHERE source = ? AND dedup_key = ? AND COALESCE(workspace_id, 'ws_default') = ?",
                (item.source, dedup_key, workspace_id),
            )
            existing = await cursor.fetchone()
            if existing is None:
                errors.append(
                    InboxIngestBatchItemError(
                        index=idx,
                        source=item.source,
                        source_item_id=item.source_item_id,
                        error="integrity_error_no_match",
                    )
                )
                continue
            deduplicated.append(_row_to_response(existing, deduplicated=True))
            continue
        except Exception as exc:  # noqa: BLE001
            await db.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            await db.execute(f"RELEASE SAVEPOINT {savepoint}")
            logger.exception("Failed to INSERT inbox batch item %d", idx)
            errors.append(
                InboxIngestBatchItemError(
                    index=idx,
                    source=item.source,
                    source_item_id=item.source_item_id,
                    error=f"insert_error: {exc}",
                )
            )
            continue

        await db.execute(f"RELEASE SAVEPOINT {savepoint}")
        fresh_item_ids.append((item_id, workspace_id))

    # Single commit for all successful inserts in the batch.
    await db.commit()

    # Re-read rows + schedule classification outside the insert loop to keep
    # the write lock held for the shortest possible duration.
    for item_id, workspace_id in fresh_item_ids:
        cursor = await db.execute(
            "SELECT * FROM inbox_items WHERE id = ?", (item_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            logger.warning("Inbox batch item %s inserted but not found post-commit", item_id)
            continue
        inserted.append(_row_to_response(row, deduplicated=False))

        try:
            from core.api.services.inbox_llm_classifier import schedule_classification

            schedule_classification(settings.db_path, workspace_id, item_id)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to schedule classifier for item %s", item_id)

        # Embed-on-write so the fresh item is immediately searchable by meaning. Uses
        # the sanitized stored title/content (matches what a later reindex reads back).
        _schedule_embed_inbox(
            item_id=item_id,
            title=row["title"],
            content=row["content"],
            workspace_id=workspace_id,
        )

    return InboxIngestBatchResponse(
        inserted=inserted,
        deduplicated=deduplicated,
        errors=errors,
        counts={
            "received": len(body.items),
            "inserted": len(inserted),
            "deduplicated": len(deduplicated),
            "errors": len(errors),
        },
    )
