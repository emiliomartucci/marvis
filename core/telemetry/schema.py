# v1.0.0 - 2026-05-27 - S2 F5: telemetry envelope + STRICT per-event props whitelist
"""``marvis.telemetry.v1`` envelope + the per-event ``props`` whitelist.

This module is the **anonymity-by-construction** layer. The no-PII guarantee is
not a policy comment — it is enforced here and proven by a CI test
(``tests/test_telemetry.py``) that rejects any ``props`` key outside its event's
whitelist (e.g. ``path``, ``slug``, ``query``, ``filename``).

Design rules (plan S2 F5 §"Schema evento"):

- The envelope carries ONLY anonymous, aggregate-safe fields:
  ``{schema, event, ts, install_id, marvis_version, os, python, props}``.
- ``props`` is a STRICT whitelist **per event**. A key outside the event's set
  → :class:`SchemaError`. There is no "allow extra" escape hatch — adding a new
  field is a deliberate code change here, reviewed against the no-PII bar.
- Values are never inspected for "looks like PII"; the defense is structural —
  the only keys that survive are counts / enums / fixed-cardinality env fields.
  We NEVER ship a path, project slug, file content, query text, filename, or any
  raw environment value.

Import side-effect free: this module touches no network and no disk. It is pure
data + validation, importable from anywhere with zero I/O.
"""
from __future__ import annotations

from typing import Any

SCHEMA = "marvis.telemetry.v1"

# ---------------------------------------------------------------------------
# Per-event props whitelist (the anonymity contract).
#
# The dict key is the event name; the value is the EXACT set of allowed `props`
# keys for that event. Empty set = the event carries no props (env-only).
# A `props` dict with ANY key outside this set is rejected.
# ---------------------------------------------------------------------------

EVENT_PROPS: dict[str, frozenset[str]] = {
    # Usage frequency/depth. ONLY the command name (an enum-like, low-cardinality
    # token such as "status"/"brief") — NEVER the args, slug, or path.
    "cli_command": frozenset({"command"}),
    # A session began (a marvis process started). No props — the envelope's
    # install_id + ts already carry everything we aggregate.
    "session_start": frozenset(),
    # User feedback signal. Rating + category ONLY (slice 1). NO free text — the
    # textual feedback loop is slice 2, routed via GitHub, not telemetry.
    "feedback_submitted": frozenset({"rating", "category"}),
    # KG scale in the field: aggregate COUNTS only. No names, no paths.
    "kg_metrics": frozenset({"n_files", "n_nodes", "n_edges"}),
    # Install funnel: which decision-tree branch was taken (fresh/repo/multi).
    "install_completed": frozenset({"branch"}),
    # How many governance hooks got wired in (a count, not their names).
    "hooks_installed": frozenset({"count"}),
    # Whether the PiR MCP server got registered (a bool, not the path/entry).
    "mcp_registered": frozenset({"registered"}),
}

# Envelope-level keys that every event MUST carry (the anonymous header).
ENVELOPE_KEYS: frozenset[str] = frozenset(
    {"schema", "event", "ts", "install_id", "marvis_version", "os", "python", "props"}
)


class SchemaError(ValueError):
    """Raised when an event name is unknown or ``props`` violates the whitelist."""


def validate_props(event: str, props: dict[str, Any]) -> dict[str, Any]:
    """Validate ``props`` for ``event`` against the strict whitelist.

    Returns the props unchanged on success (so callers can chain it). Raises
    :class:`SchemaError` if:

    - ``event`` is not a declared event, or
    - ``props`` is not a dict, or
    - ``props`` carries ANY key outside the event's allowed set.

    This is the single enforcement point the no-PII CI test pins.
    """
    allowed = EVENT_PROPS.get(event)
    if allowed is None:
        raise SchemaError(
            f"unknown telemetry event {event!r} "
            f"(declared: {sorted(EVENT_PROPS)})"
        )
    if not isinstance(props, dict):
        raise SchemaError(f"props must be a dict, got {type(props).__name__}")
    extra = set(props) - allowed
    if extra:
        raise SchemaError(
            f"event {event!r}: props keys {sorted(extra)} are NOT in the "
            f"whitelist {sorted(allowed)} — no-PII guarantee is by construction"
        )
    return props


def build_envelope(
    *,
    event: str,
    props: dict[str, Any],
    install_id: str,
    marvis_version: str,
    os_name: str,
    python_version: str,
    ts: str,
) -> dict[str, Any]:
    """Build the ``marvis.telemetry.v1`` envelope after validating ``props``.

    Validation runs FIRST: a whitelist violation raises before any envelope is
    produced, so an out-of-whitelist key can never reach the queue or the wire.
    """
    validated = validate_props(event, props)
    return {
        "schema": SCHEMA,
        "event": event,
        "ts": ts,
        "install_id": install_id,
        "marvis_version": marvis_version,
        "os": os_name,
        "python": python_version,
        "props": validated,
    }
