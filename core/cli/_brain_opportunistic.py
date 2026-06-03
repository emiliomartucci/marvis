# v1.0.0 - 2026-06-03 - SP2 U2: opportunistic ≤1×/day background brain upkeep
"""Opportunistic, throttled, fail-silent Company-Brain upkeep (SP2 U2).

Mirrors ``core/telemetry/sender.py``'s discipline — cheap gate → 24h throttle →
detached background work → fail-silent — with ONE structural difference: a brain
reflection cycle is HEAVY and long, so it runs as a DETACHED ``marvis brain run``
subprocess (``start_new_session=True``) that survives this short-lived command.
A daemon thread (the telemetry pattern, fine for a ≤2s HTTP POST) would be killed
the instant the parent CLI process exits, mid-cycle.

Gating — all cheap, NO DB read on the hot path:
- ``brain.opportunistic`` in ``settings.yaml`` must be true. The ``marvis init``
  reflection ask (U4) sets it per the user's explicit consent (decision #3); until
  then it is absent → this hook is dormant (no surprise heavy cycle on a fresh
  install).
- ≤1×/day: a file marker ``~/.marvis/brain_last_opportunistic_run``, claimed
  BEFORE the spawn so a slow or failing cycle never triggers a re-spawn storm.
- No self-recursion: the spawned child sets ``MARVIS_OPPORTUNISTIC=1`` and the
  caller skips when the invoked command is ``brain`` (see ``marvis_init._root``).

The spawned ``run_brain_cycle_once`` is itself lease-guarded (a second guard
against an overlapping run) and re-checks ``brain_enabled`` in the DB, so a
dormant/disabled brain costs at most one immediately-exiting subprocess per day.
"""
from __future__ import annotations

import os
import shutil
import sys
from typing import Any

from core.telemetry import client as _tc  # canonical ~/.marvis + settings.yaml reader

_RUN_INTERVAL_S = 24 * 3600


def _last_run_path():
    return _tc._marvis_dir() / "brain_last_opportunistic_run"


def _brain_settings() -> dict[str, Any]:
    data = _tc._read_settings()
    brain = data.get("brain")
    return brain if isinstance(brain, dict) else {}


def _opportunistic_enabled() -> bool:
    return _brain_settings().get("opportunistic") is True


def _configured_mode() -> str:
    """The reflection mode the opportunistic run uses; default the free floor."""
    mode = _brain_settings().get("reflection_mode")
    return mode if mode in ("free", "full") else "free"


def _should_run(now: float) -> bool:
    """True iff never run, or the last opportunistic run was > 24h ago."""
    try:
        path = _last_run_path()
        if not path.is_file():
            return True
        last = float(path.read_text(encoding="utf-8").strip() or 0)
        return (now - last) >= _RUN_INTERVAL_S
    except Exception:  # noqa: BLE001 — on any doubt, do not spam
        return False


def _mark_ran(now: float) -> None:
    try:
        path = _last_run_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{now:.0f}\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        pass


def _marvis_executable() -> list[str] | None:
    """How to invoke ``marvis`` as a subprocess, or None when it cannot be found."""
    exe = shutil.which("marvis")
    if exe:
        return [exe]
    # Source/dev fallback: the console-script path the shell resolved for us.
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0.endswith(("marvis", "marvis-init")) and os.path.isfile(argv0):
        return [argv0]
    return None


def _spawn_brain_run(mode: str) -> bool:
    """Launch a DETACHED ``marvis brain run --mode <mode>``. True if it started."""
    base = _marvis_executable()
    if base is None:
        return False
    import subprocess

    try:
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell, validated mode
            [*base, "brain", "run", "--mode", mode],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach: outlives this command's exit
            env={**os.environ, "MARVIS_OPPORTUNISTIC": "1"},
        )
        return True
    except Exception:  # noqa: BLE001
        return False


def maybe_run_opportunistic_brain() -> None:
    """Cheap-gated, ≤1×/day, detached, fail-silent brain upkeep.

    Safe to call on every ``marvis`` invocation: returns immediately (before any
    spawn) when opportunistic reflection is not enabled, when this process is
    itself an opportunistic child, or when the 24h throttle has not elapsed.
    """
    try:
        if os.environ.get("MARVIS_OPPORTUNISTIC") == "1":
            return  # we ARE the opportunistic child — never re-trigger
        if not _opportunistic_enabled():
            return
        from time import time

        now = time()
        if not _should_run(now):
            return
        # Claim the 24h window BEFORE spawning so a slow/failed cycle never
        # triggers a re-spawn storm; the lease in run_brain_cycle_once is the
        # second guard against an overlapping run.
        _mark_ran(now)
        _spawn_brain_run(_configured_mode())
    except Exception:  # noqa: BLE001 — opportunistic upkeep must never affect the command
        return
