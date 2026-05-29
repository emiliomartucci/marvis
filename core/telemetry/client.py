# v1.0.0 - 2026-05-27 - S2 F5: anonymous, opt-out telemetry client (gate-before-IO)
"""``emit(event, props)`` — the ONE telemetry entrypoint, opt-out by construction.

The single hard rule (CRITICAL — mem0 incident, plan S2 F5 §"Gate opt-out PRIMA
di ogni init di rete"): the opt-out gate runs **before any network/queue/disk
init**. mem0 shipped a bug where PostHog did blocking I/O *at import time*, before
the opt-out check → opt-out was ineffective AND every command got slower. Defense
here:

- **Import is side-effect free.** ``import core.telemetry.client`` opens no socket,
  reads no file, instantiates no network client. The module top imports only
  ``os`` (and a couple of stdlib helpers); everything touching disk/network is
  lazy, inside functions, AFTER the gate.
- **``emit()`` returns immediately when disabled**, before touching the queue or
  the wire. The gate (:func:`_enabled`) is the single enforcement point — every
  caller (``_runtime_ctx``, ``marvis hooks install``, ``marvis mcp register``,
  the install protocol) funnels through ``emit()``.
- **Fail-silent.** A down/slow endpoint NEVER blocks, slows, or errors a command:
  the flush is fire-and-forget on a short timeout (2s), no aggressive retry, and
  every exception in the telemetry path is swallowed. The command's exit code is
  unaffected.

Precedence (any opt-out wins):
``DO_NOT_TRACK`` set (universal standard) OR ``MARVIS_TELEMETRY`` ∈ {0,off,false}
→ disabled. ``MARVIS_TELEMETRY=log`` → print the event JSON to **stderr**, send
nothing (show-don't-send, the strongest trust signal). Else ``settings.yaml
telemetry: false`` → disabled. Else default ON.
"""
from __future__ import annotations

import os
from typing import Any

# Default transport endpoint. The infra does NOT exist yet — that is fine:
# fail-silent means the client no-ops on a connection failure, so it works before
# the endpoint is live. Override via settings.yaml `telemetry.endpoint`.
DEFAULT_ENDPOINT = "https://t.justaskmarvis.com/v1/e"

# Hard cap on the local queue so a perpetually-down endpoint never grows a file
# without bound. Oldest lines are dropped first.
_QUEUE_CAP = 500

# How many seconds we ever wait on the network. Short by design: a slow endpoint
# must not be felt by the user.
_FLUSH_TIMEOUT = 2.0


# ---------------------------------------------------------------------------
# THE GATE. Nothing above this line touches disk or network. `_enabled()` is the
# single opt-out decision; `emit()` returns on a False gate before any init.
# ---------------------------------------------------------------------------


def _enabled() -> bool:
    """Opt-out gate — runs before any network/queue/disk init (mem0 lesson).

    Precedence (any opt-out wins):

    1. ``DO_NOT_TRACK`` set to any non-empty value → disabled (universal OSS
       standard, honored alongside our own var as a courtesy to the community).
    2. ``MARVIS_TELEMETRY`` ∈ {``0``, ``off``, ``false``} → disabled.
    3. ``MARVIS_TELEMETRY=log`` → enabled, but downstream prints (never sends).
    4. else → ``settings.yaml telemetry:`` (default ON if unset).
    """
    if os.environ.get("DO_NOT_TRACK"):  # universal standard, any non-empty value
        return False
    v = os.environ.get("MARVIS_TELEMETRY", "").lower()
    if v in ("0", "off", "false"):
        return False
    if v == "log":
        return True  # handled downstream: print to stderr, send nothing
    return _settings_telemetry_on()  # default on


def _log_mode() -> bool:
    """True when ``MARVIS_TELEMETRY=log`` → show-don't-send (print to stderr)."""
    return os.environ.get("MARVIS_TELEMETRY", "").lower() == "log"


