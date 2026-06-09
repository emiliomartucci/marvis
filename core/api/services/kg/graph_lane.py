# v0.1.0 - 2026-06-04 - Track 2 #3a: STRUCTURAL graph-lane for RRF fusion (flag MARVIS_GRAPH_LANE, default OFF)
"""STRUCTURAL graph lane for hybrid-search RRF fusion (Track 2 #3a).

The fusion already has five lanes (semantic + four FTS5). The KG participates
there only as ``kg_fts`` — BM25 over node *names* (LEXICAL-on-nodes), not graph
structure. This module adds the missing STRUCTURAL lane: seed from the
already-computed fused hits, expand one hop (configurable to two) over current
KG edges, score neighbors deterministically (NO walk, NO LLM), and hand the
ranked neighbor list back to the existing ``reciprocal_rank_fusion`` as a sixth
ranking keyed ``kg_expand:<node_id>``.

Cost model — Option (1), seeded edge-weighted expansion (Practical GraphRAG
2507.03226 fuses vector similarity with graph traversal via RRF):

    graph_score(n) = max over paths(
        seed_rrf_weight * EDGE_TYPE_WEIGHT[rel] * PROXIMITY_DECAY**hops
    )

``max-over-paths`` keeps a neighbor reachable by several seeds/edges at its
strongest evidence rather than summing (summing would re-introduce hub
explosion that the per-seed fan-out cap already fights). RRF then re-weights
the lane globally, so the absolute scale of ``graph_score`` is irrelevant —
only the neighbor *ranking* it induces matters.

Everything here is gated by ``settings.graph_lane_enabled`` at the call site
(``hybrid_search``). With the flag OFF this module is never imported into the
fusion path and the fused ranking is byte-identical to the 5-lane default.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence

import aiosqlite

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Pinned 15-entry edge-type weight table (the structural analog of
# DEFAULT_WEIGHTS — the biggest quality lever for the lane). Covers ALL 15
# post-migration-085 edge types. Strong edges express direct knowledge/work
# provenance; weak edges (tag / similarity / loose mention) are recall-only
# and would otherwise flood expansion from high-degree hub nodes.
# ---------------------------------------------------------------------------
EDGE_TYPE_WEIGHT: dict[str, float] = {
    # Strong (~1.0) — direct knowledge / work / code provenance.
    "describes": 1.0,
    "documents": 1.0,
    "produces": 1.0,
    "depends_on": 1.0,
    "calls": 1.0,
    "contains": 1.0,
    # Medium (~0.6) — citation / applicability / reference / module bridge.
    "cites": 0.6,
    "applies_to": 0.6,
    "refers_to": 0.6,
    "resolves_to": 0.6,
    # Weak (~0.2) — tag overlap / similarity / loose mention / structural code.
    "shares_tag": 0.2,
    "similar_to": 0.2,
    "mentions": 0.2,
    "imports": 0.2,
    "defines": 0.2,
}

# 2-hop neighbor reached through average-strength edges is worth ~¼ of a 1-hop
# neighbor — caps blow-up without the per-query cost of PPR / random walks.
PROXIMITY_DECAY = 0.5

# Default edge subset for expansion = all 15 (callers may pass a narrower set).
DEFAULT_EDGE_TYPES: tuple[str, ...] = tuple(EDGE_TYPE_WEIGHT.keys())


def edge_weight(relation: str) -> float:
    """Static edge-type weight; unknown relations contribute nothing (0.0)."""
    return EDGE_TYPE_WEIGHT.get(relation, 0.0)


def score_path(seed_rrf_weight: float, relation: str, hops: int) -> float:
    """Deterministic contribution of ONE path to a neighbor's graph_score.

    ``seed_rrf_weight`` — the seed's own fused RRF score (its standing as a
    starting point). ``relation`` — edge type traversed. ``hops`` — path length
    (1 = direct neighbor). No walk, no LLM: pure arithmetic.
    """
    return seed_rrf_weight * edge_weight(relation) * (PROXIMITY_DECAY ** hops)


def graph_score(
    neighbor_paths: Sequence[tuple[float, str, int]],
) -> float:
    """``max`` over all paths that reach a neighbor (see module docstring).

    ``neighbor_paths`` — ``[(seed_rrf_weight, relation, hops), ...]`` for every
    way this neighbor was reached during expansion. Empty → 0.0.
    """
    if not neighbor_paths:
        return 0.0
    return max(score_path(w, rel, hops) for (w, rel, hops) in neighbor_paths)


def rank_neighbors(
    paths_by_neighbor: Mapping[str, Sequence[tuple[float, str, int]]],
    *,
    exclude: Sequence[str] = (),
) -> list[str]:
    """Rank neighbor node_ids by graph_score descending → the graph lane order.

    ``paths_by_neighbor`` — ``{node_id: [(seed_w, rel, hops), ...]}``.
    ``exclude`` — node_ids to drop (typically the seed ids themselves, so the
    lane surfaces RELATED nodes, not the seeds it already had). Ties broken by
    node_id for a stable, deterministic ranking.
    """
    excluded = set(exclude)
    scored: list[tuple[str, float]] = [
        (nid, graph_score(paths))
        for nid, paths in paths_by_neighbor.items()
        if nid not in excluded
    ]
    scored = [(nid, s) for (nid, s) in scored if s > 0.0]
    scored.sort(key=lambda kv: (-kv[1], kv[0]))
    return [nid for (nid, _s) in scored]


# Fusion-key prefix → KG node-id prefix, for resolving an already-fused hit to
# a seed node. Mirrors the inverse of rrf._KG_PREFIX_TO_DOC_TYPE: only the
# artifact id-spaces that round-trip cleanly are resolvable as seeds.
_FUSION_DOC_TYPE_TO_KG_PREFIX: dict[str, str] = {
    "task": "task",
    "learning": "learning",
    "handoff": "handoff",
    "audit": "audit",
}


def resolve_seed_node_id(fusion_key: str) -> str | None:
    """Resolve an already-fused RRF key to the KG node_id to seed from.

    - ``kg:<node_id>``      → ``<node_id>`` (already a KG node — the lexical KG
      lane hit IS a graph node).
    - ``sem:<doc_type>:<id>`` / ``task_fts:<id>`` / ``learn_fts:<id>`` →
      ``<prefix>:artifact:<id>`` when ``<doc_type>`` is a resolvable artifact
      id-space, else ``None``.
    - ``inbox_fts:<id>`` / unknown / file / project → ``None`` (no clean KG
      node-id twin; not seeded).
    """
    if fusion_key.startswith("kg:"):
        node_id = fusion_key[len("kg:") :]
        return node_id or None
    if fusion_key.startswith("sem:"):
        rest = fusion_key[len("sem:") :]
        parts = rest.split(":", 1)
        if len(parts) != 2:
            return None
        doc_type, doc_id = parts
    elif fusion_key.startswith("task_fts:"):
        doc_type, doc_id = "task", fusion_key[len("task_fts:") :]
    elif fusion_key.startswith("learn_fts:"):
        doc_type, doc_id = "learning", fusion_key[len("learn_fts:") :]
    else:
        return None
    prefix = _FUSION_DOC_TYPE_TO_KG_PREFIX.get(doc_type)
    if prefix is None or not doc_id:
        return None
    return f"{prefix}:artifact:{doc_id}"


async def expand_seeds(
    seed_weights: Mapping[str, float],
    db_path: str,
    *,
    edge_types: Sequence[str] = DEFAULT_EDGE_TYPES,
    fanout: int = 25,
) -> dict[str, list[tuple[float, str, int]]]:
    """One batched 1-hop expansion over CURRENT edges (``valid_until IS NULL``).

    Generalizes ``_batch_edge_paths`` (which hard-codes ``relation='describes'``)
    to a configurable subset of the 15 edge types in ONE ``IN(...)`` query, with
    a per-seed fan-out cap to avoid hub explosion. Honors the bitemporal filter
    (current edges only) exactly like the existing helper.

    Returns ``{neighbor_id: [(seed_rrf_weight, relation, 1), ...]}`` — the raw
    path evidence consumed by ``graph_score`` / ``rank_neighbors``. Outgoing
    direction only (``source_id`` in seeds), matching ``_batch_edge_paths``.

    Pre-migration / unopenable DB / empty seeds degrade to ``{}`` (the lane is
    additive — it must never block the fused result).
    """
    seed_ids = [s for s in seed_weights if s]
    if not seed_ids:
        return {}
    rels = [r for r in edge_types if r in EDGE_TYPE_WEIGHT]
    if not rels:
        return {}
    try:
        conn = await aiosqlite.connect(db_path)
    except Exception:  # pragma: no cover — DB not openable
        logger.warning("graph_lane.expand_seeds: cannot open %s", db_path)
        return {}
    try:
        conn.row_factory = aiosqlite.Row
        seed_ph = ",".join("?" * len(seed_ids))
        rel_ph = ",".join("?" * len(rels))
        try:
            # Per-seed fan-out cap via a window function keeps high-degree hub
            # nodes (popular module, shared tag) from flooding the lane.
            cur = await conn.execute(
                f"""
                SELECT source_id, target_id, relation FROM (
                    SELECT source_id, target_id, relation,
                           ROW_NUMBER() OVER (
                               PARTITION BY source_id ORDER BY target_id
                           ) AS rn
                    FROM graph_edges
                    WHERE source_id IN ({seed_ph})
                      AND relation IN ({rel_ph})
                      AND valid_until IS NULL
                )
                WHERE rn <= ?
                """,
                [*seed_ids, *rels, fanout],
            )
            rows = await cur.fetchall()
        except aiosqlite.OperationalError:
            return {}
        out: dict[str, list[tuple[float, str, int]]] = {}
        for r in rows:
            src = r["source_id"]
            tgt = r["target_id"]
            rel = r["relation"]
            w = float(seed_weights.get(src, 0.0))
            out.setdefault(tgt, []).append((w, rel, 1))
        return out
    finally:
        await conn.close()
