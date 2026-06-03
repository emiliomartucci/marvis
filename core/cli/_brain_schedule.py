# v1.0.0 - 2026-06-03 - SP2 U3: `marvis brain schedule` cross-OS timer primitive
"""Deterministic cross-OS scheduler for the Company-Brain reflection cycle (U3).

Installs / removes / inspects an OS-native timer that wakes
``marvis brain run --mode full`` once a day. This is the agent-INVOKED primitive
(decision #4) — the agent never hand-writes launchd/systemd/cron artifacts; it
calls ``marvis brain schedule`` which owns them idempotently.

Backends (auto-detected):
- **macOS → launchd LaunchAgent** (`~/Library/LaunchAgents/com.marvisx.brain.plist`).
  ``StartCalendarInterval`` runs the job on WAKE if the Mac was asleep at the
  scheduled time (laptop-friendly catch-up). Installed via modern
  ``launchctl bootstrap gui/<uid>`` / removed via ``launchctl bootout`` — never
  the legacy ``load``/``unload``.
- **Linux → systemd ``--user`` timer** (`~/.config/systemd/user/marvis-brain.{service,timer}`).
  ``loginctl enable-linger`` is MANDATORY (a --user timer otherwise stops at
  logout); ``Persistent=true`` catches up a run missed while asleep/off.
- **Fallback → cron** (a single ``# marvis-brain``-marked line). cron skips runs
  while the machine sleeps (no catch-up) → lossy on laptops; the U2 opportunistic
  ≤1×/day run is the safety net for both timer and cron misses.

``status()`` reports the REAL OS-level state (linger on? unit enabled+active?
agent bootstrapped? cron line present?), never just "we wrote a file" — the same
fail-loud discipline as F1's search readiness.

Every subprocess call funnels through :func:`_run` (one mockable seam) so tests
never touch the host's real launchd/systemd/cron.
"""
from __future__ import annotations

import getpass
import os
import shutil
import sys
from pathlib import Path
from typing import Any

# launchd Label / systemd unit base / cron marker — one identity across backends.
_LABEL = "com.marvisx.brain"
_UNIT = "marvis-brain"
_CRON_MARKER = "# marvis-brain"

# Default daily fire time (local), off-peak.
_HOUR = 3
_MINUTE = 0


# ---------------------------------------------------------------------------
# Subprocess seam (the ONE place tests mock)
# ---------------------------------------------------------------------------


def _run(cmd: list[str], *, input_text: str | None = None) -> tuple[int, str, str]:
    """Run a command, return (returncode, stdout, stderr). Never raises."""
    import subprocess

    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv lists, no shell
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


# ---------------------------------------------------------------------------
# Backend detection + marvis path
# ---------------------------------------------------------------------------


def detect_backend() -> str:
    """Return ``launchd`` | ``systemd`` | ``cron`` | ``unsupported``."""
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("win"):
        return "unsupported"
    if shutil.which("systemctl"):
        return "systemd"
    if shutil.which("crontab"):
        return "cron"
    return "unsupported"


def _marvis_path() -> str | None:
    """Absolute path to the installed ``marvis`` console script, or None."""
    exe = shutil.which("marvis")
    if exe:
        return exe
    argv0 = sys.argv[0] if sys.argv else ""
    if argv0.endswith(("marvis", "marvis-init")) and os.path.isfile(argv0):
        return os.path.abspath(argv0)
    return None


# ---------------------------------------------------------------------------
# Artifact builders (PURE — unit-tested directly)
# ---------------------------------------------------------------------------


def plist_text(marvis_path: str, *, hour: int = _HOUR, minute: int = _MINUTE) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        f"  <key>Label</key>\n  <string>{_LABEL}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>{marvis_path}</string>\n"
        "    <string>brain</string>\n"
        "    <string>run</string>\n"
        "    <string>--mode</string>\n"
        "    <string>full</string>\n"
        "  </array>\n"
        "  <key>StartCalendarInterval</key>\n"
        "  <dict>\n"
        f"    <key>Hour</key>\n    <integer>{hour}</integer>\n"
        f"    <key>Minute</key>\n    <integer>{minute}</integer>\n"
        "  </dict>\n"
        # Do NOT run at load — only on the calendar schedule (+ wake catch-up).
        "  <key>RunAtLoad</key>\n  <false/>\n"
        "  <key>ProcessType</key>\n  <string>Background</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def systemd_service_text(marvis_path: str) -> str:
    return (
        "[Unit]\n"
        "Description=MarvisX Company-Brain reflection cycle (one-shot)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"ExecStart={marvis_path} brain run --mode full\n"
    )


