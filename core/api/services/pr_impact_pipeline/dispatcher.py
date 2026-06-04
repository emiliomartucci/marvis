# v1.1.0 - 2026-05-16 - KG PR-Impact sub-01 D4: startup replay + periodic sweep
"""BackgroundTasks job runner for the PR-impact populator.

This module is the API-side counterpart to `scripts/populate_pr_impact.py`.
It owns the lifecycle of one `pr_impact_jobs` row:

    queued -> running -> done | failed | dead

D2 shipped the minimal viable surface: `enqueue_job` + a single-shot
`dispatch_job` coroutine. D4 adds:

- `restart_replay()` — re-enqueues any rows stuck in `running` after the
  API process restarted, so a crash mid-populator doesn't strand the queue
- `periodic_pr_impact_sweep()` — a background coroutine that retries
  `failed` rows with a coarse exponential backoff and promotes stuck
  `running` rows past their `claim_lease_until`
- `requeue_stale_jobs()` — the SQL primitive the sweep + replay both call

The dispatcher is gated by `Settings.pr_impact_enabled`:

    "off"    -> noop, return immediately
    "shadow" -> run populator but DO NOT broadcast pr_changed events
    "on"     -> run populator AND broadcast WebSocket events (sub-02 wires this)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from core.api.paths import repo_path

logger = logging.getLogger(__name__)


SECRET_RE = re.compile(
    r"((?:api[_-]?key|secret|token|password|bearer|authorization)"
    r"[\"\s:=]+)([\w./+=-]{8,})",
    re.IGNORECASE,
)


def redact_secrets(line: str) -> str:
    """Mask common secret-ish tokens in subprocess output before logging."""
    return SECRET_RE.sub(r"\1***REDACTED***", line)[:500]


def _minimal_env_allowlist() -> dict[str, str]:
    """Pass only the env vars the populator subprocess actually needs."""
    allowed_keys = {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "PYTHONPATH",
        "PYTHONUNBUFFERED",
        "TZ",
        "XDG_RUNTIME_DIR",
    }
    return {k: v for k, v in os.environ.items() if k in allowed_keys}


async def resolve_pr_row_id(
    db: aiosqlite.Connection,
    pr_id_or_task_id: str,
) -> str | None:
    """Resolve a task_id OR canonical pull_requests.id to the FK target.

    The FK on `pr_impact_jobs.pr_id` references `pull_requests.id`. Callers
    upstream (webhook, admin backfill) typically know the task_id (since
    that's the canonical Marvis identifier). This helper accepts either form
    and returns the row id, or None when no PR row exists yet.
    """
    async with db.execute(
        "SELECT id FROM pull_requests WHERE id=? OR task_id=? "
        "ORDER BY created_at DESC LIMIT 1",
        (pr_id_or_task_id, pr_id_or_task_id),
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else None


async def enqueue_job(
    db: aiosqlite.Connection,
    *,
    pr_id: str,
    delivery_id: str | None,
    payload: dict[str, Any] | None = None,
    project_id: str | None = None,
) -> str:
    """Insert one `pr_impact_jobs` row in `queued` status and return its uuid.

    `pr_id` may be either the canonical `pull_requests.id` or the
    `pull_requests.task_id` — we resolve to the FK target before inserting
    so the FK constraint never fires at the call site.

    Callers MUST also schedule `dispatch_job(job_id)` via BackgroundTasks so
    the row actually progresses. Decoupled here so the webhook handler can
    commit the row atomically with its idempotency check before yielding.

    Raises ValueError when neither the id nor the task_id resolves to a
    pull_requests row — callers should surface that as a 404/400 to the
    user rather than letting it fall through as a server error.
    """
    pr_row_id = await resolve_pr_row_id(db, pr_id)
    if pr_row_id is None:
        raise ValueError(f"pull_requests row not found for pr_id={pr_id!r}")

    job_id = str(uuid.uuid4())
    payload_json = json.dumps(payload or {}, sort_keys=True)
    await db.execute(
        """
        INSERT INTO pr_impact_jobs (
            job_id, delivery_id, pr_id, status, payload_json, project_id
        ) VALUES (?, ?, ?, 'queued', ?, ?)
        """,
        (job_id, delivery_id, pr_row_id, payload_json, project_id),
    )
    await db.commit()
    logger.info(
        "pr_impact_jobs queued job=%s pr_row_id=%s (input=%s) delivery_id=%s",
        job_id, pr_row_id, pr_id, delivery_id,
    )
    return job_id


async def dispatch_job(
    job_id: str,
    *,
    db_path: str,
    script_path: str | None = None,
    timeout_seconds: int = 300,
) -> None:
    """Run the populator subprocess for one job.

    Transitions the `pr_impact_jobs` row through the state machine and logs
    the populator's stdout/stderr with secret redaction. Designed to be
    invoked via `BackgroundTasks.add_task(dispatch_job, job_id, db_path=...)`.

    The populator script is resolved dynamically so prod (under /data/pir)
    and dev (under the repo) both work.
    """
    if script_path is None:
        script_path = _default_script_path()

    # Mark running before spawning so concurrent dispatchers don't double-pick.
    claimed = await _claim_running(db_path, job_id)
    if not claimed:
        logger.info("dispatch_job: %s already claimed or not queued", job_id)
        return

    payload = await _load_job_payload(db_path, job_id)
    pr_id = payload.get("pr_id")
    if not pr_id:
        await _mark_failed(db_path, job_id, "missing pr_id in job payload")
        return

    cmd = [
        sys.executable,
        script_path,
        "--pr-id",
        pr_id,
        "--db",
        db_path,
        "--job-id",
        job_id,
    ]
    if payload.get("incremental", True):
        cmd.append("--incremental")

    logger.info("dispatch_job spawning populator job=%s cmd=%s", job_id, cmd)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_minimal_env_allowlist(),
        )
    except OSError as exc:
        await _mark_failed(db_path, job_id, f"spawn failed: {exc}")
        return

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_seconds
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        await _mark_failed(db_path, job_id, f"populator timed out after {timeout_seconds}s")
        return

    for line in stdout_bytes.decode("utf-8", errors="replace").splitlines():
        logger.info("populator[%s] stdout: %s", job_id, redact_secrets(line))
    for line in stderr_bytes.decode("utf-8", errors="replace").splitlines():
        logger.warning("populator[%s] stderr: %s", job_id, redact_secrets(line))

    if proc.returncode != 0:
        await _mark_failed(
            db_path,
            job_id,
            f"populator exited rc={proc.returncode}",
        )
        return

    await _mark_done(db_path, job_id)


def _default_script_path() -> str:
    """Locate populate_pr_impact.py relative to this module.

    Prod layout: `/data/pir/scripts/populate_pr_impact.py`
    Dev layout : `<repo>/scripts/populate_pr_impact.py`
    """
    prod = Path("/data/pir/scripts/populate_pr_impact.py")
    if prod.exists():
        return str(prod)
    repo_relative = repo_path(__file__, "scripts", "populate_pr_impact.py")
    return str(repo_relative)


async def _claim_running(db_path: str, job_id: str) -> bool:
    async with aiosqlite.connect(db_path, timeout=10.0) as db:
        cursor = await db.execute(
            """
            UPDATE pr_impact_jobs
               SET status='running',
                   started_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                   attempts=attempts+1
             WHERE job_id=? AND status='queued'
            """,
            (job_id,),
        )
        await db.commit()
        return cursor.rowcount > 0


async def _load_job_payload(db_path: str, job_id: str) -> dict[str, Any]:
    async with aiosqlite.connect(db_path, timeout=10.0) as db:
        async with db.execute(
            "SELECT pr_id, payload_json FROM pr_impact_jobs WHERE job_id=?",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return {}
            pr_id, payload_json = row
            try:
                payload = json.loads(payload_json or "{}")
            except json.JSONDecodeError:
                payload = {}
            payload.setdefault("pr_id", pr_id)
            return payload


async def _mark_done(db_path: str, job_id: str) -> None:
    async with aiosqlite.connect(db_path, timeout=10.0) as db:
        await db.execute(
            """
            UPDATE pr_impact_jobs
               SET status='done',
                   finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
             WHERE job_id=?
            """,
            (job_id,),
        )
        await db.commit()


async def _mark_failed(db_path: str, job_id: str, error: str) -> None:
    async with aiosqlite.connect(db_path, timeout=10.0) as db:
        await db.execute(
            """
            UPDATE pr_impact_jobs
               SET status=CASE
                   WHEN attempts >= max_attempts THEN 'dead'
                   ELSE 'failed'
               END,
                   finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                   last_error=?
             WHERE job_id=?
            """,
            (error[:1000], job_id),
        )
        await db.commit()
    logger.warning("pr_impact_jobs job=%s failed: %s", job_id, error)


# --------------------------------------------------------------------------
# D4: restart replay + periodic sweep
# --------------------------------------------------------------------------


# Coarse exponential backoff for the periodic sweep. Index = previous
# attempts count; out-of-bounds clamps to the last value. We deliberately
# stay coarse — the sweep tick interval is already 5 min, no need for
# millisecond precision.
_SWEEP_BACKOFF_SECONDS = (60, 180, 600, 1800, 3600)
_DEFAULT_CLAIM_LEASE_SECONDS = 600
_SWEEP_TICK_SECONDS = 300


async def requeue_stale_jobs(db_path: str) -> int:
    """Promote stranded `running` rows + retry-eligible `failed` rows back to queued.

    Returns the number of rows updated. Used by both `restart_replay` (one-shot
    on process startup) and the periodic sweep coroutine.
    """
    total = 0
    async with aiosqlite.connect(db_path, timeout=10.0) as db:
        # Stranded `running` jobs: started_at > 10 min ago AND either no lease
        # or lease has expired. Promote BUT bump attempts so they don't loop forever.
        cursor = await db.execute(
            """
            UPDATE pr_impact_jobs
               SET status='queued',
                   started_at=NULL
             WHERE status='running'
               AND started_at IS NOT NULL
               AND started_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-10 minutes')
               AND (claim_lease_until IS NULL
                    OR claim_lease_until < strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            """
        )
        total += cursor.rowcount or 0

        # Retry-eligible `failed` jobs that haven't exhausted attempts. We
        # apply a simple backoff against finished_at + table-driven step.
        # `dead` jobs are NOT touched — those need a manual DLQ replay.
        for idx, backoff in enumerate(_SWEEP_BACKOFF_SECONDS):
            cursor = await db.execute(
                """
                UPDATE pr_impact_jobs
                   SET status='queued', last_error=NULL
                 WHERE status='failed'
                   AND attempts = ?
                   AND finished_at IS NOT NULL
                   AND finished_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?)
                """,
                (idx + 1, f"-{backoff} seconds"),
            )
            total += cursor.rowcount or 0

        await db.commit()
    if total:
        logger.info("requeue_stale_jobs promoted %d row(s)", total)
    return total


async def restart_replay(db_path: str) -> int:
    """Run once on API startup to clean up jobs stranded by the previous process.

    Identifies `running` rows that the previous process crashed in the middle
    of and promotes them back to `queued` so the next sweep tick (or an
    explicit admin call) picks them up.
    """
    return await requeue_stale_jobs(db_path)


async def periodic_pr_impact_sweep(
    db_path: str,
    *,
    tick_seconds: int = _SWEEP_TICK_SECONDS,
) -> None:
    """Background coroutine: requeue stranded/failed rows + dispatch queued.

    Sleep-FIRST per learning 4d4278e4 — never grab a write inside the same
    tick where the loop body executes, because the writer lock can deadlock
    against the API startup pool init.
    """
    logger.info(
        "periodic_pr_impact_sweep started (tick=%ds, backoff=%s)",
        tick_seconds,
        _SWEEP_BACKOFF_SECONDS,
    )
    while True:
        try:
            await asyncio.sleep(tick_seconds)
            promoted = await requeue_stale_jobs(db_path)
            if promoted:
                async with aiosqlite.connect(db_path, timeout=10.0) as db:
                    async with db.execute(
                        """
                        SELECT job_id FROM pr_impact_jobs
                         WHERE status='queued'
                         ORDER BY enqueued_at
                         LIMIT 10
                        """
                    ) as cur:
                        rows = await cur.fetchall()
                for (job_id,) in rows:
                    asyncio.create_task(dispatch_job(job_id, db_path=db_path))
        except asyncio.CancelledError:
            logger.info("periodic_pr_impact_sweep cancelled — exiting")
            return
        except Exception as exc:  # noqa: BLE001 — must not crash the loop
            logger.exception("periodic_pr_impact_sweep tick failed: %s", exc)


__all__ = [
    "enqueue_job",
    "dispatch_job",
    "redact_secrets",
    "requeue_stale_jobs",
    "restart_replay",
    "periodic_pr_impact_sweep",
]
