# v1.0.0 - 2026-06-05 - Track 2 #1-S3: write-time temporal decision (PURE) + soft-supersede helper
"""Write-time consolidation decision — the Mem0-style ADD/NOOP/SUPERSEDE gate.

Track 2 #1-S3 (plan ``docs/plans/2026-06-04-track2-engine-moat-roadmap-plan.md``,
lines 327-334, 348). This is the WRITE half of the bi-temporal memory model whose
READ half (the ``invalid_at`` filter) shipped in S2. Everything here is gated by the
SAME flag, ``settings.temporal_memory_enabled`` (alias ``MARVIS_TEMPORAL_MEMORY``,
DEFAULT False): when OFF, this module is never reached and ``create_learning`` /
``update_learning`` are byte-for-byte unchanged.

Two pieces:

1. :func:`decide_write_action` — a **PURE** function. Cosine two-band on the new
   row's vector vs the top-k LIVE neighbour vectors the caller fetched. No model,
   no DB, no I/O → trivially unit-testable with FAKE vectors of known cosine.

   * ``sim < ADD_FLOOR`` (0.80)           → ``ADD``  (distinct; insert a new live row)
   * ``sim > NOOP_CEIL`` (0.97)           → ``NOOP`` (near-duplicate; optionally REINFORCE)
   * ``ADD_FLOOR <= sim <= NOOP_CEIL``    → ``SUPERSEDE_CANDIDATE`` (PROPOSE, never auto-apply)

   The 0.80-0.97 band is the false-merge surface (two distinct learnings on the
   same module sit ~0.90), so the band NEVER auto-applies — it only proposes a row
   for the human/approval gate. NEVER auto-DELETE; NEVER auto-invalidate here.

2. :func:`apply_supersede` — the **soft-invalidate** helper the APPROVAL path calls
   (never this module, never the write path automatically). Sets ``invalid_at=now``
   + ``superseded_by`` + ``supersede_reason`` on the OLD row. The old row is NEVER
   deleted (reversible, audited; the vector stays so ``as_of`` semantic search still
   works). The new row stays live and untouched.

Schema notes (validated against migrations):
* mig 148 added ``valid_from`` / ``invalid_at`` / ``superseded_by`` /
  ``supersede_reason`` on ``learnings`` (S1).
* SUPERSEDE_CANDIDATE proposals are written into the EXISTING
  ``brain_memory_operations`` approval table (mig 129) so the human Triage queue
  that already drains brain proposals also drains write-time ones — see
  :func:`propose_supersede_candidate`. That table is brain-cycle-bound (``run_id``
  FK ON DELETE RESTRICT → ``brain_runs``), so we lazily ensure ONE stable,
  fixed write-time envelope (``cycle_key='write_time'``) instead of inventing a
  parallel table.
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

import aiosqlite

# ---------------------------------------------------------------------------
# Pinned thresholds (two-band cosine). DO NOT lower ADD_FLOOR below ~0.80 without
# an eval pass — the band is the false-merge surface and a lower floor merges
# distinct learnings (data loss). See plan line 334.
# ---------------------------------------------------------------------------

ADD_FLOOR: float = 0.80   # sim below this → certainly distinct → ADD (no proposal)
NOOP_CEIL: float = 0.97   # sim above this → near-duplicate → NOOP/REINFORCE (no new fact)
TOP_K_NEIGHBORS: int = 5  # how many live neighbours the band compares against (Mem0 top-s)


class WriteAction(str, Enum):
    """The verdict of the write-time decision."""

    ADD = "add"                                  # distinct → insert a new live row
    NOOP = "noop"                                # near-duplicate → skip (optionally REINFORCE)
    SUPERSEDE_CANDIDATE = "supersede_candidate"  # mid-band → PROPOSE, human confirms


@dataclass(frozen=True)
class WriteDecision:
    """Result of :func:`decide_write_action`.

    ``action``    — the verdict (see :class:`WriteAction`).
    ``neighbor_id`` — id of the closest live neighbour (``None`` when there are no
                      neighbours → always ADD).
    ``score``     — cosine similarity to that closest neighbour in ``[-1, 1]``
                    (``None`` when there are no neighbours).
    """

    action: WriteAction
    neighbor_id: str | None
    score: float | None


# ---------------------------------------------------------------------------
# PURE: cosine two-band decision (no model, no DB)
# ---------------------------------------------------------------------------


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain cosine on two equal-length vectors. Returns 0.0 if either is zero-norm.

    Pure; no numpy dependency (keeps the decision unit-testable with bare lists).
    """
    if len(a) != len(b):
        raise ValueError(f"vector length mismatch: {len(a)} != {len(b)}")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def decide_write_action(
    new_vec: list[float],
    neighbors: list[tuple[str, list[float]]],
) -> WriteDecision:
    """Decide ADD / NOOP / SUPERSEDE_CANDIDATE for a new learning vector. PURE.

    ``new_vec``   — the embedding of the row being written (the caller reads it
                    back from the embed-on-create mirror; this function never
                    runs a model).
    ``neighbors`` — ``[(learning_id, vector, ...)]`` of the top-k LIVE neighbours
                    (``invalid_at IS NULL``), fetched by the caller. Only the first
                    two tuple elements (id, vector) are read, so callers may pass
                    richer tuples.

    Logic (two-band cosine to the SINGLE closest live neighbour):
      * no neighbours              → ADD (nothing to collide with)
      * sim < ADD_FLOOR (0.80)     → ADD (distinct)
      * sim > NOOP_CEIL (0.97)     → NOOP (near-duplicate)
      * 0.80 <= sim <= 0.97        → SUPERSEDE_CANDIDATE (propose, do NOT auto-apply)
    """
    best_id: str | None = None
    best_score: float | None = None
    for entry in neighbors:
        neighbor_id = entry[0]
        neighbor_vec = entry[1]
        sim = cosine_similarity(new_vec, neighbor_vec)
        if best_score is None or sim > best_score:
            best_score = sim
            best_id = neighbor_id

    if best_score is None:
        return WriteDecision(action=WriteAction.ADD, neighbor_id=None, score=None)

    if best_score < ADD_FLOOR:
        action = WriteAction.ADD
    elif best_score > NOOP_CEIL:
        action = WriteAction.NOOP
    else:
        action = WriteAction.SUPERSEDE_CANDIDATE

    return WriteDecision(action=action, neighbor_id=best_id, score=best_score)


