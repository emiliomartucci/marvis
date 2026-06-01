# v1.0.0 - 2026-04-30 - Phase 1.5 E6: KG edge enrichment background (LLM #2)
"""LLM-driven KG edge enrichment, async background task post-saga done.

Pipeline:
  1. saga completes (status=done) → asyncio.create_task(enrich_kg_for_node)
  2. read node + neighbors + candidate targets (read-only DB)
  3. LLM call (local Gateway tier-fast): suggest edges
  4. confidence-gate >= 0.70 + cap 10 + whitelist edge_types
  5. UPSERT graph_edges (UNIQUE constraint already in schema)
  6. UPDATE graph_nodes.kg_enriched_at (mig 100)

Failure mode: log + skip (no inline retry). Cron Phase 2 future re-trigger
nodes with kg_enriched_at IS NULL.

Reuses E5 infra (LLM client + bounded structured JSON + sanitize) for consistency.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.api.db import acquire_db, acquire_write_db
from core.api.services.inbox_llm_classifier import _sanitize
from core.api.services.ingest.llm.local_gateway import complete_structured_json
from core.api.services.pii_redactor import redact

logger = logging.getLogger(__name__)

LLM_MODEL = "tier-fast"
LLM_TIMEOUT_S = 30
EXCERPT_MAX_CHARS = 1500
MAX_OUTPUT_TOKENS = 600

# Edge types tier-fast may suggest. Subset of the 15 graph_edges relations.
ALLOWED_EDGE_TYPES = frozenset({"mentions", "refers_to", "similar_to", "cites"})

# Confidence floor: edges below this are dropped before INSERT.
CONFIDENCE_THRESHOLD = 0.70

# Hard cap on edges per node (avoid runaway LLM suggesting 100s).
MAX_EDGES_PER_NODE = 10


class SuggestedEdge(BaseModel):
    target_node_id: str = Field(min_length=5, max_length=256)
    relation: Literal["mentions", "refers_to", "similar_to", "cites"]
    confidence: float = Field(ge=0.0, le=1.0)
    reasoning: str = Field(max_length=200)


class KGEnrichment(BaseModel):
    suggested_edges: list[SuggestedEdge] = Field(default_factory=list, max_length=MAX_EDGES_PER_NODE)
    reasoning: str = Field(max_length=400)


_SYSTEM_PROMPT = """Sei un graph-enrichment agent per il KG MarvisX.

Dato un nodo (con title, type, project, content excerpt) e la lista dei suoi
neighbors esistenti + candidate target nodes, suggerisci edges KG verso altri
artefatti.

Edge types ammessi (SOLO questi 4):
- mentions: il nodo nomina/cita un altro artefatto (link weak)
- refers_to: riferimento esplicito (link strong, pari a `cites`)
- similar_to: similarity semantica (stesso topic/dominio)
- cites: citazione formale (URL, ID, path esplicito)

