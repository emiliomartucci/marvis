#!/usr/bin/env python3
# v1.4.0 - 2026-04-24 - Recency decay + type weights + source-volume normalization
"""Populate project:artifact:<slug> super-nodes and aggregate cross-project edges.

v1.4.0 changes:
- `_compute_aggregated_edges` ora calcola un peso continuo per ogni
  edge aggregata invece di un semplice COUNT(*). Formula:
    weight_raw = SUM_over_primitive_edges(type_w * exp(-age_days/180))
    weight_final = weight_raw / sqrt(N_artifacts_src_project)
  Type weights: depends_on=3.0, applies_to=2.0, refers_to=1.5, mentions=1.0.
  Edge non in queste 4 relations vengono ignorate.
  Vantaggi: progetti rumorosi (molti artifact) non dominano la canvas;
  mention vecchie pesano meno di mention recenti (half-life 180 giorni);
  edge "forti" (depends_on) pesano piu' di edge "deboli" (mentions).
  L'edge aggregata adotta come `relation` la primitive con max contribution.
  Threshold: edge con weight_final < 0.01 vengono droppate (rumore).
  Timestamp source = COALESCE(last_seen_at, created_at): `last_seen_at` viene
  aggiornato dai populator ad ogni re-assert, quindi rappresenta il livello di
  freschezza piu' recente conosciuto dal grafo.

v1.3.0 changes:
- Align `project:artifact:<slug>` ID construction with
  populate_cross_project.py::_project_node_id sanitizer to avoid duplicate
  raw/sanitized super-nodes. Slugs containing chars outside NODE_ID_PATTERN
  (`&`, `+`, ...) were producing two distinct super-nodes: one raw (illegal)
  and one sanitized (canonical, targeted by mentions/depends_on edges). All
  4 raw constructions are now routed through a local `_project_node_id`
  helper mirroring the cross-project script.

v1.2.0 changes:
- Filter aggregated edges whose source/target super-node wasn't created
  (orphan project_id in graph_nodes from deleted projects). Prevents FK
  rollback that previously zeroed out the whole aggregate-edge INSERT.

v1.1.0 changes:
- Fixed aggregated edge INSERT: source='manual' (was 'populate_project_nodes', fails CHECK constraint)
- Default DB path already correct (/data/pir/console.db via _resolve_db_path)

## Scope

For every project discovered by the same slug-discovery as populate_cross_project.py:
  - Upsert a graph_nodes row: id=f"project:artifact:{slug}", type="project",
    label=slug, metadata={repo_path, metadata_path, lifecycle, language}.
  - DELETE all aggregated edges (identified by metadata containing '"aggregated":true').
  - Compute aggregate cross-project edges (relations: mentions, depends_on,
    applies_to, refers_to) con peso continuo (v1.4.0 formula): type-weight *
    recency-decay, normalizzato per sqrt(N_artifacts_src).
  - Bulk INSERT aggregated edges con `metadata.weight` (float) e
    `metadata.weight_raw` (pre-normalize) per debug.

## Single-writer contract (PAT-2)

Opens sqlite3.connect() sync, WAL mode, busy_timeout=30000.
Uses BEGIN IMMEDIATE per-project transaction. Idempotent (UPSERT + DELETE+INSERT).

## Invocation

    python -m core.scripts.populate_project_nodes [--dry-run] [--incremental] [--db <path>]
    python -m core.scripts.populate_project_nodes --dry-run   # no COMMIT, log only

## Exit codes

    0 = success (all projects processed)
    1 = one or more per-project failures (others continue)
"""
from __future__ import annotations

import argparse
import heapq
import json
import logging
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger("populate_project_nodes")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROJECTS_ROOT = Path("/data/projects")

# Type weights per relation (v1.4.0). Edge primitive con relation NON in questa
# tabella vengono ignorate dall'aggregato cross-project.
TYPE_WEIGHTS: dict[str, float] = {
    "depends_on": 3.0,
    "applies_to": 2.0,
    "refers_to": 1.5,
    "mentions": 1.0,
}

# Half-life recency decay (giorni). exp(-age_days / HALF_LIFE_DAYS).
# 180gg = mention vecchia ~6 mesi pesa la metà di una mention recente.
# Usato SEMPRE con flag MARVIS_TEMPORAL_MEMORY off (comportamento legacy,
# uniforme su ogni relation) e come fallback per relation non in
# HALF_LIFE_BY_RELATION quando il flag e' on.
HALF_LIFE_DAYS: float = 180.0