# ---------------------------------------------------------------------------
# Vector read-back (NO model): unpack the embed-on-create mirror from vec_documents
# ---------------------------------------------------------------------------


async def fetch_learning_vector(
    db: aiosqlite.Connection, learning_id: str
) -> list[float] | None:
    """Read a learning's vector back from the search-index mirror. NEVER runs a model.

    The embedder mirrors a learning into ``documents`` (``file_path =
    'learning:<id>'``) + ``vec_documents`` (the sqlite-vec kNN table) on create,
    fire-and-forget. We read those PRE-COMPUTED bytes and ``struct``-unpack them —
    no inference. Returns ``None`` when the mirror isn't present yet (embed still
    in flight, or the embedder is unavailable), in which case the caller treats the
    write as ADD (current behaviour) and lets the dream cycle catch the merge later.

    ``vec_documents.embedding`` is a vec0 column whose raw value is the packed
    little-endian float32 blob (``embedding_service.serialize_f32`` produced it).
    """
    cur = await db.execute(
        "SELECT id FROM documents WHERE file_path = ?",
        [f"learning:{learning_id}"],
    )
    doc_row = await cur.fetchone()
    if doc_row is None:
        return None
    doc_id = doc_row["id"]

    from core.api.db import ensure_vec_documents

    if not await ensure_vec_documents(db):
        return None

    cur = await db.execute(
        "SELECT embedding FROM vec_documents WHERE doc_id = ?",
        [doc_id],
    )
    vec_row = await cur.fetchone()
    if vec_row is None or vec_row["embedding"] is None:
        return None
    raw = vec_row["embedding"]
    n = len(raw) // 4
    return list(struct.unpack(f"<{n}f", raw))


