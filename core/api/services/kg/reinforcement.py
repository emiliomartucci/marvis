# v0.1.0 - 2026-08-16 - Fase 2 mielinizzazione U2: effective salience from the reinforcement ledger
"""Effective-salience adjustments from the ``salience_boosts`` ledger (READ-ONLY).

Plan 2026-08-16 "Fase 2 mielinizzazione minima" (v3), unit U2. This module is
the ONLY read-path consumer of the migration-174 ledger:

    salience_effettiva = salience_base + clamp(Σ weight·2^(−age_days/half_life), 0, cap)

Contract (R2/R3/R4, KTD5/KTD6):
- computed ONLY on the post-lane candidate set (the doc_ids the fusion already
  emitted) — never a full-table JOIN;
- decay is computed in PYTHON on those few rows — no ``pow()`` inside SQLite
  on the hot path;
- the lower clamp at 0 is the floor guarantee: misled without prior positives
  is a no-op on ranking (floor = salience_base, KTD6);
- stale MISLED rows (weight < 0 recorded against a ``doc_content_hash`` that
  no longer matches ``documents.content_hash``) are excluded at read time
  (R10); a confidential purge NULLs the doc hash, so pre-purge misled ages out
  through the same filter;
- boosts created before ``reinforcement_boost_epoch`` are ignored — a ledger
  reset without DELETE (R4);
- ZERO writes anywhere: the ``documents`` JOIN is read-only, the mig-136 FTS
  triggers can never fire from here (R3).

Two entry points share the same SQL fragment and the same pure computation
(:func:`_compute_adjustments`):

- :func:`effective_salience_adjustments` — canonical async per-doc API on an
  existing ``aiosqlite`` connection (unit tests, future U4/U5 consumers);
- :func:`apply_reinforcement_to_grouped` — the ranking innesto. It runs the
  two tiny SELECTs on a POOLED worker thread with plain ``sqlite3`` via
  ``asyncio.to_thread``, on a CACHED read-only (``mode=ro``) connection: an
  ``aiosqlite`` connection costs a dedicated-thread spawn+join per call and a
  fresh ``sqlite3`` connection re-parses the full tenant schema (~4ms) — each
  alone blew the U2 latency gate (flag-on p50 ≤ +10% of flag-off) on the
  benchmark corpus. The read-only URI also makes this path structurally
  incapable of writing (R3 hardening). Cache: tiny LRU keyed by db_path,
  lock-serialized, dropped and reopened on any sqlite error (fail open).

Ranking innesto (see ``hybrid_search``): the caller is a single structural
``if settings.reinforcement_mode == "on"`` — off/shadow never import nor
execute this module, so their output stays byte-identical to the pre-plan
path. When on, the per-doc delta is added to the emitted ``salience`` field
and document hits are re-ordered by ``rrf_score + delta/(rrf_k+1)``: the
1/(rrf_k+1) factor maps salience units onto the RRF scale of a rank-1
unit-weight lane contribution, so a full-cap boost (0.3 default) weighs about
as much as a #1 semantic-lane hit — a real but bounded nudge that never
touches the RRF weights or the other lanes.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from core.api.config import settings

logger = logging.getLogger(__name__)

# Cached read-only sqlite3 connections for the ranking worker (see module
# docstring). One per db_path (one per tenant in practice); tiny LRU so long
# test sessions with many throwaway DB paths don't leak file descriptors.
# All access happens under _READ_CONN_LOCK — queries are serialized, which is
# fine: two SELECTs over a handful of indexed rows per search call.
_READ_CONN_CACHE_MAX = 8
_READ_CONN_LOCK = threading.Lock()
_read_conns: dict[str, sqlite3.Connection] = {}


def _cached_read_conn(db_path: str) -> sqlite3.Connection:
    """Read-only cached connection (caller holds _READ_CONN_LOCK)."""
    conn = _read_conns.pop(db_path, None)
    if conn is None:
        # mode=ro: never creates the file, structurally cannot write (R3).
        # resolve() first: a RELATIVE db_path makes as_uri() raise ValueError
        # (dev/OSS setups); any residual ValueError is caught by the caller
        # and the search fails open.
        conn = sqlite3.connect(
            f"{Path(db_path).resolve().as_uri()}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        while len(_read_conns) >= _READ_CONN_CACHE_MAX:
            # dict preserves insertion order and hits are re-inserted at the
            # end (pop + reassign below), so the FIRST key is the true LRU —
            # popitem() would evict the most-recently-used instead.
            oldest = _read_conns.pop(next(iter(_read_conns)))
            try:
                oldest.close()
            except sqlite3.Error:  # pragma: no cover — best-effort close
                pass
    _read_conns[db_path] = conn  # re-insert → most-recently-used
    return conn


def _drop_read_conn(db_path: str) -> None:
    """Evict a broken cached connection (caller holds _READ_CONN_LOCK)."""
    conn = _read_conns.pop(db_path, None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:  # pragma: no cover — best-effort close
            pass

# Shared SELECT for ledger rows on the candidate set. The (doc_id, created_at)
# index (mig 174) serves the IN() probe; the documents JOIN is read-only and
# only supplies the CURRENT content_hash for the stale-misled filter.
_BOOST_SELECT = (
    "SELECT b.doc_id, b.weight, b.created_at, b.doc_content_hash, "
    "d.content_hash "
    "FROM salience_boosts AS b "
    "JOIN documents AS d ON d.id = b.doc_id "
    "WHERE b.doc_id IN ({placeholders})"
)
# datetime(?) normalizes any ISO epoch form to sqlite's 'YYYY-MM-DD HH:MM:SS'
# so the TEXT comparison against created_at is well-defined.
_EPOCH_CLAUSE = " AND b.created_at >= datetime(?)"

# Invalid epoch values already warned about (one warning per value per
# process): an unparseable MARVIS_REINFORCEMENT_BOOST_EPOCH would make
# sqlite's datetime(?) return NULL and silently exclude EVERY boost — instead
# we warn once and FAIL OPEN (epoch ignored, all ledger boosts count).
_warned_epochs: set[str] = set()


def _validated_epoch(epoch: str | None) -> str | None:
    """The epoch iff it parses as ISO in Python; invalid → warn once + None."""
    if not epoch:
        return None
    try:
        datetime.fromisoformat(epoch)
    except ValueError:
        if epoch not in _warned_epochs:
            _warned_epochs.add(epoch)
            logger.warning(
                "reinforcement: invalid MARVIS_REINFORCEMENT_BOOST_EPOCH %r — "
                "ignoring the epoch filter (all ledger boosts count)",
                epoch,
            )
        return None
    return epoch


def _as_utc_naive(value: datetime) -> datetime:
    """Normalize aware datetimes to naive UTC (ledger timestamps are naive UTC)."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _parse_ledger_ts(raw: object) -> datetime | None:
    """Parse a ledger ``created_at`` (sqlite ``datetime('now','utc')`` text)."""
    try:
        return _as_utc_naive(datetime.fromisoformat(str(raw)))
    except (TypeError, ValueError):
        return None


