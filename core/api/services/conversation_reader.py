# api/services/conversation_reader.py
# v2.1.0 - 2026-03-04 - Extract AskUserQuestion tool_use blocks for DevX classifier

import asyncio
import json
import re
from pathlib import Path

CLAUDE_PROJECTS_PATH = Path.home() / ".claude" / "projects"
_UUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.IGNORECASE)


def _validate_conversation_id(conversation_id: str) -> bool:
    """Validates that conversation_id is a safe UUID-like string (no path traversal)."""
    return bool(_UUID_RE.match(conversation_id))


def _find_jsonl_path(conversation_id: str) -> Path | None:
    """Finds JSONL path safely using direct path construction (no rglob)."""
    if not CLAUDE_PROJECTS_PATH.exists():
        return None
    for project_dir in CLAUDE_PROJECTS_PATH.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{conversation_id}.jsonl"
        if candidate.exists() and CLAUDE_PROJECTS_PATH in candidate.resolve().parents:
            return candidate
    return None


def _read_conversation_sync(
    jsonl_path: Path,
    limit: int = 20,
    role: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Sync: reads JSONL tail, filters, returns last N messages."""
    messages = []
    file_size = jsonl_path.stat().st_size
    tail_bytes = min(file_size, 524288)  # 512KB tail

    with jsonl_path.open('rb') as f:
        if tail_bytes < file_size:
            f.seek(file_size - tail_bytes)
            f.readline()  # skip partial first line
        raw_lines = f.readlines()

    for raw_line in raw_lines:
        try:
            line = raw_line.decode('utf-8', errors='replace')
            obj = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if obj.get("type") not in ("user", "assistant"):
            continue
        if role and obj.get("type") != role:
            continue
        if since and obj.get("timestamp", "") < since:
            continue
        msg = obj.get("message", {})
        content = msg.get("content", "")
        text = ""
        if isinstance(content, list):
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    text += c.get("text", "")
                elif isinstance(c, dict) and c.get("type") == "tool_use" and c.get("name") == "AskUserQuestion":
                    for q in c.get("input", {}).get("questions", []):
                        question = q.get("question", "")
                        opts = q.get("options", [])
                        opt_lines = [f"{i}. {o.get('label', '')}" for i, o in enumerate(opts, 1)]
                        text += f"[AskUserQuestion] {question}\n" + "\n".join(opt_lines) + "\n"
        else:
            text = str(content)
        messages.append({
            "role": obj.get("type"),
            "text": text[:2000],
            "timestamp": obj.get("timestamp"),
        })
    return messages[-limit:]


async def read_conversation(
    conversation_id: str,
    limit: int = 20,
    role: str | None = None,
    since: str | None = None,
) -> list[dict] | None:
    """Async wrapper: finds JSONL file safely, reads conversation messages."""
    if not _validate_conversation_id(conversation_id):
        raise ValueError(f"Invalid conversation_id format: {conversation_id!r}")
    jsonl_path = _find_jsonl_path(conversation_id)
    if jsonl_path is None:
        return None
    loop = asyncio.get_running_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(None, _read_conversation_sync, jsonl_path, limit, role, since),
            timeout=30.0,
        )
    except TimeoutError:
        return []