async def fetch_live_neighbor_vectors(
    db: aiosqlite.Connection,
    workspace_id: str,
    new_vec: list[float],
    *,
    exclude_learning_id: str,
    k: int = TOP_K_NEIGHBORS,
) -> list[tuple[str, list[float]]]:
    """Fetch the top-k LIVE learning neighbours (id, vector) via sqlite-vec kNN.

    LIVE = ``invalid_at IS NULL`` (post-kNN filter, mirroring the S2 read path —
    the temporal filter is applied in the SQL join AFTER the kNN scan, never inside
    the per-connection vec setup; footgun learning d8ce1871). Workspace-scoped and
    self-excluding (the just-written row must not match itself). Returns ``[]`` when
    sqlite-vec isn't loadable. NEVER runs a model — ``new_vec`` is the already-read
    embedding.
    """
    from core.api.db import ensure_vec_documents
    from core.api.services import embedding_service

    if not await ensure_vec_documents(db):
        return []

    vec_bytes = embedding_service.serialize_f32(new_vec)
    # Overcollect: vec0 MATCH filters AFTER the kNN scan; we drop self + non-live +
    # non-learning + cross-workspace rows below, so pull extra.
    overcollect = (k + 1) * 4
    cur = await db.execute(
        """
        SELECT d.file_path, l.id AS learning_id, l.invalid_at, v.embedding
        FROM vec_documents v
        JOIN documents d ON d.id = v.doc_id
        JOIN learnings l
          ON d.file_path = 'learning:' || l.id
         AND COALESCE(l.workspace_id, 'ws_default') = ?
        WHERE v.embedding MATCH ? AND v.k = ?
          AND l.invalid_at IS NULL
          AND l.id != ?
        ORDER BY v.distance
        """,
        [workspace_id, vec_bytes, overcollect, exclude_learning_id],
    )
    rows = await cur.fetchall()
    out: list[tuple[str, list[float]]] = []
    for row in rows:
        raw = row["embedding"]
        if raw is None:
            continue
        n = len(raw) // 4
        out.append((row["learning_id"], list(struct.unpack(f"<{n}f", raw))))
        if len(out) >= k:
            break
    return out


# ---------------------------------------------------------------------------
# SUPERSEDE proposal — write into the EXISTING brain_memory_operations gate
# ---------------------------------------------------------------------------

# Fixed write-time envelope so the write-time proposal lands in the SAME approval
# table the brain cycle uses, WITHOUT a real cycle. status='succeeded' +
# trigger='manual' keeps it valid against the CHECK enums (mig 127); the single
# fixed run_id keeps the partial unique index uniq_brain_runs_active_cycle happy
# (exactly one active 'write_time' run ever exists). cycle_key='write_time' is the
# sentinel the cycle aggregator excludes.
_WRITE_TIME_RUN_ID = "run_write_time"
_WRITE_TIME_CYCLE_KEY = "write_time"
_PROPOSAL_EXPIRY_DAYS = 30


async def _ensure_write_time_run(db: aiosqlite.Connection, now_iso: str) -> None:
    """Idempotently ensure the sentinel brain_runs envelope for write-time proposals."""
    await db.execute(
        """
        INSERT OR IGNORE INTO brain_runs
            (run_id, workspace_id, cycle_key, cycle_window_start_utc,
             cycle_window_end_utc, cutoff_hour_utc_at_run, scope_type, scope_key,
             trigger, status, started_at, finished_at)
        VALUES (?, 'ws_default', ?, ?, ?, 0, 'company', '__company__',
                'manual', 'succeeded', ?, ?)
        """,
        [
            _WRITE_TIME_RUN_ID,
            _WRITE_TIME_CYCLE_KEY,
            now_iso,
            now_iso,
            now_iso,
            now_iso,
        ],
    )


