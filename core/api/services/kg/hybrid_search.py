# v0.2.0 - 2026-04-16 - KG Phase 6.8: cross-source dedupe + weight re-tune
# v0.1.0 - 2026-04-16 - KG Phase 6.6: hybrid search extended (5 sources — tasks_fts + inbox_fts + learnings_fts)
# v0.0.0 - 2026-04-16 - KG Phase 6.5 A: hybrid search (semantic + FTS5 KG + RRF fusion)
"""Hybrid search service — fuse semantic (embedding + sqlite-vec) with multiple FTS5 sources.

Phase 6.6 extends the Phase 6.5 pipeline with three additional FTS5
retrievers so that the task / inbox / learning domains participate in
BM25 scoring (fixing the post-Phase-6.5 regression where those three
domains fell through to semantic-only and got crowded out by graph_nodes
BM25 hits in RRF fusion).

Composes five retrievers in parallel via ``asyncio.TaskGroup`` so that
The semantic retriever and the four SQLite FTS5 indices never block each other. Each
branch is wrapped in a ``(result, error)`` helper for graceful
degradation: any single failure returns partial results with a logged
warning, the others still flow through.

Fusion uses weighted Reciprocal Rank Fusion (see ``api/services/kg/rrf.py``).
Default weights keep semantic as the majority signal (0.4) while giving
tasks / learnings / inbox each a dedicated BM25 lane so they are no
longer starved by graph_nodes_fts.

Output shape contract: matches ``api/models/search.SearchHit`` — existing
``doc_type / doc_id / title / project / score / salience / path / status``
plus the Phase 6.5 extensions ``edge_path`` (list of node IDs), ``rrf_score``,
and ``edge_path_summary``.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import re
import sys
from dataclasses import dataclass
from typing import Any

import aiosqlite

from core.api.config import settings
from core.api.services._fts import fts5_safe_query
from core.api.services.kg.rrf import normalize_key, reciprocal_rank_fusion

logger = logging.getLogger(__name__)


async def _filter_learnings_temporal(
    grouped: dict[str, list[dict]],
    db_path: str,
    as_of: str | None,
) -> None:
    """Track 2 #1-S2: drop superseded learnings from the fused result, IN PLACE.

    The learning lane reaches ``grouped["learning"]`` via TWO retrievers — the
    semantic vec/kNN lane (``sem:learning:<id>``) and the lexical ``learn_fts``
    lane — both keyed by the learning ``doc_id``. Neither carries ``invalid_at``,
    so we resolve liveness with ONE post-fusion SQL probe against the ``learnings``
    table (the "temporal filter applied in the SQL join AFTER the kNN" the spec
    calls for): the vector is never dropped, and because this runs AFTER RRF —
    where salience/recency already ordered the hits — a tombstone cannot leak back
    into top-k through ranking.

    MECHANICAL + BINARY exclusion (never a down-weight). No-op (byte-identical
    output) when ``MARVIS_TEMPORAL_MEMORY`` is OFF or there are no learning hits.
    ``as_of`` relaxes the live filter to the point-in-time window. Fail-open: any
    DB error leaves the result unchanged rather than degrading search.
    """
    if not settings.temporal_memory_enabled:
        return
    hits = grouped.get("learning") or []
    if not hits:
        return

    doc_ids = [h["doc_id"] for h in hits if h.get("doc_id")]
    if not doc_ids:
        return

    if as_of is None:
        temporal_sql, temporal_params = "AND invalid_at IS NULL", []
    else:
        temporal_sql, temporal_params = (
            "AND valid_from <= ? AND (invalid_at IS NULL OR invalid_at > ?)",
            [as_of, as_of],
        )

    placeholders = ",".join("?" * len(doc_ids))
    sql = (
        f"SELECT id FROM learnings WHERE id IN ({placeholders}) {temporal_sql}"
    )
    try:
        conn = await aiosqlite.connect(db_path)
    except Exception:  # noqa: BLE001 — cannot probe → leave result unchanged
        logger.warning("temporal learning filter: cannot open %s", db_path)
        return
    try:
        conn.row_factory = aiosqlite.Row
        try:
            cur = await conn.execute(sql, [*doc_ids, *temporal_params])
            rows = await cur.fetchall()
        except aiosqlite.OperationalError as exc:
            # Pre-migration (no invalid_at/valid_from columns) → fail open.
            logger.warning("temporal learning filter degraded: %s", exc)
            return
        live_ids = {r["id"] for r in rows}
    finally:
        await conn.close()

    grouped["learning"] = [h for h in hits if h.get("doc_id") in live_ids]

# Default per-source weights — Phase 6.8 re-tune.
#
# Phase 6.6 shipped with semantic=0.40 / tasks_fts=0.15. The live probe on
# "iperammortamento" then revealed that semantic cosine was handing a score
# of ~0.40 to ~14 loosely-related learnings (merge / PR / deploy chatter)
# that share NO lexical overlap with the query. At semantic=0.40 >>
# tasks_fts=0.15 those learnings dominated the top-20 fused output even
# though 21 tasks had a perfect tasks_fts MATCH.
#
# Re-tune rationale:
#   * semantic (0.4 → 0.3): -0.10. Still meaningful cross-domain coverage
#     but no longer strong enough alone to bury a lexically perfect task.
#   * kg_fts (0.3 → 0.25): -0.05. Keeps parity with tasks_fts — graph_nodes
#     BM25 and tasks_fts BM25 are both lexical; there is no principled
#     reason for one FTS5 lane to outweigh the other.
#   * tasks_fts (0.15 → 0.25): +0.10. Elevated to parity with kg_fts so
#     task recall matches file recall on lexical queries.
#   * inbox_fts (0.05 → 0.10): +0.05. Inbox content has rich lexical
#     material (newsletter titles, article bodies) — the 0.05 tie-breaker
#     role under-used the signal.
#   * learnings_fts (0.10 → 0.10): unchanged. Low-cardinality, modest
#     weight still protects top-N from learning floods.
#
# Phase 6.9 adds documents_fts as an independent lexical lane. Before this,
# documents_fts only ran inside the semantic service, so search returned zero
# when embeddings were runtime-gated even though BM25 had an exact body match.
#
# Total = 0.25 + 0.20 + 0.10 + 0.25 + 0.10 + 0.10 = 1.00 (gated by
# test_fusion_weights_default_sum_to_one).
# Mutable per-call via the ``weights`` kwarg for future tuning.
DEFAULT_WEIGHTS: dict[str, float] = {
    "semantic": 0.25,
    "kg_fts": 0.20,
    "documents_fts": 0.10,
    "tasks_fts": 0.25,
    "learnings_fts": 0.10,
    "inbox_fts": 0.10,
}
DEFAULT_RRF_K = 60
DEFAULT_SEMANTIC_BRANCH_TIMEOUT_SECONDS = 4.0

# Map graph_node.type → search doc_type bucket (SearchResponse grouping).
_NODE_TYPE_TO_DOC_TYPE: dict[str, str] = {
    "handoff": "handoff",
    "learning": "learning",
    "task": "task",
    "solution": "file",
    "plan": "file",
    "brainstorm": "file",
    "audit": "audit",
    "spike": "file",
    "analysis": "file",
    "research": "file",
    "rubric": "file",
    "guide": "file",
    "mockup": "file",
    "file": "file",
    "function": "file",
    "module": "file",
    "class": "file",
}


@dataclass(frozen=True, slots=True)
class KgFtsHit:
    """Single hit from FTS5 KG retriever (graph_nodes_fts)."""

    node_id: str
    node_type: str
    name: str
    project_id: str
    bm25_score: float


@dataclass(frozen=True, slots=True)
class RowFtsHit:
    """Single hit from a row-mirroring FTS5 retriever (tasks / inbox / learnings).

    Generic container reused for all three new Phase 6.6 sources so the
    fusion layer can treat them uniformly. ``source`` records which FTS5
    table produced the hit for downstream edge_path_summary labeling.
    """

    doc_id: str
    title: str
    project: str
    status: str
    bm25_score: float
    source: str  # "task" | "inbox_item" | "learning"


@dataclass(frozen=True, slots=True)
class DocumentFtsHit:
    """Single hit from documents_fts, already resolved to a documents row."""

    doc_id: str
    doc_type: str
    title: str
    project: str
    path: str
    salience: float
    bm25_score: float
    span_text: str | None = None
    span_path: str | None = None
    span_line_start: int | None = None
    span_line_end: int | None = None


_QUERY_LITERAL_RE = re.compile(r'"([^"]+)"|[\w./:@#-]+', re.UNICODE)
_FTS_OPERATORS = {"and", "or", "not", "near"}
_DOCUMENT_SPAN_READ_MAX_BYTES = 500_000


def _literal_terms_for_span(q: str) -> list[str]:
    """Extract deterministic literal terms that can anchor a document FTS span."""
    terms: list[str] = []
    whole = q.strip().strip('"')
    if whole:
        terms.append(whole)
    for match in _QUERY_LITERAL_RE.finditer(q):
        term = (match.group(1) or match.group(0) or "").strip().strip('"')
        if not term or term.lower() in _FTS_OPERATORS:
            continue
        terms.append(term)

    seen: set[str] = set()
    out: list[str] = []
    for term in sorted(terms, key=len, reverse=True):
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(term)
    return out


def _find_literal_span(raw: bytes, q: str) -> tuple[int, int] | None:
    """Find the first exact-ish query term in raw UTF-8 bytes."""
    raw_lower = raw.lower()
    for term in _literal_terms_for_span(q):
        needle = term.encode("utf-8", "ignore")
        if not needle:
            continue
        idx = raw.find(needle)
        if idx < 0:
            idx = raw_lower.find(needle.lower())
        if idx >= 0:
            return idx, idx + len(needle)
    return None


async def _span_fields_for_file_match(path: str, q: str) -> dict[str, object]:
    """Return span fields for an exact document-FTS match, fail-soft."""

    def _read() -> bytes | None:
        try:
            p = Path(path)
            if (
                not path.startswith("/")
                or not p.is_file()
                or p.is_symlink()
                or p.stat().st_size > _DOCUMENT_SPAN_READ_MAX_BYTES
            ):
                return None
            return p.read_bytes()
        except OSError:
            return None

    raw = await asyncio.to_thread(_read)
    if raw is None:
        return {}
    span = _find_literal_span(raw, q)
    if span is None:
        return {}

    from core.api.services import embedding_service

    text, line_start, line_end = embedding_service.expand_span_to_window(
        raw,
        span[0],
        span[1],
    )
    if not text.strip():
        return {}
    return {
        "span_text": text,
        "span_path": path,
        "span_line_start": line_start,
        "span_line_end": line_end,
    }


def _apply_document_span(enriched: dict[str, Any], hit: DocumentFtsHit | None) -> None:
    """Prefer exact lexical evidence over diluted semantic chunk anchors."""
    if hit is None or not hit.span_text:
        return
    enriched["span_text"] = hit.span_text
    enriched["span_path"] = hit.span_path
    enriched["span_line_start"] = hit.span_line_start
    enriched["span_line_end"] = hit.span_line_end


async def kg_full_text_search(
    q: str,
    db_path: str,
    limit: int = 40,
) -> list[KgFtsHit]:
    """Run MATCH over graph_nodes_fts and return ranked hits.

    Returns ordered-by-bm25 (smaller = better in SQLite FTS5 convention),
    capped to ``limit``. Non-existent FTS table (pre-migration 078) degrades
    to empty list.
    """
    q_fts = fts5_safe_query(q)
    if not q_fts:
        return []
    try:
        conn = await aiosqlite.connect(db_path)
    except Exception:  # pragma: no cover — DB not openable
        logger.warning("kg_full_text_search: cannot open %s", db_path)
        return []
    try:
        conn.row_factory = aiosqlite.Row
        try:
            cur = await conn.execute(
                """
                SELECT id, type, name, project_id, bm25(graph_nodes_fts) AS score
                FROM graph_nodes_fts
                WHERE graph_nodes_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                [q_fts, limit],
            )
            rows = await cur.fetchall()
        except aiosqlite.OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" in msg or "syntax" in msg or "no such column" in msg:
                # Pre-migration state or user query with FTS5-hostile chars.
                return []
            raise
        return [
            KgFtsHit(
                node_id=r["id"],
                node_type=r["type"] or "",
                name=r["name"] or "",
                project_id=r["project_id"] or "",
                bm25_score=float(r["score"] or 0.0),
            )
            for r in rows
        ]
    finally:
        await conn.close()


