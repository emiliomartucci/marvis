from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Any

from core.api.db import acquire_db, write_db
from core.api.services.inbox_digest import upsert_digest_selection
from core.api.services.inbox_digest_deep_research import precompute_deep_research_for_items

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DigestCandidate:
    item_id: str
    domain_key: str
    freshness_at: datetime
    created_at: datetime
    source_score: float
    classifier_decision: str | None
    classifier_confidence: float
    existing_state: str | None
    existing_expires_at: datetime | None


@dataclass(slots=True)
class DigestSettings:
    mode: str
    cutoff_hour_utc: int
    readiness_target_hour_utc: int
    admission_threshold: float
    overflow_ttl_days: int
    deep_research_enabled: bool
    visible_cap: int
    concurrency: int
    allow_cloud_fallback: bool
    lease_ttl_minutes: int


@dataclass(slots=True)
class DigestSelectionPlan:
    cycle_key: str
    mode: str
    cutoff_at: datetime
    readiness_target_at: datetime
    selected: list[dict[str, Any]]


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


async def _get_setting(db, key: str, default: str) -> str:
    row = await (
        await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
    ).fetchone()
    if row is None:
        return default
    return row[0] if not hasattr(row, "keys") else row["value"]


def _as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str, default: int, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    if minimum is not None:
        return max(minimum, parsed)
    return parsed


async def _load_digest_settings(db) -> DigestSettings:
    mode = await _get_setting(db, "inbox_daily_digest_enabled", "shadow")
    freeze_hour = _as_int(
        await _get_setting(db, "inbox_daily_digest_freeze_hour_utc", "6"),
        6,
    )
    cutoff_hour = _as_int(
        await _get_setting(
            db,
            "inbox_daily_digest_precompute_start_hour_utc",
            str(freeze_hour),
        ),
        freeze_hour,
    )
    readiness_target_hour = _as_int(
        await _get_setting(db, "inbox_daily_digest_readiness_target_hour_utc", "6"),
        6,
    )
    return DigestSettings(
        mode=mode,
        cutoff_hour_utc=cutoff_hour,
        readiness_target_hour_utc=readiness_target_hour,
        admission_threshold=float(
            await _get_setting(db, "inbox_daily_digest_admission_threshold", "1.0")
        ),
        overflow_ttl_days=_as_int(
            await _get_setting(db, "inbox_daily_digest_overflow_ttl_days", "3"),
            3,
            minimum=1,
        ),
        deep_research_enabled=_as_bool(
            await _get_setting(db, "inbox_daily_digest_deep_research_enabled", "false")
        ),
        visible_cap=_as_int(
            await _get_setting(db, "inbox_daily_digest_visible_cap", "24"),
            24,
            minimum=1,
        ),
        concurrency=_as_int(
            await _get_setting(db, "inbox_daily_digest_deep_research_concurrency", "1"),
            1,
            minimum=1,
        ),
        allow_cloud_fallback=_as_bool(
            await _get_setting(
                db,
                "inbox_daily_digest_deep_research_allow_cloud_fallback",
                "false",
            )
        ),
        lease_ttl_minutes=_as_int(
            await _get_setting(
                db, "inbox_daily_digest_precompute_lease_ttl_minutes", "120"
            ),
            120,
            minimum=5,
        ),
    )


def _score_candidate(candidate: DigestCandidate, *, now: datetime) -> float:
    age_hours = max(0.0, (now - candidate.freshness_at).total_seconds() / 3600)
    freshness_bonus = max(0.0, 3.0 - (age_hours / 24.0))
    source_bonus = min(2.0, candidate.source_score / 10.0)

    classifier_bonus = 0.0
    if candidate.classifier_decision == "read":
        classifier_bonus = 2.0 * candidate.classifier_confidence
    elif candidate.classifier_decision == "ignore":
        classifier_bonus = -2.0 * candidate.classifier_confidence

    return round(freshness_bonus + source_bonus + classifier_bonus, 6)


def _digest_state_for_candidate(
    candidate: DigestCandidate,
    *,
    score: float,
    rank_in_domain: int,
    admission_threshold: float,
) -> str:
    if candidate.classifier_decision == "ignore":
        return "overflow"
    if rank_in_domain <= 3 and score >= admission_threshold:
        return "visible"
    return "overflow"


def _current_cycle_key(now: datetime, freeze_hour_utc: int) -> str:
    if now.hour < freeze_hour_utc:
        return (now.date() - timedelta(days=1)).isoformat()
    return now.date().isoformat()


