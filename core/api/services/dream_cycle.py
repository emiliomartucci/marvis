# v1.0.0 - 2026-06-05 - Track 2 #1-S5: nightly dream-cycle consolidation (SHADOW/dry-run)
"""Nightly dream-cycle consolidation — SHADOW / dry-run only (Track 2 #1-S5).

Plan ``docs/plans/2026-06-04-track2-engine-moat-roadmap-plan.md`` (#1, sub-increment
S5, lines 343/350). This is the *batch* analogue of the S3 write-time gate. Where S3
decides ADD / NOOP / SUPERSEDE_CANDIDATE for ONE just-inserted learning, the dream
cycle scans the WHOLE live learnings store and, for each near-match pair in the
supersede band, writes a pending SUPERSEDE_CANDIDATE proposal into the SAME approval
gate the brain cycle drains (``brain_memory_operations``, mig 129).

DRY-RUN, NON-NEGOTIABLE. This module is SHADOW only — it ``propose``s, it NEVER
``apply``s:
  * it NEVER calls :func:`temporal_write.apply_supersede`;
  * it NEVER sets ``invalid_at`` / ``superseded_by`` on any row;
  * it NEVER deletes a learning (no NOOP-style hard-remove like the write path does).
The verdict surfaces ONLY as a pending proposal for the human/Triage gate. Diffing
those proposals against the gold set (and only then enabling an auto-apply
high-confidence tier) is a deliberate, eval-gated, OFF-host follow-up — see plan
lines 341/343.

Reuses S3 verbatim (does NOT modify ``temporal_write`` / ``learnings``):
  * :func:`temporal_write.decide_write_action` + the pinned 0.80 / 0.97 bands;
  * :func:`temporal_write.propose_supersede_candidate` (the ``INSERT OR IGNORE`` on a
    stable BLAKE2b ``operation_id`` is what makes a re-run idempotent — no duplicate
    proposals);
  * default vector source = :func:`temporal_write.fetch_learning_vector` +
    :func:`temporal_write.fetch_live_neighbor_vectors`, which read PRE-COMPUTED
    vectors back from the search-index mirror. **NO embedding model is EVER run.**

Flag gate: the SAME ``settings.temporal_memory_enabled`` (alias
``MARVIS_TEMPORAL_MEMORY``, default False). When OFF, :func:`run_dream_cycle_shadow`
is a no-op that returns an empty report and scans nothing.

Host-safety + testability: the neighbour/vector source is INJECTABLE via the
``neighbor_provider`` callable. Tests pass FAKE vectors of known cosine through it,
so the unit suite needs neither sqlite-vec nor a model. The default provider only
reads mirrored bytes — still model-free.

What this module deliberately does NOT do: it does NOT build or run a real nightly
scheduler (cron/launchd), and it does NOT run a real cycle over the prod store.
Wiring this STEP to the nightly schedule and flipping the (default-off)
warehouse-consolidation pass on are separate, eval-gated, off-host steps. This is the
pure batch step + its dry-run guarantee, unit-testable in isolation.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import aiosqlite

from core.api.config import settings
from core.api.services import temporal_write as tw

# A neighbour provider resolves, for a given live learning id, the pair
# ``(its_vector, [(neighbor_id, neighbor_vector), ...])`` that the PURE band decision
# consumes. ``None`` for the vector means "no mirrored embedding yet" → skip that row
# (mirrors the S3 write-path fallback: stay ADD, catch it on a later run). Injectable
# so tests feed FAKE vectors and the default reads mirrored bytes — neither runs a model.
NeighborProvider = Callable[
    [aiosqlite.Connection, str, str],
    Awaitable[tuple[list[float] | None, list[tuple[str, list[float]]]]],
]


@dataclass(frozen=True)
class DreamCycleReport:
    """Outcome of one SHADOW dream-cycle pass.

    ``scanned``  — live learnings examined (``invalid_at IS NULL``).
    ``proposed`` — DISTINCT supersede-candidate pairs proposed this pass (the pair is
                   canonicalised by id, so the reverse-direction scan of the same pair
                   does NOT double-count). On a fresh store this equals the rows added
                   to the gate; on a re-run the gate's ``INSERT OR IGNORE`` on a stable
                   ``operation_id`` keeps it duplicate-free, so re-running adds zero
                   gate rows even though it re-proposes (``proposed`` may be > 0 again).
    ``skipped``  — scanned learnings that produced NO new proposal: no mirrored vector
                   yet, no neighbours, a non-band verdict (ADD / NOOP), or the closest
                   neighbour forms a pair already proposed earlier in THIS pass.

    Invariant: ``scanned == proposed + skipped``.
    """

    scanned: int
    proposed: int
    skipped: int


async def _default_neighbor_provider(
    db: aiosqlite.Connection,
    workspace_id: str,
    learning_id: str,
) -> tuple[list[float] | None, list[tuple[str, list[float]]]]:
    """Default vector source — reads PRE-COMPUTED vectors back. NEVER runs a model.

    Mirrors the S3 write path exactly: pull the learning's own mirrored vector, then
    its top-k LIVE (``invalid_at IS NULL``) neighbours via sqlite-vec kNN. Returns
    ``(None, [])`` when the mirror isn't present yet so the caller skips the row.
    """
    new_vec = await tw.fetch_learning_vector(db, learning_id)
    if new_vec is None:
        return None, []
    neighbors = await tw.fetch_live_neighbor_vectors(
        db, workspace_id, new_vec, exclude_learning_id=learning_id
    )
    return new_vec, neighbors


async def _live_learning_ids(
    db: aiosqlite.Connection, workspace_id: str
) -> list[tuple[str, str | None]]:
    """All LIVE learnings (``invalid_at IS NULL``) for the workspace: (id, project).

    Workspace-scoped with the same ``COALESCE(workspace_id, 'ws_default')`` default as
    the S3 neighbour fetch, so a row and its neighbours live in one scope. Stable order
    so a re-run walks the store identically.
    """
    cur = await db.execute(
        """
        SELECT id, project
        FROM learnings
        WHERE invalid_at IS NULL
          AND COALESCE(workspace_id, 'ws_default') = ?
        ORDER BY id
        """,
        [workspace_id],
    )
    rows = await cur.fetchall()
    return [(row["id"], row["project"]) for row in rows]


async def run_dream_cycle_shadow(
    db: aiosqlite.Connection,
    workspace_id: str,
    *,
    neighbor_provider: NeighborProvider | None = None,
) -> DreamCycleReport:
    """Scan the full LIVE learnings store and PROPOSE supersede candidates. DRY-RUN.

    For each live learning, resolve ``(its_vector, live_neighbours)`` via the injected
    ``neighbor_provider`` (default = mirrored-vector reader, no model), run the PURE
    two-band cosine :func:`temporal_write.decide_write_action`, and on a
    ``SUPERSEDE_CANDIDATE`` verdict write ONE pending proposal into the existing
    approval gate via :func:`temporal_write.propose_supersede_candidate`.

    SHADOW guarantee: this only ever PROPOSES. It NEVER calls ``apply_supersede``,
    NEVER stamps ``invalid_at`` / ``superseded_by``, NEVER deletes a row — not even on
    a NOOP verdict (the write path retires a brand-new duplicate; a batch pass over the
    persisted store must not, so NOOP here is a skip).

    Idempotency: the (old, new) pair is canonicalised by sorted id BEFORE proposing, so
    scanning A→B and later B→A target the SAME proposal. Combined with
    ``propose_supersede_candidate``'s stable ``operation_id`` + ``INSERT OR IGNORE``, a
    re-run adds zero duplicate rows to the gate.

    Flag OFF (``settings.temporal_memory_enabled`` False): no-op — returns an empty
    report and scans nothing.
    """
    if not settings.temporal_memory_enabled:
        return DreamCycleReport(scanned=0, proposed=0, skipped=0)

    provider = neighbor_provider or _default_neighbor_provider

    scanned = 0
    proposed = 0
    skipped = 0
    seen_pairs: set[tuple[str, str]] = set()

    for learning_id, project in await _live_learning_ids(db, workspace_id):
        scanned += 1

        new_vec, neighbors = await provider(db, workspace_id, learning_id)
        if new_vec is None or not neighbors:
            skipped += 1
            continue

        decision = tw.decide_write_action(new_vec, neighbors)
        if (
            decision.action is not tw.WriteAction.SUPERSEDE_CANDIDATE
            or decision.neighbor_id is None
        ):
            # ADD (distinct) / NOOP (near-dup) verdicts produce no proposal in shadow.
            skipped += 1
            continue

        # Canonicalise the pair by id so A→B and B→A collapse to ONE proposal. The
        # in-run ``seen_pairs`` guard makes ``proposed`` count DISTINCT pairs this pass
        # (the reverse-direction scan is a skip, not a second proposal); the persisted
        # gate is *also* dedup'd by propose_supersede_candidate's stable operation_id +
        # INSERT OR IGNORE, so a re-run across passes adds nothing either.
        pair = tuple(sorted((learning_id, decision.neighbor_id)))
        if pair in seen_pairs:
            skipped += 1
            continue
        seen_pairs.add(pair)

        a, b = pair
        score = decision.score if decision.score is not None else 0.0
        await tw.propose_supersede_candidate(
            db,
            old_id=a,
            new_id=b,
            score=score,
            summary=(
                f"Dream-cycle near-match between learnings '{a}' and '{b}' "
                f"(cosine={score:.4f}) — confirm supersede or keep both."
            ),
            project=project,
        )
        proposed += 1

    await db.commit()
    return DreamCycleReport(scanned=scanned, proposed=proposed, skipped=skipped)
