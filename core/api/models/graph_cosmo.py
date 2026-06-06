# v1.3.0 - 2026-04-24 - Project.degree: float (SUM weight aggregate, was int count)
# v1.2.0 - 2026-04-24 - SatelliteSummary.items: top-12 SatelliteItem cliccabili
# v1.1.0 - 2026-04-24 - SatelliteSummary: count + latest_at per file-dot semantici
# v1.0.0 - 2026-04-24 - Cosmo graph adapter response models (PR #3)
"""Pydantic models per GET /api/v1/graph/cosmo (canvas Cosmo dataset).

Shape deliberatamente stretto: 8 kind enum + 2 edge relation enum. Il `program`
e' un identificatore free-form (`str`) validato/normalizzato lato service da un
set data-driven (baseline generico + programs.yaml locale), non un Literal con
nomi tenant. Il canvas `/graph` consuma questo shape via Zod schema mirror in
`console/src/components/graph/cosmo/types.ts`.

Edge e' una classe (non tuple) — rename silenzioso su tuple = bug; rename su
oggetto = compile error (M-BE-07 piano).

v1.1.0: `Project.satellites` passa da `list[Kind]` a `list[SatelliteSummary]`.
Ogni satellite porta `count` (numero artifact totali del kind nel project) e
`latest_at` (recency ISO). Il FE usa `count` per renderizzare N file-dot
semantici (`min(count, 6)`) e `latest_at` per decidere se il primo dot e'
fresh-recent (accent) o cold (bone).

v1.2.0: aggiunto `items: list[SatelliteItem]` (top-12 by recency DESC). Ogni
item porta `id` (graph_node id), `title` (nome leggibile), `latest_at` (per
color-tier), `importance` (incoming edge degree, per radius-tier), `path`
(opzionale, per click → finder). Il FE usa la lista per renderizzare dot
cliccabili reali sostituendo i dot decorativi sintetici.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal[
    "plan",
    "brainstorm",
    "solution",
    "audit",
    "research",
    "handoff",
    "task",
    "learning",
]

# Free-form program identifier. The valid set is data-driven (generic baseline
# in core + tenant/customer program names from programs.yaml, local and not
# mirrored); the service layer normalizes/validates it via _normalize_program.
# Kept as `str` (like Settings.deploy_mode) so no tenant name is hardcoded here.
Program = str

EdgeRelation = Literal["mentions", "depends_on"]


class SatelliteItem(BaseModel):
    """Singolo artifact dentro un satellite (file-dot cliccabile).

    Mappato da `graph_nodes` row tramite Q2:
      - `id` = `gn.id` (es. "plan:artifact:abc123") per share URL
      - `title` = `gn.name` (per tooltip)
      - `latest_at` = MAX(last_seen_at, created_at) ISO per color-tier (fresh
        < 7d → accent; warm < 30d → bone-300; cold → bone-500/0.6)
      - `importance` = COUNT(*) incoming edge in (cites, mentions, refers_to,
        applies_to, similar_to) → guida radius-tier (log-scale)
      - `path` = `gn.file_path` opzionale; se null nessun click handler.
    """

    id: str = Field(..., min_length=1, max_length=256)
    title: str = Field(..., min_length=1, max_length=512)
    latest_at: str
    importance: int = Field(..., ge=0)
    path: str | None = None


class SatelliteSummary(BaseModel):
    """Riassunto kind-aggregato per un satellite di project.

    `count` = numero artifact totali del kind nel project (no cap top-8).
    `latest_at` = ISO timestamp dell'artifact piu' recente (None se 0,
    impossibile via Q2 ma reso esplicitamente nullable per defense-in-depth).
    `items` = top-12 artifact by latest_at DESC (cap fissato in SQL Q2).
    """

    kind: Kind
    count: int = Field(..., ge=0)
    latest_at: str | None = None
    items: list[SatelliteItem] = Field(default_factory=list)


class Project(BaseModel):
    """Super-nodo progetto per il canvas cosmo.

    v1.3.0: `degree` e' un float continuo = SUM(weight) outgoing aggregate
    edges (post weight-recency-decay populator). Storicamente era un int
    (count plain). Cambio motivato: peso continuo riflette la nuova formula
    populator (decay+normalize), cosi' progetti con poche connessioni forti
    pesano piu' di progetti con tante connessioni deboli. Il FE radius
    helper `projectRadius(degree)` accetta float senza modifiche (curva
    `degree^0.52 * 5.2` clampata).
    """

    slug: str = Field(..., min_length=1, max_length=128)
    program: Program
    degree: float = Field(..., ge=0.0)
    satellites: list[SatelliteSummary]


class Edge(BaseModel):
    """Edge cross-project aggregato.

    `weight` = peso continuo post-formula v1.4 in `populate_project_nodes`:
    `SUM(type_w * exp(-age/180)) / sqrt(N_artifacts_src)`. Storicamente era un
    int (count primitive); float a partire da PR weight-recency-decay.
    """

    source: str = Field(..., min_length=1, max_length=128)
    target: str = Field(..., min_length=1, max_length=128)
    relation: EdgeRelation
    weight: float = Field(..., ge=0.0)


class GraphCosmoOut(BaseModel):
    """Bundle response GET /graph/cosmo: project super-nodi + aggregated edges."""

    projects: list[Project]
    edges: list[Edge]