def _boost_sql_and_params(
    doc_ids: list[int], epoch: str | None
) -> tuple[str, list[object]]:
    epoch = _validated_epoch(epoch)
    sql = _BOOST_SELECT.format(placeholders=",".join("?" * len(doc_ids)))
    params: list[object] = list(doc_ids)
    if epoch:
        sql += _EPOCH_CLAUSE
        params.append(epoch)
    return sql, params


def _decayed_weights(
    rows: list[tuple], now_utc: datetime, half_life: float
) -> Iterator[tuple[tuple, float, float]]:
    """Yield ``(row, weight, decay)`` for ledger rows passing the shared filters.

    Shared by ranking (:func:`_compute_adjustments`) and telemetry
    (``reinforcement_metrics``) so the stale-misled rule (R10) and the decay
    curve can never drift apart. Skips stale MISLED rows (weight < 0 recorded
    against a ``doc_content_hash`` that no longer matches the doc — positives
    keep counting regardless of version) and rows with an unreadable
    timestamp (never fail search). Rows start with the ``_BOOST_SELECT``
    columns; trailing extras are ignored.
    """
    for row in rows:
        weight = float(row[1])
        if weight < 0 and row[3] is not None and row[3] != row[4]:
            continue
        created = _parse_ledger_ts(row[2])
        if created is None:
            continue
        age_days = max((now_utc - created).total_seconds() / 86400.0, 0.0)
        decay = 2.0 ** (-age_days / half_life) if half_life > 0 else 1.0
        yield row, weight, decay


def _compute_adjustments(
    rows: list[tuple],
    now: datetime,
    *,
    half_life: float,
    cap_total: float,
) -> dict[int, float]:
    """Pure per-doc delta from raw ledger rows: decay, filters, clamp."""
    now_utc = _as_utc_naive(now)
    totals: dict[int, float] = {}
    for row, weight, decay in _decayed_weights(rows, now_utc, half_life):
        doc_id = int(row[0])
        totals[doc_id] = totals.get(doc_id, 0.0) + weight * decay

    adjustments: dict[int, float] = {}
    for doc_id, total in totals.items():
        delta = min(max(total, 0.0), cap_total)
        if delta != 0.0:
            adjustments[doc_id] = delta
    return adjustments


async def effective_salience_adjustments(
    db: aiosqlite.Connection,
    doc_ids: list[int],
    now: datetime,
) -> dict[int, float]:
    """Per-doc ledger delta for the given candidate ``doc_ids`` (READ-ONLY).

    Returns ``{doc_id: delta}`` with ``delta = clamp(Σ decayed weights, 0,
    cap_total)`` — only docs with a non-zero delta appear. Empty dict when the
    flag is not ``on``, the candidate set is empty, or the ledger tables are
    absent (pre-migration DB → fail open, ranking unchanged).
    """
    if settings.reinforcement_mode != "on" or not doc_ids:
        return {}

    sql, params = _boost_sql_and_params(doc_ids, settings.reinforcement_boost_epoch)
    try:
        cur = await db.execute(sql, params)
        rows = await cur.fetchall()
    except aiosqlite.OperationalError as exc:
        logger.warning("reinforcement read degraded: %s", exc)
        return {}
    return _compute_adjustments(
        [tuple(row) for row in rows],
        now,
        half_life=float(settings.reinforcement_half_life_days),
        cap_total=float(settings.reinforcement_cap_total),
    )