async def propose_supersede_candidate(
    db: aiosqlite.Connection,
    *,
    old_id: str,
    new_id: str,
    score: float,
    summary: str,
    project: str | None = None,
) -> str:
    """Write a pending SUPERSEDE_CANDIDATE proposal into brain_memory_operations.

    The write-time decision PROPOSES; the human/approval gate (or a future
    high-confidence auto-band, S4) CONFIRMS by calling :func:`apply_supersede`.
    This function NEVER touches the OLD row's ``invalid_at`` — proposing is not
    applying. Returns the ``operation_id``.

    Row shape (mig 129 ``brain_memory_operations``):
      * ``operation_type='supersede_candidate'``
      * ``source_ref = 'learning:<old_id>'`` (the row that would be retired)
      * ``target_ref = 'learning:<new_id>'`` (the replacement live row)
      * ``proposed_write_target_type='learning'`` + a ``proposed_write_json``
        carrying ``{old_id, new_id, supersede_reason}`` so the apply path has
        everything it needs.
      * ``approval_state='pending'`` → drains through the existing Triage queue.
    """
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    detected_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    expires_at = now.replace(microsecond=0)
    expires_at = expires_at.isoformat().replace("+00:00", "Z")

    await _ensure_write_time_run(db, now_iso)

    source_ref = f"learning:{old_id}"
    target_ref = f"learning:{new_id}"
    # Stable BLAKE2b operation_id mirroring brain memory_ops.make_operation_id, but
    # self-contained (avoids importing the brain service into the write path).
    import hashlib

    evidence = sorted({source_ref, target_ref})
    ev_hash = hashlib.blake2b(
        "|".join(evidence).encode("utf-8"), digest_size=32
    ).hexdigest()  # 64 hex chars (CHECK length = 64)
    op_seed = (
        f"{_WRITE_TIME_CYCLE_KEY}|supersede_candidate|project|"
        f"{project or '__company__'}|{source_ref}|{target_ref}|{ev_hash}"
    )
    operation_id = hashlib.blake2b(op_seed.encode("utf-8"), digest_size=16).hexdigest()

    reason = f"write-time near-match (cosine={score:.4f}) — human-confirm supersede"
    proposed_write_json = json.dumps(
        {
            "old_id": old_id,
            "new_id": new_id,
            "supersede_reason": reason,
            "score": score,
            "origin": "write_time",
        }
    )
    scope_type = "project" if project else "company"
    scope_key = project or "__company__"
    involved = json.dumps([project] if project else [])
    recurrence_key = hashlib.blake2b(
        f"supersede_candidate|{scope_type}|{scope_key}|{source_ref}|{target_ref}".encode(),
        digest_size=8,
    ).hexdigest()

    await db.execute(
        """
        INSERT OR IGNORE INTO brain_memory_operations
            (operation_id, run_id, cycle_key, detected_at, operation_type,
             schema_version, scope_type, scope_key, program_key,
             source_ref, target_ref, score, recurrence_key, recurrence_count,
             first_seen_cycle_key, last_seen_cycle_key, involved_projects_json,
             evidence_hash, summary, proposed_write_target_type, proposed_write_json,
             requires_approval, approval_state, expires_at)
        VALUES (?, ?, ?, ?, 'supersede_candidate',
                1, ?, ?, NULL,
                ?, ?, ?, ?, 1,
                ?, ?, ?,
                ?, ?, 'learning', ?,
                1, 'pending', ?)
        """,
        [
            operation_id,
            _WRITE_TIME_RUN_ID,
            _WRITE_TIME_CYCLE_KEY,
            detected_at,
            scope_type,
            scope_key,
            source_ref,
            target_ref,
            max(0.0, min(1.0, score)),
            recurrence_key,
            _WRITE_TIME_CYCLE_KEY,
            _WRITE_TIME_CYCLE_KEY,
            involved,
            ev_hash,
            summary[:2000],
            proposed_write_json,
            expires_at,
        ],
    )
    return operation_id


# ---------------------------------------------------------------------------
# APPLY (soft-invalidate) — APPROVAL path only, NEVER called automatically
# ---------------------------------------------------------------------------


async def apply_supersede(
    db: aiosqlite.Connection,
    *,
    old_id: str,
    new_id: str,
    reason: str,
) -> None:
    """Soft-invalidate the OLD learning, pointing it at the new live row.

    This is what the APPROVAL path runs once a human (or a future high-confidence
    auto-band, S4) confirms a SUPERSEDE_CANDIDATE. It is exposed but NEVER called
    automatically by the write path — proposing is not applying.

    Sets, on the OLD row only:
      * ``invalid_at = now``        (system-time retraction; NULL→timestamp = no longer live)
      * ``superseded_by = new_id``  (audit chain pointer to the replacement)
      * ``supersede_reason = reason``

    The old row is NEVER deleted (reversible, audited; its vector stays so ``as_of``
    semantic search still resolves it). The new row is left untouched and live.
    Caller owns the transaction/commit.
    """
    now = datetime.now(timezone.utc).isoformat()
    await db.execute(
        "UPDATE learnings SET invalid_at = ?, superseded_by = ?, supersede_reason = ? "
        "WHERE id = ? AND invalid_at IS NULL",
        (now, new_id, reason, old_id),
    )
