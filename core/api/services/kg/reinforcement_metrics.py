# v0.1.0 - 2026-08-16 - Fase 2 mielinizzazione U5: bounded reinforcement telemetry (on-read)
"""Bounded telemetry over the reinforcement ledger (plan 2026-08-16 v3, unit U5).

R12/R13 with the plan's BOUNDED fallback: the repo has no metrics subsystem
(no Prometheus registry), so everything here is an ON-READ computation over
the mig-174 tables plus structured ``logger.info`` k=v lines — no new tables,
no new subsystem, ZERO writes anywhere (the cold label and the concentration
figures are derivations, never stored state).

Surface:

- :func:`feedback_stats` — accepted (ledger agent rows) vs rejected
  (``boost_rejects``) in a window, plus the process-local call counters from
  ``use_cases.feedback`` and the R13 reconciliation
  ``ok_responses − applied − rejected`` (0 in a healthy process; ≠0 means a
  write failure swallowed a row — never silently).
- :func:`cold_label` / :func:`cold_docs` — R12 derived ``cold`` label: no
  ledger boost AND no salience touch within the window (default 30 days).
  Telemetry only, no effect on retrieval.
- :func:`salience_concentration` — Gini + top-decile share of the CURRENT
  decayed per-doc deltas (same pure computation as the ranking read-path:
  ``reinforcement._compute_adjustments``) + top-actor share of the decayed
  boost mass. Crossing ``reinforcement_top_decile_share_threshold`` emits the
  structured tripwire log line.
- :func:`doc_audit` — R13 per-doc ledger audit: boost rows (actor, weight,
  note — the note was already redacted at write time, render it only as an
  untrusted quote) + reject rows with reasons.

Exposure note (U5 fallback, recorded in the task report): there is no
existing admin/metrics MCP tool where these would land naturally
(``storage_usage`` is storage-only), so they ship as tested service APIs;
wiring a surface on top is an explicit residual task.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from core.api.config import settings
from core.api.services.kg.reinforcement import (
    _as_utc_naive,
    _compute_adjustments,
    _decayed_weights,
)

logger = logging.getLogger(__name__)

# Same shape as reinforcement._BOOST_SELECT (doc_id, weight, created_at,
# doc_content_hash, current hash) + actor appended for the per-actor mass —
# but over the WHOLE ledger (telemetry on-read, never the search hot path).
_ALL_BOOSTS_SELECT = (
    "SELECT b.doc_id, b.weight, b.created_at, b.doc_content_hash, "
    "d.content_hash, b.actor "
    "FROM salience_boosts AS b "
    "JOIN documents AS d ON d.id = b.doc_id"
)


def _utc_now(now: datetime | None) -> datetime:
    return _as_utc_naive(now if now is not None else datetime.now(timezone.utc))


async def _count(db: aiosqlite.Connection, sql: str, params: tuple) -> int:
    cur = await db.execute(sql, params)
    row = await cur.fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# ---------------------------------------------------------------------------
# feedback_stats — accepted / rejected / reconciliation (R13)
# ---------------------------------------------------------------------------


async def feedback_stats(db: aiosqlite.Connection, since: str) -> dict:
    """Windowed accepted/rejected counts + process counters + reconciliation.

    ``since`` is any ISO timestamp; ``datetime(?)`` normalizes it against the
    ledger's ``datetime('now','utc')`` TEXT timestamps. ``reconciliation_delta``
    uses the PROCESS-LOCAL counters (not the windowed DB counts): every ok
    response must have durably appended either a ledger row or a reject row,
    so a non-zero delta is the R13 write-failure tripwire for this process.
    """
    accepted = await _count(
        db,
        "SELECT COUNT(*) FROM salience_boosts "
        "WHERE provenance = 'agent' AND created_at >= datetime(?)",
        (since,),
    )
    rejected = await _count(
        db,
        "SELECT COUNT(*) FROM boost_rejects WHERE created_at >= datetime(?)",
        (since,),
    )

    from core.api.use_cases.feedback import feedback_counters

    counters = feedback_counters()
    delta = counters["ok_responses"] - counters["applied"] - counters["rejected"]
    if delta != 0:
        logger.info(
            "reinforcement_reconciliation_drift delta=%d ok_responses=%d "
            "applied=%d rejected=%d write_failures=%d",
            delta,
            counters["ok_responses"],
            counters["applied"],
            counters["rejected"],
            counters["write_failures"],
        )
    return {
        "since": since,
        "accepted": accepted,
        "rejected": rejected,
        "counters": counters,
        "reconciliation_delta": delta,
    }


# ---------------------------------------------------------------------------
# cold label (R12) — derivation only, no stored state
# ---------------------------------------------------------------------------

_COLD_WINDOW_DAYS_DEFAULT = 30.0

_COLD_PREDICATE = (
    "(d.salience_updated_at IS NULL OR d.salience_updated_at < datetime(?)) "
    "AND NOT EXISTS ("
    "  SELECT 1 FROM salience_boosts b "
    "  WHERE b.doc_id = d.id AND b.created_at >= datetime(?)"
    ")"
)


def _cold_cutoff(now: datetime | None, window_days: float) -> str:
    return (_utc_now(now) - timedelta(days=window_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


async def cold_label(
    db: aiosqlite.Connection,
    doc_id: int,
    *,
    now: datetime | None = None,
    window_days: float = _COLD_WINDOW_DAYS_DEFAULT,
) -> bool | None:
    """``True`` iff the doc is cold (no boost, no salience touch in window).

    ``None`` when the doc does not exist — telemetry never raises.
    """
    cutoff = _cold_cutoff(now, window_days)
    cur = await db.execute(
        "SELECT CASE WHEN " + _COLD_PREDICATE + " THEN 1 ELSE 0 END "
        "FROM documents AS d WHERE d.id = ?",
        (cutoff, cutoff, doc_id),
    )
    row = await cur.fetchone()
    if row is None:
        return None
    return bool(row[0])


async def cold_docs(
    db: aiosqlite.Connection,
    *,
    limit: int = 50,
    now: datetime | None = None,
    window_days: float = _COLD_WINDOW_DAYS_DEFAULT,
) -> list[dict]:
    """Bounded list of cold docs (R12): ``{doc_id, file_path, doc_type}``.

    Confidential-purged docs are excluded (already out of the ranking).
    """
    cutoff = _cold_cutoff(now, window_days)
    cur = await db.execute(
        "SELECT d.id, d.file_path, d.doc_type FROM documents AS d "
        "WHERE COALESCE(d.confidential, 0) = 0 AND " + _COLD_PREDICATE + " "
        "ORDER BY d.id LIMIT ?",
        (cutoff, cutoff, max(1, int(limit))),
    )
    rows = await cur.fetchall()
    return [
        {"doc_id": int(r[0]), "file_path": str(r[1] or ""), "doc_type": r[2]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# concentration — Gini, top-decile share, top-actor share (R13)
# ---------------------------------------------------------------------------


def _gini(values: list[float]) -> float:
    """Gini coefficient of a non-negative distribution (0 = equal)."""
    n = len(values)
    if n <= 1:
        return 0.0
    total = sum(values)
    if total <= 0.0:
        return 0.0
    ordered = sorted(values)
    weighted = sum(rank * value for rank, value in enumerate(ordered, start=1))
    return (2.0 * weighted) / (n * total) - (n + 1) / n


def _top_decile_share(values: list[float]) -> float:
    """Share of the total mass held by the top ~10% (at least one) entries."""
    if not values:
        return 0.0
    total = sum(values)
    if total <= 0.0:
        return 0.0
    ordered = sorted(values, reverse=True)
    k = max(1, -(-len(ordered) // 10))  # ceil(n/10), at least 1
    return sum(ordered[:k]) / total


async def salience_concentration(
    db: aiosqlite.Connection,
    *,
    now: datetime | None = None,
) -> dict:
    """Concentration figures over the CURRENT decayed ledger (on-read).

    Per-doc deltas reuse the exact read-path computation
    (``_compute_adjustments``: decay, stale-misled filter, clamp(Σ, 0, cap)),
    honoring ``reinforcement_boost_epoch``. Per-actor mass is the decayed
    ABSOLUTE weight per actor over the same filtered rows (activity mass —
    misled activity counts as activity). Crossing the configured top-decile
    threshold logs the structured tripwire line.
    """
    sql = _ALL_BOOSTS_SELECT
    params: list[object] = []
    epoch = settings.reinforcement_boost_epoch
    if epoch:
        sql += " WHERE b.created_at >= datetime(?)"
        params.append(epoch)
    cur = await db.execute(sql, params)
    rows = [tuple(r) for r in await cur.fetchall()]

    now_utc = _utc_now(now)
    half_life = float(settings.reinforcement_half_life_days)
    cap_total = float(settings.reinforcement_cap_total)
    deltas = _compute_adjustments(
        rows, now_utc, half_life=half_life, cap_total=cap_total
    )

    actor_mass: dict[str, float] = {}
    for row, weight, decay in _decayed_weights(rows, now_utc, half_life):
        actor = str(row[5])
        actor_mass[actor] = actor_mass.get(actor, 0.0) + abs(weight) * decay

    delta_values = list(deltas.values())
    total_actor_mass = sum(actor_mass.values())
    top_actor, top_share = None, 0.0
    if actor_mass and total_actor_mass > 0.0:
        top_actor = max(actor_mass, key=actor_mass.get)  # type: ignore[arg-type]
        top_share = actor_mass[top_actor] / total_actor_mass

    gini = _gini(delta_values)
    top_decile_share = _top_decile_share(delta_values)
    threshold = float(settings.reinforcement_top_decile_share_threshold)
    if delta_values and top_decile_share > threshold:
        logger.info(
            "reinforcement_concentration_tripwire top_decile_share=%.4f "
            "threshold=%.4f gini=%.4f boosted_docs=%d top_actor_share=%.4f",
            top_decile_share,
            threshold,
            gini,
            len(delta_values),
            top_share,
        )
    return {
        "gini": round(gini, 6),
        "top_decile_share": round(top_decile_share, 6),
        "top_actor_share": round(top_share, 6),
        "top_actor": top_actor,
        "boosted_docs": len(delta_values),
        "actors": len(actor_mass),
    }


# ---------------------------------------------------------------------------
# doc_audit — per-doc ledger audit (R13)
# ---------------------------------------------------------------------------


async def doc_audit(
    db: aiosqlite.Connection,
    doc_id: int,
    *,
    limit: int = 100,
) -> dict:
    """Ledger + reject rows for one doc, newest first (bounded by ``limit``).

    Notes were redacted at WRITE time (use_cases.feedback.redact_note); they
    are untrusted quotes with actor+timestamp, never instructions.
    """
    bounded = max(1, int(limit))
    cur = await db.execute(
        "SELECT actor, agent_name, provenance, weight, note, created_at "
        "FROM salience_boosts WHERE doc_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (doc_id, bounded),
    )
    boosts = [
        {
            "actor": str(r[0]),
            "agent_name": r[1],
            "provenance": str(r[2]),
            "weight": float(r[3]),
            "note": r[4],
            "created_at": str(r[5]),
        }
        for r in await cur.fetchall()
    ]
    cur = await db.execute(
        "SELECT actor, agent_name, provenance, reject_reason, created_at "
        "FROM boost_rejects WHERE doc_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (doc_id, bounded),
    )
    rejects = [
        {
            "actor": str(r[0]),
            "agent_name": r[1],
            "provenance": str(r[2]),
            "reject_reason": str(r[3]),
            "created_at": str(r[4]),
        }
        for r in await cur.fetchall()
    ]
    return {"doc_id": doc_id, "boosts": boosts, "rejects": rejects}