async def _row_fts_search(
    q: str,
    db_path: str,
    *,
    fts_table: str,
    title_col: str,
    status_col: str | None,
    source_label: str,
    limit: int,
) -> list[RowFtsHit]:
    """Generic MATCH over a row-mirroring FTS5 table (tasks/inbox/learnings).

    The three Phase 6.6 FTS5 virtual tables share the shape ``(id UNINDEXED,
    title, ..., project UNINDEXED, <extra UNINDEXED>)``. The `<extra>` slot
    differs across tables: tasks/inbox store ``status``, learnings stores
    ``severity``. Callers pass ``status_col`` to map whichever column should
    populate ``RowFtsHit.status`` (or ``None`` to omit). Pre-migration state
    or empty/unsafe queries gracefully degrade to an empty list.
    """
    if not q.strip():
        return []
    try:
        conn = await aiosqlite.connect(db_path)
    except Exception:  # pragma: no cover — DB not openable
        logger.warning("_row_fts_search(%s): cannot open %s", fts_table, db_path)
        return []
    try:
        conn.row_factory = aiosqlite.Row
        try:
            # fts_table / title_col / status_col are internal callers; not user input.
            status_select = f", {status_col} AS status" if status_col else ", '' AS status"
            sql = (
                f"SELECT id, {title_col} AS title, project{status_select}, "
                f"bm25({fts_table}) AS score "
                f"FROM {fts_table} "
                f"WHERE {fts_table} MATCH ? "
                f"ORDER BY score "
                f"LIMIT ?"
            )
            q_fts = fts5_safe_query(q)
            if not q_fts:
                return []
            cur = await conn.execute(sql, [q_fts, limit])
            rows = await cur.fetchall()
        except aiosqlite.OperationalError as exc:
            msg = str(exc).lower()
            if "no such table" in msg or "syntax" in msg or "no such column" in msg:
                return []
            raise
        return [
            RowFtsHit(
                doc_id=r["id"],
                title=r["title"] or "",
                project=r["project"] or "",
                status=(r["status"] or "") if status_col else "",
                bm25_score=float(r["score"] or 0.0),
                source=source_label,
            )
            for r in rows
        ]
    finally:
        await conn.close()