def systemd_timer_text(*, hour: int = _HOUR, minute: int = _MINUTE) -> str:
    return (
        "[Unit]\n"
        "Description=Daily MarvisX Company-Brain reflection\n"
        "\n"
        "[Timer]\n"
        f"OnCalendar=*-*-* {hour:02d}:{minute:02d}:00\n"
        # Persistent=true → a run missed while asleep/off catches up on resume.
        "Persistent=true\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def cron_line(marvis_path: str, *, hour: int = _HOUR, minute: int = _MINUTE) -> str:
    return f"{minute} {hour} * * * {marvis_path} brain run --mode full {_CRON_MARKER}"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def _systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user"


# ---------------------------------------------------------------------------
# launchd backend
# ---------------------------------------------------------------------------


def _launchd_enable() -> dict[str, Any]:
    marvis = _marvis_path()
    if not marvis:
        return {"backend": "launchd", "ok": False, "error": "marvis not found on PATH"}
    plist = _launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(plist_text(marvis), encoding="utf-8")
    domain = f"gui/{os.getuid()}"
    # Idempotent: bootout an existing instance first (ignore failure), then bootstrap.
    _run(["launchctl", "bootout", f"{domain}/{_LABEL}"])
    rc, _out, err = _run(["launchctl", "bootstrap", domain, str(plist)])
    return {
        "backend": "launchd",
        "ok": rc == 0,
        "plist": str(plist),
        "error": err.strip() if rc != 0 else None,
    }


def _launchd_disable() -> dict[str, Any]:
    plist = _launchd_plist_path()
    rc, _out, err = _run(["launchctl", "bootout", f"gui/{os.getuid()}/{_LABEL}"])
    removed = False
    if plist.is_file():
        plist.unlink()
        removed = True
    # bootout rc!=0 when it was not loaded — that is still a successful disable.
    return {"backend": "launchd", "ok": True, "plist_removed": removed, "bootout_rc": rc}


def _launchd_status() -> dict[str, Any]:
    plist = _launchd_plist_path()
    rc, _out, _err = _run(["launchctl", "print", f"gui/{os.getuid()}/{_LABEL}"])
    return {
        "backend": "launchd",
        "enabled": rc == 0,
        "plist_present": plist.is_file(),
    }


# ---------------------------------------------------------------------------
# systemd --user backend
# ---------------------------------------------------------------------------


def _systemd_unit_paths() -> tuple[Path, Path]:
    d = _systemd_user_dir()
    return d / f"{_UNIT}.service", d / f"{_UNIT}.timer"


def _linger_on() -> bool:
    rc, out, _err = _run(["loginctl", "show-user", getpass.getuser(), "--property=Linger"])
    if rc == 0 and "Linger=yes" in out:
        return True
    # Fallback to the on-disk marker (works without loginctl).
    return Path(f"/var/lib/systemd/linger/{getpass.getuser()}").exists()


def _systemd_enable() -> dict[str, Any]:
    marvis = _marvis_path()
    if not marvis:
        return {"backend": "systemd", "ok": False, "error": "marvis not found on PATH"}
    svc, timer = _systemd_unit_paths()
    svc.parent.mkdir(parents=True, exist_ok=True)
    svc.write_text(systemd_service_text(marvis), encoding="utf-8")
    timer.write_text(systemd_timer_text(), encoding="utf-8")

    # MANDATORY: without linger a --user timer dies at logout.
    linger_rc, _o, linger_err = _run(["loginctl", "enable-linger", getpass.getuser()])
    _run(["systemctl", "--user", "daemon-reload"])
    rc, _out, err = _run(["systemctl", "--user", "enable", "--now", f"{_UNIT}.timer"])
    return {
        "backend": "systemd",
        "ok": rc == 0,
        "service": str(svc),
        "timer": str(timer),
        "linger_ok": linger_rc == 0,
        "linger_warning": (
            None
            if linger_rc == 0
            else f"could not enable linger ({linger_err.strip()}): the timer will only "
            "run while you are logged in"
        ),
        "error": err.strip() if rc != 0 else None,
    }


def _systemd_disable() -> dict[str, Any]:
    svc, timer = _systemd_unit_paths()
    _run(["systemctl", "--user", "disable", "--now", f"{_UNIT}.timer"])
    removed = []
    for p in (timer, svc):
        if p.is_file():
            p.unlink()
            removed.append(p.name)
    _run(["systemctl", "--user", "daemon-reload"])
    # Linger is intentionally LEFT enabled — it may be shared with other --user
    # services; disabling it here could silently break them.
    return {"backend": "systemd", "ok": True, "removed": removed}


def _systemd_status() -> dict[str, Any]:
    svc, timer = _systemd_unit_paths()
    en_rc, en_out, _e = _run(["systemctl", "--user", "is-enabled", f"{_UNIT}.timer"])
    ac_rc, ac_out, _a = _run(["systemctl", "--user", "is-active", f"{_UNIT}.timer"])
    return {
        "backend": "systemd",
        "enabled": en_rc == 0 and en_out.strip() == "enabled",
        "active": ac_rc == 0 and ac_out.strip() == "active",
        "linger": _linger_on(),
        "units_present": svc.is_file() and timer.is_file(),
    }


# ---------------------------------------------------------------------------
# cron backend
# ---------------------------------------------------------------------------


def _read_crontab() -> list[str]:
    rc, out, _err = _run(["crontab", "-l"])
    if rc != 0:  # no crontab yet (rc=1) → empty
        return []
    return out.splitlines()


def _write_crontab(lines: list[str]) -> tuple[int, str]:
    body = "\n".join(lines).rstrip("\n") + "\n" if lines else "\n"
    rc, _out, err = _run(["crontab", "-"], input_text=body)
    return rc, err


def _cron_enable() -> dict[str, Any]:
    marvis = _marvis_path()
    if not marvis:
        return {"backend": "cron", "ok": False, "error": "marvis not found on PATH"}
    lines = [ln for ln in _read_crontab() if _CRON_MARKER not in ln]  # idempotent replace
    lines.append(cron_line(marvis))
    rc, err = _write_crontab(lines)
    return {"backend": "cron", "ok": rc == 0, "error": err.strip() if rc != 0 else None}


def _cron_disable() -> dict[str, Any]:
    lines = [ln for ln in _read_crontab() if _CRON_MARKER not in ln]
    rc, err = _write_crontab(lines)
    return {"backend": "cron", "ok": rc == 0, "error": err.strip() if rc != 0 else None}


def _cron_status() -> dict[str, Any]:
    present = any(_CRON_MARKER in ln for ln in _read_crontab())
    return {"backend": "cron", "enabled": present}


# ---------------------------------------------------------------------------
# Public dispatch
# ---------------------------------------------------------------------------

_ENABLE = {"launchd": _launchd_enable, "systemd": _systemd_enable, "cron": _cron_enable}
_DISABLE = {"launchd": _launchd_disable, "systemd": _systemd_disable, "cron": _cron_disable}
_STATUS = {"launchd": _launchd_status, "systemd": _systemd_status, "cron": _cron_status}


def enable(cadence: str = "daily") -> dict[str, Any]:
    backend = detect_backend()
    if backend == "unsupported":
        return {"backend": "unsupported", "ok": False, "error": _UNSUPPORTED_MSG}
    if cadence != "daily":
        return {"backend": backend, "ok": False, "error": f"unsupported cadence {cadence!r} (only 'daily')"}
    return _ENABLE[backend]()


def disable() -> dict[str, Any]:
    backend = detect_backend()
    if backend == "unsupported":
        return {"backend": "unsupported", "ok": True, "error": None}
    return _DISABLE[backend]()


def status() -> dict[str, Any]:
    backend = detect_backend()
    if backend == "unsupported":
        return {"backend": "unsupported", "enabled": False, "error": _UNSUPPORTED_MSG}
    return _STATUS[backend]()


_UNSUPPORTED_MSG = (
    "no supported scheduler on this platform (launchd/systemd/cron). The "
    "opportunistic on-invocation upkeep still runs whenever you use marvis."
)
