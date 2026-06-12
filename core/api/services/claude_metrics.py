# v2.5.0 - 2026-04-23 - PR4: shadow cost_equivalent fields + mirror for Claude (= real)
# v2.4.0 - 2026-04-22 - PR2: extend SessionMetrics (dual ctx/cost, TTL-split cache, reasoning, working_ms)
# v2.3.0 - 2026-04-22 - Re-export from model_registry + add ClaudeMetricsProvider (PR1)
# v2.2.0 - 2026-03-10 - Normalize model [1m] suffix + dynamic CONTEXT_WINDOW for 1M sessions
from __future__ import annotations

import json
import logging
import os
import re
import time as _time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable

# Re-export from model_registry for backward compat (30+ call sites import
# these names directly from claude_metrics). PR2 will migrate call sites over
# time; PR1 keeps imports working unchanged.
from core.api.services.model_registry import (  # noqa: F401  (re-exported)
    MODEL_PRICING,
    get_context_window,
    normalize_model_id,
    pricing,
)

logger = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
PANE_METRICS_DIR = Path.home() / ".claude" / "pane-metrics"
WORKTREES_BASE = Path.home() / "dev"
DEFAULT_CWD = os.environ.get("MARVIS_WORKSPACE_ROOT", str(Path.home() / "workspace"))

# Default context window (legacy constant; use get_context_window(model) for
# per-model lookup that handles Opus/Sonnet/Haiku correctly).
CONTEXT_WINDOW = 200_000


@dataclass
class SessionMetrics:
    # Existing fields — preserve exact order/defaults for back-compat
    conversation_id: str
    model: str | None = None
    context_pct: float = 0.0  # backward-compat alias — mirrors context_pct_real
    cost_usd: float = 0.0     # backward-compat alias — mirrors cost_conversation_usd
    message_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    first_timestamp: str | None = None
    last_timestamp: str | None = None
    duration_minutes: float = 0.0

    # --- PR2 additions ------------------------------------------------------
    # Context %: real vs scaled (Claude scales by 100/84 to match the
    # auto-compact threshold banner). For OpenCode, `context_pct_scaled` is
    # always None — the 84% fudge factor is Claude-specific.
    context_pct_real: float | None = None
    context_pct_scaled: float | None = None

    # Cost: conversation = this JSONL only; session = sum across resume chain.
    # `cost_session_usd` is populated at the service layer (session_metrics
    # service aggregates via `session_conversations` table). Parser fills
    # `cost_conversation_usd` with its local total.
    cost_conversation_usd: float | None = None
    cost_session_usd: float | None = None
    cost_session_incomplete: bool = False

    # TTL-split cache writes (Anthropic: 1.25× 5m, 2× 1h base rate).
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    # Extended tokens
    reasoning_tokens: int | None = None

    # Wall-clock working time derived from message pairs.
    working_seconds_msg: int | None = None

    # Resume chain: conversation ids in order (oldest → newest). Empty for
    # freshly started sessions or when the parser doesn't know the chain.
    conversation_ids: list[str] = field(default_factory=list)

    # Provenance
    metrics_refreshed_at: str | None = None
    pricing_version: str | None = None

    # --- PR4 additions ------------------------------------------------------
    # Shadow cost: what the session WOULD cost at pay-per-token API rates.
    # For Claude sessions this equals the real cost (already pay-per-token);
    # for OpenCode OAuth/free sessions (real cost=0) this exposes the
    # hypothetical API bill. `None` means "unknown pricing, do not guess"
    # (fallback_strategy=skip). `cost_equivalent_pricing_version` tracks
    # which kb/opencode-pricing-*.json produced the number.
    cost_conversation_equivalent_usd: float | None = None
    cost_session_equivalent_usd: float | None = None
    cost_equivalent_pricing_version: str | None = None


def normalize_cwd(cwd: str = DEFAULT_CWD) -> str:
    """Return an absolute cwd in the format Claude Code encodes on disk."""
    return os.path.abspath(os.path.expanduser(cwd))


def get_project_dir(cwd: str = DEFAULT_CWD) -> Path:
    """Convert a working directory to Claude Code's project path."""
    encoded = normalize_cwd(cwd).replace("/", "-")
    return CLAUDE_PROJECTS_DIR / encoded


