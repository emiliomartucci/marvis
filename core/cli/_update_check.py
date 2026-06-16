# v1.0.0 - 2026-06-03 - 0.3.6: opt-out CLI "update available" notice (notify-only)
"""Throttled, fail-silent "a new version is available" notice (NOTIFY ONLY).

NEVER self-updates — no ``pip``/``uv`` side effect. It prints one line to stderr
telling the user how to upgrade. npm-style decoupling so the notice is reliable:

- print INSTANTLY from a cached latest-version file (so it actually shows, even
  on a fast command that would outrun a background network call), then
- refresh that cache in a detached daemon thread at most once per 24h.

First run has no cache → no notice, but kicks the refresh → the next run shows it.

Opt out via ``MARVIS_NO_UPDATE_CHECK=1`` or ``settings.yaml`` ``update_check: false``.
"""
from __future__ import annotations

import os
import sys

from core.telemetry import client as _tc  # canonical ~/.marvis + settings reader

_PACKAGE = "marvisx-cli"
_PYPI_JSON = "https://pypi.org/pypi/marvisx-cli/json"
_CHECK_INTERVAL_S = 24 * 3600
_TIMEOUT = 2.5


def _last_check_path():
    return _tc._marvis_dir() / "update_last_check"


def _cached_latest_path():
    return _tc._marvis_dir() / "update_latest"


def _opted_out() -> bool:
    if os.environ.get("MARVIS_NO_UPDATE_CHECK"):
        return True
    try:
        return _tc._read_settings().get("update_check") is False
    except Exception:  # noqa: BLE001
        return False


def _installed_version() -> str | None:
    try:
        from importlib.metadata import version

        return version(_PACKAGE)
    except Exception:  # noqa: BLE001
        return None


def _read_cached_latest() -> str | None:
    try:
        path = _cached_latest_path()
        if path.is_file():
            return path.read_text(encoding="utf-8").strip() or None
    except Exception:  # noqa: BLE001
        return None
    return None


def _write_cached_latest(version: str) -> None:
    try:
        path = _cached_latest_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(version.strip() + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _should_check(now: float) -> bool:
    try:
        path = _last_check_path()
        if not path.is_file():
            return True
        last = float(path.read_text(encoding="utf-8").strip() or 0)
        return (now - last) >= _CHECK_INTERVAL_S
    except Exception:  # noqa: BLE001
        return False


def _mark_checked(now: float) -> None:
    try:
        path = _last_check_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{now:.0f}\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def _parse(v: str) -> tuple[int, ...]:
    """Numeric release tuple; a pre-release/garbage chunk truncates to its int
    prefix (0 if none) — conservative, never crashes."""
    out: list[int] = []
    for chunk in v.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _is_newer(latest: str, installed: str) -> bool:
    try:
        return _parse(latest) > _parse(installed)
    except Exception:  # noqa: BLE001
        return False


def _fetch_latest() -> str | None:
    import json
    import urllib.request

    try:
        req = urllib.request.Request(
            _PYPI_JSON, headers={"User-Agent": "marvis-update-check/1"}
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:  # noqa: S310 — https pinned
            data = json.loads(resp.read().decode("utf-8", "replace"))
        v = (data.get("info") or {}).get("version")
        return v if isinstance(v, str) and v else None
    except Exception:  # noqa: BLE001 — down/slow PyPI → silent
        return None


def _refresh_cache() -> None:
    latest = _fetch_latest()
    if latest:
        _write_cached_latest(latest)


def maybe_notify_update() -> None:
    """Print a cached "update available" notice (instant), then refresh the cache
    in the background ≤1×/day. Notify-only, fail-silent, never blocks."""
    try:
        if _opted_out():
            return

        installed = _installed_version()
        if installed:
            cached = _read_cached_latest()
            if cached and _is_newer(cached, installed):
                sys.stderr.write(
                    f"\n[marvis] update available: {installed} → {cached}. "
                    f"Upgrade with: uv tool upgrade {_PACKAGE}\n"
                )

        from time import time

        now = time()
        if _should_check(now):
            _mark_checked(now)  # claim the 24h window before the network call
            import threading

            threading.Thread(
                target=_refresh_cache, name="marvis-update-check", daemon=True
            ).start()
    except Exception:  # noqa: BLE001 — must never affect the command
        return