def _read_adjustments_sync(
    db_path: str,
    paths: list[str],
    now: datetime,
    *,
    epoch: str | None,
    half_life: float,
    cap_total: float,
) -> tuple[dict[str, int], dict[int, float]]:
    """Worker-thread body for the ranking innesto: resolve + fetch + compute.

    Plain ``sqlite3`` on a pooled thread with a cached read-only connection
    (see module docstring for why not ``aiosqlite`` / a fresh connection).
    Returns ``(id_by_path, adjustments)``; empty on any operational problem
    (fail open — ranking unchanged).
    """
    with _READ_CONN_LOCK:
        try:
            conn = _cached_read_conn(db_path)
        except (ValueError, sqlite3.Error) as exc:
            # ValueError: a path resolve()/as_uri() edge — fail open on the
            # search rather than surfacing a non-sqlite error to the caller.
            logger.warning("reinforcement: cannot open %s: %s", db_path, exc)
            return {}, {}
        try:
            placeholders = ",".join("?" * len(paths))
            id_rows = conn.execute(
                "SELECT id, file_path FROM documents "
                f"WHERE file_path IN ({placeholders})",
                paths,
            ).fetchall()
            id_by_path = {str(r[1]): int(r[0]) for r in id_rows}
            if not id_by_path:
                return {}, {}
            sql, params = _boost_sql_and_params(list(id_by_path.values()), epoch)
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            # Pre-migration DB (no salience_boosts) or a stale cached handle →
            # fail open: no adjustment, drop the connection for a clean retry.
            logger.warning("reinforcement read degraded: %s", exc)
            _drop_read_conn(db_path)
            return {}, {}
    return id_by_path, _compute_adjustments(
        rows, now, half_life=half_life, cap_total=cap_total
    )


async def apply_reinforcement_to_grouped(
    grouped: dict[str, list[dict]],
    db_path: str,
    *,
    rrf_k: int,
) -> None:
    """Adjust the already-emitted document hits IN PLACE (flag ``on`` only).

    The caller (``hybrid_search``) gates this behind the single structural
    ``reinforcement_mode == "on"`` check. Steps, all READ-ONLY:

    1. collect the emitted hits backed by a ``documents`` row — exactly the
       hits carrying a non-empty ``path`` (semantic + doc_fts lanes; kg /
       kg_expand / row-FTS hits carry ``path=None``);
    2. resolve ``file_path → documents.id`` and fetch+compute the per-doc
       delta on a pooled worker thread (same SQL + same computation as
       :func:`effective_salience_adjustments`);
    3. add the delta to the emitted ``salience`` and stable-sort each bucket
       by ``rrf_score + delta/(rrf_k+1)`` — unboosted hits keep their exact
       relative order (delta 0 → key = rrf_score, already descending).

    Fail-open: any DB problem leaves the fused result unchanged.
    """
    hits_by_path: dict[str, list[dict]] = {}
    for bucket_hits in grouped.values():
        for hit in bucket_hits:
            path = hit.get("path")
            if path:
                hits_by_path.setdefault(str(path), []).append(hit)
    if not hits_by_path:
        return

    id_by_path, deltas = await asyncio.to_thread(
        _read_adjustments_sync,
        db_path,
        list(hits_by_path),
        datetime.now(timezone.utc),
        epoch=settings.reinforcement_boost_epoch,
        half_life=float(settings.reinforcement_half_life_days),
        cap_total=float(settings.reinforcement_cap_total),
    )
    if not deltas:
        return

    delta_by_path = {
        path: deltas[doc_id]
        for path, doc_id in id_by_path.items()
        if doc_id in deltas
    }
    if not delta_by_path:
        return

    rank_scale = 1.0 / (rrf_k + 1)
    for path, delta in delta_by_path.items():
        for hit in hits_by_path[path]:
            # Explicit None check: a legitimate salience of 0.0 must stay 0.0
            # as the base — `or 0.5` would silently promote it to 0.5.
            base = hit.get("salience")
            hit["salience"] = round(
                float(0.5 if base is None else base) + delta, 6
            )

    def _adjusted_score(hit: dict) -> float:
        delta = delta_by_path.get(str(hit.get("path") or ""), 0.0)
        return float(hit.get("rrf_score") or 0.0) + delta * rank_scale

    for bucket_hits in grouped.values():
        if len(bucket_hits) > 1 and any(
            str(h.get("path") or "") in delta_by_path for h in bucket_hits
        ):
            bucket_hits.sort(key=_adjusted_score, reverse=True)