async def tasks_fts_search(q: str, db_path: str, limit: int = 40) -> list[RowFtsHit]:
    """MATCH over tasks_fts (Phase 6.6). Fixes post-6.5 task recall regression."""
    return await _row_fts_search(
        q,
        db_path,
        fts_table="tasks_fts",
        title_col="title",
        status_col="status",
        source_label="task",
        limit=limit,
    )


async def inbox_fts_search(q: str, db_path: str, limit: int = 40) -> list[RowFtsHit]:
    """MATCH over inbox_items_fts (Phase 6.6)."""
    return await _row_fts_search(
        q,
        db_path,
        fts_table="inbox_items_fts",
        title_col="title",
        status_col="status",
        source_label="inbox_item",
        limit=limit,
    )


async def learnings_fts_search(
    q: str, db_path: str, limit: int = 40
) -> list[RowFtsHit]:
    """MATCH over learnings_fts (Phase 6.6).

    learnings_fts stores ``severity`` instead of ``status`` so we project
    severity into the ``RowFtsHit.status`` slot; downstream consumers treat
    it as a string badge either way.
    """
    return await _row_fts_search(
        q,
        db_path,
        fts_table="learnings_fts",
        title_col="title",
        status_col="severity",
        source_label="learning",
        limit=limit,
    )


