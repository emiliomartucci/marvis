from __future__ import annotations

import os
import subprocess
import sys

import aiosqlite


async def has_running_brain_cycle(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
) -> bool:
    cur = await db.execute(
        "SELECT 1 FROM brain_runs "
        "WHERE workspace_id = ? AND status = 'running' LIMIT 1",
        (workspace_id,),
    )
    return await cur.fetchone() is not None


def spawn_manual_brain_run() -> None:
    """Launch the same CLI path as `marvis brain run`, detached from the API."""
    subprocess.Popen(  # noqa: S603 — fixed argv, no shell
        [
            sys.executable,
            "-m",
            "core.cli.marvis_init",
            "brain",
            "run",
            "--mode",
            "full",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "MARVIS_API_TRIGGERED_BRAIN": "1"},
    )