def _cycle_cutoff_at(now: datetime, cutoff_hour_utc: int) -> datetime:
    cycle_date = now.date()
    if now.hour < cutoff_hour_utc:
        cycle_date = cycle_date - timedelta(days=1)
    return datetime.combine(
        cycle_date,
        time(hour=cutoff_hour_utc, tzinfo=timezone.utc),
    )


def _readiness_target_at(cutoff_at: datetime, target_hour_utc: int) -> datetime:
    target = datetime.combine(
        cutoff_at.date(),
        time(hour=target_hour_utc, tzinfo=timezone.utc),
    )
    if target < cutoff_at:
        target += timedelta(days=1)
    return target


async def expire_overflow_items(
    workspace_id: str, *, now: datetime | None = None
) -> dict[str, int]:
    now = now or datetime.now(timezone.utc)
    async with acquire_db() as db:
        rows = await (
            await db.execute(
                "SELECT s.id, s.inbox_item_id FROM inbox_digest_selections s "
                "JOIN inbox_items i ON i.id = s.inbox_item_id "
                "WHERE s.workspace_id = ? AND s.state = 'overflow' "
                "AND s.expires_at IS NOT NULL AND s.expires_at <= ? "
                "AND COALESCE(i.workspace_id, 'ws_default') = ? AND i.status = 'unread'",
                (workspace_id, now.isoformat(), workspace_id),
            )
        ).fetchall()

    if not rows:
        return {"expired": 0}

    async with write_db() as db:
        for row in rows:
            await db.execute(
                "UPDATE inbox_items SET status = 'auto_ignored', updated_at = ? "
                "WHERE id = ? AND COALESCE(workspace_id, 'ws_default') = ?",
                (now.isoformat(), row["inbox_item_id"], workspace_id),
            )
            await db.execute(
                "UPDATE inbox_digest_selections SET state = 'expired', updated_at = ? WHERE id = ?",
                (now.isoformat(), row["id"]),
            )
    return {"expired": len(rows)}


def _apply_global_visible_cap(
    selected: list[dict[str, Any]],
    *,
    cap: int,
    now: datetime,
    overflow_ttl_days: int,
) -> None:
    visible_entries = [entry for entry in selected if entry["state"] == "visible"]
    if len(visible_entries) <= cap:
        return

    visible_entries.sort(
        key=lambda entry: (
            entry["score"],
            entry["freshness_at"],
            entry["created_at"],
        ),
        reverse=True,
    )
    keep_ids = {entry["item_id"] for entry in visible_entries[:cap]}
    for entry in visible_entries[cap:]:
        if entry["item_id"] in keep_ids:
            continue
        entry["state"] = "overflow"
        if not entry.get("expires_at"):
            entry["expires_at"] = (now + timedelta(days=overflow_ttl_days)).isoformat()


