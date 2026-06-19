# v1.0.0 - 2026-06-03 - Windows port Phase 1: single OS seam (paths + precedence + pwd/getuid)
"""Cross-platform OS seam for the single-user CLI runtime.

This is the ONE module allowed to call OS-specific primitives (``pwd``,
``os.getuid``, hardcoded data roots). Everything in ``core/cli`` and
``core/wizard`` routes through here, so the cross-platform contract is enforced
by ``tests/test_platform_boundary.py`` instead of scattered ``if-Windows``
guards. The dependency is one-way: this module imports only ``platformdirs`` +
stdlib, never ``core.api`` / ``core.cli`` / ``core.wizard``.

Path precedence (single source of truth): explicit ``MARVIS_*`` env var >
(settings.yaml overlay, applied elsewhere by ``runtime_settings``) >
``platformdirs`` default. The env tier lives HERE so the wizard and
``core.api.config`` are thin callers and cannot drift.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# platformdirs is a declared dependency, but the import is guarded so that a
# server venv that has not yet sync'd the new dep still BOOTS (config.py imports
# this module at load). On the server the DB path comes from MARVIS_DB_PATH, so
# the platformdirs fallback below is never actually reached in prod.
try:
    from platformdirs import PlatformDirs

    # appauthor=False → flat %LOCALAPPDATA%\marvisx on Windows (NOT doubled
    # marvisx\marvisx); ignored on macOS/Linux. ensure_exists → user_data_path
    # creates the data dir on access so the SQLite DB has a writable parent.
    _DIRS: object | None = PlatformDirs("marvisx", appauthor=False, ensure_exists=True)
except Exception:  # noqa: BLE001 — platformdirs absent → import must not crash
    _DIRS = None


def data_root() -> Path:
    """Per-user data root (the SQLite DB and projects live under here).

    Windows: ``%LOCALAPPDATA%\\marvisx`` · macOS: ``~/Library/Application Support/marvisx``
    · Linux: ``~/.local/share/marvisx`` (XDG-honored).
    """
    if _DIRS is not None:
        return _DIRS.user_data_path  # type: ignore[attr-defined]
    # Fallback only when platformdirs is unavailable (server pre-dep-sync); never
    # the prod path, and unreached in prod because MARVIS_DB_PATH wins there.
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    p = base / "marvisx"
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_default_path() -> Path:
    """Default SQLite DB path: ``$MARVIS_DB_PATH`` if set, else ``data_root()/console.db``.

    The *fallback* tier. ``core.api.config.Settings.db_path`` wires this as a
    ``default_factory`` (so the pydantic env alias still wins when set), and the
    init wizard calls it directly. The env read lives here, not at the callers.
    """
    env = (
        os.environ.get("MARVIS_DB_PATH")
        or os.environ.get("PIR_DB_PATH")
        or os.environ.get("DB_PATH")
    )
    if env:
        return Path(env).expanduser()
    return data_root() / "console.db"


def projects_root_default() -> Path:
    """Default projects root: ``$MARVIS_PROJECTS_ROOT`` if set, else ``data_root()/projects``.

    No filesystem probing — the old ``Path('/data/projects').parent.exists()``
    candidate returned ``C:\\data\\projects`` on Windows because ``C:\\`` (the
    parent of ``C:\\data``) always exists.
    """
    env = os.environ.get("MARVIS_PROJECTS_ROOT")
    if env:
        return Path(env).expanduser()
    return data_root() / "projects"


def current_user() -> str:
    """Current OS user name.

    POSIX: ``pwd.getpwuid(os.getuid()).pw_name`` EXACTLY — this matches the code
    it replaced in ``config.effective_git_runas_user`` so the prod git-run-as
    decision is unchanged. Windows / lookup failure: ``getpass.getuser()`` (reads
    ``USERNAME``). Returns ``""`` only if nothing resolves; callers treat ``""``
    as "unknown" and never skip a privilege hop on it.
    """
    try:
        import pwd  # POSIX-only

        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, AttributeError, KeyError, OSError):
        try:
            import getpass

            return getpass.getuser()
        except Exception:  # noqa: BLE001 — no user resolvable
            return ""


def current_uid() -> int | None:
    """POSIX uid, or ``None`` on Windows (which has no uid).

    Replaces the bare ``os.getuid()`` calls in ``_brain_schedule`` so the
    scheduler-domain code stays inside this cross-platform boundary.
    """
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def ensure_utf8_io() -> None:
    """Force UTF-8, error-tolerant stdout/stderr on Windows.

    The legacy Windows console (cp1252) raises ``UnicodeEncodeError`` the moment
    the CLI prints a non-ASCII character (arrows, ``·``, Rich box-drawing, emoji)
    — pervasive in ``--help`` text and command output. Reconfiguring to UTF-8 with
    ``errors="replace"`` makes that output never crash. No-op on POSIX (already
    UTF-8) and when a stream cannot be reconfigured (e.g. pytest capture). Call
    once at the console-script entry, before Typer/click emit anything.
    """
    if os.name != "nt":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # already detached / not a real stream
            pass


@dataclass(frozen=True, slots=True)
class SecureResult:
    """Outcome of restricting a path to the current owner.

    ``mechanism`` says HOW (or that nothing was applied); ``warning`` is an honest
    message a caller can surface (e.g. via ``doctor``) when the path is NOT
    actually OS-restricted.
    """

    ok: bool
    mechanism: Literal["chmod", "none"]
    warning: str | None = None


def secure_path(path: Path | str, *, mode: int = 0o600) -> SecureResult:
    """Restrict ``path`` to the current owner.

    POSIX: ``os.chmod(path, mode)`` — the owner-only intent, real and sufficient.
    Windows: a no-op — ``os.chmod`` only toggles the read-only bit and cannot set
    an ACL, so it would silently pretend the file is protected; return an honest
    ``SecureResult`` with a warning instead. A real Windows ACL (``icacls``) is a
    deferred follow-up. This keeps the owner-only intent in ONE place so the
    Windows story is honest rather than pretend.
    """
    if os.name == "posix":
        try:
            os.chmod(path, mode)
            return SecureResult(ok=True, mechanism="chmod")
        except OSError as exc:
            return SecureResult(ok=False, mechanism="chmod", warning=f"could not chmod {path}: {exc}")
    return SecureResult(
        ok=False,
        mechanism="none",
        warning=(
            f"{path} is not OS-restricted on this platform (owner-only perms need an "
            "ACL, not yet applied); protect it manually if the filesystem is shared"
        ),
    )