async def documents_fts_search(
    q: str,
    db_path: str,
    workspace_id: str,
    limit: int = 40,
) -> list[DocumentFtsHit]:
    """MATCH over documents_fts without requiring the embedding backend."""
    try:
        conn = await aiosqlite.connect(db_path)
    except Exception:  # pragma: no cover — DB not openable
        logger.warning("documents_fts_search: cannot open %s", db_path)
        return []
    try:
        conn.row_factory = aiosqlite.Row
        from core.api.services import embedding_service

        bm25_hits = await embedding_service._bm25_documents_search(
            conn,
            q,
            limit=limit,
        )
        rows_by_doc_id = await embedding_service._fetch_document_rows(
            conn,
            [hit.doc_id for hit in bm25_hits],
            workspace_id,
        )
        hits: list[DocumentFtsHit] = []
        for hit in bm25_hits:
            row = rows_by_doc_id.get(hit.doc_id)
            if row is None:
                continue
            base = embedding_service._base_result_item(row)
            span_fields = await _span_fields_for_file_match(str(base["path"]), q)
            hits.append(
                DocumentFtsHit(
                    doc_id=str(base["doc_id"]),
                    doc_type=str(row.get("doc_type") or "file"),
                    title=str(base["title"]),
                    project=str(base["project"]),
                    path=str(base["path"]),
                    salience=float(base["salience"]),
                    bm25_score=hit.score,
                    span_text=span_fields.get("span_text") if span_fields else None,
                    span_path=span_fields.get("span_path") if span_fields else None,
                    span_line_start=span_fields.get("span_line_start") if span_fields else None,
                    span_line_end=span_fields.get("span_line_end") if span_fields else None,
                )
            )
        return hits
    finally:
        await conn.close()


async def _batch_edge_paths(
    node_ids: list[str],
    db_path: str,
) -> dict[str, list[str]]:
    """Single IN(...) query to fetch describes-edge neighbors for each node.

    Avoids N+1. Only returns direct outgoing `describes` edges (valid_until
    IS NULL). The caller uses this to build a structured edge_path.
    """
    if not node_ids:
        return {}
    conn = await aiosqlite.connect(db_path)
    try:
        conn.row_factory = aiosqlite.Row
        placeholders = ",".join("?" * len(node_ids))
        try:
            cur = await conn.execute(
                f"""
                SELECT source_id, target_id
                FROM graph_edges
                WHERE source_id IN ({placeholders})
                  AND relation = 'describes'
                  AND valid_until IS NULL
                """,
                node_ids,
            )
            rows = await cur.fetchall()
        except aiosqlite.OperationalError:
            return {}
        out: dict[str, list[str]] = {}
        for r in rows:
            out.setdefault(r["source_id"], []).append(r["target_id"])
        return out
    finally:
        await conn.close()


def _summary_from_path(path: list[str]) -> str:
    """Short LLM-friendly path label: types inferred from node id prefixes."""
    parts: list[str] = []
    for nid in path:
        # Node id convention: <type>:<subtype>:<slug>
        head = nid.split(":", 1)[0] if ":" in nid else "node"
        parts.append(head)
    return " -> ".join(parts)


def _semantic_branch_timeout_seconds() -> float:
    raw = os.environ.get("SEARCH_SEMANTIC_BRANCH_TIMEOUT_SECONDS")
    if not raw:
        return DEFAULT_SEMANTIC_BRANCH_TIMEOUT_SECONDS
    try:
        return max(float(raw), 0.1)
    except ValueError:
        logger.warning("Invalid SEARCH_SEMANTIC_BRANCH_TIMEOUT_SECONDS=%r", raw)
        return DEFAULT_SEMANTIC_BRANCH_TIMEOUT_SECONDS


async def _run_semantic_branch(
    q: str,
    workspace_id: str,
    db_path: str,
    vec0_path: str,
    top_k: int,
) -> tuple[dict[str, list[dict]] | None, BaseException | None]:
    """Thin wrapper with (result, error) semantics for graceful degradation."""
    try:
        from core.api.services import embedding_service

        if not embedding_service.is_available():
            return None, RuntimeError("semantic-embedding-unavailable")
        grouped = await embedding_service.search_by_type(
            query=q,
            workspace_id=workspace_id,
            db_path=db_path,
            vec0_path=vec0_path,
            top_k=top_k,
        )
        return grouped, None
    except BaseException as exc:  # noqa: BLE001 — intentional: degrade gracefully
        logger.warning("hybrid.semantic_branch failed: %s", exc)
        return None, exc