async def prepare_digest_selection_plan(
    workspace_id: str,
    *,
    now: datetime | None = None,
) -> tuple[DigestSelectionPlan | None, DigestSettings]:
    now = now or datetime.now(timezone.utc)

    async with acquire_db() as db:
        settings = await _load_digest_settings(db)
        if settings.mode == "false":
            return None, settings

        cycle_key = _current_cycle_key(now, settings.cutoff_hour_utc)
        cutoff_at = _cycle_cutoff_at(now, settings.cutoff_hour_utc)
        readiness_target_at = _readiness_target_at(
            cutoff_at,
            settings.readiness_target_hour_utc,
        )

        rows = await (
            await db.execute(
                "SELECT i.id, i.domain_key, COALESCE(i.freshness_at, i.created_at) AS freshness_at, i.created_at, "
                "i.metadata_json, COALESCE(sc.score, 0) AS source_score, ds.state AS existing_state, ds.expires_at AS existing_expires_at "
                "FROM inbox_items i "
                "LEFT JOIN source_scores sc ON sc.workspace_id = COALESCE(i.workspace_id, 'ws_default') AND sc.source_key = i.domain_key "
                "LEFT JOIN inbox_digest_selections ds ON ds.workspace_id = COALESCE(i.workspace_id, 'ws_default') AND ds.inbox_item_id = i.id AND ds.state IN ('visible', 'overflow') "
                "WHERE COALESCE(i.workspace_id, 'ws_default') = ? AND i.status = 'unread' "
                "AND i.domain_key IS NOT NULL AND i.domain_key != '' AND i.created_at <= ?",
                (workspace_id, cutoff_at.isoformat()),
            )
        ).fetchall()

    grouped: dict[str, list[tuple[DigestCandidate, float]]] = defaultdict(list)
    for row in rows:
        metadata = {}
        if row["metadata_json"]:
            try:
                parsed = json.loads(row["metadata_json"])
                if isinstance(parsed, dict):
                    metadata = parsed
            except Exception:
                metadata = {}
        classifier = metadata.get("classifier") or {}
        candidate = DigestCandidate(
            item_id=row["id"],
            domain_key=row["domain_key"],
            freshness_at=_parse_dt(row["freshness_at"]) or now,
            created_at=_parse_dt(row["created_at"]) or now,
            source_score=float(row["source_score"] or 0),
            classifier_decision=classifier.get("decision"),
            classifier_confidence=float(classifier.get("confidence") or 0.0),
            existing_state=row["existing_state"],
            existing_expires_at=_parse_dt(row["existing_expires_at"]),
        )
        score = _score_candidate(candidate, now=now)
        grouped[candidate.domain_key].append((candidate, score))

    selected: list[dict[str, Any]] = []
    for domain_key, entries in grouped.items():
        entries.sort(
            key=lambda item: (item[1], item[0].freshness_at, item[0].created_at),
            reverse=True,
        )
        for idx, (candidate, score) in enumerate(entries, start=1):
            state = _digest_state_for_candidate(
                candidate,
                score=score,
                rank_in_domain=idx,
                admission_threshold=settings.admission_threshold,
            )
            expires_at = None
            if state == "overflow":
                if (
                    candidate.existing_state == "overflow"
                    and candidate.existing_expires_at
                ):
                    expires_at = candidate.existing_expires_at.isoformat()
                else:
                    expires_at = (
                        now + timedelta(days=settings.overflow_ttl_days)
                    ).isoformat()
            selected.append(
                {
                    "item_id": candidate.item_id,
                    "domain_key": domain_key,
                    "score": score,
                    "rank_in_domain": idx,
                    "state": state,
                    "expires_at": expires_at,
                    "freshness_at": candidate.freshness_at,
                    "created_at": candidate.created_at,
                }
            )

    if settings.deep_research_enabled:
        _apply_global_visible_cap(
            selected,
            cap=settings.visible_cap,
            now=now,
            overflow_ttl_days=settings.overflow_ttl_days,
        )

    return (
        DigestSelectionPlan(
            cycle_key=cycle_key,
            mode=settings.mode,
            cutoff_at=cutoff_at,
            readiness_target_at=readiness_target_at,
            selected=selected,
        ),
        settings,
    )


async def publish_digest_selection_plan(
    workspace_id: str,
    plan: DigestSelectionPlan,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    selected = list(plan.selected)
    async with write_db() as db:
        item_ids = [entry["item_id"] for entry in selected]
        unread_ids: set[str] = set()
        if item_ids:
            placeholders = ",".join("?" for _ in item_ids)
            rows = await (
                await db.execute(
                    "SELECT id FROM inbox_items "
                    f"WHERE id IN ({placeholders}) AND COALESCE(workspace_id, 'ws_default') = ? AND status = 'unread'",
                    (*item_ids, workspace_id),
                )
            ).fetchall()
            unread_ids = {row["id"] for row in rows}
        selected = [entry for entry in selected if entry["item_id"] in unread_ids]

        await db.execute(
            "DELETE FROM inbox_digest_selections WHERE workspace_id = ? AND state IN ('visible', 'overflow')",
            (workspace_id,),
        )
        for entry in selected:
            await upsert_digest_selection(
                db,
                inbox_item_id=entry["item_id"],
                workspace_id=workspace_id,
                digest_cycle_key=plan.cycle_key,
                state=entry["state"],
                domain_key=entry["domain_key"],
                score=entry["score"],
                rank_in_domain=entry["rank_in_domain"],
                expires_at=entry["expires_at"],
            )
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            ("inbox_daily_digest_last_cycle_key", plan.cycle_key, now.isoformat()),
        )

    visible = sum(1 for item in selected if item["state"] == "visible")
    overflow = sum(1 for item in selected if item["state"] == "overflow")
    return {
        "status": "ok",
        "cycle_key": plan.cycle_key,
        "mode": plan.mode,
        "visible": visible,
        "overflow": overflow,
        "candidates": len(selected),
        "cutoff_at": plan.cutoff_at.isoformat(),
        "readiness_target_at": plan.readiness_target_at.isoformat(),
    }


