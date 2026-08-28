# v1.0.0 - 2026-08-18 - Per-tool usage counter: append-only, privacy-safe, migration-free.
"""Per-tool MCP usage counter — the raw MEASURE for data-driven tool pruning.

Records exactly ONE line per HTTP MCP tool call: ``{tool, actor, ts}`` — never
the call arguments (privacy: no tenant data or secrets ever reach this log).
The write is BEST-EFFORT and NON-BLOCKING: any failure (full disk, permission,
bad path) is swallowed so a broken counter can never slow or break a customer
tool call.

Storage is MIGRATION-FREE by design (``migrations/`` is a protected fleet path):
a per-tenant append-only JSONL file written with ``O_APPEND``. A single short
line is below ``PIPE_BUF`` (4096 bytes on Linux), so concurrent ``O_APPEND``
writes from multiple workers interleave atomically without a lock.

This module is PURE (stdlib only, no fastmcp / no DB): the middleware that feeds
it lives in :mod:`core.api.mcp.tool_usage`, and the operator report that reads
it lives in ``core/scripts/tool_usage_report.py``. The report is OPERATOR-SIDE
and READ-ONLY — the usage log is never exposed as an MCP tool (a tenant must not
be able to read its own usage statistics; see memory 'ottica prodotto
multi-tenant').
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable, Iterator, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger("marvis.tool_usage")

_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")
_DEFAULT_TENANT = "tenant"


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _safe_segment(value: str, *, fallback: str) -> str:
    cleaned = _SAFE_SEGMENT.sub("-", (value or "").strip()).strip("-")
    return cleaned or fallback


def normalize_actor(actor_kind: str | None) -> str:
    """Coerce a caller identity to a short, bounded actor label.

    The MCP context yields ``human`` or ``agent`` (a scheduled routine connects
    with the same tenant identity as an agent). Anything unexpected collapses to
    ``agent`` so the log stays a small, closed vocabulary.
    """
    value = _SAFE_SEGMENT.sub("_", (actor_kind or "").strip().lower()).strip("_")
    return value[:32] or "agent"


def resolve_tenant_id(tenant_id: str | None = None) -> str:
    """Tenant scope for the log file — explicit arg, else the hosted env, else default."""
    if tenant_id and tenant_id.strip():
        return tenant_id.strip()
    env_value = (
        os.environ.get("TENANT_ID") or os.environ.get("MARVIS_TENANT_ID") or ""
    ).strip()
    return env_value or _DEFAULT_TENANT


def resolve_log_dir(log_dir: str | os.PathLike[str] | None = None) -> Path:
    """Directory holding the per-tenant usage logs.

    Priority: explicit arg → ``MARVIS_TOOL_USAGE_DIR`` → next to the tenant DB
    (so an operator's DB backup/rotation already covers it) → ``~/.marvis``.
    """
    if log_dir:
        return Path(log_dir)
    override = os.environ.get("MARVIS_TOOL_USAGE_DIR", "").strip()
    if override:
        return Path(override)
    db_path = (
        os.environ.get("MARVIS_DB_PATH") or os.environ.get("PIR_DB_PATH") or ""
    ).strip()
    if db_path:
        return Path(db_path).expanduser().resolve().parent / "tool_usage"
    return Path.home() / ".marvis" / "tool_usage"


def resolve_log_path(
    tenant_id: str | None = None,
    *,
    log_dir: str | os.PathLike[str] | None = None,
) -> Path:
    """Absolute path of the append-only JSONL log for one tenant."""
    tenant = _safe_segment(resolve_tenant_id(tenant_id), fallback=_DEFAULT_TENANT)
    return resolve_log_dir(log_dir) / f"tool-usage-{tenant}.jsonl"


def record_tool_call(
    tool_name: str,
    actor_kind: str | None,
    *,
    tenant_id: str | None = None,
    now: datetime | None = None,
    log_dir: str | os.PathLike[str] | None = None,
) -> bool:
    """Append one ``{tool, actor, ts}`` line. Best-effort; NEVER raises.

    Returns ``True`` when the line was written, ``False`` on any failure. The
    caller (middleware) relies on this never propagating: a counter failure must
    not touch the tool call it is measuring.
    """
    try:
        tool = _SAFE_SEGMENT.sub("_", (tool_name or "").strip()).strip("_") or "unknown"
        record = {
            "tool": tool[:128],
            "actor": normalize_actor(actor_kind),
            "ts": _iso(now or datetime.now(timezone.utc)),
        }
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
        path = resolve_log_path(tenant_id, log_dir=log_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
        return True
    except Exception:  # noqa: BLE001 - a broken counter must never break a tool call
        logger.debug("tool usage record failed", exc_info=True)
        return False


# --------------------------------------------------------------------------- #
# Read side — used ONLY by the operator report and by tests. Never a tool.
# --------------------------------------------------------------------------- #
def iter_events(paths: Iterable[str | os.PathLike[str]]) -> Iterator[dict[str, Any]]:
    """Yield well-formed ``{tool, actor, ts}`` events from JSONL logs.

    Malformed or partial lines (e.g. a torn final write) are skipped, not fatal —
    the report must survive a log that is being appended to concurrently.
    """
    for raw_path in paths:
        path = Path(raw_path)
        try:
            handle = path.open("r", encoding="utf-8")
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
                if not isinstance(record, Mapping):
                    continue
                tool = record.get("tool")
                ts = record.get("ts")
                if not isinstance(tool, str) or not isinstance(ts, str):
                    continue
                actor = record.get("actor")
                yield {
                    "tool": tool,
                    "actor": actor if isinstance(actor, str) else "agent",
                    "ts": ts,
                }


def discover_logs(log_dir: str | os.PathLike[str] | None = None) -> list[Path]:
    """All per-tenant usage logs under the resolved directory (sorted)."""
    directory = resolve_log_dir(log_dir)
    try:
        return sorted(directory.glob("tool-usage-*.jsonl"))
    except OSError:
        return []


def _month_of(ts: str) -> str | None:
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        # Tolerate a trailing 'Z' from non-Python writers.
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return f"{parsed.year:04d}-{parsed.month:02d}"


def aggregate_monthly(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Bucket events into ``{month: {tool: {total, by_actor}}}``.

    ``month`` is ``YYYY-MM`` in UTC derived from each event's ``ts``. Events with
    an unparseable timestamp are dropped from the aggregate (they still exist in
    the raw log). ``by_actor`` maps each actor label to its call count.
    """
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for event in events:
        ts = event.get("ts")
        tool = event.get("tool")
        if not isinstance(ts, str) or not isinstance(tool, str):
            continue
        month = _month_of(ts)
        if month is None:
            continue
        actor = event.get("actor")
        actor = actor if isinstance(actor, str) and actor else "agent"
        tool_bucket = result.setdefault(month, {}).setdefault(
            tool, {"total": 0, "by_actor": {}}
        )
        tool_bucket["total"] += 1
        tool_bucket["by_actor"][actor] = tool_bucket["by_actor"].get(actor, 0) + 1
    return result


__all__ = [
    "aggregate_monthly",
    "discover_logs",
    "iter_events",
    "normalize_actor",
    "record_tool_call",
    "resolve_log_dir",
    "resolve_log_path",
    "resolve_tenant_id",
]
