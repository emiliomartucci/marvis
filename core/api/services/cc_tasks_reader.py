# api/services/cc_tasks_reader.py
# v1.0.0 - 2026-03-02 - CC native tasks reader (DevX Sprint 4)
"""Read Claude Code native task files from ~/.claude/tasks/{conversation_id}/*.json.

Task format (one JSON file per task):
  { "id": "1", "subject": "...", "description": "...", "activeForm": "...",
    "status": "pending|in_progress|completed", "blocks": [], "blockedBy": [] }
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

# Primary: container mount (OpenClaw Docker), secondary: host path
_CLAUDE_TASKS_PATHS = [
    Path("/data/claude/tasks"),
    Path.home() / ".claude" / "tasks",
]

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _find_tasks_dir(conversation_id: str) -> Path | None:
    """Return the tasks directory for conversation_id, or None if not found."""
    for base in _CLAUDE_TASKS_PATHS:
        candidate = base / conversation_id
        if candidate.is_dir() and base in candidate.resolve().parents:
            return candidate
    return None


def _read_tasks_sync(tasks_dir: Path) -> list[dict]:
    """Sync: read all *.json task files, return sorted by numeric id."""
    tasks = []
    for json_file in tasks_dir.iterdir():
        if json_file.suffix != ".json" or json_file.name.startswith("."):
            continue
        try:
            obj = json.loads(json_file.read_text())
            if isinstance(obj, dict) and "id" in obj:
                tasks.append(obj)
        except (json.JSONDecodeError, OSError):
            continue
    # Sort by numeric id if possible
    def _sort_key(t: dict) -> tuple:
        try:
            return (0, int(t["id"]))
        except (ValueError, TypeError):
            return (1, str(t["id"]))
    return sorted(tasks, key=_sort_key)


async def read_cc_tasks(conversation_id: str) -> list[dict] | None:
    """Async: read CC native tasks for a conversation.

    Returns list of task dicts, or None if tasks dir not found.
    Raises ValueError if conversation_id format is invalid.
    """
    if not _UUID_RE.match(conversation_id):
        raise ValueError(f"Invalid conversation_id format: {conversation_id!r}")
    tasks_dir = _find_tasks_dir(conversation_id)
    if tasks_dir is None:
        return None
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _read_tasks_sync, tasks_dir),
            timeout=10.0,
        )
    except TimeoutError:
        return []
