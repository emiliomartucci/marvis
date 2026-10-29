# v1.0.0 - 2026-09-21 - Plan 2026-09-21 KTD6/R9: per-call Jev usage ledger.
"""Jev usage ledger — one JSONL line per ACTUAL provider call (plan KTD6/R9).

Same storage pattern as :mod:`core.api.services.tool_usage`: per-tenant
append-only JSONL, ``O_APPEND`` below ``PIPE_BUF`` (atomic without a lock),
best-effort synchronous write that NEVER raises, no DB table (``migrations/``
is a protected fleet path). Lines carry the call outcome so the calibration
harness (plan U4) can separate enriched calls from degraded ones, and cost is
recorded only when a price list exists — never invented (learning 0cfa9f5f).

Skip policy (plan review): tenants with the flag OFF (default) produce NO
line — a fleet-wide per-call ``skip_flag`` row would be pure noise.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("marvis.jev.ledger")

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_TENANT = "tenant"

# Closed vocabulary of outcomes (plan R16 mirror: the curation block reports
# the same values so agents and operators see one truth).
OUTCOMES = (
    "ok",
    "timeout_fallback",
    "breaker_open",
    "ceiling_hit",
    "provider_error",
    "error",
)


def record_call(
    *,
    tenant_id: str | None,
    surface: str,
    outcome: str,
    model: str = "",
    latency_ms: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float | None = None,
    extra: dict[str, Any] | None = None,
    attempted: bool = True,
    now: datetime | None = None,
    log_dir: str | os.PathLike[str] | None = None,
) -> bool:
    """Append one ledger line. Best-effort; NEVER raises (plan KTD6).

    ``extra`` carries small bounded per-surface facts (e.g. the redaction
    gate's ``kept`` count); values are coerced to compact JSON scalars.
    """
    try:
        tenant = _SAFE_SEGMENT.sub(
            "-", (tenant_id or "").strip()
        ).strip("-") or _DEFAULT_TENANT
        record: dict[str, Any] = {
            "surface": _SAFE_SEGMENT.sub("_", surface)[:64] or "unknown",
            "outcome": outcome if outcome in OUTCOMES else "error",
            "ts": (now or datetime.now(timezone.utc)).isoformat(),
            "attempted": attempted,
        }
        if extra:
            for key, value in extra.items():
                if isinstance(value, (int, float, str, bool)):
                    record[str(key)[:32]] = value
        if model:
            record["model"] = model[:32]
        record["latency_ms"] = max(0, int(latency_ms))
        record["input_tokens"] = max(0, int(input_tokens))
        if output_tokens:
            record["output_tokens"] = max(0, int(output_tokens))
        if cost_usd is not None:
            record["cost_usd"] = round(float(cost_usd), 6)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"

        directory = Path(log_dir) if log_dir else _resolve_log_dir()
        path = directory / f"jev-usage-{tenant}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - a broken ledger must never break a call
        logger.debug("jev usage record failed", exc_info=True)
        return False


def _resolve_log_dir() -> Path:
    override = os.environ.get("MARVIS_JEV_USAGE_DIR", "").strip()
    if override:
        return Path(override)
    db_path = (
        os.environ.get("MARVIS_DB_PATH") or os.environ.get("PIR_DB_PATH") or ""
    ).strip()
    if db_path:
        return Path(db_path).expanduser().resolve().parent / "jev_usage"
    return Path.home() / ".marvis" / "jev_usage"


# --- Task 7af2b38c (Laya round-2 U1): opt-in payload capture, server-only ---
#
# The usage ledger above counts calls; training round-2 needs the actual
# state/questions/answers pairs. Capture is OFF unless the tenant env sets
# MARVIS_SCORING_CAPTURE=1 (config.settings.scoring_capture_enabled), payload
# lines pass the SAME sanitizer used at the egress port (secrets and internal
# identifiers redacted before anything hits disk), live in daily files with a
# 30-day rotation, and NEVER leave the tenant's own server. Best-effort like
# record_call: a broken capture must never break a scoring call.

_CAPTURE_RETENTION_DAYS = 30
_CAPTURE_MAX_FIELD_CHARS = 4000


def _capture_truncate(value: Any) -> Any:
    """Cap every string so a pathological 19k-token brain state cannot make
    the capture file unbounded (task 808e32eb measured such payloads live)."""
    if isinstance(value, str):
        return value[:_CAPTURE_MAX_FIELD_CHARS]
    if isinstance(value, list):
        return [_capture_truncate(item) for item in value]
    if isinstance(value, dict):
        return {key: _capture_truncate(item) for key, item in value.items()}
    return value


def _prune_old_captures(directory: Path, *, today: date) -> None:
    cutoff = today - timedelta(days=_CAPTURE_RETENTION_DAYS)
    for path in directory.glob("capture-*.jsonl"):
        parts = path.stem.split("-")
        try:
            file_day = date(int(parts[-3]), int(parts[-2]), int(parts[-1]))
        except (ValueError, IndexError):
            continue
        if file_day < cutoff:
            try:
                path.unlink()
            except OSError:
                pass


def record_capture(
    *,
    tenant_id: str | None,
    surface: str,
    state: Any,
    questions: Any,
    answers: Any,
    outcome: str,
    model: str = "",
    latency_ms: int = 0,
    now: datetime | None = None,
    log_dir: str | os.PathLike[str] | None = None,
) -> bool:
    """Append one sanitized payload line (opt-in, server-only, never raises)."""
    try:
        from core.api import config as _config

        if not _config.settings.scoring_capture_enabled:
            return False
        from core.api.services.jev.outbound import sanitize_payload

        moment = now or datetime.now(timezone.utc)
        record = {
            "ts": moment.isoformat(),
            "surface": _SAFE_SEGMENT.sub("_", surface)[:64] or "unknown",
            "outcome": outcome if outcome in OUTCOMES else "error",
            "model": model[:64],
            "latency_ms": max(0, int(latency_ms)),
            "state": _capture_truncate(sanitize_payload(state)),
            "questions": _capture_truncate(
                sanitize_payload(questions)
            ),
            "answers": _capture_truncate(
                sanitize_payload(answers)
            )
            if answers is not None
            else None,
        }
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"

        tenant = _SAFE_SEGMENT.sub(
            "-", (tenant_id or "").strip()
        ).strip("-") or _DEFAULT_TENANT
        directory = (
            Path(log_dir) if log_dir else _resolve_log_dir() / "scoring_capture"
        )
        directory.mkdir(parents=True, exist_ok=True)
        _prune_old_captures(directory, today=moment.date())
        path = directory / f"capture-{tenant}-{moment.strftime('%Y-%m-%d')}.jsonl"
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - capture must never break a scoring call
        logger.debug("scoring capture record failed", exc_info=True)
        return False


def iter_events(
    paths: Iterable[str | os.PathLike[str]],
) -> Iterator[Mapping[str, Any]]:
    """Yield well-formed ledger lines (operator report / calibration side)."""
    for raw_path in paths:
        try:
            handle = Path(raw_path).open("r", encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(record, Mapping) and isinstance(
                    record.get("surface"), str
                ):
                    yield record


def daily_attempts(tenant_id: str, day: str) -> int:
    """Count completed ledger attempts; ambiguous legacy rows count conservatively.

    Laya also reserves each in-flight attempt in the shared SQLite quota table
    before sending it, so a hard crash cannot reset its daily ceiling.
    """
    tenant = _SAFE_SEGMENT.sub("-", tenant_id.strip()).strip("-") or _DEFAULT_TENANT
    path = _resolve_log_dir() / f"jev-usage-{tenant}.jsonl"
    count = 0
    try:
        handle = path.open("r", encoding="utf-8")
    except FileNotFoundError:
        return 0
    with handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                # A torn/invalid row might be an attempted call today.
                count += 1
                continue
            if not isinstance(row, dict):
                count += 1
                continue
            if not str(row.get("ts", "")).startswith(day):
                continue
            legacy_attempt = row.get("outcome") in {"ok", "timeout_fallback", "provider_error", "error"} and "kept" not in row
            if row.get("attempted", legacy_attempt):
                count += 1
    return count


def reserve_daily_attempt(tenant_id: str, day: str, ceiling: int) -> bool:
    """Atomically reserve a Laya call across API workers before sending it.

    The JSONL usage ledger records completion; it cannot cap simultaneous
    requests in separate processes. A small local SQLite transaction reserves
    each attempt durably, including calls interrupted before their receipt.
    Storage errors deny the call instead of silently resetting the ceiling.
    """
    if ceiling < 1:
        return False
    tenant = _SAFE_SEGMENT.sub("-", tenant_id.strip()).strip("-") or _DEFAULT_TENANT
    db = None
    try:
        directory = _resolve_log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(directory / "laya-quota.sqlite3", timeout=2.0)
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS daily_attempts ("
            "tenant TEXT NOT NULL, day TEXT NOT NULL, count INTEGER NOT NULL, "
            "PRIMARY KEY (tenant, day))"
        )
        row = db.execute(
            "SELECT count FROM daily_attempts WHERE tenant=? AND day=?",
            (tenant, day),
        ).fetchone()
        recorded = daily_attempts(tenant, day)
        used = max(int(row[0]), recorded) if row is not None else recorded
        if used >= ceiling:
            db.rollback()
            return False
        db.execute(
            "INSERT INTO daily_attempts(tenant,day,count) VALUES(?,?,?) "
            "ON CONFLICT(tenant,day) DO UPDATE SET count=excluded.count",
            (tenant, day, used + 1),
        )
        db.commit()
        return True
    except (OSError, UnicodeError, sqlite3.Error, TypeError, ValueError):
        if db is not None:
            db.rollback()
        logger.warning("Laya daily quota reservation unavailable; denying call", exc_info=True)
        return False
    finally:
        if db is not None:
            db.close()


__all__ = ["OUTCOMES", "daily_attempts", "iter_events", "record_call", "reserve_daily_attempt"]