async def run_digest_freeze_cycle(
    workspace_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    plan, _settings = await prepare_digest_selection_plan(workspace_id, now=now)
    if plan is None:
        return {"status": "disabled"}
    return await publish_digest_selection_plan(workspace_id, plan, now=now)


def _lease_key(workspace_id: str) -> str:
    return f"inbox_daily_digest_precompute_lease_{workspace_id}"


async def _claim_precompute_lease(
    *,
    workspace_id: str,
    cycle_key: str,
    now: datetime,
    ttl_minutes: int,
) -> dict[str, Any]:
    key = _lease_key(workspace_id)
    async with write_db() as db:
        row = await (
            await db.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
        ).fetchone()
        if row is not None:
            raw_value = row[0] if not hasattr(row, "keys") else row["value"]
            try:
                current = json.loads(raw_value or "{}")
            except json.JSONDecodeError:
                current = {}
            started_at = _parse_dt(current.get("started_at"))
            if (
                current.get("cycle_key") == cycle_key
                and started_at is not None
                and now - started_at < timedelta(minutes=ttl_minutes)
            ):
                return {
                    "claimed": False,
                    "cycle_key": cycle_key,
                    "started_at": started_at.isoformat(),
                }

        value = json.dumps(
            {"cycle_key": cycle_key, "started_at": now.isoformat()},
            ensure_ascii=False,
        )
        await db.execute(
            "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now.isoformat()),
        )
        return {"claimed": True, "cycle_key": cycle_key, "started_at": now.isoformat()}


async def _release_precompute_lease(*, workspace_id: str) -> None:
    async with write_db() as db:
        await db.execute(
            "DELETE FROM app_settings WHERE key = ?", (_lease_key(workspace_id),)
        )


async def _run_precompute_then_publish(
    *,
    workspace_id: str,
    plan: DigestSelectionPlan,
    settings: DigestSettings,
    now: datetime,
) -> dict[str, Any]:
    lease = await _claim_precompute_lease(
        workspace_id=workspace_id,
        cycle_key=plan.cycle_key,
        now=now,
        ttl_minutes=settings.lease_ttl_minutes,
    )
    if not lease["claimed"]:
        return {
            "status": "already_running",
            "cycle_key": plan.cycle_key,
            "precompute_started_at": lease.get("started_at"),
        }

    enrichment: dict[str, Any] = {}
    try:
        visible_ids = [
            entry["item_id"] for entry in plan.selected if entry["state"] == "visible"
        ]
        enrichment = await precompute_deep_research_for_items(
            inbox_item_ids=visible_ids,
            workspace_id=workspace_id,
            cycle_key=plan.cycle_key,
            concurrency=settings.concurrency,
            allow_cloud_fallback=settings.allow_cloud_fallback,
        )
        result = await publish_digest_selection_plan(workspace_id, plan, now=now)
        result["deep_research"] = enrichment
        return result
    finally:
        await _release_precompute_lease(workspace_id=workspace_id)


async def run_digest_jobs_if_due(
    *, now: datetime | None = None, workspace_id: str = "ws_default"
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    async with acquire_db() as db:
        settings = await _load_digest_settings(db)
        if settings.mode == "false":
            return {"status": "disabled"}
        last_cycle_key = await _get_setting(db, "inbox_daily_digest_last_cycle_key", "")

    expired = await expire_overflow_items(workspace_id, now=now)
    cycle_key = _current_cycle_key(now, settings.cutoff_hour_utc)
    if now.hour < settings.cutoff_hour_utc or last_cycle_key == cycle_key:
        return {"status": "idle", "cycle_key": cycle_key, **expired}

    plan, settings = await prepare_digest_selection_plan(workspace_id, now=now)
    if plan is None:
        return {"status": "disabled", **expired}
    if settings.deep_research_enabled:
        result = await _run_precompute_then_publish(
            workspace_id=workspace_id,
            plan=plan,
            settings=settings,
            now=now,
        )
    else:
        result = await publish_digest_selection_plan(workspace_id, plan, now=now)
    result.update(expired)
    return result


async def recompute_digest_now(
    workspace_id: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    expired = await expire_overflow_items(workspace_id, now=now)
    plan, settings = await prepare_digest_selection_plan(workspace_id, now=now)
    if plan is None:
        return {"status": "disabled", **expired}
    if settings.deep_research_enabled:
        result = await _run_precompute_then_publish(
            workspace_id=workspace_id,
            plan=plan,
            settings=settings,
            now=now,
        )
    else:
        result = await publish_digest_selection_plan(workspace_id, plan, now=now)
    result.update(expired)
    return result
