"""Codex JSONL metrics provider.

Codex CLI stores session transcripts under ~/.codex/sessions/YYYY/MM/DD/*.jsonl.
The JSONL includes a session_meta event with the stable session id and periodic
token_count events with current context and cumulative token usage.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from core.api.services.claude_metrics import SessionMetrics

logger = logging.getLogger(__name__)

CODEX_SESSIONS_DIR = Path.home() / ".codex" / "sessions"
_WORKSPACE_ROOT = os.environ.get("MARVIS_WORKSPACE_ROOT", str(Path.home() / "workspace"))
_PROC_ROOT = Path("/proc")
_CODEX_DETECT_GRACE_SECONDS = 5.0
_CODEX_DETECT_WINDOW_SECONDS = 60.0
_SESSION_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class CodexSessionMeta:
    session_id: str
    timestamp: datetime
    cwd: str | None
    path: Path


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iter_session_files() -> Iterable[Path]:
    if not CODEX_SESSIONS_DIR.is_dir():
        return ()
    return CODEX_SESSIONS_DIR.glob("**/*.jsonl")


def _session_id_from_path(path: Path) -> str | None:
    match = _SESSION_ID_RE.search(path.stem)
    return match.group(1).lower() if match else None


def _is_codex_session_path(path: Path) -> bool:
    if path.suffix != ".jsonl":
        return False
    try:
        path.resolve(strict=False).relative_to(
            CODEX_SESSIONS_DIR.resolve(strict=False)
        )
    except (OSError, ValueError):
        return False
    return True


def _read_meta(path: Path) -> CodexSessionMeta | None:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload") or {}
                session_id = payload.get("id")
                timestamp = _parse_timestamp(payload.get("timestamp"))
                if not session_id or timestamp is None:
                    return None
                return CodexSessionMeta(
                    session_id=session_id,
                    timestamp=timestamp,
                    cwd=payload.get("cwd"),
                    path=path,
                )
    except OSError:
        return None
    return None


def find_session_path(session_id: str) -> Path | None:
    """Find the JSONL file for a Codex session id."""
    if not session_id or not CODEX_SESSIONS_DIR.is_dir():
        return None
    for path in _iter_session_files():
        if session_id in path.stem:
            return path
    for path in _iter_session_files():
        meta = _read_meta(path)
        if meta and meta.session_id == session_id:
            return path
    return None


def _iter_process_tree_pids(root_pid: int) -> Iterable[int]:
    """Yield root_pid and descendants using Linux procfs child lists."""
    seen: set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in seen or pid <= 0:
            continue
        seen.add(pid)
        yield pid

        children_path = _PROC_ROOT / str(pid) / "task" / str(pid) / "children"
        try:
            children = children_path.read_text(encoding="utf-8").split()
        except OSError:
            continue
        for child in children:
            try:
                stack.append(int(child))
            except ValueError:
                continue


def _iter_open_session_files(pid: int) -> Iterable[Path]:
    fd_dir = _PROC_ROOT / str(pid) / "fd"
    try:
        fds = list(fd_dir.iterdir())
    except OSError:
        return ()

    paths: list[Path] = []
    for fd_path in fds:
        try:
            target = fd_path.readlink()
        except OSError:
            continue
        target_text = str(target)
        if target_text.endswith(" (deleted)"):
            target_text = target_text.removesuffix(" (deleted)")
        target_path = Path(target_text)
        if not target_path.is_absolute() or not _is_codex_session_path(target_path):
            continue
        paths.append(target_path)
    return paths


def detect_codex_for_process(
    pid: int | None,
    already_linked: Iterable[str] = (),
) -> str | None:
    """Detect the Codex session id from a live process tree's open JSONL fd.

    Resumed Codex sessions keep writing to their original JSONL, whose
    session_meta timestamp can be days older than the tmux pane. Inspecting the
    live process fds is the stable correlation path for those sessions.
    """
    if pid is None or pid <= 0:
        return None

    linked = set(already_linked)
    seen_paths: set[Path] = set()
    candidates: list[tuple[float, str]] = []
    for process_pid in _iter_process_tree_pids(pid):
        for path in _iter_open_session_files(process_pid):
            try:
                canonical = path.resolve(strict=False)
            except OSError:
                canonical = path
            if canonical in seen_paths:
                continue
            seen_paths.add(canonical)

            meta = _read_meta(path)
            session_id = meta.session_id if meta else _session_id_from_path(path)
            if not session_id or session_id in linked:
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                mtime = 0.0
            candidates.append((mtime, session_id))

    candidates.sort(reverse=True)
    return candidates[0][1] if candidates else None


def detect_codex_for_session(
    pane_start_epoch: float | None,
    cwd: str | None = None,
    already_linked: Iterable[str] = (),
) -> str | None:
    """Detect the Codex session id that started shortly after a tmux pane.

    Codex JSONL files do not currently record the tmux session name. The safest
    stable correlation available is the launch timestamp: Console-created Codex
    sessions write session_meta within a few seconds of tmux creation. The match
    window is intentionally bounded to avoid stealing a later unrelated Codex
    session from an idle pane that never received a prompt.
    """
    if pane_start_epoch is None:
        return None
    linked = set(already_linked)
    lower = pane_start_epoch - _CODEX_DETECT_GRACE_SECONDS
    upper = pane_start_epoch + _CODEX_DETECT_WINDOW_SECONDS
    candidates: list[CodexSessionMeta] = []
    for path in _iter_session_files():
        meta = _read_meta(path)
        if not meta or meta.session_id in linked:
            continue
        started = meta.timestamp.timestamp()
        if lower <= started <= upper:
            # Codex currently launches from the shared workspace. If a future
            # version records a provider-specific cwd, prefer exact matches but
            # do not reject workspace-root sessions.
            if (
                cwd
                and meta.cwd
                and meta.cwd != cwd
                and meta.cwd != _WORKSPACE_ROOT
            ):
                continue
            candidates.append(meta)
    candidates.sort(key=lambda item: item.timestamp)
    return candidates[0].session_id if candidates else None


def parse_session_file(path: Path) -> SessionMetrics | None:
    if not path.exists():
        return None

    session_id: str | None = None
    model: str | None = None
    first_ts: str | None = None
    last_ts: str | None = None
    message_count = 0
    last_token_info: dict | None = None

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                obj_ts = obj.get("timestamp")
                if obj_ts and first_ts is None:
                    first_ts = obj_ts
                if obj_ts:
                    last_ts = obj_ts

                obj_type = obj.get("type")
                payload = obj.get("payload") or {}
                if obj_type == "session_meta":
                    session_id = payload.get("id") or session_id
                    first_ts = payload.get("timestamp") or first_ts
                elif obj_type == "turn_context":
                    model = payload.get("model") or model
                elif obj_type == "event_msg":
                    event_type = payload.get("type")
                    if event_type == "agent_message":
                        message_count += 1
                    elif event_type == "token_count":
                        info = payload.get("info")
                        if isinstance(info, dict):
                            last_token_info = info
    except OSError:
        return None

    if not session_id:
        meta = _read_meta(path)
        session_id = (
            meta.session_id if meta else (_session_id_from_path(path) or path.stem)
        )

    if not last_token_info:
        return SessionMetrics(
            conversation_id=session_id,
            model=model,
            message_count=message_count,
            first_timestamp=first_ts,
            last_timestamp=last_ts,
        )

    total_usage = last_token_info.get("total_token_usage") or {}
    last_usage = last_token_info.get("last_token_usage") or {}
    input_tokens = int(total_usage.get("input_tokens") or 0)
    output_tokens = int(total_usage.get("output_tokens") or 0)
    cache_read_tokens = int(total_usage.get("cached_input_tokens") or 0)
    reasoning_tokens = int(total_usage.get("reasoning_output_tokens") or 0)

    window = int(last_token_info.get("model_context_window") or 0)
    last_total = int(last_usage.get("total_tokens") or 0)
    if window > 0 and last_total > 0:
        context_pct = round(min((last_total / window) * 100, 100.0), 1)
    else:
        context_pct = 0.0

    duration_minutes = 0.0
    first_dt = _parse_timestamp(first_ts)
    last_dt = _parse_timestamp(last_ts)
    if first_dt and last_dt and last_dt >= first_dt:
        duration_minutes = round((last_dt - first_dt).total_seconds() / 60, 1)

    return SessionMetrics(
        conversation_id=session_id,
        model=model,
        context_pct=context_pct,
        cost_usd=0.0,
        message_count=message_count,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=0,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        duration_minutes=duration_minutes,
        context_pct_real=context_pct,
        context_pct_scaled=None,
        cost_conversation_usd=0.0,
        cost_session_usd=None,
        reasoning_tokens=reasoning_tokens,
        working_seconds_msg=None,
        pricing_version=None,
        cost_conversation_equivalent_usd=None,
        cost_session_equivalent_usd=None,
        cost_equivalent_pricing_version=None,
    )


class CodexMetricsProvider:
    name = "codex"

    def parse_session(
        self,
        session_id: str,
        cwd: str | None = None,
    ) -> SessionMetrics | None:
        _ = cwd
        path = find_session_path(session_id)
        return parse_session_file(path) if path else None

    def get_last_context_pct(
        self,
        session_id: str,
        cwd: str | None = None,
    ) -> float | None:
        metrics = self.parse_session(session_id, cwd)
        return metrics.context_pct_real if metrics else None
