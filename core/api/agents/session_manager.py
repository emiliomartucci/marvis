# v1.0.0 - 2026-03-02 - DevX Session Manager Agent
"""Session Manager Agent — DevX Layer Sprint 3.

Runs every 10 minutes via host cron or manual trigger.
Monitors sessions with agent_managed=True, runs health checks, acts on results.

Usage:
    cd /data/pir && python -m api.agents.session_manager
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from core.api.config import settings
from core.api.agents.session_health import check_session_health
from core.api.models import SessionInfo
from core.api.services import tmux

logger = logging.getLogger(__name__)

ERRORS_DIR = Path("/data/pir/logs/errors")

# ── helpers ──────────────────────────────────────────────────────────────────

async def list_agent_managed_sessions() -> list[SessionInfo]:
    """Return all sessions with agent_managed=True via the sessions API.

    Uses the internal API endpoint to get live activity_state (computed from tmux,
    not stored in DB). Falls back to empty list on error.
    """
    import urllib.request
    from core.api.config import settings as cfg
    token = cfg.tasks_api_token if hasattr(cfg, "tasks_api_token") else os.environ.get("TASKS_API_TOKEN", "")
    api_base = os.environ.get("PIR_INTERNAL_BASE", "http://localhost:8100")
    try:
        req = urllib.request.Request(
            f"{api_base}/api/v1/sessions?agent_managed=true",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Agent-Name": "marvisx",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return [SessionInfo(**s) for s in data]
    except Exception as exc:
        logger.error("Failed to list agent_managed sessions: %s", exc)
        return []


async def get_last_agent_action(db: aiosqlite.Connection, session_name: str) -> datetime | None:
    """Return the last time an automated action was taken on this session."""
    try:
        cursor = await db.execute(
            "SELECT MAX(created_at) FROM agent_actions WHERE session_name = ?",
            (session_name,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            # Parse ISO string from SQLite — may be naive (no tz offset)
            ts_str: str = row[0]
            ts_str = ts_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            # SQLite stores UTC without offset — assume UTC if naive
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
    except Exception as exc:
        logger.warning("Failed to get last agent action for %s: %s", session_name, exc)
    return None


async def log_agent_action(
    db: aiosqlite.Connection,
    session_name: str,
    action: str,
    detail: str | None,
) -> None:
    """Log an automated action taken on a session."""
    try:
        await db.execute(
            """
            INSERT INTO agent_actions (agent_name, session_name, action, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("session-manager", session_name, action, detail, datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()
    except Exception as exc:
        logger.error("Failed to log agent action: %s", exc)


async def send_session_message(session_name: str, text: str) -> bool:
    """Send a message to a tmux session."""
    try:
        return await tmux.send_keys(session_name, text)
    except Exception as exc:
        logger.error("Failed to send message to session %s: %s", session_name, exc)
        return False


async def send_telegram_alert(text: str) -> None:
    """Send a Telegram alert (best-effort)."""
    import urllib.request
    if not settings.telegram_bot_token or not settings.telegram_owner_chat_id:
        logger.info("Telegram not configured, skipping alert")
        return
    try:
        url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
        data = json.dumps({
            "chat_id": settings.telegram_owner_chat_id,
            "text": text,
            "parse_mode": "HTML",
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:
        logger.warning("Telegram alert failed: %s", exc)


async def log_error_to_knowledge(session_name: str, error_text: str) -> None:
    """Log an error to the knowledge errors store for downstream analysis."""
    ERRORS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    error_file = ERRORS_DIR / f"{ts}-session-manager-{session_name}.txt"
    try:
        error_file.write_text(
            f"session: {session_name}\nerror: {error_text}\nts: {ts}\n"
        )
    except Exception as exc:
        logger.error("Failed to write error file: %s", exc)


# ── core loop ────────────────────────────────────────────────────────────────

async def _process_session(
    db: aiosqlite.Connection,
    session: SessionInfo,
) -> None:
    """Process a single session — isolated to not block others."""
    last_action_at = await get_last_agent_action(db, session.name)
    result = await check_session_health(session, last_action_at)

    if result.action == "send_message" and result.message:
        success = await send_session_message(session.name, result.message)
        if success:
            await log_agent_action(db, session.name, "health_check:C", result.message)
            logger.info("Sent health check message to session %s", session.name)
        else:
            logger.warning("Failed to send message to session %s", session.name)

    elif result.action == "escalate" and result.escalation_reason:
        await send_telegram_alert(
            f"<b>DevX Escalation</b> — <code>{session.name}</code>\n\n{result.escalation_reason}"
        )
        await log_agent_action(db, session.name, "escalate", result.escalation_reason)
        logger.warning("Escalated session %s: %s", session.name, result.escalation_reason)


async def run_session_manager() -> None:
    """Main loop: monitor agent_managed sessions, run health checks."""
    logger.info("Session Manager starting...")
    from core.api.db import write_db
    async with write_db() as db:
        sessions = await list_agent_managed_sessions()
        if not sessions:
            logger.info("No agent_managed sessions to monitor")
            return

        logger.info("Checking %d agent_managed session(s)...", len(sessions))

        results = await asyncio.gather(
            *[_process_session(db, s) for s in sessions],
            return_exceptions=True,
        )

        for session, result in zip(sessions, results):
            if isinstance(result, Exception):
                logger.error(
                    "Health check failed for session %s: %s", session.name, result
                )
                await log_error_to_knowledge(session.name, str(result))

    logger.info("Session Manager complete")


# ── CLI entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    asyncio.run(run_session_manager())
