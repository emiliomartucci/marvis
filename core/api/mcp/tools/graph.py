# v1.0.0 - 2026-05-27 - S1 F3.1b: graph KG MCP tool group (use_cases-direct, no HTTP)
"""Graph (Knowledge-Graph) MCP tools — port of the Node ``graph_*`` group, use_cases-direct.

Same template as ``tasks.py`` / ``learnings.py``: the Node HTTP proxy
(``get``/``post``/``del`` -> ``:8100``) is replaced by an in-process
``await graph_uc.<fn>(LOCAL_CTX, db, ...)``. Docstrings are copied VERBATIM from
``core/mcp-pir/index.mjs`` (curated, carry the QUANDO USARLO / NON USARLO /
RESTITUISCE blocks).

Schema port (Zod -> Pydantic), per S1 F3:
  * ``z.enum([...])``                -> ``Literal[...]``
  * ``z.string().max(N)``            -> ``Annotated[str, Field(max_length=N)]``
  * ``z.number().int().min().max()`` -> ``Annotated[int, Field(ge=, le=)]``
  * optional                         -> ``X | None = None`` (or ``= <default>``)
  * ``z.array(edgeTypeEnum)``        -> ``list[EdgeType] | None``

Return typing (S1 F3): reads return ``dict[str, Any]`` / ``list[dict]``; the pin
mutators (``pin_graph_node`` / ``unpin_graph_node``) return the ``PinOut``/``dict``
via ``dump()``. ``graph_capabilities`` returns the ``GraphCapabilities`` DTO dump.

Visibility: the MCP surface is local single-user (no ``UserInfo.teams``), so every
tool passes ``visible_projects=None`` = unrestricted (the same DECISION 1 + DECISION B
collapse the four-eyes / PR gates take on the local surface, S1 §AUTH). The
project-scoped 403 / oracle-avoidance 404 enforcement that ``routers/graph.py``
layers on top (DECISION B) is a multi-tenant transport concern that has no meaning
for a single operator — the pure use_case data work is what the MCP surface needs.

fastapi-free invariant (the collapse must stay honest — zero fastapi in the MCP
import path): ``use_cases.graph`` itself is already fastapi-free (it depends only
on the fastapi-free ``graph_service`` / ``graph_ranker``), so it is a module-top
import. The ONLY fastapi-importing dependency in this family is
``api.visibility.filter_visible_edges`` (the macro-overview RBAC edge filter,
DECISION A). It is NEVER imported here; instead ``graph_overview`` injects a
LOCAL no-op edge filter (single-user sees all edges, hidden_cross_project_count=0),
which both keeps fastapi out of the MCP path AND matches the local "unrestricted"
stance.

SKIPPED (no clean use_case — adapter/fastapi-bound, ported in a later increment):
  * ``share_function`` / ``graph_function_share`` — the use_case only carries the
    PURE pieces (validate_qualified_name / lookup_function_node /
    build_share_function_blocks). Their headline value (the signed share_url + the
    on-disk source preview) lives in the router and pulls fastapi via
    ``services.share_links`` (``validate_repo_path`` / ``create_shared_link_record``)
    + the router-local ``_read_file_preview``. Porting without the URL would be a
    degraded, misleading tool, so it is skipped (S1 F3 SKIP rule), not faked.
  * ``graph_cosmo`` — adapter-only (``graph_cosmo_service`` imports fastapi,
    DECISION A); no ``use_cases.graph`` entry.
  * ``graph_pr_impact`` / ``graph_branches`` / ``graph_conflicts`` /
    ``graph_semantic_modules`` — PR-Impact/Codex-lens family; logic was not
    extracted into ``use_cases.graph`` (still router/service-bound).
  * ``kg_reindex_path`` / ``kg_rebuild`` / ``kg_watcher_control`` — KG control
    plane; no use_case (systemd / sentinel side effects in the router).
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    LOCAL_CTX,
    acquire_db,
    acquire_write_db,
    dump,
    raise_mcp_error,
)
from core.api.use_cases import graph as graph_uc
from core.api.use_cases._errors import ServiceError

# Zod enums -> Literals (mirror the Node tool signatures).
NeighborRelation = Literal["calls", "imports", "defines"]
NeighborDirection = Literal["incoming", "outgoing", "both"]
NeighborRank = Literal["none", "suspect_write"]
HotspotWindow = Literal["7d", "30d", "total"]
HotspotTypeFilter = Literal["function", "file", "all"]
OverviewLevel = Literal["macro", "module"]
# edgeTypeEnum (index.mjs) — the 16 relation types valid as edge_types filters.
EdgeType = Literal[
    "calls", "imports", "defines",
    "produces", "contains",
    "describes", "documents", "cites", "applies_to",
    "depends_on", "mentions", "refers_to", "shares_tag", "similar_to",
    "resolves_to",
    "modifies",
]

# Node ID + qualified-name patterns (mirror the Node Zod regexes).
_NODE_ID_PATTERN = (
    r"^(py|ts|task|pr|commit|handoff|solution|learning|audit|spike|analysis|"
    r"research|rubric|guide|mockup|project|file|hook|skill|command|plugin|plan|"
    r"brainstorm|inbox|xlsx|policy|contract|transcript|record|report):"
    r"(function|file|module|artifact|sheet):[a-zA-Z0-9_\-.]+$"
)
_PIN_NODE_ID_PATTERN = r"^[a-z]+:[a-z]+:.+$"
_AS_OF_PATTERN = r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2}(\.\d+)?)?Z?$"
_PROJECT_PATTERN = r"^[a-z0-9][a-z0-9&\-]+$"
_ORPHAN_SCOPE_PATTERN = r"^(project|module):artifact:.+$"


async def _local_no_op_edge_filter(
    _db: Any, _user: Any, edges: list[dict]
) -> tuple[list[dict], int]:
    """Local single-user edge filter for ``graph_overview`` macro (unrestricted).

    The HTTP surface injects ``api.visibility.filter_visible_edges`` (which imports
    fastapi, DECISION A). The MCP surface MUST NOT pull fastapi into its import
    path, and the single operator sees every project anyway, so the local injection
    is this no-op: all edges kept, ``hidden_cross_project_count = 0``.
    """
    return edges, 0


def register(mcp) -> None:
    """Register the graph (KG) tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def graph_neighbors(
        node_id: Annotated[str, Field(pattern=_NODE_ID_PATTERN, max_length=256)],
        relation: NeighborRelation | None = None,
        edge_types: list[EdgeType] | None = None,
        project: Annotated[str, Field(max_length=50, pattern=_PROJECT_PATTERN)] | None = None,
        direction: NeighborDirection = "both",
        rank: NeighborRank = "none",
        as_of: Annotated[str, Field(pattern=_AS_OF_PATTERN, max_length=32)] | None = None,
    ) -> dict[str, Any]:
        """1-hop: chi tocca / dipende direttamente da un nodo. Funziona su codice ('py:function:...') E su progetti ('project:artifact:<slug>').
        ORCHESTRAZIONE: per i vicini diretti di un PROGETTO (chi dipende da X) passa node_id='project:artifact:<slug>' con edge_types=['depends_on']. Codice: caller/callee 1-hop di una function.
        QUANDO USARLO: vicinato diretto senza il bundle completo; caller inattesi; time-travel as_of; rank=suspect_write per bug write-through-read. Per il blast radius transitivo usa graph_impact / project_impact.
        RESTITUISCE: list of {node_id, relation, direction, rank_score?, freshness} cap 200."""
        try:
            async with acquire_db() as db:
                # Node proxy hardcodes limit=200 (the surface exposes no limit param).
                result = await graph_uc.graph_neighbors(
                    LOCAL_CTX,
                    db,
                    node_id=node_id,
                    relation=relation,
                    edge_types=list(edge_types) if edge_types else None,
                    project=project,
                    direction=direction,
                    limit=200,
                    rank=rank or "none",
                    as_of=as_of,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def graph_hotspots(
        window: HotspotWindow = "30d",
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
        type_filter: HotspotTypeFilter = "file",
    ) -> dict[str, Any]:
        """DORA-style churn ranking: files/functions sorted by touch_count 7d/30d with bus-factor warning — per context completo su un progetto usa get_project(deep=true). Usa questo tool per analisi architetturale di rischio o pianificazione refactor.
        QUANDO USARLO: identificare moduli ad alto rischio prima di una release; trovare ownership gaps (bus_factor=1); pianificare tech debt.
        QUANDO NON USARLO: NOT per context di un progetto specifico -> usa get_project(deep=true). NOT per BFS da un nodo -> usa graph_impact.
        RESTITUISCE: list of {node_id, touch_count_7d, touch_count_30d, authors[], bus_factor} top N."""
        try:
            async with acquire_db() as db:
                result = await graph_uc.graph_hotspots(
                    LOCAL_CTX,
                    db,
                    window=window,
                    limit=limit,
                    type_filter=type_filter,
                    project=None,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def graph_impact(
        node_id: Annotated[str, Field(pattern=_NODE_ID_PATTERN, max_length=256)],
        depth: Annotated[int, Field(ge=1, le=5)] = 2,
        limit: Annotated[int, Field(ge=1, le=200)] = 50,
        edge_types: list[EdgeType] | None = None,
        project: Annotated[str, Field(max_length=50, pattern=_PROJECT_PATTERN)] | None = None,
    ) -> dict[str, Any]:
        """Impact / blast radius: 'cosa si rompe — o quali progetti si bloccano — se cambio / pauso / chiudo X?'. BFS sui rami transitivi + ranker sui diretti.
        ORCHESTRAZIONE DI PORTAFOGLIO: per sapere quali PROGETTI si bloccano se pausi/chiudi un progetto, passa node_id='project:artifact:<slug>' con edge_types=['depends_on','mentions','refers_to'] — oppure usa il tool dedicato project_impact(slug). Livello-codice: per 'cosa si rompe se cambio questa function' passa un node 'py:function:...'.
        QUANDO USARLO: prima di pausare/chiudere/de-prioritizzare un progetto (blast radius cross-project); prima di eliminare/rinominare una function critica; dependency cascade analysis; sequenziare lavoro (cosa sblocca cosa).
        RESTITUISCE: {direct (callers/dependents)[], transitive_list[], rank_score, freshness (indexed_sha vs HEAD)} depth configurabile."""
        try:
            async with acquire_db() as db:
                result = await graph_uc.graph_impact(
                    LOCAL_CTX,
                    db,
                    node_id=node_id,
                    depth=depth,
                    limit=limit,
                    edge_types=list(edge_types) if edge_types else None,
                    project=project,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def project_impact(
        slug: Annotated[str, Field(max_length=50, pattern=_PROJECT_PATTERN)],
        depth: Annotated[int, Field(ge=1, le=5)] = 2,
    ) -> dict[str, Any]:
        """Portfolio blast radius: 'se pauso / chiudo / de-prioritizzo questo PROGETTO, quali altri progetti si bloccano?'. Il tool di prima classe per l'orchestrazione cross-progetto — distinto dal grafo-codice.
        Risolve 'project:artifact:<slug>' e fa l'impact BFS sui soli edge progetto->progetto (depends_on / mentions / refers_to). La response porta il segnale freshness (indexed_sha vs HEAD) per non fidarsi di un indice stantio.
        QUANDO USARLO: prima di pausare/chiudere/de-prioritizzare un progetto; sequenziare un portafoglio (cosa sblocca cosa); capire le dipendenze cross-progetto di una decisione.
        RESTITUISCE: {direct_dependents[], transitive_list[], rank_score, freshness}."""
        try:
            async with acquire_db() as db:
                result = await graph_uc.graph_impact(
                    LOCAL_CTX,
                    db,
                    node_id=f"project:artifact:{slug}",
                    depth=depth,
                    limit=100,
                    edge_types=["depends_on", "mentions", "refers_to"],
                    project=None,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def graph_context(
        node_id: Annotated[str, Field(pattern=_NODE_ID_PATTERN, max_length=256)],
        per_category_limit: Annotated[int, Field(ge=1, le=20)] = 5,
        project: Annotated[str, Field(max_length=50, pattern=_PROJECT_PATTERN)] | None = None,
    ) -> dict[str, Any]:
        """Rationale chain di un nodo: function/progetto → commit → PR → task → handoff → learning in 1 call. Il PERCHE' dietro qualcosa.
        QUANDO USARLO: perche' esiste / da dove viene una function o una decisione di progetto; audit trail; linkare codice o progetto a un decision record.
        RESTITUISCE: chain {commits[], PR?, task?, handoffs[], learnings[]} per_category_limit configurabile."""
        try:
            async with acquire_db() as db:
                result = await graph_uc.graph_context(
                    LOCAL_CTX,
                    db,
                    node_id=node_id,
                    per_category_limit=per_category_limit,
                    project=project,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def graph_pattern(
        scope: Annotated[str, Field(max_length=256)],
        limit: Annotated[int, Field(ge=1, le=100)] = 20,
    ) -> dict[str, Any]:
        """Learnings scoped a un modulo/path specifico via KG — per learnings generici usa check_learnings. Usa questo tool per learnings chirurgici su un file/modulo specifico prima di toccare codice in quella area.
        QUANDO USARLO: 'quali learnings esistono per api.db?' prima di toccare il DB module; scoping pre-deployment di un servizio specifico.
        QUANDO NON USARLO: NOT per ricerca semantica generica pre-azione -> usa check_learnings. NOT per context di un nodo -> usa graph_context.
        RESTITUISCE: list of {learning_id, title, prevention, severity, scope_match_score}."""
        try:
            async with acquire_db() as db:
                result = await graph_uc.graph_pattern(
                    LOCAL_CTX, db, scope=scope, limit=limit, project=None,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def graph_capabilities() -> dict[str, Any]:
        """KG schema metadata per agent discovery (edge_types + node_kinds + prefixes + versions).
        QUANDO USARLO: prima di costruire query graph_* o validare node_id pattern; cold-start agent discovery.
        QUANDO NON USARLO: per query topologiche -> usa graph_neighbors/impact/context.
        RESTITUISCE: {edge_types[], node_kinds[], node_prefixes[], schema_version}.
        FALLBACK: se 5xx, usa lista statica edge_types noti 2026-04 (vedi kb/knowledge-graph.md)."""
        try:
            async with acquire_db() as db:
                result = await graph_uc.get_capabilities(LOCAL_CTX, db)
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def pin_graph_node(
        node_id: Annotated[
            str, Field(min_length=6, max_length=256, pattern=_PIN_NODE_ID_PATTERN)
        ],
        note: Annotated[str, Field(max_length=500)] | None = None,
    ) -> dict[str, Any]:
        """Save a KG node as a personal bookmark (pin). Idempotent — pinning the same node twice updates the note. Use to bookmark frequently visited nodes (functions, files, tasks). Appears in graph_landing() saved_nodes slice."""
        try:
            async with acquire_write_db(label="mcp.pin_graph_node") as db:
                result = await graph_uc.create_graph_pin(
                    LOCAL_CTX,
                    db,
                    workspace_id=LOCAL_CTX.workspace_id,
                    user_id=LOCAL_CTX.user_id,
                    node_id=node_id,
                    note=note,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def unpin_graph_node(
        node_id: Annotated[
            str, Field(min_length=6, max_length=256, pattern=_PIN_NODE_ID_PATTERN)
        ],
    ) -> dict[str, Any]:
        """Remove a personal bookmark (pin) for a KG node. Returns 404 if the pin does not exist for the current user."""
        try:
            async with acquire_write_db(label="mcp.unpin_graph_node") as db:
                result = await graph_uc.delete_graph_pin(
                    LOCAL_CTX, db, user_id=LOCAL_CTX.user_id, node_id=node_id,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def list_graph_pins() -> list[dict[str, Any]]:
        """List all personal KG bookmarks (pins) for the current user, ordered by most recently pinned. Pins on soft-deleted nodes are excluded."""
        try:
            async with acquire_db() as db:
                result = await graph_uc.list_graph_pins(
                    LOCAL_CTX, db, user_id=LOCAL_CTX.user_id,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def graph_resolve(
        path: Annotated[str, Field(min_length=1, max_length=1024)],
    ) -> dict[str, Any]:
        """Resolve a file path to its KG node_id. Useful when you have a file path (e.g. 'api/db.py') and need the graph_nodes id for neighbors/impact/context queries. Returns 404 if the file is not indexed or not visible."""
        try:
            async with acquire_db() as db:
                # visible_projects=None -> local single-user sees all (DECISION 1).
                result = await graph_uc.graph_resolve(
                    LOCAL_CTX, db, path=path, visible_projects=None,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def graph_landing() -> dict[str, Any]:
        """Get the KG landing bundle: top-10 hotspots (30d), last-20 recent artifacts (commits/PRs/tasks/handoffs), and your saved pins. Cached 60s per workspace. Use as the first call when opening the KG explorer or needing a quick project overview."""
        try:
            async with acquire_db() as db:
                # The 60s TTLCache is a transport concern living in the HTTP adapter;
                # the MCP surface computes the slices fresh each call (pass no cache).
                bundle, _hotspots, _recent, _saved = await graph_uc.graph_landing(
                    LOCAL_CTX,
                    db,
                    workspace_id=LOCAL_CTX.workspace_id,
                    user_id=LOCAL_CTX.user_id,
                )
                return dump(bundle)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def graph_overview(
        level: OverviewLevel,
        scope: Annotated[str, Field(max_length=256)] | None = None,
        cross_project: bool = True,
    ) -> dict[str, Any]:
        """Get a LOD (level-of-detail) overview of the knowledge graph at macro or module level. macro = project hub nodes + aggregated cross-project edges. module = module/folder nodes within a scope. RBAC-filtered — cross-project edges to invisible projects are omitted with hidden_cross_project_count."""
        try:
            async with acquire_db() as db:
                # visible_projects=None + a LOCAL no-op edge filter (single-user sees
                # all). filter_visible_edges (api.visibility) imports fastapi and must
                # NOT enter the MCP import path; the local injection keeps it out.
                # limit mirrors the HTTP Query default (300).
                result = await graph_uc.graph_overview(
                    LOCAL_CTX,
                    db,
                    level=level,
                    scope=scope,
                    cross_project=cross_project,
                    limit=300,
                    user=LOCAL_CTX,
                    visible_projects=None,
                    filter_visible_edges=_local_no_op_edge_filter,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def graph_orphans(
        scope: Annotated[
            str, Field(min_length=1, max_length=256, pattern=_ORPHAN_SCOPE_PATTERN)
        ],
    ) -> dict[str, Any]:
        """Find file nodes with no edges (orphans) within a project or module scope. Orphans are grouped by folder with deterministic colors. Useful for identifying dead code, stale docs, or unlinked files. Each sub-cluster is capped at 30 files (overflow_count reflects the rest)."""
        try:
            async with acquire_db() as db:
                # visible_projects=None -> local single-user sees all (DECISION 1).
                result = await graph_uc.graph_orphans(
                    LOCAL_CTX, db, scope=scope, visible_projects=None,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
