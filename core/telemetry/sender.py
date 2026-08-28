# v1.0.0 - 2026-05-29 - open-core funnel Phase 0: daily rollup sender (provision + send)
"""Opportunistic, fail-silent sender of the daily aggregate rollup to the cloud.

Reuses the event client's contract verbatim (``core/telemetry/client.py``): the SAME
consent gate, the SAME random ``install_id``, the SAME fire-and-forget / fail-silent
discipline. This module never blocks, slows, or errors a ``marvis`` command.

Flow on a ``marvis`` invocation (wired in ``marvis_init._telemetry_root_hook``):
  gate (``_enabled``) -> throttle (last send > 24h) -> resolve console.db -> rollup
  -> provision (mint + store an api key once) -> ``POST /v1/ingest`` Bearer key.
Everything past the cheap gate/throttle runs on a detached daemon thread (≤2s timeout),
so the command never waits on it. ``MARVIS_TELEMETRY=log`` prints the would-send payload
to stderr and sends nothing (show-don't-send parity with the event client).

Endpoints (overridable via ``settings.yaml telemetry.{ingest,provision}_endpoint``):
  ``POST https://cloud.justaskmarvis.com/v1/installs``  (provision -> api_key, returned once)
  ``POST https://cloud.justaskmarvis.com/v1/ingest``    (Bearer key, idempotent daily upsert)
"""
from __future__ import annotations

import os
from typing import Any

from core.telemetry import client as _tc

DEFAULT_INGEST_ENDPOINT = "https://cloud.justaskmarvis.com/v1/ingest"
DEFAULT_PROVISION_ENDPOINT = "https://cloud.justaskmarvis.com/v1/installs"

_SEND_INTERVAL_S = 24 * 3600
_TIMEOUT = 2.0
_DAYS_BACK = 7


def _endpoint(setting_key: str, default: str) -> str:
    """Settings-overridable endpoint (``telemetry.<setting_key>``), else the default."""
    data = _tc._read_settings()
    tele = data.get("telemetry")
    if isinstance(tele, dict):
        ep = tele.get(setting_key)
        if isinstance(ep, str) and ep.strip():
            return ep.strip()
    return default


def _key_path():
    return _tc._marvis_dir() / "telemetry_key"


def _last_sent_path():
    return _tc._marvis_dir() / "telemetry_last_sent"


def _load_key() -> str | None:
    try:
        path = _key_path()
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    except Exception:  # noqa: BLE001
        return None
    return None


def _store_secret(path, value: str) -> None:
    """Write a secret file chmod 600, best-effort (read-only home must not break)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.rstrip("\n") + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:  # noqa: BLE001
        pass


def _should_send(now: float) -> bool:
    """True iff we have never sent, or the last send was > 24h ago."""
    try:
        path = _last_sent_path()
        if not path.is_file():
            return True
        last = float(path.read_text(encoding="utf-8").strip() or 0)
        return (now - last) >= _SEND_INTERVAL_S
    except Exception:  # noqa: BLE001 — on any doubt, do not spam
        return False


def _db_path() -> str | None:
    """Resolve the local ``console.db`` from ``settings.yaml`` ``storage.db_path``."""
    data = _tc._read_settings()
    storage = data.get("storage")
    if isinstance(storage, dict):
        db = storage.get("db_path")
        if isinstance(db, str) and db.strip():
            from pathlib import Path

            return str(Path(db).expanduser())
    return None


def _http_post_json(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> tuple[int, dict | None]:
    """POST JSON with a short timeout. Returns (status, parsed_body_or_None). Never raises."""
    import json
    import urllib.error
    import urllib.request

    body = json.dumps(payload).encode("utf-8")
    final_headers = {"Content-Type": "application/json", "User-Agent": "marvis-telemetry/1"}
    if headers:
        final_headers.update(headers)
    req = urllib.request.Request(url, data=body, method="POST", headers=final_headers)
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            status = getattr(resp, "status", resp.getcode())
            try:
                parsed = json.loads(resp.read().decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                parsed = None
            return status, parsed if isinstance(parsed, dict) else None
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:  # noqa: BLE001 — down/slow endpoint → fail-silent
        return 0, None


def _ensure_key(install_id: str) -> str | None:
    """Return the stored api key, provisioning one exactly once if absent."""
    key = _load_key()
    if key:
        return key
    status, parsed = _http_post_json(
        _endpoint("provision_endpoint", DEFAULT_PROVISION_ENDPOINT),
        {"install_id": install_id, "version": _tc._marvis_version(), "os": _tc._os_name()},
    )
    if status == 201 and parsed and isinstance(parsed.get("api_key"), str):
        _store_secret(_key_path(), parsed["api_key"])
        claim = parsed.get("claim_code")
        if isinstance(claim, str):  # lets the user link this install to a web account later
            _store_secret(_tc._marvis_dir() / "telemetry_claim_code", claim)
        return parsed["api_key"]
    return None


def _build_payload() -> dict[str, Any] | None:
    """Compute the rollup payload, or None if there is no local DB / nothing to send."""
    db = _db_path()
    if not db:
        return None
    from core.telemetry import rollup as _rollup

    try:
        days = _rollup.compute_rollup(db, days_back=_DAYS_BACK)
    except Exception:  # noqa: BLE001
        return None
    if not days:
        return None
    return {
        "install_id": _tc._install_id(),
        "version": _tc._marvis_version(),
        "os": _tc._os_name(),
        "days": days,
    }


def _send_once() -> bool:
    """Provision-if-needed then POST the rollup. True on a 2xx ingest. Fail-silent."""
    payload = _build_payload()
    if not payload:
        return False
    key = _ensure_key(payload["install_id"])
    if not key:
        return False
    status, _ = _http_post_json(
        _endpoint("ingest_endpoint", DEFAULT_INGEST_ENDPOINT),
        payload,
        headers={"Authorization": f"Bearer {key}"},
    )
    return 200 <= status < 300


def _run(now: float) -> None:
    try:
        if _send_once():
            _store_secret(_last_sent_path(), f"{now:.0f}")
    except Exception:  # noqa: BLE001
        pass


def _print_log() -> None:
    """``MARVIS_TELEMETRY=log`` → print the would-send rollup payload to stderr, send nothing."""
    import json
    import sys

    payload = _build_payload()
    if payload is not None:
        sys.stderr.write("[rollup] " + json.dumps(payload, ensure_ascii=False) + "\n")


def maybe_send_rollup() -> None:
    """Opportunistic, throttled, fail-silent rollup send. Safe to call on every command.

    Returns immediately (before any DB/network I/O) when telemetry is opted out or the
    24h throttle has not elapsed. In ``log`` mode it prints the payload (no send). Real
    sends run on a detached daemon thread so the command never waits on them.
    """
    try:
        if not _tc._enabled():
            return
        if _tc._log_mode():
            import threading

            threading.Thread(target=_print_log, name="marvis-rollup-log", daemon=True).start()
            return
        from time import time

        now = time()
        if not _should_send(now):
            return
        import threading

        threading.Thread(target=_run, args=(now,), name="marvis-rollup-sender", daemon=True).start()
    except Exception:  # noqa: BLE001 — telemetry must never affect the command
        return