# Fase B (kg-freshness, flag-gated MARVIS_TEMPORAL_MEMORY) — half-life per tipo
# di relation: relazioni diverse invecchiano a ritmi diversi. Una dipendenza
# (`depends_on`) segue il churn di codice/infra → decade in fretta; un rimando
# stabile (`refers_to`) e' un fatto durevole → decade lento. Valori iniziali,
# tarabili su query reali; flag-off questi NON vengono usati (resta HALF_LIFE_DAYS).
HALF_LIFE_BY_RELATION: dict[str, float] = {
    "depends_on": 30.0,
    "applies_to": 90.0,
    "mentions": 120.0,
    "refers_to": 180.0,
}

# Fase B — pavimento di decay per relation (flag-on): un fatto stabile e vecchio
# non deve collassare a ~0 solo per eta'. exp(-age/hl) viene alzato a questo
# pavimento. Piu' durevole la relation, piu' alto il pavimento. Relation non in
# tabella → pavimento 0 (nessun floor). Flag-off: nessun pavimento applicato.
DECAY_FLOOR: dict[str, float] = {
    "depends_on": 0.05,
    "applies_to": 0.15,
    "mentions": 0.10,
    "refers_to": 0.20,
}

# Soglia minima sotto la quale l'edge aggregata viene scartata come rumore.
MIN_WEIGHT_THRESHOLD: float = 0.01

# Tetto sul numero di primitive edge che CONTRIBUISCONO al peso di una singola
# coppia (src_project, tgt_project) — at-scale #3.
#
# Senza tetto il peso aggregato somma LINEARMENTE ogni menzione: una coppia con
# ~13k archi meccanici (un progetto molto verboso verso un altro, citazioni
# file-per-file ripetute) schiaccia la dipendenza reale e inquina
# centralita'/SPOF-ranking. Il vincolo
# UNIQUE(source,target,relation) su graph_edges gia' deduplica le triple identiche;
# il rumore residuo e' qui, nell'aggregazione a super-nodo progetto. NON deduplica
# righe (sono coppie di nodi distinte) — cappa il CONTRIBUTO, tenendo i top-K
# contributi per coppia (i piu' recenti/forti, perche' contrib = type_w * decay).
# Una dipendenza ampia e genuina (molte coppie distinte, ognuna modesta) resta
# intatta; solo la verbosita' meccanica concentrata su una coppia viene cappata.
# Override via env MARVIS_EDGE_MENTION_CAP. <=0 disabilita il tetto (legacy lineare).
try:
    MENTION_CAP_PER_PAIR: int = int(os.environ.get("MARVIS_EDGE_MENTION_CAP", "25"))
except ValueError:
    MENTION_CAP_PER_PAIR = 25


def _parse_edge_timestamp(ts: str | None) -> datetime | None:
    """Parse SQLite datetime string ('YYYY-MM-DD HH:MM:SS' o ISO) → tz-aware UTC.

    Tollerante: ritorna None se ts è null o non parsabile (chi chiama tratta
    None come età 0, peso pieno).
    """
    if not ts:
        return None
    try:
        # SQLite default 'YYYY-MM-DD HH:MM:SS' o ISO con T
        candidate = ts.replace(" ", "T")
        parsed = datetime.fromisoformat(candidate)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (ValueError, TypeError):
        return None


def _temporal_memory_enabled() -> bool:
    """Legge il flag canonico MARVIS_TEMPORAL_MEMORY (stessa sorgente dell'API:
    pydantic settings, che carica .env). Import pigro per non accoppiare lo
    script a core.api al load; fallback su env var grezza se l'import fallisce."""
    try:
        from core.api.config import settings
        return bool(settings.temporal_memory_enabled)
    except Exception:
        return os.environ.get("MARVIS_TEMPORAL_MEMORY", "").strip().lower() in (
            "1", "true", "yes", "on",
        )


def _recency_decay(relation: str, age_days: float, temporal_enabled: bool) -> float:
    """Fattore di decay recency in (0, 1] per il peso di una primitive edge.

    flag-off: half-life uniforme HALF_LIFE_DAYS, nessun pavimento → BYTE-IDENTICAL
      al comportamento pre-Fase-B (`exp(-age_days / 180)`).
    flag-on:  half-life per-relation (HALF_LIFE_BY_RELATION, fallback HALF_LIFE_DAYS)
      + pavimento per-relation (DECAY_FLOOR, fallback 0) → un fatto durevole vecchio
      non collassa a ~0 solo per eta'.
    """
    if not temporal_enabled:
        return math.exp(-age_days / HALF_LIFE_DAYS)
    half_life = HALF_LIFE_BY_RELATION.get(relation, HALF_LIFE_DAYS)
    decay = math.exp(-age_days / half_life)
    floor = DECAY_FLOOR.get(relation, 0.0)
    return decay if decay > floor else floor


