# v1.3.0 - 2026-04-24 - Project.degree = SUM(weight) outgoing aggregate (was COUNT)
# v1.2.0 - 2026-04-24 - Q2 estesa con sat_items CTE: top-12 SatelliteItem clickable
# v1.1.0 - 2026-04-24 - SatelliteSummary: Q2 ritorna count + latest_at
# v1.0.0 - 2026-04-24 - Cosmo canvas data aggregator
"""Service layer per GET /api/v1/graph/cosmo.

3 query SQL (projects / satellites windowed / aggregated edges) dentro
una single transaction BEGIN DEFERRED → COMMIT per snapshot isolation vs
populator che gira in parallelo (M-BE-10 piano).

`program` NON e' nel metadata KG (populate_project_nodes.py non lo scrive):
lo joiniamo live da `programs.yaml` tramite _load_programs_map() con cache
mtime-based (M-BE-03 opzione A).

v1.2.0: Q2 estesa con CTE `sat_items` che ritorna top-12 artifact per
(project, kind) ordinati per latest_at DESC. Ogni item porta id/title/path/
latest_at/importance per file-dot cliccabili FE. Performance: subquery
`importance` O(N_items × edge_lookup) — assume `idx_graph_edges_target`
su `graph_edges.target_id` (verificato in produzione 2026-04-24). Cap teorico
payload: 71 project × 8 kind × 12 item ≈ 6816 row × ~150 byte ≈ ~1MB.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

import aiosqlite
import yaml

from core.api.models import UserInfo
from core.api.models.graph_cosmo import (
    Edge,
    GraphCosmoOut,
    Project,
    SatelliteItem,
    SatelliteSummary,
)
from core.api.visibility import get_visible_projects

logger = logging.getLogger(__name__)

# Keep in sync con scripts/populate_project_nodes.py v1.3.0: qualsiasi char
# fuori da [a-zA-Z0-9_\-.] viene sostituito con `_` nel node_id del progetto.
# Serve per matchare `graph_nodes.project_id` (che puo' contenere `&`, es.
# cartelle `c&i-tool`) al `project:artifact:<slug>` super-nodo reale (`c_i-tool`).
_SLUG_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-.]")


def _safe_project_slug(raw: str) -> str:
    """Sanitize project slug per costruire node_id stabili (c&i-tool → c_i-tool)."""
    return _SLUG_SAFE_RE.sub("_", raw)

# Kind whitelist per satellites: derivato dal prefix del node_id (H-11 piano).
# Schema CHECK di `graph_nodes.type` esclude plan/brainstorm/audit/research,
# quindi la classificazione vera sta nel prefix `{kind}:artifact:...`.
ALLOWED_KINDS: tuple[str, ...] = (
    "plan",
    "brainstorm",
    "solution",
    "audit",
    "research",
    "handoff",
    "task",
    "learning",
)

# Generic program baseline shipped in core (tenant-agnostic). Tenant/customer
# program names are NOT hardcoded here: they come from programs.yaml (local,
# not mirrored) and are unioned into the valid set by _valid_programs().
_KNOWN_PROGRAMS_BASELINE: frozenset[str] = frozenset({"marvis", "personal"})

# --- programs.yaml cache (mtime-based, thread-safe) ---

_programs_map_cache: dict[str, str] | None = None
_programs_map_mtime: float = 0.0
_programs_map_lock = threading.Lock()
_PROGRAMS_YAML_PATH = Path.home() / "workspace" / "programs.yaml"


def _load_programs_map() -> dict[str, str]:
    """Carica programs.yaml → {project_slug: program_name} con cache mtime.

    Formato programs.yaml:
        <program_name>:
          projects:
            - <slug1>
            - <slug2>

    Ritorna dict che mappa ogni slug al program di appartenenza. Slug non
    listati in programs.yaml → non presenti nel dict (default 'personal'
    applicato dal caller tramite _normalize_program).
    """
    global _programs_map_cache, _programs_map_mtime

    try:
        current_mtime = _PROGRAMS_YAML_PATH.stat().st_mtime
    except FileNotFoundError:
        logger.warning("cosmo: programs.yaml non trovato a %s", _PROGRAMS_YAML_PATH)
        return {}

    with _programs_map_lock:
        if _programs_map_cache is not None and current_mtime == _programs_map_mtime:
            return _programs_map_cache

        try:
            raw = yaml.safe_load(_PROGRAMS_YAML_PATH.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — yaml errori vari
            logger.exception("cosmo: parse programs.yaml fallito")
            return _programs_map_cache or {}

        result: dict[str, str] = {}
        for prog_name, prog_data in raw.items():
            if not isinstance(prog_data, dict):
                continue
            projects = prog_data.get("projects") or []
            for slug in projects:
                if isinstance(slug, str):
                    result[slug] = prog_name

        _programs_map_cache = result
        _programs_map_mtime = current_mtime
        return result


def _valid_programs() -> frozenset[str]:
    """Insieme dei program validi: baseline generico + nomi da programs.yaml.

    Il baseline (`marvis`, `personal`) e' tenant-agnostico e vive in core. Ogni
    program definito in programs.yaml (locale, non mirrorato) e' considerato
    valido — cosi' nessun nome tenant/cliente e' hardcoded in core ma il
    comportamento live resta identico. La punteggiatura `&` viene normalizzata
    (`c&i` → `ci`) come per _normalize_program.
    """
    extra = {_strip_program_punctuation(name) for name in _load_programs_map().values()}
    return _KNOWN_PROGRAMS_BASELINE | extra


def _strip_program_punctuation(raw: str) -> str:
    """Normalizza il nome program (punteggiatura): `c&i` → `ci`."""
    return raw.replace("&", "")


def _normalize_program(raw: str | None) -> str:
    """Normalizza program name a un valore valido (baseline + programs.yaml).

    Mappa esplicita:
      - 'c&i' → 'ci' (rimozione punteggiatura, coerente con i nomi mirror FE)
      - None / non riconosciuto → 'personal' (con warning)
    """
    if not raw:
        logger.warning("cosmo: project missing program, defaulting to 'personal'")
        return "personal"
    normalized = _strip_program_punctuation(raw)
    if normalized not in _valid_programs():
        logger.warning("cosmo: unknown program %r, defaulting to 'personal'", raw)
        return "personal"
    return normalized


async def fetch_cosmo_graph(
    db: aiosqlite.Connection, user: UserInfo
) -> GraphCosmoOut:
    """Ritorna il bundle {projects, edges} per il canvas cosmo.

    Snapshot isolation via BEGIN DEFERRED — evita che populator in corso
    causi conteggi disallineati tra le 3 query.

    RBAC: projects filtrati via get_visible_projects (None = tutti, set =
    whitelist). Edges scartati se un endpoint non e' nel whitelist (post-filter
    Python).
    """
    # Dedup row_factory: il caller (get_db) lo setta gia' ma per sicurezza.
    db.row_factory = aiosqlite.Row

    # Transazione DEFERRED: nessun lock prima del primo SELECT. Rilascia in
    # finally per garantire COMMIT/ROLLBACK anche su eccezioni query.
    await db.execute("BEGIN DEFERRED")
    try:
        # --- Query 1: project super-nodi + degree ---
        # v1.4.0: degree = SUM(weight) INCOMING aggregate (chi e' citato/dipeso
        # da molti = importante = bubble grande). v1.3.0 usava OUTGOING ma
        # premiava i citatori (small project con depends_on forti emergono al
        # top, marvisx scompare perche' la sua weight outgoing e' normalizzata
        # per sqrt(2700 artifact)). v1.4.0 corregge: marvisx riceve mention da
        # tutti -> incoming sum alto -> bubble grande. Tool-bolletta cita
        # marvisx ma non e' citato da nessuno -> piccolo.
        q_projects = """
            SELECT gn.id AS node_id,
                   gn.name AS slug,
                   (SELECT COALESCE(SUM(CAST(json_extract(ge.metadata, '$.weight') AS REAL)), 0.0)
                      FROM graph_edges ge
                      WHERE ge.target_id = gn.id
                        AND ge.relation IN (
                            'mentions','depends_on','refers_to','shares_tag','similar_to'
                        )
                        AND json_extract(ge.metadata, '$.aggregated') = 1
                        AND ge.valid_until IS NULL
                   ) AS degree
              FROM graph_nodes gn
             WHERE gn.id LIKE 'project:artifact:%'
               AND gn.type = 'project'
               AND gn.deprecated_at IS NULL
             ORDER BY degree DESC, gn.id ASC
        """
        async with db.execute(q_projects) as cur:
            project_rows: list[aiosqlite.Row] = await cur.fetchall()

        # --- Query 2: satellites top-8 per project via graph_nodes.project_id ---
        # Gli edge work-chain (produces/contains/describes/documents) non sono
        # popolati in produzione (0 righe). I nodi artifact pero' hanno
        # `project_id` valorizzato dal populator (migration 073 + ingest path),
        # quindi deriviamo i satellites direttamente dalla colonna — senza
        # passare per `graph_edges`. Una riga per (project_id, kind) con:
        #   - count = COUNT(*) artifact totali del kind nel project (per file-dot)
        #   - latest_at = MAX(last_seen_at, created_at) ISO recency
        # ROW_NUMBER ordinato per latest_at DESC, kind ASC tiebreak. Cap a 8
        # kind distinti per project. Il service ricostruira' il node_id
        # `project:artifact:<slug>` in fase di aggregazione.
        #
        # v1.2.0: aggiunta CTE `sat_items` con top-12 artifact per (project,
        # kind). Performance critica: la subquery `importance` (incoming edges)
        # gira ~6800 volte. Mitigazione: indice `idx_graph_edges_target`
        # esistente su `graph_edges.target_id`. Stima dev DB ~500ms; produzione
        # da monitorare con SQLITE_TIMER se si osserva degrado.
        kinds_placeholders = ",".join("?" * len(ALLOWED_KINDS))
        q_satellites = f"""
            WITH sat_counts AS (
                SELECT gn.project_id AS project_slug,
                       gn.type AS kind,
                       COUNT(*) AS count,
                       MAX(COALESCE(gn.last_seen_at, gn.created_at)) AS latest_at
                  FROM graph_nodes gn
                 WHERE gn.project_id IS NOT NULL
                   AND gn.deprecated_at IS NULL
                   AND gn.type IN ({kinds_placeholders})
                 GROUP BY gn.project_id, gn.type
            ),
            sat_ranked AS (
                SELECT project_slug, kind, count, latest_at,
                       ROW_NUMBER() OVER (
                           PARTITION BY project_slug
                           ORDER BY latest_at DESC, kind ASC
                       ) AS rn
                  FROM sat_counts
            ),
            sat_items AS (
                SELECT gn.project_id AS project_slug,
                       gn.type AS kind,
                       gn.id AS item_id,
                       gn.name AS title,
                       gn.file_path AS path,
                       COALESCE(gn.last_seen_at, gn.created_at) AS latest_at,
                       (SELECT COUNT(*) FROM graph_edges ge
                          WHERE ge.target_id = gn.id
                            AND ge.relation IN (
                                'cites','mentions','refers_to',
                                'applies_to','similar_to'
                            )
                            AND ge.valid_until IS NULL
                       ) AS importance,
                       ROW_NUMBER() OVER (
                           PARTITION BY gn.project_id, gn.type
                           ORDER BY COALESCE(gn.last_seen_at, gn.created_at) DESC,
                                    gn.id ASC
                       ) AS rn
                  FROM graph_nodes gn
                 WHERE gn.project_id IS NOT NULL
                   AND gn.deprecated_at IS NULL
                   AND gn.type IN ({kinds_placeholders})
            )
            SELECT sr.project_slug, sr.kind, sr.count, sr.latest_at,
                   (
                     SELECT json_group_array(json_object(
                         'id', si.item_id,
                         'title', si.title,
                         'path', si.path,
                         'latest_at', si.latest_at,
                         'importance', si.importance
                     ) ORDER BY si.rn ASC)
                     FROM sat_items si
                     WHERE si.project_slug = sr.project_slug
                       AND si.kind = sr.kind
                       AND si.rn <= 12
                   ) AS items_json
              FROM sat_ranked sr
             WHERE sr.rn <= 8
        """
        # Doppio param-set: il placeholder appare 2 volte (sat_counts + sat_items)
        sat_query_params: list[str] = list(ALLOWED_KINDS) + list(ALLOWED_KINDS)
        async with db.execute(q_satellites, sat_query_params) as cur:
            sat_rows: list[aiosqlite.Row] = await cur.fetchall()

        # --- Query 3: edges cross-project aggregated ---
        # JOIN graph_nodes per leggere `name` (raw slug, es. `c&i-master`) invece
        # di `substr(source_id, 18)` che restituirebbe il safe slug del node_id
        # (`c_i-master`). Q1 emette `Project.slug = gn.name` (raw): se gli edge
        # usassero substr (safe), su FE `activeSlug = "c&i-master"` non
        # matcherebbe mai `Edge.source = "c_i-master"` → no edge highlight su
        # hover (bug Cosmo canvas, sessione 162). weight e' dentro metadata JSON.
        # aggregated flag letto via json_extract (robust vs varianti di
        # spacing nel JSON seriale: "aggregated":true vs "aggregated": true).
        # weight: REAL (float) post v1.4 populator. Storicamente era int (count
        # primitive), ora e' un float continuo (decay+normalize). Cast esplicito
        # a REAL per evitare di perdere edge con peso < 1.0 (CAST AS INTEGER
        # tronca 0.4 -> 0 -> filtrato dal `> 0`). FE Zod schema rilassato
        # a `z.number().nonnegative()` di pari passo (consumer accettano float).
        q_edges = """
            SELECT src.name AS slug_a,
                   tgt.name AS slug_b,
                   ge.relation AS relation,
                   CAST(json_extract(ge.metadata, '$.weight') AS REAL) AS weight
              FROM graph_edges ge
              JOIN graph_nodes src ON src.id = ge.source_id
              JOIN graph_nodes tgt ON tgt.id = ge.target_id
             WHERE ge.source_id LIKE 'project:artifact:%'
               AND ge.target_id LIKE 'project:artifact:%'
               AND ge.relation IN ('mentions','depends_on')
               AND json_extract(ge.metadata, '$.aggregated') = 1
               AND ge.valid_until IS NULL
               AND CAST(json_extract(ge.metadata, '$.weight') AS REAL) > 0.0
        """
        async with db.execute(q_edges) as cur:
            edge_rows: list[aiosqlite.Row] = await cur.fetchall()

        await db.commit()
    except Exception:
        await db.rollback()
        raise

    # --- Aggregazione satellites per project_id ricostruito ---
    # Q2 restituisce `project_slug` (valore della colonna `gn.project_id`, es.
    # 'marvisx'). Ricostruiamo la chiave completa `project:artifact:<slug>` per
    # allinearla al `node_id` dei project super-nodi di Q1. Applichiamo la
    # stessa `_safe_project_slug` del populator: altrimenti slug con `&` (es.
    # `c&i-tool`) non matchano mai i super-nodi (`project:artifact:c_i-tool`)
    # e i satelliti/edges dei progetti c&i-* restano orfani.
    #
    # v1.1.0: Q2 ritorna anche `count` (artifact totali del kind, per
    # file-dot semantici N=min(count,6)) + `latest_at` (recency ISO, per
    # accent burning su primo dot solo se < 7 giorni).
    # v1.2.0: Q2 ritorna anche `items_json` (lista top-12 SatelliteItem
    # serializzata via json_group_array). Deserializza con json.loads e
    # popola SatelliteSummary.items.
    satellites_by_pid: dict[str, list[SatelliteSummary]] = {}
    for r in sat_rows:
        slug_raw = r["project_slug"]
        kind = r["kind"]
        if kind not in ALLOWED_KINDS:  # defense-in-depth vs schema drift
            continue
        pid = f"project:artifact:{_safe_project_slug(slug_raw)}"
        items_raw = r["items_json"] or "[]"
        try:
            items_decoded = json.loads(items_raw)
        except (TypeError, ValueError):
            logger.warning("cosmo: items_json parse failed for pid=%s kind=%s", pid, kind)
            items_decoded = []
        items_out: list[SatelliteItem] = []
        for it in items_decoded:
            if not isinstance(it, dict):
                continue
            item_id = it.get("id")
            title = it.get("title")
            latest_at_item = it.get("latest_at")
            if not isinstance(item_id, str) or not isinstance(title, str):
                continue
            if not isinstance(latest_at_item, str):
                continue
            path_val = it.get("path")
            items_out.append(
                SatelliteItem(
                    id=item_id,
                    title=title,
                    latest_at=latest_at_item,
                    importance=int(it.get("importance") or 0),
                    path=path_val if isinstance(path_val, str) else None,
                )
            )
        satellites_by_pid.setdefault(pid, []).append(
            SatelliteSummary(
                kind=kind,  # type: ignore[arg-type]  # filtered above
                count=int(r["count"] or 0),
                latest_at=r["latest_at"],
                items=items_out,
            )
        )

    # --- Program join via programs.yaml ---
    programs_map = _load_programs_map()

    # --- Visibility filter ---
    visible = await get_visible_projects(db, user)

    projects_out: list[Project] = []
    for r in project_rows:
        slug: str = r["slug"]
        if visible is not None and slug not in visible:
            continue
        program = _normalize_program(programs_map.get(slug))
        sats = satellites_by_pid.get(r["node_id"], [])
        projects_out.append(
            Project(
                slug=slug,
                program=program,  # type: ignore[arg-type]  # Literal runtime-checked
                degree=float(r["degree"] or 0.0),
                satellites=sats,
            )
        )

    # --- Edge filter + shape ---
    visible_slugs = {p.slug for p in projects_out} if visible is not None else None
    edges_out: list[Edge] = []
    for r in edge_rows:
        a: str = r["slug_a"]
        b: str = r["slug_b"]
        if visible_slugs is not None and (a not in visible_slugs or b not in visible_slugs):
            continue
        edges_out.append(
            Edge(
                source=a,
                target=b,
                relation=r["relation"],  # type: ignore[arg-type]  # CHECK in SQL
                weight=float(r["weight"] or 0.0),
            )
        )

    return GraphCosmoOut(projects=projects_out, edges=edges_out)


# --- Test helpers (invalidazione cache) ---


def _reset_programs_cache_for_tests() -> None:
    """Reset programs.yaml cache — solo per unit test."""
    global _programs_map_cache, _programs_map_mtime
    with _programs_map_lock:
        _programs_map_cache = None
        _programs_map_mtime = 0.0