def emit(event: str, props: dict[str, Any] | None = None) -> None:
    """Record one telemetry event. No-op when telemetry is disabled.

    The gate runs FIRST: on a False gate (or in ``log`` mode after printing) this
    returns before touching the queue or the network. Then it validates ``props``
    against the strict whitelist, appends to the capped local queue, and triggers
    a detached best-effort flush. EVERY exception in this path is swallowed —
    telemetry can never break, block, or slow a command.
    """
    if not _enabled():
        return  # exits BEFORE touching the queue / the wire

    props = props or {}
    try:
        # Lazy imports: schema is pure, but we keep the import inside the gate so
        # a disabled run does no extra work at all.
        from core.telemetry import schema as _schema

        envelope = _schema.build_envelope(
            event=event,
            props=props,
            install_id=_install_id(),
            marvis_version=_marvis_version(),
            os_name=_os_name(),
            python_version=_python_version(),
            ts=_now_iso(),
        )
    except Exception:  # noqa: BLE001 — a bad/whitelist-violating event must not crash
        # A whitelist violation IS surfaced loudly in tests (validate_props is
        # called directly there). In a live command we fail-silent: telemetry
        # never raises into the user's command path.
        return

    if _log_mode():
        # Show, don't send: print exactly what we WOULD transmit, to stderr.
        _print_log(envelope)
        return

    try:
        _enqueue(envelope)
        _flush_detached()
    except Exception:  # noqa: BLE001 — fail-silent on any queue/flush error
        return


# ---------------------------------------------------------------------------
# Everything below is lazy — only reached AFTER the gate said "enabled".
# ---------------------------------------------------------------------------


def _marvis_dir():  # -> Path
    """``~/.marvis`` (or ``$MARVIS_VAULT_DIR``), created lazily on first write."""
    from pathlib import Path

    vault_dir = os.environ.get("MARVIS_VAULT_DIR")
    base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
    return base


def _settings_path():  # -> Path
    from pathlib import Path

    settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
    if settings_path:
        return Path(settings_path).expanduser()
    return _marvis_dir() / "settings.yaml"


def _read_settings() -> dict[str, Any]:
    """Best-effort read of ``settings.yaml`` (missing/malformed → ``{}``)."""
    path = _settings_path()
    if not path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _settings_telemetry_on() -> bool:
    """Default-ON: telemetry stays on unless ``settings.yaml`` opts out.

    Accepts both shapes the codebase uses for the flag:
    - ``telemetry: false`` (a bare bool), or
    - ``telemetry: {enabled: false}`` (a nested dict).
    Anything else (or no key) → ON.
    """
    data = _read_settings()
    tele = data.get("telemetry")
    if isinstance(tele, dict):
        return bool(tele.get("enabled", True))
    if isinstance(tele, bool):
        return tele
    return True  # default on (key absent / unexpected type)


def _endpoint() -> str:
    """Resolve the transport endpoint (settings-overridable, else default)."""
    data = _read_settings()
    tele = data.get("telemetry")
    if isinstance(tele, dict):
        ep = tele.get("endpoint")
        if isinstance(ep, str) and ep.strip():
            return ep.strip()
    return DEFAULT_ENDPOINT


def _install_id() -> str:
    """Random uuid4 persisted in ``~/.marvis/telemetry_id`` (chmod 600).

    NOT derived from MAC/hostname/username/email — it is pure ``uuid.uuid4()``, so
    it cannot be traced back to a person; it only deduplicates "same install" for
    aggregate counts. Regenerated if the file is deleted.
    """
    import uuid

    path = _marvis_dir() / "telemetry_id"
    try:
        if path.is_file():
            existing = path.read_text(encoding="utf-8").strip()
            if existing:
                return existing
    except Exception:  # noqa: BLE001
        pass

    new_id = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new_id + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:  # noqa: BLE001 — read-only home must not break telemetry
        pass
    return new_id


def _marvis_version() -> str:
    """The CLI version (kept in lockstep with ``marvis version``)."""
    return "0.1.0"


def _os_name() -> str:
    """Coarse OS bucket: ``linux`` / ``macos`` / ``windows`` / ``other``."""
    import sys

    plat = sys.platform
    if plat.startswith("linux"):
        return "linux"
    if plat == "darwin":
        return "macos"
    if plat.startswith("win"):
        return "windows"
    return "other"


def _python_version() -> str:
    """``major.minor`` only (e.g. ``3.12``) — never the full build string."""
    import sys

    return f"{sys.version_info.major}.{sys.version_info.minor}"


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _queue_path():  # -> Path
    return _marvis_dir() / "telemetry_queue.jsonl"