def _safe_mtime(path: Path) -> float:
    """Safely get mtime; returns 0.0 on OSError (sorts to end of list)."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def find_conversation_for_worktree(worktree_path: str) -> str | None:
    """Find the most recent Claude conversation JSONL for a given worktree path.

    Claude Code stores conversations at:
      ~/.claude/projects/{encoded_cwd}/{conversation_id}.jsonl
    where encoded_cwd = cwd.replace("/", "-")  (same logic as get_project_dir)

    Security: validates worktree_path is under WORKTREES_BASE to prevent traversal.
    Returns conversation_id (JSONL stem) of most-recent file by mtime, or None.
    """
    if not worktree_path:
        return None
    # Security: reject paths outside ~/dev/
    resolved = Path(worktree_path.rstrip("/")).resolve()
    if not resolved.is_relative_to(WORKTREES_BASE.resolve()):
        logger.warning("Worktree path outside WORKTREES_BASE rejected: %s", worktree_path)
        return None
    # Reuse get_project_dir() — same encoding as Claude Code uses
    project_dir = get_project_dir(worktree_path.rstrip("/"))
    if not project_dir.is_dir():
        logger.debug(
            "No Claude project dir for worktree %s (expected %s)",
            worktree_path,
            project_dir,
        )
        return None
    try:
        files = sorted(project_dir.glob("*.jsonl"), key=_safe_mtime, reverse=True)
        return files[0].stem if files else None
    except OSError as exc:
        logger.warning("Filesystem error scanning worktree %s: %s", worktree_path, exc)
        return None


_PRICING_VERSION = "2026-04-22"


def parse_conversation(jsonl_path: Path) -> SessionMetrics | None:
    """Parse a Claude Code conversation JSONL file for metrics.

    PR2 additions:
      - Split `cache_creation` into 5m/1h TTL buckets when JSONL provides
        `ephemeral_5m_input_tokens`/`ephemeral_1h_input_tokens`. Legacy entries
        with only `cache_creation_input_tokens` fall back to the 5m bucket
        (conservative — 5m is the default TTL).
      - Cost uses `model_registry.pricing(model)` TTL-split rates.
      - `context_pct_real` = (inp + cr + cw5 + cw1) / context_window * 100.
      - `context_pct_scaled` = real * 100 / 84 (Claude only, capped 100).
      - `working_seconds_msg` pairs each user → next assistant timestamp,
        skipping `<synthetic>` assistants and negative deltas.
    """
    if not jsonl_path.exists():
        return None

    conversation_id = jsonl_path.stem
    model: str | None = None
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write_5m = 0
    total_cache_write_1h = 0
    message_count = 0
    first_ts: str | None = None
    last_ts: str | None = None
    last_context_tokens = 0

    cost = 0.0

    # For working_seconds_msg: list of (kind, ts_epoch, is_synthetic)
    # kind: "user" | "assistant"
    events: list[tuple[str, float, bool]] = []

    def _ts_to_epoch(ts: str | None) -> float | None:
        if not ts:
            return None
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None

    try:
        with open(jsonl_path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")
                if msg_type == "user":
                    ts_epoch = _ts_to_epoch(data.get("timestamp"))
                    if ts_epoch is not None:
                        events.append(("user", ts_epoch, False))
                    continue

                if msg_type == "assistant":
                    msg = data.get("message", {})
                    usage = msg.get("usage", {})
                    msg_model = msg.get("model")
                    is_synthetic = msg_model == "<synthetic>"

                    ts_epoch = _ts_to_epoch(data.get("timestamp"))
                    if ts_epoch is not None:
                        events.append(("assistant", ts_epoch, is_synthetic))

                    if not usage:
                        continue

                    if msg_model and not is_synthetic:
                        model = normalize_model_id(msg_model)

                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cr = usage.get("cache_read_input_tokens", 0)
                    cw_total = usage.get("cache_creation_input_tokens", 0)

                    # TTL split: prefer explicit breakdown, fall back to 5m
                    creation = usage.get("cache_creation") or {}
                    cw5: int = 0
                    cw1: int = 0
                    if isinstance(creation, dict) and (
                        "ephemeral_5m_input_tokens" in creation
                        or "ephemeral_1h_input_tokens" in creation
                    ):
                        cw5 = int(creation.get("ephemeral_5m_input_tokens", 0) or 0)
                        cw1 = int(creation.get("ephemeral_1h_input_tokens", 0) or 0)
                    else:
                        # Legacy format: single cache_creation_input_tokens
                        # Default to 5m TTL (cheaper, Anthropic's default)
                        cw5 = int(cw_total or 0)
                        cw1 = 0

                    total_input += int(inp or 0)
                    total_output += int(out or 0)
                    total_cache_read += int(cr or 0)
                    total_cache_write_5m += cw5
                    total_cache_write_1h += cw1
                    message_count += 1

                    last_context_tokens = int(inp or 0) + int(cr or 0) + cw5 + cw1

                    # TTL-split pricing
                    p = pricing(model)
                    cost += (
                        int(inp or 0) * p.input
                        + int(out or 0) * p.output
                        + int(cr or 0) * p.cache_read
                        + cw5 * p.cache_write_5m
                        + cw1 * p.cache_write_1h
                    ) / 1_000_000

                    ts = data.get("timestamp")
                    if ts:
                        if not first_ts:
                            first_ts = ts
                        last_ts = ts

    except (OSError, PermissionError) as e:
        logger.error("Failed to read JSONL %s: %s", jsonl_path, e)
        return None

    if message_count == 0:
        return None

    duration_min = 0.0
    if first_ts and last_ts:
        try:
            t1 = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
            t2 = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            duration_min = (t2 - t1).total_seconds() / 60
        except ValueError:
            pass

    ctx_window = get_context_window(model)
    context_pct = (last_context_tokens / ctx_window * 100) if last_context_tokens else 0.0
    context_pct_real = round(context_pct, 1)
    # Scaled to match Claude Code auto-compact threshold banner (raw 84% ≈ full)
    context_pct_scaled = round(min(context_pct_real * 100 / 84, 100.0), 1)

    # working_seconds_msg: sum of (next_assistant - user) where next assistant
    # is NOT <synthetic>, skipping negative deltas (clock skew / reordering).
    working_seconds_msg = _pair_working_seconds(events)

    cost_conversation = round(cost, 4)
    total_cache_write = total_cache_write_5m + total_cache_write_1h

    return SessionMetrics(
        conversation_id=conversation_id,
        model=model,
        # Legacy aliases
        context_pct=context_pct_real,
        cost_usd=cost_conversation,
        message_count=message_count,
        input_tokens=total_input,
        output_tokens=total_output,
        cache_read_tokens=total_cache_read,
        cache_write_tokens=total_cache_write,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        duration_minutes=round(duration_min, 1),
        # PR2
        context_pct_real=context_pct_real,
        context_pct_scaled=context_pct_scaled,
        cost_conversation_usd=cost_conversation,
        cost_session_usd=None,  # populated at service layer
        cost_session_incomplete=False,
        cache_write_5m_tokens=total_cache_write_5m,
        cache_write_1h_tokens=total_cache_write_1h,
        reasoning_tokens=None,  # Claude JSONL doesn't expose reasoning
        working_seconds_msg=working_seconds_msg,
        pricing_version=_PRICING_VERSION,
        # PR4: Claude is always pay-per-token → equivalent mirrors real.
        cost_conversation_equivalent_usd=cost_conversation,
        cost_session_equivalent_usd=None,  # aggregated at service layer
        cost_equivalent_pricing_version=_PRICING_VERSION,
    )


def _pair_working_seconds(events: list[tuple[str, float, bool]]) -> int:
    """Pair each user message with the next non-synthetic assistant.

    Returns total seconds (int). Skips negative deltas (clock skew).
    `events` must be in file/append order — i.e. chronological for Claude.
    """
    total = 0.0
    i = 0
    n = len(events)
    while i < n:
        kind, ts, _ = events[i]
        if kind != "user":
            i += 1
            continue
        # Find next assistant (non-synthetic)
        j = i + 1
        while j < n:
            nkind, nts, syn = events[j]
            if nkind == "assistant":
                if syn:
                    j += 1
                    continue
                delta = nts - ts
                if delta > 0:
                    total += delta
                break
            j += 1
        i = j if j > i else i + 1
    return int(total)


def get_jsonl_mtime(conversation_id: str, cwd: str = DEFAULT_CWD) -> float | None:
    """Return the mtime of a conversation JSONL file, or None if not found.

    The encoded path is deterministic from cwd — no directory scan needed.
    Pass the actual working directory (incl. worktree path) as cwd for an
    accurate lookup.
    """
    primary = get_project_dir(cwd) / f"{conversation_id}.jsonl"
    try:
        return primary.stat().st_mtime
    except OSError:
        return None


def get_last_context_pct(
    conversation_id: str,
    cwd: str = DEFAULT_CWD,
    chunk_size: int = 32768,
) -> float | None:
    """Fast context % via tail-read: reads only the last chunk_size bytes.

    Scans lines in reverse to find the last assistant message with usage data.
    O(chunk_size) instead of O(file_size). Falls back to larger chunks if needed.
    Returns context_pct (0-100) or None if not found.
    """
    path = get_project_dir(cwd) / f"{conversation_id}.jsonl"
    if not path.exists():
        return None

    try:
        file_size = path.stat().st_size
    except OSError:
        return None

    for attempt in range(3):  # try up to 3 chunk sizes: 32K, 128K, 512K
        current_chunk = chunk_size * (4 ** attempt)
        read_size = min(current_chunk, file_size)
        offset = max(0, file_size - read_size)

        try:
            with open(path, "rb") as f:
                f.seek(offset)
                raw = f.read(read_size)
        except OSError:
            return None

        # Decode with replacement to handle any encoding quirks
        text = raw.decode("utf-8", errors="replace")

        # Split lines and iterate in reverse (skip first partial line if offset > 0)
        lines = text.split("\n")
        start = 1 if offset > 0 else 0  # skip first line (may be partial)

        for line in reversed(lines[start:]):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if data.get("type") != "assistant":
                continue
            usage = data.get("message", {}).get("usage", {})
            if not usage:
                continue
            inp = usage.get("input_tokens", 0)
            cr = usage.get("cache_read_input_tokens", 0)
            cw = usage.get("cache_creation_input_tokens", 0)
            total = inp + cr + cw
            msg_model = normalize_model_id(data.get("message", {}).get("model"))
            if total > 0:
                ctx_window = get_context_window(msg_model)
                return round(total / ctx_window * 100, 1)

        # If we read the whole file and found nothing, stop
        if read_size >= file_size:
            break

    return None


def find_conversation_by_id(conversation_id: str, cwd: str = DEFAULT_CWD) -> SessionMetrics | None:
    """Parse a specific conversation by its UUID.

    The encoded project directory is deterministic from cwd — pass the exact
    working directory (including the worktree path for worktree sessions) so
    the lookup is a single stat, not a directory scan.
    """
    primary = get_project_dir(cwd) / f"{conversation_id}.jsonl"
    if primary.exists():
        return parse_conversation(primary)
    return None


def find_conversation_cwd(
    conversation_id: str,
    candidate_cwds: Iterable[str],
) -> str | None:
    """Return the first cwd whose Claude project dir contains the conversation."""
    seen: set[str] = set()
    for cwd in candidate_cwds:
        normalized = normalize_cwd(cwd)
        if normalized in seen:
            continue
        seen.add(normalized)
        if (get_project_dir(normalized) / f"{conversation_id}.jsonl").exists():
            return normalized
    return None


def find_recent_conversations(cwd: str = DEFAULT_CWD, limit: int = 20) -> list[tuple[str, float]]:
    """List recent conversations sorted by modification time. Returns (uuid, mtime) pairs."""
    project_dir = get_project_dir(cwd)
    if not project_dir.exists():
        return []

    files = []
    for jsonl_path in project_dir.glob("*.jsonl"):
        try:
            mtime = jsonl_path.stat().st_mtime
            files.append((jsonl_path.stem, mtime))
        except OSError:
            continue

    files.sort(key=lambda x: x[1], reverse=True)
    return files[:limit]


def detect_conversation_for_session(
    session_created_epoch: float,
    cwd: str = DEFAULT_CWD,
) -> str | None:
    """Find the conversation JSONL for a tmux session.

    Strategy:
    1. Try direct lookup in the cwd project dir first (fast path).
    2. Find JSONL whose first timestamp is closest to pane_start_time (within 5 min)
    3. If no timestamp match, find the JSONL whose first timestamp is AFTER
       pane_start_time and most recently modified (the active conversation in that pane)

    Falls back to scanning recent project dirs only when cwd direct lookup fails.
    Caps the scan at MAX_DIRS project directories (sorted by mtime) to bound cost.
    """
    from datetime import datetime

    MAX_DIRS = 20  # only inspect the 20 most-recently-modified project dirs

    # Fast path: if cwd is known, start with its project dir only
    cwd_project_dir = get_project_dir(cwd)

    # Collect JSONL files — primary dir first, then recent project dirs up to cap
    all_convs: dict[str, tuple[float, Path]] = {}  # conv_id -> (mtime, path)

    def _collect_from_dir(pdir: Path) -> None:
        for jsonl_path in pdir.glob("*.jsonl"):
            try:
                conv_id = jsonl_path.stem
                mtime = jsonl_path.stat().st_mtime
                if conv_id not in all_convs or mtime > all_convs[conv_id][0]:
                    all_convs[conv_id] = (mtime, jsonl_path)
            except OSError:
                continue

    if cwd_project_dir.is_dir():
        _collect_from_dir(cwd_project_dir)

    if CLAUDE_PROJECTS_DIR.exists():
        # Sort project dirs by mtime descending; cap at MAX_DIRS to avoid full scan
        try:
            project_dirs = sorted(
                (d for d in CLAUDE_PROJECTS_DIR.iterdir() if d.is_dir() and d != cwd_project_dir),
                key=_safe_mtime,
                reverse=True,
            )[:MAX_DIRS]
        except OSError:
            project_dirs = []
        for pdir in project_dirs:
            _collect_from_dir(pdir)

    recent = sorted(all_convs.items(), key=lambda x: x[1][0], reverse=True)[:50]

    best_id = None
    best_delta = float("inf")

    # Fallback: JSONL started after pane, most recently modified
    fallback_id = None
    fallback_mtime = 0.0

    for conv_id, (mtime, jsonl_path) in recent:
        try:
            # Scan first few lines — JSONL may start with file-history-snapshot
            # entries (no timestamp) before the first real user/assistant message.
            ts_str = None
            with open(jsonl_path) as f:
                for _ in range(5):
                    line = f.readline()
                    if not line:
                        break
                    try:
                        ts_str = json.loads(line).get("timestamp")
                    except json.JSONDecodeError:
                        continue
                    if ts_str:
                        break
            if not ts_str:
                continue

            file_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
            delta = abs(file_ts - session_created_epoch)

            # Strategy 1: closest match within 5 minutes
            if delta < best_delta and delta < 300:
                best_delta = delta
                best_id = conv_id

            # Strategy 2: conversation started after pane, most recently modified
            if file_ts >= session_created_epoch and mtime > fallback_mtime:
                fallback_mtime = mtime
                fallback_id = conv_id
        except (OSError, json.JSONDecodeError, ValueError):
            continue

    return best_id or fallback_id


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def detect_conversation_by_pid(claude_pid: int) -> str | None:
    """Detect conversation_id by inspecting /proc/{pid}/fd/ for tasks directory.

    Claude Code opens ~/.claude/tasks/{conversation_id}/ during a session.
    Claude 4.6 may also open an FD on ~/.claude/tasks (the directory itself)
    without a UUID subdirectory in the path — in that case we fall back to
    scanning tasks/ for the most recently modified UUID subdirectory.
    """
    fd_dir = Path(f"/proc/{claude_pid}/fd")
    if not fd_dir.exists():
        return None

    tasks_prefix = str(Path.home() / ".claude" / "tasks")
    found_tasks_dir = False
    try:
        for entry in fd_dir.iterdir():
            try:
                target = os.readlink(str(entry))
            except OSError:
                continue
            # Remove " (deleted)" suffix if present
            target = target.replace(" (deleted)", "")
            if target.startswith(tasks_prefix):
                rel = target[len(tasks_prefix):].strip("/")
                # Direct UUID match in path
                match = _UUID_RE.match(rel)
                if match:
                    return match.group(0)
                # FD points to tasks directory itself (Claude 4.6 pattern)
                if not rel:
                    found_tasks_dir = True
    except OSError as e:
        logger.debug("Failed to read /proc/%d/fd: %s", claude_pid, e)

    # Fallback: if we found the tasks directory but no UUID in FDs,
    # scan tasks/ for the most recently modified UUID subdirectory.
    # Best-effort for multi-session scenarios.
    if found_tasks_dir:
        tasks_path = Path.home() / ".claude" / "tasks"
        try:
            best_uuid = None
            best_mtime = 0.0
            for entry in tasks_path.iterdir():
                if entry.is_dir() and _UUID_RE.match(entry.name):
                    try:
                        mtime = entry.stat().st_mtime
                        if mtime > best_mtime:
                            best_mtime = mtime
                            best_uuid = entry.name
                    except OSError:
                        continue
            return best_uuid
        except OSError:
            pass

    return None


def detect_activity_by_mtime(
    conversation_id: str,
    cwd: str = DEFAULT_CWD,
    working_threshold: float = 15.0,
) -> str:
    """Detect activity state by JSONL file modification time.

    Used as fallback when pane is too small for content analysis.
    If JSONL was modified within threshold seconds → working, else → idle.
    """
    import time

    project_dir = get_project_dir(cwd)
    jsonl_path = project_dir / f"{conversation_id}.jsonl"
    try:
        mtime = jsonl_path.stat().st_mtime
        age = time.time() - mtime
        return "working" if age < working_threshold else "idle"
    except OSError:
        return "working"  # can't read → assume working


@dataclass
class PaneMetrics:
    """Metrics from statusline.sh per-pane file."""
    session_id: str
    used_pct: float
    cost_usd: float
    model: str | None = None
    timestamp: str | None = None


def read_pane_metrics(pane_id: str, max_age: float = 120.0) -> PaneMetrics | None:
    """Read per-pane metrics written by statusline.sh.

    Each Claude Code session writes ~/.claude/pane-metrics/{pane_num}.json
    on every status line update. Contains session_id (= conversation_id),
    used_pct (Claude's own context %), cost, model.

    Args:
        pane_id: tmux pane ID (e.g., "%70" or "70" — digits extracted)
        max_age: ignore files older than this (seconds), default 2 min

    Returns PaneMetrics or None if file missing/stale/invalid.
    """
    # Extract digits from pane_id (e.g., "%70" → "70")
    digits = re.sub(r"[^0-9]", "", pane_id)
    if not digits:
        return None

    path = PANE_METRICS_DIR / f"{digits}.json"
    try:
        stat = path.stat()
        age = _time.time() - stat.st_mtime
        if age > max_age:
            return None
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    sid = data.get("session_id")
    if not sid or not _UUID_RE.match(sid):
        return None

    return PaneMetrics(
        session_id=sid,
        used_pct=float(data.get("used_pct", 0)),
        cost_usd=float(data.get("cost_usd", 0)),
        model=normalize_model_id(data.get("model")) or None,
        timestamp=data.get("timestamp"),
    )


def read_pane_session_id(pane_id: str) -> str | None:
    """Read only session_id from pane-metrics file, ignoring staleness.

    The session_id (= conversation_id) is stable for the lifetime of a
    Claude session — it doesn't change when Claude is idle. So we can
    read it regardless of file age, unlike read_pane_metrics() which
    enforces a max_age check.
    """
    digits = re.sub(r"[^0-9]", "", pane_id)
    if not digits:
        return None

    path = PANE_METRICS_DIR / f"{digits}.json"
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, ValueError):
        return None

    sid = data.get("session_id")
    if sid and _UUID_RE.match(sid):
        return sid
    return None


def cleanup_pane_metrics(active_pane_ids: set[str]) -> None:
    """Remove pane metrics files for dead panes."""
    if not PANE_METRICS_DIR.is_dir():
        return
    active_digits = {re.sub(r"[^0-9]", "", p) for p in active_pane_ids}
    try:
        for path in PANE_METRICS_DIR.glob("*.json"):
            if path.stem not in active_digits:
                path.unlink(missing_ok=True)
    except OSError:
        pass


# --------------------------------------------------------------------------
# MetricsProvider adapter (PR1 — see docs/plans/2026-04-22-feat-metrics-
# provider-consistency-plan.md §Phase 1). Wraps existing functions to satisfy
# the Protocol defined in api/services/metrics_providers.py. No behavior
# change for Claude sessions.
# --------------------------------------------------------------------------


class ClaudeMetricsProvider:
    """MetricsProvider for Claude Code JSONL-backed sessions."""

    name = "claude"

    def parse_session(
        self,
        session_id: str,
        cwd: str | None = None,
    ) -> SessionMetrics | None:
        """Parse a Claude conversation JSONL by id. Thin wrapper over find_conversation_by_id."""
        return find_conversation_by_id(session_id, cwd or DEFAULT_CWD)

    def get_last_context_pct(
        self,
        session_id: str,
        cwd: str | None = None,
    ) -> float | None:
        """Fast tail-read of the last assistant message's context %."""
        return get_last_context_pct(session_id, cwd or DEFAULT_CWD)
