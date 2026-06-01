# v1.0.0 - 2026-04-14 - KG Fase 1b: ranker signal-to-noise (suspect_write)
"""Graph ranker — turns the neighbour list from noise into signal (Fase 1b).

Ranker `suspect_write`:
  Given the callers of a read-only primitive (e.g. `api.db.get_db`), score
  each caller on how likely it is to be writing through a read-only handle.
  Signals:
    - intent: HTTP verb from metadata (POST/PATCH/DELETE/PUT → 1.0, GET → 0.0,
      None → 0.3)
    - verb: lexical hint from the caller's function name (create/update/... →
      1.0, get/list/... → 0.1, unknown → 0.4)
    - distance: BFS distance from the caller to a write primitive (direct
      caller → 0.0, unreachable → 0.5)

Score = 0.6 * intent + 0.2 * verb + 0.2 * distance.
Classification: >= 0.7 → suspect, <= 0.3 → legitimate, else uncertain.

Read-only service (pattern MarvisX single-writer). Uses the same
aiosqlite.Connection FastAPI yields from get_db — never opens a writer.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Awaitable, Callable

import aiosqlite

from core.api.models.graph import RankClassification, RankedNeighbor

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module constants (pattern salience_service.HALF_LIFE_DAYS — no config.py)
# ---------------------------------------------------------------------------

WEIGHT_INTENT: float = 0.6
WEIGHT_VERB: float = 0.2
WEIGHT_DISTANCE: float = 0.2

THRESHOLD_SUSPECT: float = 0.7
THRESHOLD_LEGITIMATE: float = 0.3

# HTTP verb → intent score. Mutating verbs dominate the score; None covers
# internal helpers (no endpoint context) with a neutral prior.
INTENT_SCORES: dict[str | None, float] = {
    "POST": 1.0,
    "PATCH": 1.0,
    "DELETE": 1.0,
    "PUT": 1.0,
    "GET": 0.0,
    "HEAD": 0.0,
    "OPTIONS": 0.0,
    None: 0.3,
}

# Lexical heuristics over the caller's *name* (not qualified_name).
# Lowercased + split on underscore; matches first recognised prefix.
MUTATIVE_VERBS: frozenset[str] = frozenset({
    "create", "update", "delete", "set", "add", "save",
    "insert", "remove", "put", "patch", "upsert", "replace",
})
READ_VERBS: frozenset[str] = frozenset({
    "get", "list", "fetch", "find", "read", "query", "load", "view",
})

VERB_SCORE_MUTATIVE: float = 1.0
VERB_SCORE_READ: float = 0.1
VERB_SCORE_UNKNOWN: float = 0.4

# Direct BFS distances toward write primitives.
DISTANCE_SCORES: dict[int | None, float] = {
    1: 0.0,
    2: 0.2,
    3: 0.4,
}
DISTANCE_SCORE_UNREACHABLE: float = 0.5  # >3 or unreachable → uncertain

MAX_BFS_DEPTH: int = 3

# Write primitives (target set for BFS). Fase 1a indexes them as `py:function:`
# nodes; the test pre-check verifies this.
WRITE_PRIMITIVES: frozenset[str] = frozenset({
    "py:function:api.db.write_db",
    "py:function:api.db.get_write_db",
    "py:function:api.db.acquire_write_db",
})

# Split "do_thing_x" → ["do", "thing", "x"]
_NAME_SPLIT = re.compile(r"[_\-]+")


# ---------------------------------------------------------------------------
# Pure scoring helpers
# ---------------------------------------------------------------------------


def intent_score(http_verb: str | None) -> float:
    """Score based on HTTP verb metadata.

    None (non-endpoint) → 0.3 neutral prior; unknown string → 0.3 as well
    (defensive; parser only emits verbs in the FastAPI set).
    """
    if http_verb is None:
        return INTENT_SCORES[None]
    return INTENT_SCORES.get(http_verb, INTENT_SCORES[None])


def verb_score(name: str | None) -> float:
    """Score based on the first lexical token of the function name.

    `create_recipient` → mutative (1.0).
    `get_compose` → read (0.1).
    `list_recipients` starts with `list` → read (0.1) — this is exactly the
    newsletter bug: a read-shaped name that nonetheless performs writes.
    Unknown prefix (e.g. `post_preview`) → 0.4 ambiguous.
    """
    if not name:
        return VERB_SCORE_UNKNOWN
    tokens = [t for t in _NAME_SPLIT.split(name.lower()) if t]
    if not tokens:
        return VERB_SCORE_UNKNOWN
    first = tokens[0]
    if first in MUTATIVE_VERBS:
        return VERB_SCORE_MUTATIVE
    if first in READ_VERBS:
        return VERB_SCORE_READ
    return VERB_SCORE_UNKNOWN


def distance_score(distance: int | None) -> float:
    """Score based on BFS distance to a write primitive (lower = legit)."""
    if distance is None:
        return DISTANCE_SCORE_UNREACHABLE
    if distance in DISTANCE_SCORES:
        return DISTANCE_SCORES[distance]
    return DISTANCE_SCORE_UNREACHABLE


def classify(score: float) -> RankClassification:
    """Derive the classification bucket from a raw score."""
    if score >= THRESHOLD_SUSPECT:
        return "suspect"
    if score <= THRESHOLD_LEGITIMATE:
        return "legitimate"
    return "uncertain"


# ---------------------------------------------------------------------------
# Batched BFS toward WRITE_PRIMITIVES
# ---------------------------------------------------------------------------


async def compute_distances_batch(
    db: aiosqlite.Connection,
    caller_ids: list[str],
    target_set: frozenset[str] | set[str] = WRITE_PRIMITIVES,
    max_depth: int = MAX_BFS_DEPTH,
) -> dict[str, int | None]:
    """Iterative Python BFS — 1 SQL query per depth level (max_depth total).

    Reverse semantics: starts from each caller and walks `calls` edges
    outgoing. Returns `{caller_id: depth or None}` — None = unreachable
    within `max_depth`.

    Pattern choice: NOT a recursive CTE. SQLite CTEs are not used anywhere
    else in MarvisX, and a Python loop is easier to audit, easier to bound,
    and easier to instrument (logger hits per depth level). Performance
    target per plan: 3 queries total → ~15-20ms on 50 callers.
    """
    if not caller_ids:
        return {}

    # Origin → current frontier node (we keep the origin so we can attribute
    # the final depth back to the right caller).
    frontier: dict[str, str] = {c: c for c in caller_ids}
    # Every node we've visited (per-origin) to avoid cycles.
    visited: dict[str, set[str]] = {c: {c} for c in caller_ids}
    result: dict[str, int | None] = {}

    for depth in range(1, max_depth + 1):
        if not frontier:
            break

        # Unique current-frontier nodes for this depth.
        nodes_now = list({n for n in frontier.values()})
        placeholders = ",".join("?" * len(nodes_now))
        sql = (
            f"SELECT source_id, target_id FROM graph_edges "
            f"WHERE source_id IN ({placeholders}) AND relation = 'calls'"
        )
        cur = await db.execute(sql, nodes_now)
        rows = await cur.fetchall()

        # Group edges by source for O(N) lookup next loop.
        out: dict[str, list[str]] = {}
        for src, tgt in rows:
            out.setdefault(src, []).append(tgt)

        new_frontier: dict[str, str] = {}
        for origin, current in frontier.items():
            if origin in result:
                continue
            hit_target = False
            next_node: str | None = None
            for tgt in out.get(current, ()):
                if tgt in target_set:
                    result[origin] = depth
                    hit_target = True
                    break
                if tgt in visited[origin]:
                    continue
                # Take the first fresh target as the next frontier for this
                # origin. BFS fans out per-origin but stays bounded because we
                # only track one walker per origin; this is a shortest-path
                # approximation, acceptable here because we classify on bucket
                # boundaries (distance 1/2/3), not exact hop counts.
                if next_node is None:
                    next_node = tgt
                    visited[origin].add(tgt)
            if hit_target:
                continue
            if next_node is not None:
                new_frontier[origin] = next_node

        frontier = new_frontier

    for c in caller_ids:
        result.setdefault(c, None)
    return result


# ---------------------------------------------------------------------------
# Ranker implementations
# ---------------------------------------------------------------------------


async def rank_suspect_write(
    db: aiosqlite.Connection,
    neighbors: list[dict[str, Any]],
) -> list[RankedNeighbor]:
    """Rank callers by how suspect they look as write-through-read paths.

    `neighbors` is the output of graph_service.get_neighbors() and is used
    as a metadata snapshot (no re-fetch — data-integrity H3 / race safety).
    """
    if not neighbors:
        return []

    caller_ids = [n["id"] for n in neighbors]
    distances = await compute_distances_batch(db, caller_ids, WRITE_PRIMITIVES)

    ranked: list[RankedNeighbor] = []
    for n in neighbors:
        metadata = n.get("metadata") or {}
        http_verb = metadata.get("http_verb")
        i_raw = intent_score(http_verb)
        v_raw = verb_score(n.get("name"))
        d_hops = distances.get(n["id"])
        d_raw = distance_score(d_hops)

        score = (
            WEIGHT_INTENT * i_raw
            + WEIGHT_VERB * v_raw
            + WEIGHT_DISTANCE * d_raw
        )
        # Clamp defensively — combination can't exceed 1.0 by construction,
        # but clamp absorbs any future weight/prior tweak without surprising
        # downstream consumers.
        score = max(0.0, min(1.0, score))

        ranked.append({
            **n,  # echo every field from get_neighbors
            "score": round(score, 4),
            "classification": classify(score),
            "signals": {
                "intent_score": round(i_raw, 4),
                "verb_score": round(v_raw, 4),
                "distance_score": round(d_raw, 4),
                "http_verb": http_verb,
                "distance_hops": d_hops,
                "weights": {
                    "intent": WEIGHT_INTENT,
                    "verb": WEIGHT_VERB,
                    "distance": WEIGHT_DISTANCE,
                },
            },
        })

    # Most-suspect-first ordering (stable by qualified_name for ties).
    ranked.sort(key=lambda r: (-r["score"], r.get("qualified_name") or ""))
    return ranked


# ---------------------------------------------------------------------------
# Registry — extensibility (Fase 1f will add graph_impact, etc.)
# ---------------------------------------------------------------------------

Ranker = Callable[
    [aiosqlite.Connection, list[dict[str, Any]]],
    Awaitable[list[RankedNeighbor]],
]

RANKERS: dict[str, Ranker] = {
    "suspect_write": rank_suspect_write,
}