def _enqueue(envelope: dict[str, Any]) -> None:
    """Append one envelope to the capped local queue (``telemetry_queue.jsonl``).

    The queue is a light buffer so a down endpoint never loses the most recent
    events nor grows unbounded: once over :data:`_QUEUE_CAP` lines, the oldest are
    dropped. All errors are swallowed by the caller (``emit``).
    """
    import json

    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(envelope, ensure_ascii=False)

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")

    # Trim to the cap (cheap: telemetry volume is low; we only read on overflow).
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        if len(lines) > _QUEUE_CAP:
            with open(path, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-_QUEUE_CAP:])
    except Exception:  # noqa: BLE001 — trimming is best-effort
        pass


def _print_log(envelope: dict[str, Any]) -> None:
    """``MARVIS_TELEMETRY=log`` → dump the exact event JSON to stderr, send nothing."""
    import json
    import sys

    sys.stderr.write(json.dumps(envelope, ensure_ascii=False) + "\n")


def _flush_detached() -> None:
    """Trigger the best-effort flush OFF the command's critical path.

    The flush runs in a detached daemon thread so the command never waits on it:
    even with a fast endpoint the network round-trip is not in the command's
    latency. The thread itself is fail-silent (:func:`_flush_once`).
    """
    import threading

    t = threading.Thread(target=_flush_once, name="marvis-telemetry-flush", daemon=True)
    t.start()


def _flush_once() -> None:
    """Drain the queue to the endpoint, fire-and-forget. Swallows everything.

    No aggressive retry, no long backoff: on ANY failure (endpoint down, DNS,
    timeout) we simply leave the events in the queue for a later command and
    return. A down endpoint is therefore invisible to the user.
    """
    import json

    path = _queue_path()
    try:
        if not path.is_file():
            return
        with open(path, "r", encoding="utf-8") as fh:
            raw_lines = [ln.strip() for ln in fh if ln.strip()]
    except Exception:  # noqa: BLE001
        return
    if not raw_lines:
        return

    events: list[dict[str, Any]] = []
    for ln in raw_lines:
        try:
            events.append(json.loads(ln))
        except Exception:  # noqa: BLE001 — skip a corrupt line, never crash
            continue
    if not events:
        # All lines corrupt — clear the buffer so it can't wedge forever.
        try:
            path.unlink()
        except Exception:  # noqa: BLE001
            pass
        return

    sent = _post_batch(events)
    if sent:
        # Delivered → clear the queue. (We re-read nothing: low volume, simple.)
        try:
            path.unlink()
        except Exception:  # noqa: BLE001
            pass


def _post_batch(events: list[dict[str, Any]]) -> bool:
    """POST the batch to the endpoint with a short timeout. True iff 2xx.

    Uses stdlib ``urllib`` (no new dependency, no client instantiated at import
    time). Any exception → ``False`` (fail-silent); the caller leaves the queue
    in place for next time.
    """
    import json
    import urllib.error
    import urllib.request

    payload = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        _endpoint(),
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "marvis-telemetry/1"},
    )
    try:
        with urllib.request.urlopen(req, timeout=_FLUSH_TIMEOUT) as resp:
            return 200 <= getattr(resp, "status", resp.getcode()) < 300
    except Exception:  # noqa: BLE001 — down/slow endpoint → fail-silent
        return False


# ---------------------------------------------------------------------------
# First-run notice — ONE-TIME stderr line, not a blocking prompt.
# ---------------------------------------------------------------------------


def maybe_first_run_notice() -> None:
    """Print the one-time anonymous-telemetry notice to stderr (then never again).

    Respects the gate: when telemetry is disabled (opt-out), we still set the flag
    so the notice never appears later either — it would be noise for someone who
    already opted out. NOT a blocking prompt (telemetry is opt-out, not opt-in).
    The flag lives in ``~/.marvis/telemetry_notice_shown``.
    """
    flag = _marvis_dir() / "telemetry_notice_shown"
    try:
        if flag.exists():
            return
    except Exception:  # noqa: BLE001
        return

    if _enabled() and not _log_mode():
        import sys

        sys.stderr.write(
            "Anonymous telemetry on (no content, no PII). "
            "Turn off with `marvis telemetry off`.\n"
        )

    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text("1\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