async def _run_semantic_branch_with_timeout(
    q: str,
    workspace_id: str,
    db_path: str,
    vec0_path: str,
    top_k: int,
) -> tuple[dict[str, list[dict]] | None, BaseException | None]:
    timeout = _semantic_branch_timeout_seconds()
    try:
        return await asyncio.wait_for(
            _run_semantic_branch(q, workspace_id, db_path, vec0_path, top_k),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("hybrid.semantic_branch timed out after %.2fs", timeout)
        return None, RuntimeError(f"semantic-embedding-timeout:{timeout:.2f}s")


def _semantic_reason(sem_err: BaseException | None) -> str | None:
    """Classify a dead semantic lane into a SANITIZED enum for the API surface.

    F1: the surfaced reason must NEVER be the raw ``_load_error`` string — it can
    embed a filesystem path or backend name (OSS no-leak rule). The raw error stays
    in server logs only; here we map it to one of three coarse, leak-free buckets.
    """
    if sem_err is None:
        return None
    raw = ""
    try:
        from core.api.services import embedding_service

        raw = (embedding_service.load_error_message() or "")
    except Exception:  # noqa: BLE001 — classification is best-effort
        raw = ""
    blob = f"{raw} {sem_err}".lower()
    if "timeout" in blob:
        return "semantic-timeout"
    if "vec0" in blob or "sqlite-vec" in blob or "sqlite_vec" in blob or "vec_documents" in blob:
        return "vec0-not-loadable"
    if "model" in blob or "onnx" in blob or "granite" in blob or "tokeniz" in blob:
        return "model-not-loadable"
    return "runtime-gate"


async def _run_kg_branch(
    q: str,
    db_path: str,
    limit: int,
) -> tuple[list[KgFtsHit] | None, BaseException | None]:
    try:
        return await kg_full_text_search(q, db_path, limit=limit), None
    except BaseException as exc:  # noqa: BLE001
        logger.warning("hybrid.kg_branch failed: %s", exc)
        return None, exc


async def _run_row_fts_branch(
    fn: Any,
    name: str,
    q: str,
    db_path: str,
    limit: int,
) -> tuple[list[RowFtsHit] | None, BaseException | None]:
    """Generic (result, error) wrapper for the three Phase 6.6 row-FTS branches."""
    try:
        return await fn(q, db_path, limit=limit), None
    except BaseException as exc:  # noqa: BLE001
        logger.warning("hybrid.%s_branch failed: %s", name, exc)
        return None, exc


async def _run_documents_fts_branch(
    q: str,
    db_path: str,
    workspace_id: str,
    limit: int,
) -> tuple[list[DocumentFtsHit] | None, BaseException | None]:
    try:
        return await documents_fts_search(q, db_path, workspace_id, limit=limit), None
    except BaseException as exc:  # noqa: BLE001
        logger.warning("hybrid.documents_branch failed: %s", exc)
        return None, exc


async def hybrid_search(
    q: str,
    workspace_id: str,
    db_path: str,
    vec0_path: str,
    limit: int = 20,
    rrf_k: int = DEFAULT_RRF_K,
    weights: dict[str, float] | None = None,
    *,
    graph_lane: bool = False,
    graph_lane_weight: float = 0.12,
    graph_lane_seeds: int = 10,
    graph_lane_fanout: int = 25,
    as_of: str | None = None,
) -> tuple[dict[str, list[dict]], dict[str, object]]:
    """Run semantic + 4x FTS5 retrievers in parallel, fuse via weighted RRF.

    Phase 6.6 extends Phase 6.5 by adding tasks_fts, inbox_items_fts, and
    learnings_fts so task/inbox/learning domains participate in BM25 scoring
    alongside graph_nodes_fts. This removes the task-recall regression
    where semantic-only cosine scores got dominated by graph_nodes BM25.

    Track 2 #3a adds a sixth, STRUCTURAL lane behind ``graph_lane`` (DEFAULT
    OFF → fusion byte-identical to the 5-lane path). When enabled it seeds from
    the already-computed fused hits, expands one hop over current KG edges,
    scores neighbors deterministically, and joins the ONE RRF blend as
    ``kg_expand_lane`` (keys ``kg_expand:<node_id>``). The lane weight is
    stolen from semantic+kg_fts so the six weights still sum to 1.0.

    Returns (grouped_hits_by_doc_type, meta):
        grouped_hits_by_doc_type — {"task": [...], "project": [...], ...}
            matching ``SearchResponse`` grouping; each hit dict includes
            ``edge_path``, ``edge_path_summary``, ``rrf_score`` fields.
        meta — diagnostics: per-source availability flags, fused_total,
            chosen_k, suggested_next_tool (None unless empty).

    Gracefully degrades when any branch fails (returns the others' hits).
    If all branches fail → empty grouping with suggested_next_tool populated.
    """
    weights = weights if weights is not None else dict(DEFAULT_WEIGHTS)
    # fetch larger N from each retriever so RRF has enough to fuse.
    fetch_n = max(limit * 2, 20)

    # Parallel retrievers with structured concurrency.
    if sys.version_info >= (3, 11):
        async with asyncio.TaskGroup() as tg:
            sem_task = tg.create_task(
                _run_semantic_branch_with_timeout(q, workspace_id, db_path, vec0_path, fetch_n)
            )
            kg_task = tg.create_task(_run_kg_branch(q, db_path, fetch_n))
            docs_task = tg.create_task(
                _run_documents_fts_branch(q, db_path, workspace_id, fetch_n)
            )
            tasks_task = tg.create_task(
                _run_row_fts_branch(tasks_fts_search, "tasks", q, db_path, fetch_n)
            )
            inbox_task = tg.create_task(
                _run_row_fts_branch(inbox_fts_search, "inbox", q, db_path, fetch_n)
            )
            learn_task = tg.create_task(
                _run_row_fts_branch(
                    learnings_fts_search, "learnings", q, db_path, fetch_n
                )
            )
        sem_result, sem_err = sem_task.result()
        kg_hits, kg_err = kg_task.result()
        docs_hits, docs_err = docs_task.result()
        tasks_hits, tasks_err = tasks_task.result()
        inbox_hits, inbox_err = inbox_task.result()
        learn_hits, learn_err = learn_task.result()
    else:  # pragma: no cover — 3.10 fallback, keep source compatible.
        sem_result, sem_err = await _run_semantic_branch_with_timeout(
            q, workspace_id, db_path, vec0_path, fetch_n
        )
        kg_hits, kg_err = await _run_kg_branch(q, db_path, fetch_n)
        docs_hits, docs_err = await _run_documents_fts_branch(
            q, db_path, workspace_id, fetch_n
        )
        tasks_hits, tasks_err = await _run_row_fts_branch(
            tasks_fts_search, "tasks", q, db_path, fetch_n
        )
        inbox_hits, inbox_err = await _run_row_fts_branch(
            inbox_fts_search, "inbox", q, db_path, fetch_n
        )
        learn_hits, learn_err = await _run_row_fts_branch(
            learnings_fts_search, "learnings", q, db_path, fetch_n
        )

    # Flatten semantic hits into a ranked list of (doc_type, doc_dict) pairs.
    semantic_ranking: list[tuple[str, dict]] = []
    if sem_result:
        # search_by_type already returns sorted top-N per bucket; flatten.
        for dt, items in sem_result.items():
            for it in items:
                semantic_ranking.append((dt, it))
        # Stable sort by score desc so the ranking reflects relevance.
        semantic_ranking.sort(key=lambda p: p[1].get("score", 0.0), reverse=True)

    kg_ranking: list[KgFtsHit] = list(kg_hits or [])
    docs_ranking: list[DocumentFtsHit] = list(docs_hits or [])
    tasks_ranking: list[RowFtsHit] = list(tasks_hits or [])
    inbox_ranking: list[RowFtsHit] = list(inbox_hits or [])
    learn_ranking: list[RowFtsHit] = list(learn_hits or [])

    # Build RRF input. Keys are namespaced so a doc reached by multiple
    # retrievers does not accidentally collide across sources.
    # For row-FTS sources, we namespace by source-label so downstream
    # dedupe can resolve (doc_type, doc_id) identity before emitting.
    sem_keys = [f"sem:{dt}:{it['doc_id']}" for dt, it in semantic_ranking]
    kg_keys = [f"kg:{h.node_id}" for h in kg_ranking]
    docs_keys = [f"doc_fts:{h.doc_type}:{h.doc_id}" for h in docs_ranking]
    tasks_keys = [f"task_fts:{h.doc_id}" for h in tasks_ranking]
    inbox_keys = [f"inbox_fts:{h.doc_id}" for h in inbox_ranking]
    learn_keys = [f"learn_fts:{h.doc_id}" for h in learn_ranking]

    base_rankings: dict[str, list[str]] = {
        "semantic": sem_keys,
        "kg_fts": kg_keys,
        "documents_fts": docs_keys,
        "tasks_fts": tasks_keys,
        "inbox_fts": inbox_keys,
        "learnings_fts": learn_keys,
    }
    fused = reciprocal_rank_fusion(
        rankings=base_rankings,
        weights=weights,
        k=rrf_k,
    )

    # Track 2 #3a — STRUCTURAL graph lane (behind ``graph_lane`` flag).
    #
    # Seed from the ALREADY-computed fused hits (no second retrieval), resolve
    # each to a KG node, expand one hop over CURRENT edges, score neighbors
    # deterministically, and join the ONE RRF blend as a sixth lane. The lane
    # weight is stolen from semantic+kg_fts so the six weights still sum to
    # 1.0. With the flag OFF this block is skipped entirely → fusion identical
    # to the 5-lane default.
    kg_expand_meta: dict[str, object] = {"graph_lane_enabled": graph_lane}
    if graph_lane and fused:
        from core.api.services.kg import graph_lane as _gl

        # Seed = top-s fused hits, each weighted by its own fused RRF score.
        seed_weights: dict[str, float] = {}
        for fr in fused[:graph_lane_seeds]:
            node_id = _gl.resolve_seed_node_id(fr.item)
            if node_id is None:
                continue
            # max — a node reachable from several fused keys keeps its strongest.
            seed_weights[node_id] = max(seed_weights.get(node_id, 0.0), fr.score)

        paths_by_neighbor = await _gl.expand_seeds(
            seed_weights,
            db_path,
            edge_types=_gl.DEFAULT_EDGE_TYPES,
            fanout=graph_lane_fanout,
        )
        # Rank neighbors EXCLUDING the seeds themselves (lane surfaces RELATED
        # nodes, not the seeds it already had via lexical/semantic lanes).
        kg_expand_order = _gl.rank_neighbors(
            paths_by_neighbor, exclude=list(seed_weights)
        )
        kg_expand_keys = [f"kg_expand:{nid}" for nid in kg_expand_order]
        kg_expand_meta["graph_lane_seeds_used"] = len(seed_weights)
        kg_expand_meta["graph_lane_neighbors"] = len(kg_expand_keys)

        if kg_expand_keys:
            # Re-tune to sum 1.0: steal ``graph_lane_weight`` proportionally
            # from semantic + kg_fts (the two strongest lanes), per spec §5.
            fused_weights = dict(weights)
            steal_from = [
                s for s in ("semantic", "kg_fts") if s in fused_weights
            ]
            if steal_from:
                per = graph_lane_weight / len(steal_from)
                for s in steal_from:
                    fused_weights[s] = max(0.0, fused_weights[s] - per)
            fused_weights["kg_expand_lane"] = graph_lane_weight
            fused = reciprocal_rank_fusion(
                rankings={**base_rankings, "kg_expand_lane": kg_expand_keys},
                weights=fused_weights,
                k=rrf_k,
            )

    # Phase 6.8 — cross-source dedupe.
    #
    # When a task (or inbox_item / learning) is surfaced by BOTH the semantic
    # lane ("sem:task:<uuid>") AND its row-FTS lane ("task_fts:<uuid>"), the
    # pre-6.8 code emitted two separate entries in `fused`. Each consumed a
    # slot in top-N and their contributions never added up — a task with
    # perfect lexical MATCH + decent semantic similarity could still lose to
    # 14 loosely-related learnings.
    #
    # Fix: after RRF, merge entries that share the same canonical
    # `(doc_type, doc_id)` identity (per ``normalize_key``). We keep the
    # HIGHEST-ranked namespaced key as the "winner" (its source-specific
    # enrichment, e.g. edge_path_summary, is what the grouping layer will
    # display), but we SUM the score contributions from all its twins so
    # the canonical entry bubbles up.
    #
    # kg:* keys never dedupe — kg nodes have their own id-space distinct
    # from semantic doc_ids (see docstring on ``rrf.normalize_key``).
    seen_canonical: dict[tuple[str, str], int] = {}
    merged_fused: list[Any] = []
    for fr in fused:
        canonical = normalize_key(fr.item)
        if canonical is None:
            merged_fused.append(fr)
            continue
        if canonical in seen_canonical:
            winner_idx = seen_canonical[canonical]
            winner = merged_fused[winner_idx]
            # NamedTuple is immutable; rebuild with summed score.
            merged_sources = list(dict.fromkeys(winner.sources + fr.sources))
            merged_fused[winner_idx] = winner._replace(
                score=winner.score + fr.score,
                sources=merged_sources,
            )
        else:
            seen_canonical[canonical] = len(merged_fused)
            merged_fused.append(fr)
    # Re-sort after merges — summed scores may overtake other entries.
    merged_fused.sort(key=lambda r: r.score, reverse=True)
    fused = merged_fused

    # Batch edge_path lookup for graph_nodes hits we actually keep.
    kept_kg_ids = [h.node_id for h in kg_ranking[:limit]]  # heuristic cap
    edges_map = await _batch_edge_paths(kept_kg_ids, db_path)

    # Index per-source lookups by key for fast retrieval after fusion.
    sem_by_key: dict[str, tuple[str, dict]] = {
        f"sem:{dt}:{it['doc_id']}": (dt, it) for dt, it in semantic_ranking
    }
    kg_by_key: dict[str, KgFtsHit] = {f"kg:{h.node_id}": h for h in kg_ranking}
    docs_by_key: dict[str, DocumentFtsHit] = {
        f"doc_fts:{h.doc_type}:{h.doc_id}": h for h in docs_ranking
    }
    docs_by_canonical: dict[tuple[str, str], DocumentFtsHit] = {
        (h.doc_type, h.doc_id): h for h in docs_ranking
    }
    tasks_by_key: dict[str, RowFtsHit] = {
        f"task_fts:{h.doc_id}": h for h in tasks_ranking
    }
    inbox_by_key: dict[str, RowFtsHit] = {
        f"inbox_fts:{h.doc_id}": h for h in inbox_ranking
    }
    learn_by_key: dict[str, RowFtsHit] = {
        f"learn_fts:{h.doc_id}": h for h in learn_ranking
    }

    # Map row-FTS source labels → SearchResponse grouping bucket.
    _ROW_FTS_BUCKET: dict[str, str] = {
        "task": "task",
        "inbox_item": "inbox_item",
        "learning": "learning",
    }

    grouped: dict[str, list[dict]] = {
        "task": [],
        "project": [],
        "file": [],
        "handoff": [],
        "learning": [],
        "inbox_item": [],
        "audit": [],
    }
    seen: set[tuple[str, str]] = set()  # (doc_type, doc_id) dedupe

    def _total() -> int:
        return sum(len(v) for v in grouped.values())

    for fr in fused:
        if _total() >= limit:
            break
        key = fr.item
        if key.startswith("sem:") and key in sem_by_key:
            dt, it = sem_by_key[key]
            ded = (dt, it["doc_id"])
            if ded in seen:
                continue
            seen.add(ded)
            enriched = dict(it)
            enriched["rrf_score"] = fr.score
            enriched["edge_path"] = None
            enriched["edge_path_summary"] = None
            _apply_document_span(
                enriched,
                docs_by_canonical.get((dt, str(it["doc_id"]))),
            )
            if dt in grouped:
                grouped[dt].append(enriched)
        elif key.startswith("kg:") and key in kg_by_key:
            hit = kg_by_key[key]
            dt = _NODE_TYPE_TO_DOC_TYPE.get(hit.node_type, "file")
            doc_id = hit.node_id
            ded = (dt, doc_id)
            if ded in seen:
                continue
            seen.add(ded)
            edge_targets = edges_map.get(hit.node_id, [])
            path_ids = [hit.node_id] + edge_targets[:1]  # 1-hop for now
            enriched = {
                "doc_id": doc_id,
                "title": hit.name or doc_id,
                "project": hit.project_id,
                "path": None,
                "score": 0.5,  # bm25-derived scale differs; approximate display
                "salience": 0.5,
                "rrf_score": fr.score,
                "edge_path": path_ids,
                "edge_path_summary": _summary_from_path(path_ids),
            }
            if dt in grouped:
                grouped[dt].append(enriched)
        elif key.startswith("doc_fts:") and key in docs_by_key:
            hit = docs_by_key[key]
            dt = hit.doc_type if hit.doc_type in grouped else "file"
            ded = (dt, hit.doc_id)
            if ded in seen:
                continue
            seen.add(ded)
            enriched = {
                "doc_id": hit.doc_id,
                "title": hit.title or hit.doc_id,
                "project": hit.project,
                "path": hit.path,
                "score": 0.5,
                "salience": hit.salience,
                "rrf_score": fr.score,
                "rank_bm25": None,
                "edge_path": None,
                "edge_path_summary": "document (fts)",
            }
            _apply_document_span(enriched, hit)
            grouped[dt].append(enriched)
        elif key.startswith("kg_expand:"):
            # Track 2 #3a — a STRUCTURAL neighbor that survived dedupe (i.e. has
            # NO semantic/FTS doc twin; the twin case already merged onto its
            # canonical key via normalize_key). Emit as a relational hit so
            # "what depends on X" style answers surface graph-only nodes.
            node_id = key[len("kg_expand:") :]
            prefix = node_id.split(":", 1)[0] if ":" in node_id else "node"
            dt = _NODE_TYPE_TO_DOC_TYPE.get(prefix, "file")
            ded = (dt, node_id)
            if ded in seen:
                continue
            seen.add(ded)
            enriched = {
                "doc_id": node_id,
                "title": node_id,
                "project": "",
                "path": None,
                "score": 0.5,  # display only — RRF is the sole cross-lane order
                "salience": 0.5,
                "rrf_score": fr.score,
                "edge_path": [node_id],
                "edge_path_summary": "kg (structural)",
            }
            if dt in grouped:
                grouped[dt].append(enriched)
        else:
            # Row-FTS branches share the same shape; dispatch by prefix.
            row_hit: RowFtsHit | None = None
            if key.startswith("task_fts:"):
                row_hit = tasks_by_key.get(key)
            elif key.startswith("inbox_fts:"):
                row_hit = inbox_by_key.get(key)
            elif key.startswith("learn_fts:"):
                row_hit = learn_by_key.get(key)
            if row_hit is None:
                continue
            dt = _ROW_FTS_BUCKET.get(row_hit.source, "file")
            ded = (dt, row_hit.doc_id)
            if ded in seen:
                continue
            seen.add(ded)
            enriched = {
                "doc_id": row_hit.doc_id,
                "title": row_hit.title or row_hit.doc_id,
                "project": row_hit.project,
                "path": None,
                "score": 0.5,
                "salience": 0.5,
                "status": row_hit.status or None,
                "rrf_score": fr.score,
                "edge_path": None,
                # Label the fusion source so agents can see which lane surfaced
                # the hit (task / inbox_item / learning direct lexical match).
                "edge_path_summary": f"{row_hit.source} (fts)",
            }
            if dt in grouped:
                grouped[dt].append(enriched)

    # Track 2 #1-S2 — temporal filter on the LEARNING lane, AFTER fusion +
    # grouping (so salience/ranking already applied; no tombstone leaks into
    # top-k). No-op unless MARVIS_TEMPORAL_MEMORY is ON.
    await _filter_learnings_temporal(grouped, db_path, as_of)

    total_hits = _total()
    meta: dict[str, object] = {
        "semantic_available": sem_err is None,
        "semantic_reason": _semantic_reason(sem_err),
        "kg_available": kg_err is None,
        "documents_fts_available": docs_err is None,
        "tasks_fts_available": tasks_err is None,
        "inbox_fts_available": inbox_err is None,
        "learnings_fts_available": learn_err is None,
        "fused_total": total_hits,
        "chosen_k": rrf_k,
        "weights": dict(weights),
        "graph_lane": kg_expand_meta,
    }
    if total_hits == 0:
        meta["suggested_next_tool"] = _suggest_empty(
            sem_ok=sem_err is None,
            kg_ok=kg_err is None,
            docs_ok=docs_err is None,
            tasks_ok=tasks_err is None,
            inbox_ok=inbox_err is None,
            learn_ok=learn_err is None,
        )
    return grouped, meta


def _suggest_empty(
    *,
    sem_ok: bool,
    kg_ok: bool,
    docs_ok: bool,
    tasks_ok: bool,
    inbox_ok: bool,
    learn_ok: bool,
) -> list[str]:
    """Give the agent 1-2 concrete next steps when search returns nothing."""
    out: list[str] = []
    if not sem_ok:
        out.append("semantic retriever unavailable — retry, or pass hybrid=false")
    if not kg_ok:
        out.append("KG retriever unavailable — ensure migration 078 deployed")
    if not docs_ok:
        out.append("documents FTS retriever unavailable — ensure migration 136 deployed")
    if not (tasks_ok and inbox_ok and learn_ok):
        out.append(
            "one or more FTS5 sources unavailable — ensure migration 080 deployed"
        )
    if sem_ok and kg_ok and docs_ok and tasks_ok and inbox_ok and learn_ok:
        out.extend(
            [
                "try broader keywords (acronym expansion)",
                "try list_projects or search_handoffs with literal match",
            ]
        )
    return out
