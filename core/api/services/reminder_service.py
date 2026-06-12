# v1.0.0 - 2026-03-10 - Task reminder service with Telegram notifications
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timezone

from core.api.config import settings

logger = logging.getLogger(__name__)

def _send_telegram(text: str) -> None:
    """Send a Telegram message via urllib. Errors only logged, never raised."""
    token = settings.telegram_bot_token
    chat_id = settings.telegram_owner_chat_id
    if not token or not chat_id:
        logger.info("reminder_service: Telegram not configured, skipping notification")
        return

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = json.dumps({
            "chat_id": chat_id,
            "text": text,
        }).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            resp.read()
    except Exception as exc:
        logger.warning("reminder_service: Telegram notify failed: %s", exc)


async def check_and_send_reminders() -> int:
    """Query due tasks and send Telegram notifications for unsent reminders.

    Returns the number of reminders sent.
    """
    from core.api.db import write_db
    async with write_db() as db:

        cursor = await db.execute(
            """SELECT id, title, due_date, project
               FROM tasks
               WHERE due_date IS NOT NULL
                 AND due_date <= date('now', '+1 day')
                 AND reminder_sent_at IS NULL
                 AND status NOT IN ('completed', 'rejected', 'failed')
                 AND deleted_at IS NULL
            """
        )
        tasks = await cursor.fetchall()

        if not tasks:
            return 0

        sent = 0
        now = datetime.now(timezone.utc).isoformat()

        for task in tasks:
            try:
                text = (
                    f"\u23f0 Task in scadenza: {task['title']}\n"
                    f"\U0001f4c5 Scadenza: {task['due_date']}\n"
                    f"\U0001f4c1 Progetto: {task['project']}"
                )
                _send_telegram(text)

                await db.execute(
                    "UPDATE tasks SET reminder_sent_at = ? WHERE id = ?",
                    (now, task["id"]),
                )
                sent += 1
                logger.info(
                    "Reminder sent for task %s (due %s)", task["id"], task["due_date"]
                )
            except Exception:
                logger.exception("Failed to send reminder for task %s", task["id"])

        if sent > 0:
            await db.commit()

        return sent