# ---------------------------------------------------------------------------
# Node ID helper — mirrors populate_cross_project._project_node_id
# ---------------------------------------------------------------------------

# Mirror of scripts.populate_cross_project._project_node_id — duplicated here to
# avoid cross-importing script modules. Both must stay in sync with NODE_ID_PATTERN
# ([a-zA-Z0-9_\-.]+) defined in api/services/graph_service.py.
_SLUG_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-.]")


def _project_node_id(slug: str) -> str:
    """Build a NODE_ID_PATTERN-compliant `project:artifact:<safe>` id."""
    safe = _SLUG_SAFE_RE.sub("_", slug)
    return f"project:artifact:{safe}"


# ---------------------------------------------------------------------------
# DB path resolution — mirrors populate_cross_project._resolve_db_path
# ---------------------------------------------------------------------------

def _resolve_db_path(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    prod = Path("/data/pir/console.db")
    if prod.exists():
        return str(prod)
    return str(REPO_ROOT / "console.db")


# ---------------------------------------------------------------------------
# Project discovery — reuses the same helper as populate_cross_project
# ---------------------------------------------------------------------------

def _discover_projects(
    projects_root: Path,
) -> dict[str, dict[str, Any]]:
    """Discover all projects with a project.yaml under projects_root.

    Returns {slug: {metadata_path, repo_path, lifecycle, language}}.
    """
    if not projects_root.exists():
        logger.warning("projects_root %s does not exist — returning empty", projects_root)
        return {}

    result: dict[str, dict[str, Any]] = {}
    for project_dir in sorted(projects_root.iterdir()):
        if not project_dir.is_dir():
            continue
        yaml_path = project_dir / "project.yaml"
        if not yaml_path.exists():
            continue
        slug = project_dir.name
        py: dict[str, Any] = {}
        if yaml is not None:
            try:
                py = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            except Exception as e:
                logger.warning("project.yaml parse error for %s: %s", slug, e)

        # Resolve repo_path
        repo_path_raw = py.get("repo_path")
        repo_path: str | None = None
        if repo_path_raw and isinstance(repo_path_raw, str):
            rp = Path(repo_path_raw)
            if rp.is_dir():
                repo_path = str(rp)

        result[slug] = {
            "slug": slug,
            "metadata_path": str(project_dir),
            "repo_path": repo_path,
            "lifecycle": py.get("lifecycle") or "active",
            "language": py.get("language"),
        }

    return result


# ---------------------------------------------------------------------------
# Aggregated edge computation
# ---------------------------------------------------------------------------

def _compute_aggregated_edges(
    conn: sqlite3.Connection,
    slugs: frozenset[str],
) -> list[dict[str, Any]]:
    """Compute aggregate cross-project edges con peso continuo (v1.4.0).

    Algoritmo:
      1. Una sola query SQL pesca tutte le primitive edge cross-project con
         relation in {mentions, depends_on, applies_to, refers_to}, includendo
         `created_at`/`updated_at` per il decay e il `project_id` di src/tgt.
      2. Per ogni primitive edge calcola
            contribution = type_w[relation] * recency_decay(relation, age_days)
         e somma le contribution per coppia (src_slug, tgt_slug). Il decay e'
         uniforme (half-life 180gg) con flag MARVIS_TEMPORAL_MEMORY off, oppure
         per-relation + pavimento con flag on (vedi _recency_decay).
      3. Normalizza per `sqrt(N_artifacts_src_project)` per evitare che progetti
         con tanti artifact dominino la canvas via volume "passivo".
      4. La `relation` finale dell'edge aggregata e' la primitive con max
         contribution per la pair (informativa, non usata dal render).
      5. Drop edge con weight_final < 0.01 (rumore) o referenti super-nodi
         non creati (orphan project_id in graph_nodes).

    Returns list of edge dicts ready for INSERT.
    """
    # Build project node id set (sanitize to match on-disk super-nodes)
    project_ids = {_project_node_id(slug) for slug in slugs}

    # Query 1: tutte le primitive edge con metadata necessari per peso/decay.
    # `gn_tgt.project_id` ci serve per i casi in cui il target NON e' un
    # super-node ma un artifact con project_id (es. depends_on cross-project
    # tra due py:file:*). Quando il target e' gia' `project:artifact:<slug>`
    # (mentions/refers_to su super-node) usiamo direttamente l'id.
    #
    # Timestamp recency: `last_seen_at` viene aggiornato dai populator ad ogni
    # re-assert dell'edge (vedi populate_cross_project::ON CONFLICT DO UPDATE),
    # quindi e' il proxy migliore per "quanto e' fresca questa relazione".
    # Fallback su `created_at` per edge mai re-asserted.
    try:
        rows = conn.execute(
            """
            SELECT
                gn_src.project_id AS src_slug,
                ge.target_id      AS tgt_id,
                gn_tgt.project_id AS tgt_artifact_slug,
                ge.relation       AS relation,
                COALESCE(ge.last_seen_at, ge.created_at) AS edge_ts
            FROM graph_edges ge
            JOIN graph_nodes gn_src ON gn_src.id = ge.source_id
            LEFT JOIN graph_nodes gn_tgt ON gn_tgt.id = ge.target_id
            WHERE ge.relation IN ('mentions','depends_on','applies_to','refers_to')
              AND gn_src.project_id IS NOT NULL
            """
        ).fetchall()
    except Exception as e:
        logger.warning("Failed to fetch primitive cross-project edges: %s", e)
        return []

    # Query 2: COUNT artifact per project per la normalizzazione.
    # Esclude:
    #   - deprecated_at IS NOT NULL (artifact tombstoned non contano)
    #   - type='project' (il super-node ha project_id=slug ma rappresenta
    #     metadata, non volume reale di artifact: lo escludiamo per non
    #     gonfiare il denominatore di 1 quando N=0)
    try:
        artifact_count_rows = conn.execute(
            """
            SELECT project_id, COUNT(*) AS n
            FROM graph_nodes
            WHERE project_id IS NOT NULL
              AND deprecated_at IS NULL
              AND type != 'project'
            GROUP BY project_id
            """
        ).fetchall()
    except Exception:
        # deprecated_at potrebbe non esistere su schemi vecchi → fallback senza filtro
        artifact_count_rows = conn.execute(
            """
            SELECT project_id, COUNT(*) AS n
            FROM graph_nodes
            WHERE project_id IS NOT NULL
              AND type != 'project'
            GROUP BY project_id
            """
        ).fetchall()
    artifact_counts: dict[str, int] = {row[0]: row[1] for row in artifact_count_rows}

    # Aggrega in Python: per ogni primitive edge calcola contribution + somma.
    now = datetime.now(timezone.utc)
    # Fase B: flag letto una volta sola → decay per-relation + pavimento quando on,
    # half-life uniforme byte-identico quando off.
    temporal_enabled = _temporal_memory_enabled()
    # key = (src_slug, tgt_slug)
    agg: dict[tuple[str, str], dict[str, Any]] = {}

    for src_slug, tgt_id, tgt_artifact_slug, relation, edge_ts in rows:
        type_w = TYPE_WEIGHTS.get(relation, 0.0)
        if type_w == 0.0:
            continue

        # Resolve target slug.
        # Caso A: target e' gia' un super-node `project:artifact:<slug>` →
        #   estrai slug dal suffix (questo e' il slug RAW dal node id, gia'
        #   sanitized se il populator l'ha creato bene; per la chiave di
        #   aggregazione puo' rimanere cosi' visto che noi ricostruiamo il
        #   node_id finale via _project_node_id() sotto).
        # Caso B: target e' un artifact con project_id → usa quello.
        if isinstance(tgt_id, str) and tgt_id.startswith("project:artifact:"):
            tgt_slug = tgt_id[len("project:artifact:"):]
        elif tgt_artifact_slug:
            tgt_slug = tgt_artifact_slug
        else:
            # Target senza project_id e senza prefisso super-node → non e' una
            # primitive edge cross-project rilevante. Skip.
            continue

        if not src_slug or not tgt_slug or tgt_slug == src_slug:
            continue

        # Recency decay.
        ts = _parse_edge_timestamp(edge_ts)
        if ts is None:
            age_days = 0.0
        else:
            age_seconds = (now - ts).total_seconds()
            age_days = max(0.0, age_seconds / 86400.0)
        decay = _recency_decay(relation, age_days, temporal_enabled)
        contrib = type_w * decay

        key = (src_slug, tgt_slug)
        slot = agg.get(key)
        if slot is None:
            slot = {
                # min-heap bounded a MENTION_CAP_PER_PAIR: trattiene i top-K
                # contributi (i piu' recenti/forti). Memoria O(K) per coppia
                # anche con 13k menzioni.
                "top_contribs": [],
                "weight_uncapped": 0.0,  # somma piena (metrica / legacy se cap<=0)
                "n_total": 0,            # numero reale di menzioni (trasparenza)
                "max_contrib": 0.0,
                "dominant_relation": relation,
            }
            agg[key] = slot
        slot["weight_uncapped"] += contrib
        slot["n_total"] += 1
        if MENTION_CAP_PER_PAIR > 0:
            heap = slot["top_contribs"]
            if len(heap) < MENTION_CAP_PER_PAIR:
                heapq.heappush(heap, contrib)
            else:
                heapq.heappushpop(heap, contrib)
        if contrib > slot["max_contrib"]:
            slot["max_contrib"] = contrib
            slot["dominant_relation"] = relation

    # Normalize + emit.
    edges: list[dict[str, Any]] = []
    for (src_slug, tgt_slug), info in agg.items():
        n_total = info["n_total"]
        # Peso = somma dei top-K contributi (cap on) o somma piena (cap off).
        # math.fsum per stabilita' numerica sulla somma dei float.
        if MENTION_CAP_PER_PAIR > 0:
            weight_raw = math.fsum(info["top_contribs"])
        else:
            weight_raw = info["weight_uncapped"]
        n_artifacts = artifact_counts.get(src_slug, 1)
        # max(1, n) per evitare div/0 e per project con 0 artifact non
        # gonfiare artificialmente il peso.
        weight_final = weight_raw / math.sqrt(max(1, n_artifacts))
        if weight_final < MIN_WEIGHT_THRESHOLD:
            continue

        src_node_id = _project_node_id(src_slug)
        tgt_node_id = _project_node_id(tgt_slug)
        # Confidence: capped at 1.0, scala leggera con weight_raw.
        confidence = min(1.0, 0.5 + weight_raw * 0.05)

        edges.append({
            "source_id": src_node_id,
            "target_id": tgt_node_id,
            "relation": info["dominant_relation"],
            "confidence": confidence,
            "source": "manual",
            "metadata": json.dumps(
                {
                    "aggregated": True,
                    "weight": round(weight_final, 3),
                    "weight_raw": round(weight_raw, 3),
                    "n_artifacts_source": n_artifacts,
                    # Trasparenza: quante menzioni reali rappresenta l'edge e
                    # quante hanno effettivamente contribuito (cap top-K).
                    "n_mentions": n_total,
                    "n_mentions_capped": (
                        min(n_total, MENTION_CAP_PER_PAIR)
                        if MENTION_CAP_PER_PAIR > 0
                        else n_total
                    ),
                    "mention_cap": MENTION_CAP_PER_PAIR,
                },
                separators=(",", ":"),
            ),
        })

    # Filter: drop edges whose source or target super-node was not created.
    # Root cause: graph_nodes.project_id may contain orphan slugs (e.g. deleted
    # projects whose file nodes weren't cleaned up). The aggregate SQL pulls
    # those into candidate edges, but we only create super-nodes for slugs with
    # a project.yaml on disk — so the orphan references violate FK on INSERT.
    # Logging the count helps surface DB-cleanup debt separately from script health.
    before = len(edges)
    edges = [
        e for e in edges
        if e["source_id"] in project_ids and e["target_id"] in project_ids
    ]
    dropped = before - len(edges)
    if dropped:
        logger.info(
            "Filtered %d aggregated edges referencing unknown project super-nodes "
            "(orphan project_id in graph_nodes; see /data/projects/ vs DB diff)",
            dropped,
        )
    return edges


# ---------------------------------------------------------------------------
# Main populate function
# ---------------------------------------------------------------------------

def populate_project_nodes(
    db: str,
    projects_root: Path = DEFAULT_PROJECTS_ROOT,
    dry_run: bool = False,
) -> int:
    """Upsert project super-nodes and aggregated edges.

    Returns 0 on full success, 1 if any per-project error occurred.
    """
    projects = _discover_projects(projects_root)
    if not projects:
        logger.error("No projects discovered under %s", projects_root)
        return 1

    logger.info("Discovered %d projects", len(projects), extra={} if True else {})
    print(f"[populate_project_nodes] discovered {len(projects)} projects", file=sys.stderr)

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")

        any_error = False
        nodes_written = 0
        edges_written = 0

        for slug, info in projects.items():
            node_id = _project_node_id(slug)
            metadata = json.dumps({
                "repo_path": info["repo_path"],
                "metadata_path": info["metadata_path"],
                "lifecycle": info["lifecycle"],
                "language": info["language"],
            }, sort_keys=True)

            try:
                if dry_run:
                    logger.info("[dry-run] would upsert project node: %s", node_id)
                    nodes_written += 1
                    continue

                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        """
                        INSERT INTO graph_nodes
                            (id, type, name, qualified_name, file_path, line_number,
                             metadata, last_seen_at, project_id)
                        VALUES (?, 'project', ?, ?, NULL, NULL, ?, datetime('now'), ?)
                        ON CONFLICT(id) DO UPDATE SET
                            type = 'project',
                            name = excluded.name,
                            qualified_name = excluded.qualified_name,
                            metadata = excluded.metadata,
                            last_seen_at = datetime('now'),
                            updated_at = datetime('now'),
                            project_id = excluded.project_id
                        """,
                        (node_id, slug, f"project.{slug}", metadata, slug),
                    )
                    conn.commit()
                    nodes_written += 1
                    logger.info("Upserted project node: %s", node_id)
                except Exception as e:
                    conn.rollback()
                    logger.error("Failed to upsert project node %s: %s", node_id, e)
                    any_error = True

            except Exception as e:
                logger.error("Unexpected error for project %s: %s", slug, e)
                any_error = True

        print(f"[populate_project_nodes] nodes written: {nodes_written}", file=sys.stderr)

        # Delete all aggregated edges and recompute
        slugs = frozenset(projects.keys())
        aggregated_edges = _compute_aggregated_edges(conn, slugs)

        if dry_run:
            logger.info(
                "[dry-run] would delete aggregated edges and insert %d aggregate edges",
                len(aggregated_edges),
            )
            print(
                f"[populate_project_nodes] [dry-run] aggregate edges to insert: {len(aggregated_edges)}",
                file=sys.stderr,
            )
        else:
            # Delete existing aggregated edges
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "DELETE FROM graph_edges "
                        "WHERE source_id LIKE 'project:artifact:%' "
                        "  AND metadata LIKE '%\"aggregated\":true%'"
                    )
                    conn.commit()
                    logger.info("Deleted existing aggregated edges")
                except Exception as e:
                    conn.rollback()
                    logger.error("Failed to delete aggregated edges: %s", e)
                    any_error = True
            except Exception as e:
                logger.error("Unexpected error deleting aggregated edges: %s", e)
                any_error = True

            # Insert new aggregated edges in one transaction
            if aggregated_edges and not any_error:
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        for edge in aggregated_edges:
                            conn.execute(
                                """
                                INSERT INTO graph_edges
                                    (source_id, target_id, relation, confidence,
                                     source, metadata, created_at, first_seen_at, last_seen_at)
                                VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))
                                ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                                    confidence = MAX(graph_edges.confidence, excluded.confidence),
                                    metadata = excluded.metadata,
                                    last_seen_at = datetime('now')
                                """,
                                (
                                    edge["source_id"],
                                    edge["target_id"],
                                    edge["relation"],
                                    edge["confidence"],
                                    edge["source"],
                                    edge["metadata"],
                                ),
                            )
                        conn.commit()
                        edges_written = len(aggregated_edges)
                        logger.info("Inserted %d aggregated edges", edges_written)
                    except Exception as e:
                        conn.rollback()
                        logger.error("Failed to insert aggregated edges: %s", e)
                        any_error = True
                except Exception as e:
                    logger.error("Unexpected error inserting aggregated edges: %s", e)
                    any_error = True

        print(
            f"[populate_project_nodes] edges written: {edges_written}",
            file=sys.stderr,
        )
        return 1 if any_error else 0

    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    parser = argparse.ArgumentParser(
        description="Populate project:artifact super-nodes and aggregated cross-project edges."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log what would be written without committing.",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="(reserved for future use) Currently behaves identically to full run.",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Path to SQLite DB. Default: auto-resolve /data/pir/console.db.",
    )
    parser.add_argument(
        "--projects-root",
        default=str(DEFAULT_PROJECTS_ROOT),
        help="Path to projects root directory.",
    )
    args = parser.parse_args()
    db_path = _resolve_db_path(args.db)
    projects_root = Path(args.projects_root)
    logger.info("DB: %s, projects_root: %s, dry_run: %s", db_path, projects_root, args.dry_run)
    return populate_project_nodes(
        db=db_path,
        projects_root=projects_root,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
