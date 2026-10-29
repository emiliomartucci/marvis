# v1.0.0 - 2026-09-22 - Calibration capture: save Jev state+answer for owner labeling.
"""Calibration capture — persists a sample of Jev calls for blind owner labeling.

Design (from the owner review request):
- Accumulates for ~1 week, then the owner retrieves cases via chat and
  labels them one by one (giusto / sbagliato / non so).
- The state and answers are saved AFTER the redaction gate has already run:
  no credentials should be present (defense in depth: the redaction gate
  is upstream). State is truncated to keep files bounded.
- Storage: per-tenant JSONL, same pattern as the usage ledger (O_APPEND,
  sub-PIPE_BUF, best-effort, never raises). A cap prevents unbounded growth.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("marvis.jev.calibration")

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
_STATE_CAP = 2000  # chars of state JSON to keep
_ANSWER_CAP = 1000  # chars of answer JSON to keep
_MAX_LINES = 500  # cap per tenant: ~1 week at current volume
_LINE_CAP = 4096 - 64  # stay under PIPE_BUF


def capture_case(
    *,
    tenant_id: str | None,
    surface: str,
    state: Any,
    questions: list[Any],
    answers: dict[str, Any],
    model: str = "",
    now_iso: str = "",
    log_dir: str | os.PathLike[str] | None = None,
) -> bool:
    """Append one calibration case. Best-effort; NEVER raises."""
    try:
        tenant = _SAFE_SEGMENT.sub("-", (tenant_id or "").strip()).strip("-") or "tenant"
        # Serialize and truncate
        state_str = json.dumps(state, ensure_ascii=False, default=str)[:_STATE_CAP]
        answers_str = json.dumps(answers, ensure_ascii=False, default=str)
        bounded_answers = (
            json.loads(answers_str) if len(answers_str) <= _ANSWER_CAP else {
                "_truncated": True,
                "preview": answers_str[:_ANSWER_CAP - 80],
            }
        )
        questions_summary = [
            {"id": q.id, "type": q.type, "instructions": q.instructions[:120]}
            for q in questions
        ]
        record = {
            "surface": surface,
            "model": model,
            "ts": now_iso or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "state": state_str,
            "questions": questions_summary,
            "answers": bounded_answers,
            "owner_label": None,  # giusto | sbagliato | non_so — filled by the owner
        }
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        if len(line) > _LINE_CAP:
            return False  # too big to be atomic

        directory = Path(log_dir) if log_dir else _resolve_dir()
        path = directory / f"jev-calibration-{tenant}.jsonl"

        # Cap: drop the oldest line if we're at the limit
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            if len(lines) >= _MAX_LINES:
                # Keep the newest _MAX_LINES - 1
                path.write_text("".join(lines[-(_MAX_LINES - 1):]), encoding="utf-8")

        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001
        logger.debug("calibration capture failed", exc_info=True)
        return False


def _resolve_dir() -> Path:
    override = os.environ.get("MARVIS_JEV_CALIBRATION_DIR", "").strip()
    if override:
        return Path(override)
    db_path = (
        os.environ.get("MARVIS_DB_PATH") or os.environ.get("PIR_DB_PATH") or ""
    ).strip()
    if db_path:
        return Path(db_path).expanduser().resolve().parent / "jev_calibration"
    return Path.home() / ".marvis" / "jev_calibration"


def _read_tenant(tenant_id: str | None) -> str:
    raw = tenant_id if tenant_id is not None else os.environ.get("TENANT_ID", "")
    tenant = _SAFE_SEGMENT.sub("-", raw.strip()).strip("-")
    if not tenant:
        raise ValueError("calibration tenant must be explicit or configured via TENANT_ID")
    return tenant


def read_pending(
    tenant_id: str | None = None,
    *,
    surface: str | None = None,
    limit: int = 50,
    log_dir: str | os.PathLike[str] | None = None,
) -> list[dict[str, Any]]:
    """Read calibration cases with owner_label=None (pending labeling)."""
    directory = Path(log_dir) if log_dir else _resolve_dir()
    path = directory / f"jev-calibration-{_read_tenant(tenant_id)}.jsonl"
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        if record.get("owner_label") is not None:
            continue  # already labeled
        if surface and record.get("surface") != surface:
            continue
        out.append(record)
        if len(out) >= limit:
            break
    return out


def write_label(
    tenant_id: str,
    index: int,
    label: str,
    *,
    log_dir: str | os.PathLike[str] | None = None,
) -> bool:
    """Write an owner label back to the calibration file at the given index."""
    if label not in ("giusto", "sbagliato", "non_so"):
        return False
    directory = Path(log_dir) if log_dir else _resolve_dir()
    path = directory / f"jev-calibration-{_read_tenant(tenant_id)}.jsonl"
    if not path.exists():
        return False
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if index < 0 or index >= len(lines):
        return False
    try:
        record = json.loads(lines[index])
    except (ValueError, TypeError):
        return False
    record["owner_label"] = label
    lines[index] = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
    path.write_text("".join(lines), encoding="utf-8")
    return True


def labeled_stats(tenant_id: str | None = None, *, log_dir=None) -> dict:
    """Count labeled/unlabeled per surface."""
    directory = Path(log_dir) if log_dir else _resolve_dir()
    path = directory / f"jev-calibration-{_read_tenant(tenant_id)}.jsonl"
    if not path.exists():
        return {"total": 0, "labeled": 0, "pending": 0, "by_surface": {}}
    stats: dict[str, Any] = {"total": 0, "labeled": 0, "pending": 0, "by_surface": {}}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (ValueError, TypeError):
            continue
        stats["total"] += 1
        surface = record.get("surface", "unknown")
        bucket = stats["by_surface"].setdefault(surface, {"total": 0, "labeled": 0})
        bucket["total"] += 1
        if record.get("owner_label") is not None:
            stats["labeled"] += 1
            bucket["labeled"] += 1
        else:
            stats["pending"] += 1
    return stats


__all__ = ["capture_case", "labeled_stats", "read_pending", "write_label"]