Per ogni edge suggerito ritorna:
- target_node_id: ID nodo target (deve essere uno dei candidate fornite, NON inventare)
- relation: uno dei 4 type sopra
- confidence: 0.0-1.0 (>= 0.70 = high; < 0.70 verra' scartato dal client)
- reasoning: < 200 char in italiano

Cap massimo 10 edges. Se incerto, restituisci lista vuota."""


def _build_prompt(node: dict, neighbors: list[dict], candidates: list[dict]) -> str:
    return (
        "Source node:\n"
        f"  id: {node.get('id')}\n"
        f"  type: {node.get('type')}\n"
        f"  name: {node.get('name')}\n"
        f"  project: {node.get('project_id')}\n"
        f"  excerpt: {(node.get('excerpt') or '')[:EXCERPT_MAX_CHARS]}\n\n"
        f"Existing neighbors ({len(neighbors)}):\n"
        f"{json.dumps([{'id': n.get('id'), 'rel': n.get('relation')} for n in neighbors[:10]], ensure_ascii=False)}\n\n"
        f"Candidate target nodes ({len(candidates)}):\n"
        f"{json.dumps([{'id': c.get('id'), 'type': c.get('type'), 'name': c.get('name'), 'project': c.get('project_id')} for c in candidates[:30]], ensure_ascii=False, indent=2)}\n"
    )


def _excerpt_from_node_metadata(node: dict[str, Any]) -> str:
    try:
        meta = json.loads(node.get("metadata") or "{}")
    except (TypeError, ValueError):
        return str(node.get("name") or "")
    cls = meta.get("classification") if isinstance(meta, dict) else {}
    if not isinstance(cls, dict):
        return str(node.get("name") or "")

    transcript_summary = cls.get("transcript_summary")
    if isinstance(transcript_summary, dict) and transcript_summary.get("status") == "ok":
        parts = [
            str(transcript_summary.get("summary") or "").strip(),
            "Temi: "
            + ", ".join(
                str(topic)
                for topic in transcript_summary.get("topics", [])[:8]
                if str(topic).strip()
            ),
            "Keyword: "
            + ", ".join(
                str(keyword)
                for keyword in transcript_summary.get("keywords", [])[:12]
                if str(keyword).strip()
            ),
        ]
        excerpt = "\n".join(part for part in parts if part.strip(" :"))
        if excerpt.strip():
            return excerpt
    return str(cls.get("title") or node.get("name") or "")


async def _fetch_node(db: Any, node_id: str) -> dict | None:
    async with db.execute(
        "SELECT id, type, name, qualified_name, project_id, file_path, metadata "
        "FROM graph_nodes WHERE id = ?",
        (node_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    out = dict(row)
    # Surface transcript summary when available; otherwise fall back to title.
    out["excerpt"] = _excerpt_from_node_metadata(out)
    return out


async def _fetch_candidates(db: Any, node: dict, limit: int = 30) -> list[dict]:
    """Pick candidate target nodes for the LLM. Heuristic: same project + recent.
    Plus a slice of cross-project artifacts (for `mentions`/`refers_to` semantics)."""
    project_id = node.get("project_id")
    rows: list[dict] = []
    # Same-project recents
    if project_id:
        async with db.execute(
            """
            SELECT id, type, name, project_id
              FROM graph_nodes
             WHERE project_id = ?
               AND id != ?
               AND deprecated_at IS NULL
             ORDER BY last_seen_at DESC
             LIMIT ?
            """,
            (project_id, node.get("id"), limit),
        ) as cursor:
            async for row in cursor:
                rows.append(dict(row))
    # Cross-project sample (top-degree nodes per project diversity)
    async with db.execute(
        """
        SELECT id, type, name, project_id
          FROM graph_nodes
         WHERE deprecated_at IS NULL
           AND id != ?
           AND (project_id IS NULL OR project_id != ?)
         ORDER BY degree DESC
         LIMIT ?
        """,
        (node.get("id"), project_id or "", max(0, limit - len(rows))),
    ) as cursor:
        async for row in cursor:
            rows.append(dict(row))
    return rows


async def _call_enricher(
    node: dict,
    neighbors: list[dict],
    candidates: list[dict],
) -> KGEnrichment | None:
    excerpt = (node.get("excerpt") or "")[:EXCERPT_MAX_CHARS]
    sanitized = redact(_sanitize(excerpt, EXCERPT_MAX_CHARS))
    node_safe = {**node, "excerpt": sanitized}
    prompt = _build_prompt(node_safe, neighbors, candidates)

    return await complete_structured_json(
        response_model=KGEnrichment,
        system_prompt=_SYSTEM_PROMPT,
        user_prompt=prompt,
        feature="ingest_kg_enrichment",
        max_tokens=MAX_OUTPUT_TOKENS,
        timeout=LLM_TIMEOUT_S,
    )


async def enrich_kg_for_node(node_id: str) -> None:
    """Fire-and-forget background task. Never raises — failures logged + skipped."""
    if (os.environ.get("LLM_KG_ENRICHER_ENABLED", "false") or "").strip().lower() != "true":
        return

    enrichment_run_id = str(uuid.uuid4())
    inserted_count = 0
    try:
        # READ phase (read-only pool)
        async with acquire_db() as db:
            node = await _fetch_node(db, node_id)
            if node is None:
                logger.info("kg_enricher_node_missing id=%s", node_id)
                return
            # Lazy import to avoid cycle (graph_service is a heavy module).
            from core.api.services.graph_service import get_neighbors as _get_neighbors

            neighbors = await _get_neighbors(db, node_id, direction="both", limit=20)
            candidates = await _fetch_candidates(db, node)

        if not candidates:
            logger.info("kg_enricher_no_candidates node=%s", node_id)
            await _mark_enriched(node_id)
            return

        enrichment = await _call_enricher(node, neighbors, candidates)
        if enrichment is None:
            logger.info("kg_enricher_llm_no_result node=%s", node_id)
            return

        # Build a set of valid candidate IDs for fast validation.
        valid_ids = {c["id"] for c in candidates}

        # WRITE phase
        async with acquire_write_db() as wdb:
            for edge in enrichment.suggested_edges[:MAX_EDGES_PER_NODE]:
                if edge.relation not in ALLOWED_EDGE_TYPES:
                    continue
                if edge.confidence < CONFIDENCE_THRESHOLD:
                    continue
                if edge.target_node_id == node_id:
                    continue
                if edge.target_node_id not in valid_ids:
                    # LLM may hallucinate — skip targets not in the candidate set.
                    continue
                metadata_json = json.dumps(
                    {
                        "agent": "kg_enricher",
                        "model": LLM_MODEL,
                        "enrichment_run_id": enrichment_run_id,
                        "reasoning": edge.reasoning,
                    },
                    ensure_ascii=False,
                )
                try:
                    await wdb.execute(
                        """
                        INSERT INTO graph_edges
                            (source_id, target_id, relation, source, confidence,
                             metadata, last_seen_at, first_seen_at, weight, last_touched_at)
                        VALUES (?, ?, ?, 'llm', ?, ?, datetime('now'), datetime('now'), 1.0, datetime('now'))
                        ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                            confidence = excluded.confidence,
                            last_seen_at = excluded.last_seen_at,
                            metadata = excluded.metadata,
                            last_touched_at = excluded.last_touched_at
                        """,
                        (
                            node_id,
                            edge.target_node_id,
                            edge.relation,
                            edge.confidence,
                            metadata_json,
                        ),
                    )
                    inserted_count += 1
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "kg_enricher_edge_insert_failed source=%s target=%s rel=%s",
                        node_id, edge.target_node_id, edge.relation,
                        exc_info=True,
                    )
            await wdb.commit()

        await _mark_enriched(node_id)
        logger.info(
            "kg_enricher_done node=%s edges_inserted=%d run_id=%s",
            node_id, inserted_count, enrichment_run_id,
        )
    except Exception:  # noqa: BLE001 - background task must never raise to caller
        logger.exception("kg_enricher_failed node=%s", node_id)


async def _mark_enriched(node_id: str) -> None:
    try:
        async with acquire_write_db() as wdb:
            await wdb.execute(
                "UPDATE graph_nodes SET kg_enriched_at = datetime('now') WHERE id = ?",
                (node_id,),
            )
            await wdb.commit()
    except Exception:  # noqa: BLE001
        logger.warning("kg_enricher_mark_enriched_failed node=%s", node_id, exc_info=True)
