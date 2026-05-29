#!/usr/bin/env node
// DEPRECATED for OSS — replaced by the in-process Python MCP server core/api/mcp/server.py
// (FastMCP stdio, use_cases-direct, no Node/HTTP). Retained for the paid HTTP tiers while
// the HTTP API exists; final removal is decided in S3 (public-mirror scope).
// mcp-pir v2.22.0 - 2026-05-11 - Docs governance triage tool (agent-native parity)
// mcp-pir v2.21.0 - 2026-04-29 - P1.5.E7: 8 ingest triage tools (agent-native parity)
// mcp-pir v2.20.0 - 2026-04-22 - PR2: get_session(name) with dual metrics fields
// mcp-pir v2.19.0 - 2026-04-22 - Phase 7.x hygiene: graph_capabilities() tool (plan Pilastro 5)
// mcp-pir v2.18.0 - 2026-04-22 - Phase 7.2: extract NODE_ID_RE shared constant, propagate err.status on HTTP helpers, document resolves_to edge
// mcp-pir v2.17.0 - 2026-04-17 - add graph_function_share tool wrapper (GET /api/v1/graph/function-share)
// mcp-pir v2.17.0 - 2026-04-24 - PR #3 graph-cosmo: new graph_cosmo tool (Cosmo canvas dataset)
// mcp-pir v2.16.0 - 2026-04-17 - P2: 7 new graph UX tools (pin_graph_node/unpin/list_graph_pins/graph_resolve/graph_landing/graph_overview/graph_orphans)
// mcp-pir v2.15.0 - 2026-04-16 - Phase 7.0 KG Inline Lens: deep param on get_task/get_project/get_pr/get_learning + new get_handoff tool
// v2.14.0 - 2026-04-16 - KG Phase 6.5 pilastro E (partial): list_learnings + get_learning MCP wrappers
// v2.13.0 - 2026-04-16 - KG Phase 6.5 pilastro C: 3-section description template + Zod v4 .meta()
// v2.12.0 - 2026-04-15 - share_function: hand off function with KG context in 1 call (vision audit P1)
// v2.11.2 - 2026-04-14 - KG Fase 2: cross-project project/edge_types params + project/file NODE_ID
// v2.11.1 - 2026-04-14 - KG Fase 1h/1i (doc-type prefixes + auto-index md)
// v2.11.0 - 2026-04-14 - KG Fase 1f: graph_impact + graph_context + graph_pattern

import { execFile } from "node:child_process";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const execFileAsync = promisify(execFile);
const __dirname = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(__dirname, "..");
const SAFETY_BRIDGE = path.join(REPO_ROOT, "scripts", "safety_bridge.py");

const API = process.env.PIR_API_URL || "http://127.0.0.1:8100";
const TOKEN = process.env.TASKS_API_TOKEN || "";

console.error(`[MCP-DEBUG] API: ${API}`);
console.error(`[MCP-DEBUG] TOKEN: ${TOKEN ? TOKEN.substring(0, 4) + '...' + TOKEN.slice(-4) : "MISSING"}`);

// v2.18.0 (Phase 7.2): error helpers propagate err.status + err.body so callers
// can branch on HTTP status without re-parsing the message. Until now an
// upstream 404 vs 500 were both a generic Error — this opens the door for
// future typed retry/fallback logic.
async function _rejectNonOk(r) {
  const text = await r.text().catch(() => "");
  const err = new Error(`${r.status}: ${text}`);
  err.status = r.status;
  err.body = text;
  throw err;
}

async function get(path) {
  const url = `${API}${path}`;
  console.error(`[MCP-DEBUG] GET ${url}`);
  const r = await fetch(url, {
    headers: { Authorization: `Bearer ${TOKEN}`, "X-Agent-Name": "marvisx" },
  });
  if (!r.ok) await _rejectNonOk(r);
  return r.json();
}

async function post(path, body) {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Agent-Name": "claudio" },
    body: JSON.stringify(body),
  });
  if (!r.ok) await _rejectNonOk(r);
  return r.json();
}

async function patch(path, body) {
  const r = await fetch(`${API}${path}`, {
    method: "PATCH",
    headers: { Authorization: `Bearer ${TOKEN}`, "Content-Type": "application/json", "X-Agent-Name": "claudio" },
    body: JSON.stringify(body),
  });
  if (!r.ok) await _rejectNonOk(r);
  return r.json();
}

async function del(path) {
  const r = await fetch(`${API}${path}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${TOKEN}`, "X-Agent-Name": "marvisx" },
  });
  if (!r.ok) await _rejectNonOk(r);
  if (r.status === 204) return { deleted: true };
  return r.json();
}

// P1.5.E7 — multipart/form-data POST helper for ingest upload tools.
// fetch() auto-fills Content-Type with the FormData boundary, so do NOT
// pre-set Content-Type here (it would break the boundary).
async function postForm(path, form) {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${TOKEN}`, "X-Agent-Name": "claudio" },
    body: form,
  });
  if (!r.ok) await _rejectNonOk(r);
  return r.json();
}

function json(data) {
  return { content: [{ type: "text", text: JSON.stringify(data, null, 2) }] };
}

function qs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v != null && v !== "") p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

async function checkSafety(actionType, { filePath, command, cwd } = {}) {
  const args = [SAFETY_BRIDGE, "check", "--action-type", actionType];
  if (filePath) args.push("--file-path", filePath);
  if (command) args.push("--command", command);
  if (cwd) args.push("--cwd", cwd);
  const { stdout } = await execFileAsync("python3", args, { cwd: REPO_ROOT });
  return JSON.parse(stdout);
}

const server = new McpServer({ name: "pir", version: "2.22.0" });

// Phase 7.0: deep=true default for get_* tools, configurable via env
const _MCP_DEEP_ENV = "MARVIS_MCP_DEEP_DEFAULT";
function resolveDeepDefault() {
  const raw = process.env[_MCP_DEEP_ENV];
  if (raw == null) return true;  // MCP surface: default deep=true
  return String(raw).toLowerCase().trim() !== "false";
}
function effectiveDeep(deep) {
  return deep ?? resolveDeepDefault();
}

// --- Projects ---

server.tool("list_projects",
  "List all PiR-tracked projects grouped by program.\n\nQUANDO USARLO: serve un inventario (quali progetti ci sono, dammi tutti gli slug) o non conosci lo slug target.\nQUANDO NON USARLO: NOT quando hai gia' uno slug specifico e vuoi i dettagli -> usa get_project. NOT per cold-start di un progetto -> usa session_brief.\nRESTITUISCE: list of {slug, program, lifecycle, language, task_counts} senza body di context.md.",
  {},
  async () => json(await get("/api/v1/projects"))
);

server.tool("get_project",
  "Get full project detail (metadata + context.md body + handoff index + docs).\n\nQUANDO USARLO: hai uno slug noto e ti serve lo stato raw del progetto (es. leggere context.md body o elenco handoff completo). Usa ?deep=true per includere kg_context inline (neighbors, context_chain, applicable_learnings) — risparmia 2-3 tool call aggiuntivi.\nQUANDO NON USARLO: NOT per agent cold-start -> usa session_brief (aggrega project + open tasks + top learnings + salience docs in una call). NOT se non hai lo slug -> usa list_projects.\nRESTITUISCE: {slug, metadata, context_md, handoffs[], docs[], deploy_info} + kg_context se deep=true.",
  {
    slug: z.string().min(1).max(50).meta({
      description: "Project slug (from .task file)",
      examples: ["marvisx", "c&i-tool"],
    }),
    deep: z.boolean().optional().meta({ description: "Include inline KG context bundle (default true on MCP)" }),
  },
  async ({ slug, deep }) => {
    const d = effectiveDeep(deep);
    return json(await get(`/api/v1/projects/${encodeURIComponent(slug)}?deep=${d}`));
  }
);

server.tool("session_brief",
  "Cold-start aggregated context bundle for agent session resume.\n\nQUANDO USARLO: inizio sessione su un progetto (sostituisce la sequenza get_project + list_tasks + search_handoffs + Read context.md + check_learnings). BOUNDARY: session_brief = cold-start aggregato; list_tasks+list_handoffs = query mirate.\nQUANDO NON USARLO: NOT se ti serve solo il body grezzo di context.md o l'elenco completo handoff -> usa get_project. NOT per query filtrate puntuali -> usa list_tasks/list_handoffs.\nRESTITUISCE: {project, open_tasks[], latest_handoff, recent_learnings[], top_salience_docs[]} tuned per context window LLM.",
  {
    slug: z.string().min(1).max(50).meta({
      description: "Project slug",
      examples: ["marvisx"],
    }),
  },
  async ({ slug }) => json(await get(`/api/v1/projects/${encodeURIComponent(slug)}/brief`))
);

// --- Tasks ---

server.tool("list_tasks",
  "List persistent cross-session tasks with exact-match filters.\n\nQUANDO USARLO: enumerare o filtrare task per project/status/priority/program/kind (es. tutti gli approved di marvisx, idee pending high priority). BOUNDARY: list_tasks = exact filters; search = semantic multi-type; tasks_summary = counts only.\nQUANDO NON USARLO: NOT per natural-language discovery cross-type -> usa search. NOT per conteggi aggregati -> usa tasks_summary. NOT per singolo ID noto -> usa get_task.\nRESTITUISCE: list of {id, title, description, status, priority, project, ICE-D, tags, pr_state} paginato.",
  {
    project: z.string().optional().meta({
      description: "Filter by project slug",
      examples: ["marvisx", "znext"],
    }),
    status: z.string().optional().meta({
      description: "Comma-separated status values",
      examples: ["pending,approved", "in_progress", "completed"],
    }),
    kind: z.enum(["normal", "idea"]).optional().meta({
      description: "Filter by task kind (normal vs idea)",
    }),
    priority: z.string().optional().meta({
      description: "Filter by priority level",
      examples: ["high", "medium", "low"],
    }),
    program: z.string().optional().meta({
      description: "Filter by program name",
    }),
    limit: z.number().optional().default(20).meta({
      description: "Max results (1-100)",
    }),
  },
  async (p) => json(await get(`/api/v1/tasks${qs(p)}`))
);

server.tool("get_task",
  "Get full detail of a single task by UUID.\n\nQUANDO USARLO: hai un task UUID (da list_tasks o da .task-style ref) e vuoi title/description/ICE-D/tags/PR linked. Usa ?deep=true per includere kg_context inline (neighbors, context_chain, applicable_learnings) — risparmia 2-3 tool call aggiuntivi. BOUNDARY: search vs get_task = NOT usare search per singolo artifact noto per ID -> usa get_task.\nQUANDO NON USARLO: NOT se hai solo filtri (project/status) -> usa list_tasks. NOT per ricerca semantica -> usa search.\nRESTITUISCE: {id, title, description, status, priority, project, ice_d, tags, pr_task_id} + kg_context se deep=true.",
  {
    task_id: z.string().min(1).meta({
      description: "Task UUID",
      examples: ["dddecec5-..."],
    }),
    deep: z.boolean().optional().meta({ description: "Include inline KG context bundle (default true on MCP)" }),
  },
  async ({ task_id, deep }) => {
    const d = effectiveDeep(deep);
    return json(await get(`/api/v1/tasks/${task_id}?deep=${d}`));
  }
);

server.tool("create_task",
  "Persist a new cross-session task in PiR DB (survives session end, visible in Console Triage).\n\nQUANDO USARLO: PRIMA AZIONE per ogni lavoro implementativo (feat/fix/refactor/research) — richiesto da Constitution Rule 1 prima di qualunque Edit/Write. SEMPRE con ICE-D (impact/confidence/ease/delegation).\nQUANDO NON USARLO: NOT per modificare un task esistente -> usa update_task. NOT per free chat/brainstorm (session-first).\nRESTITUISCE: {id, title, status:pending, project, ...} — il task nasce pending, serve approval umano via Triage.",
  {
    title: z.string().min(1).max(200).meta({
      description: "Task title (max 200 chars)",
      examples: ["Fix auth redirect loop", "Add KG impact endpoint"],
    }),
    project: z.string().min(1).meta({
      description: "Project slug",
      examples: ["marvisx"],
    }),
    description: z.string().optional().meta({
      description: "Format: Devo {azione} perche {problema}. Attenzione a {dipendenze}.\\n-/data/projects/{slug}",
    }),
    priority: z.enum(["high", "medium", "low"]).optional().default("medium"),
    kind: z.enum(["normal", "idea"]).optional().default("normal"),
    source: z.enum(["session", "manual", "console"]).optional().default("session"),
    tags: z.array(z.string()).optional().meta({
      description: "Tag array for searchability",
      examples: [["kg", "mcp"], ["auth", "security"]],
    }),
    impact: z.number().min(1).max(10).optional().meta({
      description: "ICE impact score 1-10",
    }),
    confidence: z.number().min(1).max(10).optional().meta({
      description: "ICE confidence score 1-10",
    }),
    ease: z.number().min(1).max(10).optional().meta({
      description: "ICE ease score 1-10",
    }),
    delegation: z.enum(["agent", "hybrid", "human"]).optional().meta({
      description: "Who implements: agent / hybrid / human",
    }),
    completion_mode: z.enum(["pr", "doc", "none"]).optional().default("pr").meta({
      description: "pr (default, requires merged PR) | doc (research/plan, closed when doc exists) | none (verify/diagnose, free)",
    }),
  },
  async ({ title, project, description, priority, kind, source, tags, impact, confidence, ease, delegation, completion_mode }) =>
    json(await post("/api/v1/tasks", { title, project, description, priority, kind, source, tags, impact, confidence, ease, delegation, completion_mode }))
);

server.tool("update_task",
  "Mutate an existing PiR task (status, priority, description, tags, ICE-D, completion_mode).\n\nQUANDO USARLO: transizioni di stato lungo il lifecycle (pending -> approved -> in_progress -> review -> completed) o rifinitura scoring/description dopo creazione.\nQUANDO NON USARLO: NOT per creare un nuovo task -> usa create_task. NOT per delete permanente -> usa delete_task. Status='approved' e' BLOCCATO per agent (403) — solo umani approvano via Console Triage.\nRESTITUISCE: task record aggiornato {id, title, status, ...}.",
  {
    id: z.string().min(1).meta({
      description: "Task UUID",
    }),
    status: z.enum(["pending", "approved", "in_progress", "completed", "rejected", "failed"]).optional(),
    kind: z.enum(["normal", "idea"]).optional(),
    priority: z.enum(["high", "medium", "low"]).optional(),
    description: z.string().optional(),
    tags: z.array(z.string()).optional(),
    impact: z.number().min(1).max(10).optional().meta({
      description: "ICE impact score 1-10",
    }),
    confidence: z.number().min(1).max(10).optional().meta({
      description: "ICE confidence score 1-10",
    }),
    ease: z.number().min(1).max(10).optional().meta({
      description: "ICE ease score 1-10",
    }),
    delegation: z.enum(["agent", "hybrid", "human"]).optional().meta({
      description: "Who implements",
    }),
    completion_mode: z.enum(["pr", "doc", "none"]).optional().meta({
      description: "Change completion mode: pr | doc | none",
    }),
  },
  async ({ id, ...u }) => {
    const body = Object.fromEntries(Object.entries(u).filter(([, v]) => v !== undefined));
    return json(await patch(`/api/v1/tasks/${id}`, body));
  }
);

server.tool("delete_task",
  "Permanently remove a task from the PiR DB (non-recoverable).\n\nQUANDO USARLO: solo per spam/duplicate o entry chiaramente invalidi.\nQUANDO NON USARLO: NOT per abbandonare un task valido -> usa update_task con status='rejected' o 'failed' (preserva audit trail). Deletion cancella la history.\nRESTITUISCE: {deleted: true} o 404 se non esiste.",
  {
    task_id: z.string().min(1).meta({
      description: "Task UUID to delete permanently",
    }),
  },
  async ({ task_id }) => json(await del(`/api/v1/tasks/${task_id}`))
);

server.tool("tasks_summary",
  "Aggregate task counts grouped by status and by project (no bodies).\n\nQUANDO USARLO: dashboard cross-project 'quanti task pending?' o health check.\nQUANDO NON USARLO: NOT per titoli/description — qui ci sono solo numeri -> usa list_tasks.\nRESTITUISCE: {by_status:{pending:N,...}, by_project:{slug:{pending:N,...}}}.",
  {},
  async () => json(await get("/api/v1/tasks/summary"))
);

// --- Handoffs ---

server.tool("list_handoffs",
  "List handoff files for one project (frontmatter metadata only, no body).\n\nQUANDO USARLO: browse chronological session history per progetto noto (es. quali sessioni su marvisx ultima settimana). BOUNDARY: session_brief vs list_tasks+list_handoffs = session_brief aggrega tutto in una call.\nQUANDO NON USARLO: NOT per keyword search cross-project -> usa search_handoffs. NOT per cold-start -> usa session_brief.\nRESTITUISCE: list of {session_id, branch, tags, date, path} ordered chronological.",
  {
    slug: z.string().min(1).max(50).meta({
      description: "Project slug",
      examples: ["marvisx"],
    }),
  },
  async ({ slug }) => json(await get(`/api/v1/projects/${encodeURIComponent(slug)}/handoffs`))
);

server.tool("search_handoffs",
  "Full-text keyword search across every handoff body in every project.\n\nQUANDO USARLO: ricordi un termine ma non il progetto/data (es. 'find handoffs mentioning migration 025'). Exact keyword match, NOT semantic.\nQUANDO NON USARLO: NOT per semantic discovery multi-type -> usa search. NOT se conosci il progetto -> usa list_handoffs.\nRESTITUISCE: list of {project, session_id, path, snippet, date} con match literal.",
  {
    q: z.string().min(1).max(500).meta({
      description: "Keyword query across handoff content",
      examples: ["migration 025", "voyage", "rate limit"],
    }),
  },
  async ({ q }) => json(await get(`/api/v1/handoffs/search${qs({ q })}`))
);

server.tool("get_handoff",
  "Get a single handoff file by project and filename.\n\nQUANDO USARLO: hai project_slug + filename da list_handoffs/search_handoffs e vuoi il body completo + frontmatter. Usa ?deep=true per kg_context inline (references, mentions, context chain).\nQUANDO NON USARLO: NOT per ricerca testuale -> usa search_handoffs. NOT per lista handoff di progetto -> usa list_handoffs.\nRESTITUISCE: {project, file, frontmatter, body, kg_context?}.",
  {
    project_slug: z.string().min(1).max(63).meta({ description: "Project slug", examples: ["marvisx"] }),
    filename: z.string().min(1).max(200).meta({ description: "Handoff filename e.g. handoff-2026-04-14-titolo.md", examples: ["handoff-2026-04-14-kg-probe.md"] }),
    deep: z.boolean().optional().meta({ description: "Include kg_context bundle (default true on MCP)" }),
  },
  async ({ project_slug, filename, deep }) => {
    const d = effectiveDeep(deep);
    return json(await get(`/api/v1/handoffs/${encodeURIComponent(project_slug)}/${encodeURIComponent(filename)}?deep=${d}`));
  }
);

// --- Semantic Search ---

server.tool("search",
  "Semantic embedding-based search across tasks + projects + files + handoffs + learnings.\n\nQUANDO USARLO: natural-language discovery cross-type quando non conosci keyword esatte o doc_type (es. 'dove abbiamo parlato di caching Redis?'). Score > 0.6 generalmente rilevante.\nQUANDO NON USARLO: NOT per singolo artifact noto per ID -> usa get_task / list_handoffs con filtri. NOT per keyword exact-match -> usa search_handoffs. NOT per doc_type specifico con filtri -> usa list_tasks.\nRESTITUISCE: {tasks[], projects[], files[], handoffs[], learnings[], total, query} ranked per (70% Voyage similarity + 30% salience). 503 se Voyage non disponibile.",
  {
    q: z.string().min(1).max(500).meta({
      description: "Natural language search query",
      examples: ["caching redis optimization", "handoff orphan recovery"],
    }),
  },
  async ({ q }) => json(await get(`/api/v1/search${qs({ q })}`))
);

server.tool("reindex",
  "Rebuild Voyage embeddings index for semantic search backend.\n\nQUANDO USARLO: solo dopo bulk ops che bypassano i normali hook (direct SQL insert, file-sync script, migration) se noti search stale. Operator+.\nQUANDO NON USARLO: NOT routinely — l'index e' auto-sync su create/update normali. NOT su errori transient di search -> aspetta background reconcile.\nRESTITUISCE: type='all' -> {status:'queued'} background; tipo specifico -> sync result con counts.",
  {
    type: z.enum(["tasks", "projects", "files", "handoffs", "learnings", "inbox_items", "audits", "all"]).optional().default("all").meta({
      description: "Scope to reindex; 'all' queues background job",
    }),
  },
  async ({ type = "all" }) => json(await post(`/api/v1/search/reindex${qs({ type })}`, {}))
);

server.tool("boost_document",
  "Nudge a document's salience score upward (+0.15 capped at 1.0) for future search ranking.\n\nQUANDO USARLO: scopri che un doc e' piu' importante di quanto il score suggerisce (es. un handoff che continua a mancare nei result).\nQUANDO NON USARLO: NOT per modifica permanente -> edita frontmatter salience. Rate-limited: 1 boost per (doc, caller) per ora.\nRESTITUISCE: {doc_id, new_salience, boosted_at}.",
  {
    doc_id: z.number().int().positive().meta({
      description: "Document ID to boost",
      examples: [42, 1337],
    }),
  },
  async ({ doc_id }) => json(await post(`/api/v1/documents/${doc_id}/boost`, {}))
);

// --- KG Phase 4.5: agent control plane (kg-watcher daemon + full-rebuild) ---

server.tool("kg_reindex_path",
  "Re-index N paths in the Knowledge Graph synchronously (artifact + cross_project edges).\n\nQUANDO USARLO: un .md sotto /data/projects/<slug>/ e' stato modificato esternamente (rsync, sed batch) e vuoi refresh senza aspettare il watcher. Operator+, timeout 60s.\nQUANDO NON USARLO: NOT per semantic embeddings reindex -> usa reindex (non graph nodes/edges). NOT per full nightly rebuild su 68 progetti -> usa kg_rebuild.\nRESTITUISCE: {nodes_written, edges_written, skipped, latency_ms}.",
  {
    paths: z.array(z.string()).min(1).max(100).meta({
      description: "Absolute paths under /data/projects/<slug>/ (typically 1-10 files)",
      examples: [["/data/projects/marvisx/memory/handoff-2026-04-16-kg.md"]],
    }),
    mode: z.enum(["artifact", "cross_project", "both"]).optional().default("both").meta({
      description: "artifact = only populate_artifacts (handoff/doc/task nodes); cross_project = only mentions/refers_to/cites edges; both = both (default)",
    }),
    handle_delete: z.boolean().optional().default(false).meta({
      description: "If true, soft-delete corresponding nodes (deprecated_at). Use when file deleted from disk",
    }),
    skip_hash_gate: z.boolean().optional().default(false).meta({
      description: "Bypass file_state sha256 check — force re-index even if hash unchanged",
    }),
  },
  async ({ paths, mode = "both", handle_delete = false, skip_hash_gate = false }) =>
    json(await post(`/api/v1/kg/reindex_path`, { paths, mode, handle_delete, skip_hash_gate }))
);

server.tool("kg_rebuild",
  "Trigger pir-kg-full-rebuild.service in background (oneshot systemd unit).\n\nQUANDO USARLO: sospetti drift del KG (mismatch tra disk e graph_nodes), dopo migration su tabelle KG, o forzare rebuild fuori dal cron 03:00 UTC. ~25-60s su tutti i 68 progetti. Operator+.\nQUANDO NON USARLO: NOT per sync puntuale N path -> usa kg_reindex_path. NOT per semantic embeddings -> usa reindex.\nRESTITUISCE: {status: queued|already_running, job_id}.",
  {
    scope: z.enum(["all", "default"]).optional().default("all").meta({
      description: "'all' = --include-all-projects (~68 projects); 'default' = Fase 2 scope (~24 c&i + pilot)",
    }),
  },
  async ({ scope = "all" }) =>
    json(await post(`/api/v1/kg/rebuild`, { scope }))
);

server.tool("kg_watcher_control",
  "Pause/resume/status del kg-watcher daemon (Phase 2).\n\nQUANDO USARLO: finestre di backfill o debug senza fermare il systemd unit. Pause = daemon running, dispatch saltato (soft). Auto-resume opzionale.\nQUANDO NON USARLO: NOT per fermare davvero il process -> usa systemctl --user stop pir-kg-watcher.service. NOT per re-index storico -> usa kg_reindex_path.\nRESTITUISCE: {state: paused|running, sentinel_path, systemctl_status, auto_resume_at?}.",
  {
    action: z.enum(["pause", "resume", "status"]).meta({
      description: "pause = touch sentinel; resume = rm sentinel; status = read current state",
    }),
    duration_seconds: z.number().int().min(10).max(86400).optional().meta({
      description: "Only for action=pause. Auto-resume after N seconds (10-86400). Omit for indefinite pause",
    }),
  },
  async ({ action, duration_seconds }) =>
    json(await post(`/api/v1/kg/watcher_control`, { action, duration_seconds }))
);

// --- Sessions ---

server.tool("list_sessions",
  "List live tmux sessions on MarvisX server with project binding and metrics.\n\nQUANDO USARLO: verificare quali agent session sono running o correlare un progetto al suo tmux window attivo.\nQUANDO NON USARLO: NOT per session storiche da markdown -> usa list_handoffs. NOT per metriche server -> usa get_monitoring.\nRESTITUISCE: list of {session_id, project, state, tokens, cost, last_heartbeat, tmux_window}.",
  {},
  async () => json(await get("/api/v1/sessions"))
);

server.tool("get_session",
  "Get full detail for a single tmux session, including PR2 dual metrics (ctx_real/scaled, cost_conversation/session, IN/OUT tokens, reasoning_tokens, working_seconds_msg).\n\nQUANDO USARLO: hai il session name e vuoi il payload completo (incluse le metriche nuove migration 087). Utile per debugging 'quanto e' costata la mia conversation corrente vs il cumulativo session' o 'ctx reale vs scaled'.\nQUANDO NON USARLO: NOT per lista cross-session -> usa list_sessions. NOT se il session_id non esiste in tmux sul server.\nRESTITUISCE: SessionInfo completo con dual metrics (last_context_pct_real, last_context_pct_scaled, last_cost_conversation_usd, last_cost_session_usd, last_input_tokens, last_output_tokens, last_reasoning_tokens, working_seconds_msg, metrics_refreshed_at, pricing_version).",
  {
    name: z.string().min(1).max(64).meta({
      description: "tmux session name (e.g. 'marvisx' or 'oc-shell')",
      examples: ["marvisx"],
    }),
  },
  async ({ name }) => json(await get(`/api/v1/sessions/by-name/${encodeURIComponent(name)}`))
);

// --- Costs ---

server.tool("cost_summary",
  "Aggregate cost totals per project (all-time USD spend rolled up).\n\nQUANDO USARLO: cross-project 'which project burned the most?' dashboard.\nQUANDO NON USARLO: NOT per breakdown per-session/per-model di un progetto -> usa project_costs. NOT per billing invoice-style -> usa get_billing.\nRESTITUISCE: list of {project, total_usd, session_count, last_activity}.",
  {},
  async () => json(await get("/api/v1/costs/summary"))
);

server.tool("project_costs",
  "Detailed cost breakdown for one project (per-session + per-model + date range).\n\nQUANDO USARLO: conosci lo slug e vuoi investigare cosa ha driven lo spend (es. 'quali session di marvisx sono costate di piu'').\nQUANDO NON USARLO: NOT per overview cross-project -> usa cost_summary. NOT per numeri client invoice -> usa get_billing.\nRESTITUISCE: {project, sessions[], by_model:{}, total_usd, date_range}.",
  {
    slug: z.string().min(1).max(50).meta({
      description: "Project slug",
      examples: ["marvisx"],
    }),
  },
  async ({ slug }) => json(await get(`/api/v1/costs/by-project/${encodeURIComponent(slug)}`))
);

server.tool("get_billing",
  "Billing-shaped cost payload for a project (invoice-style totals + currency).\n\nQUANDO USARLO: numeri per client invoice o billing export (external-facing).\nQUANDO NON USARLO: NOT per analisi engineering interna (serve granularita' per-session) -> usa project_costs.\nRESTITUISCE: {project, period, subtotal, tax, total, currency} invoice-ready.",
  {
    slug: z.string().min(1).max(50).meta({
      description: "Project slug",
      examples: ["marvisx"],
    }),
  },
  async ({ slug }) => json(await get(`/api/v1/costs/billing/${encodeURIComponent(slug)}`))
);

// --- Pull Requests ---

server.tool("create_branch",
  "Atomic create branch + worktree + draft PR (orchestrator-managed).\n\nQUANDO USARLO: hai un task approved/in_progress e ti serve un worktree isolato per iniziare a lavorare. Un'unica call crea branch `feat/task-{uuid}`, worktree in `~/dev/task-{uuid}`, e draft PR row in PiR. Preferisci su `git worktree add` manuale + register_branch (2-step flow).\nQUANDO NON USARLO: NOT se il worktree esiste gia' (creato manualmente o fuori orchestrator) -> usa register_branch per attaccarlo al task. NOT se il task non e' ancora approved -> il backend risponde 400.\nRESTITUISCE: {task_id, branch_name, worktree_path, status:'draft'} idempotent.",
  {
    task_id: z.string().min(1).meta({
      description: "Task UUID to create branch+worktree for (task must be approved or in_progress)",
    }),
  },
  async ({ task_id }) =>
    json(await post(`/api/v1/pull_requests/${encodeURIComponent(task_id)}/branch`, {}))
);

server.tool("register_branch",
  "Attach an existing git branch/worktree to a task as draft PR record (idempotent).\n\nQUANDO USARLO: il worktree e' stato creato manualmente via `git worktree add` o fuori orchestrator, e serve la PR row PiR per poter chiamare submit_pr dopo. BOUNDARY: register_branch crea draft; submit_pr promuove draft -> open per review.\nQUANDO NON USARLO: NOT quando vuoi che PiR crei il worktree per te -> usa endpoint HTTP /api/v1/pull_requests/{task_id}/branch. NOT dopo submit -> il record e' gia' open.\nRESTITUISCE: {task_id, branch_name, worktree_path, status:'draft'} idempotent.",
  {
    task_id: z.string().min(1).meta({
      description: "Task UUID (must be in_progress)",
    }),
    branch_name: z.string().min(1).meta({
      description: "Git branch name",
      examples: ["feat/task-76c63a9a-..."],
    }),
    worktree_path: z.string().optional().meta({
      description: "Absolute path to worktree directory",
    }),
  },
  async ({ task_id, branch_name, worktree_path }) =>
    json(await post(`/api/v1/pull_requests/${task_id}/register`, { branch_name, worktree_path }))
);

server.tool("submit_pr",
  "Promote a draft PR to open for human Triage (draft -> open, task in_progress -> review).\n\nQUANDO USARLO: SOLO dopo tutti i commit pushati e test_command + build pass locally (Quality Gate 9.3). BOUNDARY: register_branch crea draft; submit_pr promuove draft -> open per review. Poi l'umano merge via Console Triage — l'agent NON chiama gh pr merge (Constitution Rule 3).\nQUANDO NON USARLO: NOT senza aver verificato che test + build passano. NOT se il lavoro e' abbandonato -> usa close_pr.\nRESTITUISCE: {pr_status:'open', task_status:'review', submitted_at}.",
  {
    task_id: z.string().min(1).meta({
      description: "Task UUID with an active draft PR",
    }),
    title: z.string().min(1).meta({
      description: "PR title",
    }),
    body: z.string().optional().default("").meta({
      description: "PR description (markdown)",
    }),
  },
  async ({ task_id, title, body }) =>
    json(await post(`/api/v1/pull_requests/${task_id}/submit`, { title, body }))
);

server.tool("get_pr",
  "Get current PR state for a task (status, branch, worktree, review_feedback, commit SHAs).\n\nQUANDO USARLO: verificare 'is my PR still open?' prima di continuare il lavoro, o leggere review_feedback dopo PR rimandato indietro. Usa ?deep=true per includere kg_context inline — risparmia 2-3 tool call aggiuntivi.\nQUANDO NON USARLO: NOT per PR state di piu' task in una call -> usa list_tasks (contiene pr_state).\nRESTITUISCE: {pr_status, branch, worktree_path, review_feedback?, commit_shas[], merged_at?} + kg_context se deep=true.",
  {
    task_id: z.string().min(1).meta({
      description: "Task UUID",
    }),
    deep: z.boolean().optional().meta({ description: "Include inline KG context bundle (default true on MCP)" }),
  },
  async ({ task_id, deep }) => {
    const d = effectiveDeep(deep);
    return json(await get(`/api/v1/pull_requests/${task_id}?deep=${d}`));
  }
);

server.tool("triage_docs_change",
  "Run docs governance triage for a proposed docs change and return the deterministic PR label suggestion.\n\nQUANDO USARLO: agent vuole sapere quale label `triage:*` applicare a una diff docs-governance senza shellare il bot GitHub Actions.\nQUANDO NON USARLO: NOT per applicare label su GitHub — questo tool calcola solo la decisione; il workflow o l'operatore applica la label.\nRESTITUISCE: DocsGovernanceTriage payload con pr_label, score, confidence e hard_gates.",
  {
    diff_text: z.string().min(1).meta({
      description: "Unified diff text for the docs change",
    }),
    layer: z.enum(["api", "mcp", "llm-gateway", "kg", "code-examples", "narrative", "concept"]).meta({
      description: "Docs governance layer",
    }),
    change_type: z.string().min(1).max(100).meta({
      description: "Governance change type, e.g. additive or breaking_removal",
    }),
    context: z.record(z.string(), z.unknown()).optional().meta({
      description: "Optional hard-gate context",
    }),
  },
  async ({ diff_text, layer, change_type, context }) =>
    json(await post("/api/v1/docs_governance/triage", { diff_text, layer, change_type, context }))
);

server.tool("close_pr",
  "Abandon a PR without merging (close record + unlink branch).\n\nQUANDO USARLO: il lavoro non serve piu' o deve essere redone da zero (task di solito va in rejected/failed).\nQUANDO NON USARLO: NOT quando il lavoro e' pronto per review -> usa submit_pr. NOT per failure del task (usa update_task status='failed' in parallelo).\nRESTITUISCE: {pr_status:'closed', closed_reason, closed_at}.",
  {
    task_id: z.string().min(1).meta({
      description: "Task UUID",
    }),
    reason: z.string().optional().meta({
      description: "Reason for closing",
      examples: ["superseded by task X", "approach invalidated"],
    }),
  },
  async ({ task_id, reason }) =>
    json(await post(`/api/v1/pull_requests/${task_id}/close`, { reason }))
);

// --- Learnings ---

server.tool("check_learnings",
  "Search learnings DB (past post-mortems + prevention rules) for a risky action.\n\nQUANDO USARLO: PRIMA di qualunque azione production-affecting (deploy, migration, auth change, push to main, dependency upgrade) — Quality Gate 9.4. BOUNDARY: create_learning vs check_learnings = create scrive nuovo; check cerca rilevante PRE-azione rischiosa. BOUNDARY: check_learnings vs list_learnings = check = semantic match su situazione; list = enumerazione filtrata.\nQUANDO NON USARLO: NOT quando hai gia' un file/module path e vuoi learnings scoped -> usa graph_pattern. NOT per scrivere un nuovo learning -> usa create_learning.\nRESTITUISCE: list of {id, title, prevention, severity, module} ranked per rilevanza.",
  {
    q: z.string().min(1).max(500).meta({
      description: "Search keyword or short phrase",
      examples: ["migration transaction", "worktree push"],
    }),
    module: z.string().optional().meta({
      description: "Filter by module path",
      examples: ["api.db", "console/src/auth"],
    }),
  },
  async ({ q, module }) => json(await get(`/api/v1/learnings/check${qs({ q, module })}`))
);

server.tool("create_learning",
  "Persist a new learning (post-mortem) with prevention rule after an error or incident.\n\nQUANDO USARLO: DOPO aver hit un bug che vale la pena ricordare cross-session (es. 'z.record(1-arg) crashes MCP SDK'). BOUNDARY: create_learning vs check_learnings = create scrive nuovo; check cerca rilevante PRE-azione rischiosa.\nQUANDO NON USARLO: NOT per idea forward-looking improvement -> usa create_task kind='idea'. NOT per task operativo -> usa create_task.\nRESTITUISCE: {id, title, category, severity, created_at} — indexed by check_learnings.",
  {
    title: z.string().min(1).max(200).meta({
      description: "Learning title (concise, searchable)",
      examples: ["Zod v4 z.record() requires 2-arg signature"],
    }),
    category: z.enum(["deploy", "migration", "auth", "testing", "architecture", "security", "performance"]).meta({
      description: "Learning category",
    }),
    description: z.string().min(1).meta({
      description: "What happened (incident details)",
    }),
    prevention: z.string().min(1).meta({
      description: "How to prevent in the future (actionable rule)",
    }),
    severity: z.enum(["low", "medium", "high", "critical"]).meta({
      description: "Severity level",
    }),
    module: z.string().optional().meta({
      description: "Module path affected",
      examples: ["mcp-pir/index.mjs", "api.db"],
    }),
    tags: z.array(z.string()).optional().meta({
      description: "Tags for searchability",
    }),
    project: z.string().optional().meta({
      description: "Project slug",
      examples: ["marvisx"],
    }),
  },
  async ({ title, category, description, prevention, severity, module, tags, project }) =>
    json(await post("/api/v1/learnings", { title, category, description, prevention, severity, module, tags, project }))
);

server.tool("list_learnings",
  "List learnings with optional filters (category, project, severity, tags, free text).\n\nQUANDO USARLO: enumerare tutti i learning del progetto o filtrare per category/severity/tags. Utile per review periodica pattern consolidati. BOUNDARY: check_learnings vs list_learnings = check = semantic match su situazione; list = enumerazione filtrata.\nQUANDO NON USARLO: NOT per cercare learning applicabile a situazione rischiosa corrente -> usa check_learnings (semantic). NOT per singolo ID -> usa get_learning.\nRESTITUISCE: list of {id, title, category, severity, module, project, created_at, tags} paginato ordered per frequency DESC.",
  {
    project: z.string().optional().meta({
      description: "Filter by project slug",
      examples: ["marvisx", "c&i-tool"],
    }),
    category: z.enum(["deploy", "migration", "auth", "testing", "architecture", "security", "performance"]).optional().meta({
      description: "Filter by category",
    }),
    severity: z.enum(["low", "medium", "high", "critical"]).optional().meta({
      description: "Filter by severity",
    }),
    tags: z.string().optional().meta({
      description: "Comma-separated tags (OR logic)",
      examples: ["kg,mcp", "auth"],
    }),
    search: z.string().optional().meta({
      description: "Free text search across title/description/prevention",
    }),
    limit: z.number().int().min(1).max(200).optional().default(50).meta({
      description: "Max results (1-200)",
    }),
    offset: z.number().int().min(0).optional().default(0).meta({
      description: "Pagination offset",
    }),
  },
  async ({ project, category, severity, tags, search, limit, offset }) =>
    json(await get(`/api/v1/learnings${qs({ project, category, severity, tags, search, limit, offset })}`))
);

server.tool("get_learning",
  "Get a single learning by UUID.\n\nQUANDO USARLO: hai un learning ID (da list_learnings o check_learnings) e vuoi il body completo (description + prevention + tags). Usa ?deep=true per includere kg_context inline (context chain, related learnings) — risparmia 2-3 tool call aggiuntivi.\nQUANDO NON USARLO: NOT per ricerca semantica pre-azione -> usa check_learnings. NOT per enumerazione filtrata -> usa list_learnings.\nRESTITUISCE: {id, title, category, description, prevention, severity, tags, module, project, session, created_at, updated_at, frequency, last_occurrence} + kg_context se deep=true.",
  {
    learning_id: z.string().min(1).meta({
      description: "Learning UUID",
      examples: ["LRN-abc123-..."],
    }),
    deep: z.boolean().optional().meta({ description: "Include inline KG context bundle (default true on MCP)" }),
  },
  async ({ learning_id, deep }) => {
    const d = effectiveDeep(deep);
    return json(await get(`/api/v1/learnings/${encodeURIComponent(learning_id)}?deep=${d}`));
  }
);

// --- Knowledge Graph (spike, migration 065) ---

// Shared edge-type enum for cross-project filtering.
// Using z.array(z.enum(...)) instead of comma-separated string avoids the
// Zod v4 z.record() crash seen in session 90 and produces a clean repeatable
// query param shape `?edge_types=X&edge_types=Y`.
//
// Keep synchronized with the latest graph_edges CHECK(relation) migration and
// the other 5 sync places (see scripts/_drift_check.py check B):
//   - migrations/132_kg_pr_modifies.sql CHECK(relation)  (latest)
//   - api/services/graph_service.py EDGE_TYPES
//   - api/routers/graph.py EdgeType
//   - apps/docs/lib/kg-fetcher.ts edge_types
//   - apps/docs/lib/kg-schema-snapshot.json $.edge_types
const edgeTypeEnum = z.enum([
  "calls", "imports", "defines",
  "produces", "contains",
  "describes", "documents", "cites", "applies_to",
  "depends_on", "mentions", "refers_to", "shares_tag", "similar_to",
  "resolves_to",  // Phase 7.2: module stub -> file canonical bridge
  "modifies",     // KG PR-Impact (mig 132): pr_artifact -> function_artifact
]);

// Shared NODE_ID regex. Kept aligned with api/services/graph_service.NODE_ID_PATTERN
// and scripts/ast_parser.py::NODE_ID_RE. Any new prefix/kind must be added
// here, in the server-side pattern, and in the ast_parser constant at the same
// time (Phase 6 onboarded plan/brainstorm; Phase 7.2 consumers of this regex
// are graph_neighbors/impact/context + the new pin tools).
// Phase 1.5 E5-fix9 (mig 098): added policy/contract/transcript/report.
// Migration 125: added record for factual/admin documents.
// spike/rubric kept in regex for legacy node queryability — new inserts are
// restricted via _NODE_TYPE_ALLOWLIST (api/services/ingest/insert_saga.py)
// and graph_nodes.type CHECK constraint (migration 098 + 125).
const NODE_ID_RE = /^(py|ts|task|pr|commit|handoff|solution|learning|audit|spike|analysis|research|rubric|guide|mockup|project|file|hook|skill|command|plugin|plan|brainstorm|inbox|xlsx|policy|contract|transcript|record|report):(function|file|module|artifact|sheet):[a-zA-Z0-9_\-.]+$/;

server.tool("graph_neighbors",
  "[Power-user] Surgical 1-hop neighbour query for a KG node (calls/imports/defines/mentions/...) — per context completo su un nodo usa get_task/get_project/get_handoff/get_learning(deep=true). Usa questo tool solo quando hai bisogno di ispezionare il vicinato topologico diretto senza il bundle completo.\nQUANDO USARLO: refactor impact assessment su una singola function; verificare se un nodo ha caller inattesi; time-travel as_of su commit specifico; rank=suspect_write per bug write-through-read.\nQUANDO NON USARLO: NOT per context di un task/handoff/learning -> usa get_*(deep=true). NOT per BFS transitivo -> usa graph_impact.\nRESTITUISCE: list of {node_id, relation, direction, rank_score?} cap 200.",
  {
    node_id: z.string()
      .regex(NODE_ID_RE)
      .max(256)
      .meta({
        description: "KG Node ID with prefix:type:name format",
        examples: [
          "py:function:api.db.get_db",
          "ts:function:console.src.components.modal.open",
          "task:artifact:dddecec5-1234-5678",
          "handoff:artifact:2026-04-14-titolo",
          "learning:artifact:LRN-001",
        ],
      }),
    relation: z.enum(["calls", "imports", "defines"]).optional().meta({
      description: "Deprecated: single-relation filter (Fase 1). Prefer edge_types for cross-project",
    }),
    edge_types: z.array(edgeTypeEnum).optional().meta({
      description: "Fase 2: repeatable filter by relation type; supports cross-project edges. Union semantics",
      examples: [["calls", "imports"], ["mentions", "refers_to"]],
    }),
    project: z.string().max(50).regex(/^[a-z0-9][a-z0-9&\-]+$/).optional().meta({
      description: "Fase 2: filter source-node project (ARCH-01 project_scope=source). Neighbours may cross project boundaries",
      examples: ["marvisx"],
    }),
    direction: z.enum(["incoming", "outgoing", "both"]).default("both").meta({
      description: "incoming = who points to this; outgoing = what this points to",
    }),
    rank: z.enum(["none", "suspect_write"]).optional().default("none").meta({
      description: "Ranker: 'none' = raw neighbours; 'suspect_write' = score callers for write-intent anomaly (Fase 1b)",
    }),
    as_of: z.string()
      .regex(/^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2}(\.\d+)?)?Z?$/)
      .max(32)
      .optional()
      .meta({
        description: "Fase 1d: ISO timestamp for time-travel query. Omit for live view (excludes deprecated)",
        examples: ["2026-04-01", "2026-04-01T12:00:00Z"],
      }),
  },
  async ({ node_id, relation, edge_types, project, direction, rank, as_of }) => {
    // FastAPI expects repeated `edge_types=X&edge_types=Y` so we build the
    // querystring manually for array params and append it to qs() output.
    let url = `/api/v1/graph/neighbors/${encodeURIComponent(node_id)}${qs({ relation, project, direction, limit: 200, rank, as_of })}`;
    if (edge_types && edge_types.length) {
      const sep = url.includes("?") ? "&" : "?";
      url += sep + edge_types.map(t => `edge_types=${encodeURIComponent(t)}`).join("&");
    }
    return json(await get(url));
  }
);

server.tool("graph_hotspots",
  "[Power-user] DORA-style churn ranking: files/functions sorted by touch_count 7d/30d with bus-factor warning — per context completo su un progetto usa get_project(deep=true). Usa questo tool per analisi architetturale di rischio o pianificazione refactor.\nQUANDO USARLO: identificare moduli ad alto rischio prima di una release; trovare ownership gaps (bus_factor=1); pianificare tech debt.\nQUANDO NON USARLO: NOT per context di un progetto specifico -> usa get_project(deep=true). NOT per BFS da un nodo -> usa graph_impact.\nRESTITUISCE: list of {node_id, touch_count_7d, touch_count_30d, authors[], bus_factor} top N.",
  {
    window: z.enum(["7d", "30d", "total"]).default("30d").meta({
      description: "Rolling window: 7d/30d for recent churn, total for all-time",
    }),
    limit: z.number().int().min(1).max(100).default(20).meta({
      description: "Max results (1-100)",
    }),
    type_filter: z.enum(["function", "file", "all"]).default("file").meta({
      description: "Filter by node type: file (default), function, or all",
    }),
  },
  async ({ window, limit, type_filter }) => {
    return json(await get(`/api/v1/graph/hotspots${qs({ window, limit, type_filter })}`));
  }
);

server.tool("graph_impact",
  "[Power-user] BFS impact analysis: 'cosa si rompe se cambio X?' con ranker su direct callers + transitive list — per context completo usa get_task(deep=true). Usa questo tool per delete/rename risk assessment o pre-refactor sweep.\nQUANDO USARLO: prima di eliminare/rinominare una function critica; misurare blast radius di una migration; dependency cascade analysis.\nQUANDO NON USARLO: NOT per context standard -> usa get_*(deep=true). NOT per 1-hop only -> usa graph_neighbors.\nRESTITUISCE: {direct_callers[], transitive_list[], rank_score} depth configurabile.",
  {
    node_id: z.string()
      .regex(NODE_ID_RE)
      .max(256)
      .meta({
        description: "Target node ID",
        examples: ["py:function:api.db.get_db"],
      }),
    depth: z.number().int().min(1).max(5).default(2).meta({
      description: "BFS hops for transitive callers (1 = direct only, 2 default)",
    }),
    limit: z.number().int().min(1).max(200).default(50).meta({
      description: "Max transitive callers (hard-capped at 200)",
    }),
    edge_types: z.array(edgeTypeEnum).optional().meta({
      description: "Fase 2: repeatable filter; omitted defaults to 'calls' (Fase 1f). For cross-project impact include e.g. ['calls','depends_on','mentions']",
      examples: [["calls"], ["calls", "depends_on", "mentions"]],
    }),
    project: z.string().max(50).regex(/^[a-z0-9][a-z0-9&\-]+$/).optional().meta({
      description: "Fase 2: filter source-node project (target filter, ARCH-01)",
      examples: ["marvisx"],
    }),
  },
  async ({ node_id, depth, limit, edge_types, project }) => {
    let url = `/api/v1/graph/impact/${encodeURIComponent(node_id)}${qs({ depth, limit, project })}`;
    if (edge_types && edge_types.length) {
      const sep = url.includes("?") ? "&" : "?";
      url += sep + edge_types.map(t => `edge_types=${encodeURIComponent(t)}`).join("&");
    }
    return json(await get(url));
  }
);

server.tool("graph_context",
  "[Power-user] Rationale chain: function→commit→PR→task→handoff→learning in 1 call — per context su un singolo artefatto usa get_task/get_handoff/get_learning(deep=true). Usa questo tool quando hai un node_id e vuoi la storia completa del PERCHE'.\nQUANDO USARLO: archeologia di codice legacy ('perche esiste questa function?'); audit trail pre-refactor; linkare codice a decision record.\nQUANDO NON USARLO: NOT per workflow standard su task/handoff -> usa get_*(deep=true). NOT per vicinato topologico -> usa graph_neighbors.\nRESTITUISCE: chain {commits[], PR?, task?, handoffs[], learnings[]} per_category_limit configurabile.",
  {
    node_id: z.string()
      .regex(NODE_ID_RE)
      .max(256)
      .meta({
        description: "Source node ID to trace back. Typically function or file",
        examples: ["py:function:api.services.kg.populator.run"],
      }),
    per_category_limit: z.number().int().min(1).max(20).default(5).meta({
      description: "Max items per category (commits/prs/tasks/handoffs/learnings)",
    }),
    project: z.string().max(50).regex(/^[a-z0-9][a-z0-9&\-]+$/).optional().meta({
      description: "Fase 2: filter source-node project (ARCH-01 project_scope=source)",
      examples: ["marvisx"],
    }),
  },
  async ({ node_id, per_category_limit, project }) => {
    return json(await get(`/api/v1/graph/context/${encodeURIComponent(node_id)}${qs({ per_category_limit, project })}`));
  }
);

server.tool("graph_pattern",
  "[Power-user] Learnings scoped a un modulo/path specifico via KG — per learnings generici usa check_learnings. Usa questo tool per learnings chirurgici su un file/modulo specifico prima di toccare codice in quella area.\nQUANDO USARLO: 'quali learnings esistono per api.db?' prima di toccare il DB module; scoping pre-deployment di un servizio specifico.\nQUANDO NON USARLO: NOT per ricerca semantica generica pre-azione -> usa check_learnings. NOT per context di un nodo -> usa graph_context.\nRESTITUISCE: list of {learning_id, title, prevention, severity, scope_match_score}.",
  {
    scope: z.string().max(256).meta({
      description: "File path, dotted module name, or full node ID",
      examples: ["api/db.py", "api.db", "py:function:api.db.get_db"],
    }),
    limit: z.number().int().min(1).max(100).default(20).meta({
      description: "Max learnings returned",
    }),
  },
  async ({ scope, limit }) => {
    return json(await get(`/api/v1/graph/pattern${qs({ scope, limit })}`));
  }
);

server.tool("share_function",
  "[Power-user] Share URL + preview + neighbors + context + hotspot in 1 call per handoff di function a human o altro agent — alternativa leggera a graph_context+graph_neighbors combinati.\nQUANDO USARLO: handoff di una function specifica a un collega/agent con contesto completo; link shareable da inserire in un handoff doc.\nQUANDO NON USARLO: NOT per context workflow standard -> usa get_*(deep=true). NOT per impact analysis -> usa graph_impact.\nRESTITUISCE: {share_url, preview, neighbors[], context_chain, hotspot?}.",
  {
    qualified_name: z.string()
      .max(256)
      .regex(/^[a-zA-Z0-9_.\-]+$/)
      .meta({
        description: "Python qualified name. Must match live (non-deprecated) function node",
        examples: ["api.db.get_db", "api.services.graph_service.get_neighbors"],
      }),
    include: z.string().optional().meta({
      description: "CSV of blocks to include. Allowed: preview,neighbors,context,hotspot. Omit for all four",
      examples: ["preview,context", "neighbors,hotspot"],
    }),
    hours: z.number().int().min(1).max(720).optional().default(24).meta({
      description: "Hours until share URL expires (1-720, default 24)",
    }),
  },
  async ({ qualified_name, include, hours }) => {
    const result = await get(
      `/api/v1/graph/function-share/${encodeURIComponent(qualified_name)}${qs({ include, hours })}`
    );
    // Mirror share_file: prepend the absolute share URL host.
    if (result && typeof result.share_url === "string" && result.share_url.startsWith("/")) {
      result.share_url = `https://api.justaskmarvis.com${result.share_url}`;
    }
    return json(result);
  }
);

server.tool("graph_capabilities",
  "[Power-user] KG schema metadata per agent discovery (edge_types + node_kinds + prefixes + versions).\n" +
  "QUANDO USARLO: prima di costruire query graph_* o validare node_id pattern; cold-start agent discovery.\n" +
  "QUANDO NON USARLO: per query topologiche -> usa graph_neighbors/impact/context.\n" +
  "RESTITUISCE: {edge_types[], node_kinds[], node_prefixes[], schema_version}.\n" +
  "FALLBACK: se 5xx, usa lista statica edge_types noti 2026-04 (vedi kb/knowledge-graph.md).",
  {},
  async () => json(await get(`/api/v1/graph/capabilities`))
);

// --- Git ---

server.tool("git_log",
  "Recent commits for a project repo (SHA + subject + author + date).\n\nQUANDO USARLO: riassumere 'what happened recently on this project?' o trovare un commit SHA. Read-only wrapper su `git log`.\nQUANDO NON USARLO: NOT per PR+task linked a un commit -> usa graph_context sul commit node. NOT per diff -> usa git_diff.\nRESTITUISCE: list of {sha, subject, author, date} ordered chronological DESC.",
  {
    slug: z.string().min(1).max(50).meta({
      description: "Project slug",
      examples: ["marvisx"],
    }),
    limit: z.number().int().min(1).max(200).optional().default(20).meta({
      description: "Max entries",
    }),
  },
  async ({ slug, limit }) => json(await get(`/api/v1/projects/${encodeURIComponent(slug)}/git/log${qs({ limit })}`))
);

server.tool("git_diff",
  "Diff for a project repo vs a given ref (default HEAD~1).\n\nQUANDO USARLO: review dell'ultimo commit senza shell, o branch-vs-main diff passando ref=main. Read-only.\nQUANDO NON USARLO: NOT per stage/commit -> usa git via Bash in worktree (Constitution Rule 4). NOT per storia commit -> usa git_log.\nRESTITUISCE: {diff_text, stats:{files_changed, insertions, deletions}}.",
  {
    slug: z.string().min(1).max(50).meta({
      description: "Project slug",
      examples: ["marvisx"],
    }),
    ref: z.string().optional().meta({
      description: "Git ref to diff against (default: HEAD~1)",
      examples: ["HEAD~1", "main", "origin/main"],
    }),
  },
  async ({ slug, ref }) => json(await get(`/api/v1/projects/${encodeURIComponent(slug)}/git/diff${qs({ ref })}`))
);

// --- Audit ---

server.tool("get_audit_log",
  "Return recent audit entries (task mutations, PR transitions, auth events, hook blocks).\n\nQUANDO USARLO: investigare 'who did what when?' durante incident o post-mortem.\nQUANDO NON USARLO: NOT per code commit history -> usa git_log (audit copre PiR API actions, non git).\nRESTITUISCE: list of {timestamp, action_type, actor, target, detail} ordered DESC.",
  {
    limit: z.number().int().min(1).max(100).optional().default(20).meta({
      description: "Max entries (1-100)",
    }),
  },
  async ({ limit }) => json(await get(`/api/v1/audit${qs({ limit })}`))
);

server.tool("check_safety",
  "Validate file-write / bash-command against MarvisX Constitution BEFORE executing it.\n\nQUANDO USARLO: su provider senza native PreToolUse hook (Codex, Gemini, OpenClaw container) — mirror dell'enforcement che Claude Code ha gratis via hook.\nQUANDO NON USARLO: NOT su Claude Code con hook attivi — e' ridondante. NOT come sostituto della Constitution (e' un preflight, non un substitute).\nRESTITUISCE: {allowed:bool, violations[], reason?, rule_id?}.",
  {
    action_type: z.enum(["file_write", "bash_command"]).meta({
      description: "Type of action to validate",
    }),
    file_path: z.string().optional().meta({
      description: "Target file path for file_write actions",
    }),
    command: z.string().optional().meta({
      description: "Shell command to validate for bash_command actions",
    }),
    cwd: z.string().optional().meta({
      description: "Working directory for branch/repo-sensitive checks",
    }),
  },
  async ({ action_type, file_path, command, cwd }) => json(await checkSafety(action_type, { filePath: file_path, command, cwd }))
);

// --- Monitoring ---

server.tool("get_monitoring",
  "Live snapshot of MarvisX-01 server health (CPU, memory, disk, systemd services).\n\nQUANDO USARLO: un deploy potrebbe aver rotto qualcosa, o 'is the server healthy right now?'.\nQUANDO NON USARLO: NOT per metriche storiche o time-series -> Grafana/Prometheus diretto. Questo e' point-in-time.\nRESTITUISCE: {cpu_pct, memory:{used,total}, disk:{used,total}, services:[{name,state}]}.",
  {},
  async () => json(await get("/api/v1/monitoring/current"))
);

// --- Automations (n8n) ---

server.tool("list_automations",
  "List n8n workflows registered in MarvisX (id, name, active flag) via PiR proxy.\n\nQUANDO USARLO: scoprire quale workflow_id passare a trigger_automation.\nQUANDO NON USARLO: NOT per run history -> usa list_executions.\nRESTITUISCE: list of {workflow_id, name, active, updated_at}.",
  {},
  async () => json(await get("/api/v1/automations"))
);

server.tool("trigger_automation",
  "Fire an n8n workflow manually with optional input data.\n\nQUANDO USARLO: un umano chiede esplicitamente di runnare una specifica automation, o un workflow schedulato deve girare out-of-band. Admin-only; side effects reali (email, Airtable, telegram).\nQUANDO NON USARLO: NOT per verificare una run passata -> usa list_executions invece di re-trigger. NOT senza conferma umana (side effects).\nRESTITUISCE: {execution_id, started_at, workflow_id} — poi polla via list_executions.",
  {
    workflow_id: z.string().min(1).meta({
      description: "n8n workflow ID (alphanumeric)",
    }),
    data: z.record(z.string(), z.string()).optional().meta({
      description: "Optional input data for the workflow (key-value strings)",
    }),
  },
  async ({ workflow_id, data }) =>
    json(await post(`/api/v1/automations/${encodeURIComponent(workflow_id)}/trigger`, { data }))
);

server.tool("list_executions",
  "Recent n8n workflow execution records filtered by workflow_id or status.\n\nQUANDO USARLO: debug 'did that cron run?' o 'why did the workflow fail last night?'.\nQUANDO NON USARLO: NOT per workflow catalog -> usa list_automations.\nRESTITUISCE: list of {execution_id, workflow_id, status, started_at, ended_at, error_message?}.",
  {
    workflow_id: z.string().optional().meta({
      description: "Filter by workflow ID",
    }),
    status: z.string().optional().meta({
      description: "Filter by status",
      examples: ["success", "error", "running"],
    }),
    limit: z.number().int().min(1).max(100).optional().default(20).meta({
      description: "Max results (1-100)",
    }),
  },
  async ({ workflow_id, status, limit }) =>
    json(await get(`/api/v1/automations/executions${qs({ workflow_id, status, limit })}`))
);

// --- Finder (share) ---

server.tool("share_file",
  "Generate a temporary HTTPS URL to share one file with a human (expires after N hours).\n\nQUANDO USARLO: handoff di un file (doc, image, PDF, log) via chat/email.\nQUANDO NON USARLO: NOT per una Python function con KG context -> usa share_function (bundle callers/rationale/hotspot). share_file ritorna solo URL.\nRESTITUISCE: {url:absolute_https, expires_at, path}.",
  {
    path: z.string().min(1).meta({
      description: "File path relative to finder root or repo workspace",
      examples: ["projects/contasghei/output/report.pdf", "workspace/docs/report.md"],
    }),
    hours: z.number().int().min(1).max(720).optional().default(24).meta({
      description: "Hours until link expires (1-720)",
    }),
  },
  async ({ path, hours }) => {
    const result = await post("/api/v1/share", { path, hours });
    return json({
      ...result,
      url: `https://api.justaskmarvis.com${result.url}`,
    });
  }
);

// --- Knowledge Graph UX (P2): pins / resolve / landing / overview / orphans ---

server.tool("pin_graph_node",
  "Save a KG node as a personal bookmark (pin). Idempotent — pinning the same node twice updates the note. Use to bookmark frequently visited nodes (functions, files, tasks). Appears in graph_landing() saved_nodes slice.",
  {
    node_id: z.string().min(6).max(256).regex(/^[a-z]+:[a-z]+:.+$/).describe(
      "KG node ID to pin, e.g. 'py:function:api.db.get_db' or 'task:artifact:uuid'"
    ),
    note: z.string().max(500).optional().describe("Optional annotation for this pin (max 500 chars)"),
  },
  async ({ node_id, note }) =>
    json(await post("/api/v1/graph/pins", { node_id, note }))
);

server.tool("unpin_graph_node",
  "Remove a personal bookmark (pin) for a KG node. Returns 404 if the pin does not exist for the current user.",
  {
    node_id: z.string().min(6).max(256).regex(/^[a-z]+:[a-z]+:.+$/).describe(
      "KG node ID to unpin"
    ),
  },
  async ({ node_id }) =>
    json(await del(`/api/v1/graph/pins/${encodeURIComponent(node_id)}`))
);

server.tool("list_graph_pins",
  "List all personal KG bookmarks (pins) for the current user, ordered by most recently pinned. Pins on soft-deleted nodes are excluded.",
  {},
  async () => json(await get("/api/v1/graph/pins"))
);

server.tool("graph_resolve",
  "Resolve a file path to its KG node_id. Useful when you have a file path (e.g. 'api/db.py') and need the graph_nodes id for neighbors/impact/context queries. Returns 404 if the file is not indexed or not visible.",
  {
    path: z.string().min(1).max(1024).describe(
      "Relative file path (no .. or absolute paths). e.g. 'api/db.py' or 'console/src/components/App.tsx'"
    ),
  },
  async ({ path }) => json(await get(`/api/v1/graph/resolve${qs({ path })}`))
);

server.tool("graph_landing",
  "Get the KG landing bundle: top-10 hotspots (30d), last-20 recent artifacts (commits/PRs/tasks/handoffs), and your saved pins. Cached 60s per workspace. Use as the first call when opening the KG explorer or needing a quick project overview.",
  {},
  async () => json(await get("/api/v1/graph/landing"))
);

server.tool("graph_overview",
  "Get a LOD (level-of-detail) overview of the knowledge graph at macro or module level. macro = project hub nodes + aggregated cross-project edges. module = module/folder nodes within a scope. RBAC-filtered — cross-project edges to invisible projects are omitted with hidden_cross_project_count.",
  {
    level: z.enum(["macro", "module"]).describe(
      "macro = all project nodes; module = folder breakdown within a project scope"
    ),
    scope: z.string().max(256).optional().describe(
      "Required for level=module. Format: project:artifact:<slug>"
    ),
    cross_project: z.boolean().optional().default(true).describe(
      "Include cross-project edges (default: true)"
    ),
  },
  async ({ level, scope, cross_project }) =>
    json(await get(`/api/v1/graph/overview${qs({ level, scope, cross_project })}`))
);

server.tool("graph_orphans",
  "Find file nodes with no edges (orphans) within a project or module scope. Orphans are grouped by folder with deterministic colors. Useful for identifying dead code, stale docs, or unlinked files. Each sub-cluster is capped at 30 files (overflow_count reflects the rest).",
  {
    scope: z.string().min(1).max(256).regex(/^(project|module):artifact:.+$/).describe(
      "Scope to search: project:artifact:<slug> or module:artifact:<project>/<folder>"
    ),
  },
  async ({ scope }) => json(await get(`/api/v1/graph/orphans${qs({ scope })}`))
);

server.tool("graph_cosmo",
  "Cosmo canvas dataset: project super-nodes con degree + top-8 satellites per recency + aggregated cross-project edges (mentions|depends_on). Same data UI /graph canvas renders. Per full neighborhood usa graph_neighbors(project:artifact:<slug>). Returns {projects[], edges[]} — shape fisso 4 campi per project, 4 campi per edge.",
  {},
  async () => json(await get("/api/v1/graph/cosmo"))
);

server.tool("graph_function_share",
  "Generate a shareable URL + preview bundle for a Python/TS function — includes neighbors, context chain, and hotspot in 1 call. Use for handoff of a specific function to a human or another agent.\n\nQUANDO USARLO: share a function with its full KG context via a short-lived HTTPS URL; insert link into a handoff doc.\nQUANDO NON USARLO: NOT for general impact analysis -> usa graph_impact. NOT for task/handoff context -> usa get_*(deep=true).\nRESTITUISCE: {share_url, preview, neighbors[], context_chain, hotspot?}.",
  {
    qualified_name: z.string()
      .max(256)
      .regex(/^[a-zA-Z0-9_.\-]+$/)
      .describe(
        "Python/TS qualified function name matching a live (non-deprecated) graph node. e.g. 'api.db.get_db' or 'api.services.graph_service.get_neighbors'"
      ),
    include: z.string().optional().describe(
      "Comma-separated list of blocks to include: preview,neighbors,context,hotspot. Omit for all four. e.g. 'preview,context'"
    ),
  },
  async ({ qualified_name, include }) => {
    const result = await get(
      `/api/v1/graph/function-share/${encodeURIComponent(qualified_name)}${qs({ include })}`
    );
    if (result && typeof result.share_url === "string" && result.share_url.startsWith("/")) {
      result.share_url = `https://api.justaskmarvis.com${result.share_url}`;
    }
    return json(result);
  }
);

// --- Phase 1.5 E7: Ingest triage MCP tools (agent-native parity) ----------
// Each tool wraps an existing /api/v1/ingest/* endpoint so agents can drive
// the ingest pipeline without UI/HTTP fallback. Plan ref: P1.5.E7 §K.4 v2.0.2.

const _PROJECT_SLUG_RE = /^[a-z0-9][a-z0-9_&-]+$/;

server.tool("list_ingest_pending",
  "List ingest_pending rows con filtri status/project/limit. Phase 1.5 capability via MCP per agent.\n\nQUANDO USARLO: serve la coda triage (queued/parsing/awaiting_triage/parse_error/...) per un progetto o globale (visibility-filtered).\nQUANDO NON USARLO: NOT per dettaglio singolo row -> nessun get_ingest_pending dedicato; usa la list filtrata. NOT per cambiare stato -> approve/reject/patch.\nRESTITUISCE: list of IngestPendingItem (id, file_path, project_slug, status, classification, target_folder, target_filename, ...).",
  {
    status: z.enum([
      "queued", "parsing", "classified", "awaiting_triage", "approved",
      "inserted", "done", "parse_error", "rejected", "all",
    ]).optional(),
    project: z.string().regex(_PROJECT_SLUG_RE).optional(),
    limit: z.number().int().min(1).max(250).default(50),
  },
  async ({ status, project, limit }) => {
    const params = new URLSearchParams();
    if (status && status !== "all") params.set("status", status);
    if (project) params.set("project_slug", project);
    params.set("limit", String(limit));
    return json(await get(`/api/v1/ingest/pending?${params}`));
  }
);

server.tool("approve_ingest_pending",
  "Approve ingest_pending row → triggers saga move da input/ a target_folder + KG insert. Optional classification_override (logged decision_source=agent_override; backend support post-E5).\n\nQUANDO USARLO: row in awaiting_triage con target_folder/target_filename validi → approval umano via agent.\nQUANDO NON USARLO: NOT su row in stato diverso da awaiting_triage (409). NOT per cambiare project_slug -> usa patch_ingest_pending.\nRESTITUISCE: {id, status:approved} dopo enqueue saga.",
  {
    id: z.string().uuid(),
    classification_override: z.record(z.string(), z.string()).optional(),
  },
  async ({ id, classification_override }) => {
    const body = classification_override ? { classification_override } : {};
    return json(await post(`/api/v1/ingest/pending/${id}/approve`, body));
  }
);

server.tool("reject_ingest_pending",
  "Reject ingest_pending row → status=rejected, file resta orphan in input/.\n\nQUANDO USARLO: row non vuole essere ingestita (low signal, duplicato, off-topic) → libera la coda triage.\nQUANDO NON USARLO: NOT per cancellare il file fisico (resta orphan in input/). NOT per cambiare project_slug post-rejection -> patch_ingest_pending supporta lo stato rejected.\nRESTITUISCE: {id, status:rejected}.",
  {
    id: z.string().uuid(),
    reason: z.string().max(500),
  },
  async ({ id, reason }) => {
    return json(await post(`/api/v1/ingest/pending/${id}/reject`, { reason }));
  }
);

server.tool("patch_ingest_pending",
  "Change project_slug post-upload (E4). State machine: SOLO awaiting_triage/parse_error/rejected. Atomic copy-then-rename con path containment.\n\nQUANDO USARLO: file finito nel progetto sbagliato in upload → reroute prima dell'approve.\nQUANDO NON USARLO: NOT su row in flight (queued/parsing/approved/inserted/done) -> 422. NOT per cambiare classification (target_folder/filename) -> richiede E4 follow-up non ancora live.\nRESTITUISCE: IngestPendingItem aggiornato.",
  {
    id: z.string().uuid(),
    project_slug: z.string().regex(_PROJECT_SLUG_RE).optional(),
  },
  async ({ id, project_slug }) => {
    const body = project_slug ? { project_slug } : {};
    return json(await patch(`/api/v1/ingest/pending/${id}`, body));
  }
);

server.tool("upload_ingest",
  "Upload file in /data/projects/{slug}/input/ → triggers saga (parse → classify → awaiting_triage). Either file_path (server-side existing path) OR content_b64 (inline base64) — exactly one richiesto.\n\nQUANDO USARLO: agent vuole iniettare un nuovo documento nella pipeline ingest senza UI/curl manuale.\nQUANDO NON USARLO: NOT per zip multi-file -> usa /upload-zip via HTTP diretto (MCP wrapper non incluso in E7). NOT per modificare row esistente -> usa patch/approve/reject.\nRESTITUISCE: {project_slug, uploaded_files, queued_items, skipped_files[]}.",
  {
    project_slug: z.string().regex(_PROJECT_SLUG_RE),
    file_path: z.string().optional(),
    content_b64: z.string().optional(),
    filename: z.string(),
  },
  async ({ project_slug, file_path, content_b64, filename }) => {
    if (!file_path && !content_b64) {
      throw new Error("Either file_path or content_b64 required");
    }
    if (file_path && content_b64) {
      throw new Error("Provide file_path OR content_b64, not both");
    }
    const form = new FormData();
    form.append("project_slug", project_slug);
    let buf;
    if (content_b64) {
      buf = Buffer.from(content_b64, "base64");
    } else {
      const fs = await import("node:fs/promises");
      buf = await fs.readFile(file_path);
    }
    form.append("files", new Blob([buf]), filename);
    return json(await postForm(`/api/v1/ingest/upload-folder`, form));
  }
);

server.tool("classify_ingest",
  "Force re-classify ingest_pending row. Phase 1 = deterministic re-parse (parser_router); post P1.5.E5 = trigger Haiku #1 manuale senza cambi MCP/UI.\n\nQUANDO USARLO: classification originale era sbagliata e vuoi rilanciare il classifier (es. dopo training data update).\nQUANDO NON USARLO: NOT per parse_error (usa reparse_ingest che e' lo stesso path semanticamente). NOT su done/inserted (409 stato non valido).\nRESTITUISCE: {id, status:parsing}.",
  {
    id: z.string().uuid(),
    force: z.boolean().default(false),
  },
  async ({ id }) => {
    return json(await post(`/api/v1/ingest/pending/${id}/classify-force`, {}));
  }
);

server.tool("write_haiku_frontmatter",
  "Write LLM-suggested frontmatter to file (P1.5.E5.T6 opt-in, audit trail via ingest_change_history). Placeholder oggi: ritorna 501 finche' E5 non e' deployato.\n\nQUANDO USARLO: post-E5 deploy, agent vuole consolidare frontmatter suggerito in file. Oggi: NON usare in produzione.\nQUANDO NON USARLO: pre-E5 (501). Per modificare classification senza scrivere il file -> patch_ingest_pending.\nRESTITUISCE (post-E5): audit log entry + status. Pre-E5: 501.",
  { id: z.string().uuid() },
  async ({ id }) => {
    return json(await post(`/api/v1/ingest/pending/${id}/write-frontmatter`, {}));
  }
);

server.tool("reparse_ingest",
  "Re-parse ingest_pending row (single id) o batch (status=parse_error, admin only). Riusa parser_router via API invece di scripts/reparse_failed.py.\n\nQUANDO USARLO: row in parse_error → ritenta dopo fix parser/dipendenze. Batch utile per recovery post-deploy.\nQUANDO NON USARLO: NOT con id E status insieme (usa uno o l'altro). NOT per cambiare project_slug -> patch_ingest_pending.\nRESTITUISCE: single → {id, status:parsing}; batch → {queued_count, status}.",
  {
    id: z.string().uuid().optional(),
    status: z.literal("parse_error").optional(),
  },
  async ({ id, status }) => {
    if (id && status) {
      throw new Error("Provide id OR status, not both");
    }
    if (id) {
      return json(await post(`/api/v1/ingest/pending/${id}/reparse`, {}));
    }
    if (status) {
      return json(await post(`/api/v1/ingest/reparse-batch`, { status }));
    }
    throw new Error("Either id or status required");
  }
);

// ---------------------------------------------------------------------------
// Brain v1 (sub-01..05): 18 MCP tools wrap /api/v1/brain/* endpoints.
// Cross-cutting invariants:
//   - Zod v4 schemas use 2-arg `z.record(z.string(), z.string())` (MEMORY note
//     "Zod v4 z.record() in MCP tools", sessione 90).
//   - All wrappers proxy via Bearer — never bypass RBAC/visibility server-side.
// ---------------------------------------------------------------------------

// Helper: serialize list query params (z.array → repeated &key=value).
function listQs(params) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v == null || v === "") continue;
    if (Array.isArray(v)) {
      for (const item of v) {
        if (item != null && item !== "") p.append(k, String(item));
      }
    } else {
      p.set(k, String(v));
    }
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

async function patchWithIdempotency(path, body, idempotencyKey) {
  const headers = {
    Authorization: `Bearer ${TOKEN}`,
    "Content-Type": "application/json",
    "X-Agent-Name": "marvisx",
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const r = await fetch(`${API}${path}`, {
    method: "PATCH",
    headers,
    body: JSON.stringify(body),
  });
  if (!r.ok) await _rejectNonOk(r);
  return r.json();
}

async function postWithIdempotency(path, body, idempotencyKey) {
  const headers = {
    Authorization: `Bearer ${TOKEN}`,
    "Content-Type": "application/json",
    "X-Agent-Name": "marvisx",
  };
  if (idempotencyKey) headers["Idempotency-Key"] = idempotencyKey;
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) await _rejectNonOk(r);
  return r.json();
}

// --- Brain — Sub-01 (runs / events / journal) ---

server.tool("brain_runs",
  "List Brain cycle runs (envelope) per sub-05 §2.\n\nQUANDO USARLO: verificare quale ciclo Brain ha pubblicato per ultimo, stato (running|succeeded|partial|failed|superseded), trigger (batch|manual|backfill) e contatori. Con `cycle_key='latest'` il server risolve a `MAX(cycle_key) WHERE status='succeeded' AND superseded_by_run_id IS NULL` — usalo come primitivo di discovery prima di brain_events/brain_journal/brain_drift/brain_memory_operations/brain_findings (cosi' non devi sincronizzare cycle_key tra chiamate). Default agent: `cycle_key='latest', status=['succeeded']`.\nQUANDO NON USARLO: per leggere contenuto eventi/journal — quelle sono chiamate downstream. Non usarlo come 'health check' del Brain: lo stato `partial` e' normale (una source fallita non blocca le altre). Per il singolo run usa brain_runs_get.\nRESTITUISCE: {items:[{run_id, cycle_key, status, trigger, event_count, partial_failures, duration_ms, started_at, finished_at}], next_cursor, cycle_key, total_returned}.",
  {
    cycle_key: z.string().optional().meta({
      description: "YYYY-MM-DD or 'latest' (server-resolved).",
      examples: ["2026-05-15", "latest"],
    }),
    status: z.array(z.enum(["running", "succeeded", "partial", "failed", "superseded"])).optional().meta({
      description: "Filter by run status (repeatable).",
    }),
    trigger: z.array(z.enum(["batch", "manual", "backfill"])).optional().meta({
      description: "Filter by trigger origin.",
    }),
    include_superseded: z.boolean().optional().default(false).meta({
      description: "Include superseded runs in the result.",
    }),
    cursor: z.string().optional(),
    limit: z.number().int().min(1).max(200).optional().default(50),
  },
  async ({ cycle_key, status, trigger, include_superseded, cursor, limit }) =>
    json(await get(`/api/v1/brain/runs${listQs({ cycle_key, status, trigger, include_superseded, cursor, limit })}`))
);

server.tool("brain_runs_get",
  "Fetch a single Brain cycle run by run_id.\n\nQUANDO USARLO: hai un run_id (da brain_runs o WebSocket payload) e vuoi vedere envelope completo del ciclo (event_count, partial_failures, durata, scope, trigger). Utile dopo ricezione `marvisx:brain_cycle_changed` per ispezionare il run appena cambiato.\nQUANDO NON USARLO: per cercare l'ultimo ciclo — usa brain_runs con `cycle_key='latest'`. Per il contenuto del journal/drift/memory_ops/findings — usa i tool dedicati.\nRESTITUISCE: {run_id, workspace_id, cycle_key, status, trigger, started_at, finished_at, event_count, partial_failures, duration_ms}.",
  {
    run_id: z.string().min(1).meta({ description: "Brain run UUID hex." }),
  },
  async ({ run_id }) => json(await get(`/api/v1/brain/runs/${encodeURIComponent(run_id)}`))
);

server.tool("brain_events",
  "List raw Brain digest events for a cycle (sub-01 D6).\n\nQUANDO USARLO: ispezionare gli eventi grezzi (digest) di un ciclo — la base di evidenza che drift/memory-ops/findings citano. Quando una finding cita `digest_event:abc123` e vuoi vedere il contesto, brain_events e' la fonte di verita'. Cursor pagination stable `(observed_at DESC, event_id)`. Default: `cycle_key='latest', limit=50`.\nQUANDO NON USARLO: come surface narrativo per umano — usa brain_journal (aggrega + materializza per scope). Non usarlo per 'voglio sapere se qualcosa e' cambiato' — quelli sono drift signals. Gli eventi sono fatti, non interpretazioni: non aggregarli come 'punteggio di salute progetto'.\nRESTITUISCE: {items:[DigestEvent|DigestEventRedacted], next_cursor, cycle_key, run_id, redacted_count, total_returned}.",
  {
    cycle_key: z.string().optional().meta({ description: "YYYY-MM-DD or 'latest'." }),
    run_id: z.string().optional(),
    event_type: z.array(z.string()).optional().meta({
      description: "Filter by event_type (repeatable).",
      examples: [["task_changed", "commit_changed"]],
    }),
    source_project: z.string().optional(),
    cursor: z.string().optional(),
    limit: z.number().int().min(1).max(200).optional().default(50),
  },
  async ({ cycle_key, run_id, event_type, source_project, cursor, limit }) =>
    json(await get(`/api/v1/brain/events${listQs({ cycle_key, run_id, event_type, source_project, cursor, limit })}`))
);

server.tool("brain_journal",
  "List Brain journal entries (narrative L2 layer) for a cycle.\n\nQUANDO USARLO: ottenere il narrativo company/program/project di un ciclo — `{what_changed, decisions_observed, open_loops, notable_context, sources, tomorrow_watch}`. E' la vista materializzata sopra brain_events: leggibile da umano, con `sources[]` che linkano agli event_id originali. Default: `cycle_key='latest'`. Per il journal di un progetto: `scope_type=project, scope_key=marvisx`.\nQUANDO NON USARLO: per cercare segnali di problema (usa brain_drift), proposte di azione (brain_memory_operations) o conclusioni approvabili (brain_findings). Il journal e' contesto: dice 'cosa e' successo', non 'cosa fare'. Non scriverlo a mano: e' generato dal cycle aggregator (sub-01 D2), agent-write su brain_journal_entries e' bloccato dal router.\nRESTITUISCE: {items:[JournalEntry], cycle_key, run_id, total_returned}.",
  {
    cycle_key: z.string().optional().meta({ description: "YYYY-MM-DD or 'latest'." }),
    run_id: z.string().optional(),
    scope_type: z.enum(["company", "program", "project"]).optional(),
    scope_key: z.string().optional(),
    program_key: z.string().optional(),
    limit: z.number().int().min(1).max(200).optional().default(50),
  },
  async ({ cycle_key, run_id, scope_type, scope_key, program_key, limit }) =>
    json(await get(`/api/v1/brain/journal${listQs({ cycle_key, run_id, scope_type, scope_key, program_key, limit })}`))
);

// --- Brain — Sub-02 Drift Signals ---

server.tool("brain_drift",
  "List Brain drift signals (sub-02 §11.2) — knowledge gaps between observed and expected.\n\nQUANDO USARLO: trovare drift signals aperti (default `state=['open'], severity_min='low'`) per capire dove il sistema 'sta sterzando' rispetto agli ADR/spec/playbook. Filtri: signal_type, knowledge_form (adr|spec|playbook|tribal_memory|external_update|claimed_decision|unknown), severity_min, drift_axis (intent|context|both — CE4). Cursor pagination stable per `(severity DESC, detected_at DESC)`.\nQUANDO NON USARLO: per leggere il narrativo del ciclo — usa brain_journal. Per le azioni proposte sulla memoria — usa brain_memory_operations. Drift signal != finding: signal e' fatto osservato, finding e' conclusione approvabile.\nRESTITUISCE: {items:[DriftSignal|DriftSignalRedacted], cycle_key, run_id, redacted_count, total_returned, next_cursor}.",
  {
    cycle_key: z.string().optional(),
    run_id: z.string().optional(),
    scope_type: z.enum(["company", "program", "project"]).optional(),
    scope_key: z.string().optional(),
    signal_type: z.array(z.string()).optional(),
    knowledge_form: z.array(z.string()).optional(),
    severity_min: z.enum(["low", "medium", "high", "critical"]).optional().default("low"),
    confidence_min: z.number().min(0).max(1).optional().default(0),
    state: z.array(z.enum(["open", "superseded", "resolved", "dismissed"])).optional(),
    include_resolved: z.boolean().optional().default(false),
    drift_axis: z.array(z.enum(["intent", "context", "both"])).optional(),
    rule_id: z.array(z.string()).optional(),
    cursor: z.string().optional(),
    limit: z.number().int().min(1).max(200).optional().default(50),
  },
  async (input) => json(await get(`/api/v1/brain/drift${listQs(input)}`))
);

server.tool("brain_drift_get",
  "Fetch a single drift signal by signal_id.\n\nQUANDO USARLO: una finding/handoff cita `signal:abc123` e vuoi vedere il contesto completo (observed_delta, expected_direction_ref, evidence_chain, classifier_version). Usa anche dopo PATCH per verificare il nuovo `state`.\nQUANDO NON USARLO: per cercare per recurrence_key o pattern — usa brain_drift. Per cambiare stato — usa brain_drift_patch.\nRESTITUISCE: DriftSignal completo (o DriftSignalRedacted se cross-scope con qualche progetto invisibile).",
  {
    signal_id: z.string().min(1).meta({ description: "32-hex drift signal ID." }),
  },
  async ({ signal_id }) =>
    json(await get(`/api/v1/brain/drift/${encodeURIComponent(signal_id)}`))
);

server.tool("brain_drift_patch",
  "Lifecycle action on a drift signal (dismiss / acknowledge / resolve / reopen).\n\nQUANDO USARLO: operator gate — dopo aver letto il signal (brain_drift_get), decidere se 'dismiss' (rumore), 'acknowledge' (visto, non agire), 'resolve' (corretto upstream), 'reopen' (riaperto post-resolve). `reason` raccomandato per audit. Idempotency-Key obbligatoria per evitare double-write audit log.\nQUANDO NON USARLO: NON usarlo per cambiare evidence o classification — quelle sono immutabili. NON usarlo per chiudere findings (usa brain_findings_patch). Drift signal e' osservazione, dismiss=non utile, resolve=corretto upstream — non e' la stessa cosa di 'fix' del codice.\nRESTITUISCE: DriftSignal aggiornato con nuovo state + dismissed_by/dismissed_at o resolved_at.",
  {
    signal_id: z.string().min(1),
    action: z.enum(["dismiss", "acknowledge", "resolve", "reopen"]),
    reason: z.string().max(500).optional(),
    idempotency_key: z.string().optional().meta({
      description: "Idempotency-Key header. Replay-safe.",
    }),
  },
  async ({ signal_id, action, reason, idempotency_key }) =>
    json(await patchWithIdempotency(
      `/api/v1/brain/drift/${encodeURIComponent(signal_id)}`,
      { action, reason },
      idempotency_key,
    ))
);

// --- Brain — Sub-03 Memory Operations ---

server.tool("brain_memory_operations",
  "List Brain memory operations (sub-03 §11.2) — proposte di azione sulla memoria.\n\nQUANDO USARLO: trovare proposte pendenti (default `approval_state=['pending']`) di operation_type: REINFORCE, CONSOLIDATE, SUPERSEDE_CANDIDATE, PROVENANCE_HARDENING, ORPHAN_DETECTED, CONTRADICTION_DETECTED. Ogni op contiene `proposed_write.target_type` (task|learning|adr|guide|doc_patch|context_md_append|kg_edge_metric|none). Default agent: `cycle_key='latest', approval_state=['pending'], recurrence_min=1`.\nQUANDO NON USARLO: per signal di drift — usa brain_drift. Per findings approvabili (output finale) — usa brain_findings. Per applicare una proposta: brain_memory_operations_apply ritorna GUIDANCE, NON scrive.\nRESTITUISCE: {items:[MemoryOperation|MemoryOperationRedacted], cycle_key, run_id, redacted_count, total_returned, next_cursor}.",
  {
    cycle_key: z.string().optional(),
    run_id: z.string().optional(),
    scope_type: z.enum(["company", "program", "project"]).optional(),
    scope_key: z.string().optional(),
    operation_type: z.array(z.string()).optional(),
    approval_state: z.array(z.string()).optional(),
    include_terminal: z.boolean().optional().default(false),
    recurrence_min: z.number().int().min(1).optional().default(1),
    score_min: z.number().min(0).max(1).optional().default(0),
    cursor: z.string().optional(),
    limit: z.number().int().min(1).max(200).optional().default(50),
  },
  async (input) => json(await get(`/api/v1/brain/memory-operations${listQs(input)}`))
);

server.tool("brain_memory_operations_get",
  "Fetch a single memory operation by operation_id.\n\nQUANDO USARLO: hai un operation_id (da brain_memory_operations) e vuoi vedere proposed_write completo + evidence chain + recurrence info. Necessario prima di chiamare brain_memory_operations_apply per leggere il guidance contract.\nQUANDO NON USARLO: per cercare per recurrence_key — usa brain_memory_operations. Per applicare: vedi brain_memory_operations_apply (NO write).\nRESTITUISCE: MemoryOperation completa (o MemoryOperationRedacted se cross-scope con progetto invisibile).",
  {
    operation_id: z.string().min(1),
  },
  async ({ operation_id }) =>
    json(await get(`/api/v1/brain/memory-operations/${encodeURIComponent(operation_id)}`))
);

server.tool("brain_memory_operations_patch",
  "Lifecycle action on a memory operation (approve / dismiss / reject).\n\nQUANDO USARLO: operator gate — approva (sblocca apply guidance), dismiss (non utile per ora), reject (proposta sbagliata). `applied_artifact_ref` opzionale dopo aver creato artifact via guidance. Idempotency-Key obbligatoria.\nQUANDO NON USARLO: per applicare l'azione concreta — l'apply NON scrive, ritorna SOLO guidance. La scrittura effettiva e' agente-driven, MAI inline qui. Per bulk: brain_memory_operations_bulk_patch.\nRESTITUISCE: MemoryOperation aggiornata con nuovo approval_state.",
  {
    operation_id: z.string().min(1),
    approval_state: z.enum(["approved", "dismissed", "rejected"]),
    reason: z.string().max(500).optional(),
    applied_artifact_ref: z.string().max(500).optional(),
    idempotency_key: z.string().optional(),
  },
  async ({ operation_id, approval_state, reason, applied_artifact_ref, idempotency_key }) =>
    json(await patchWithIdempotency(
      `/api/v1/brain/memory-operations/${encodeURIComponent(operation_id)}`,
      { approval_state, reason, applied_artifact_ref },
      idempotency_key,
    ))
);

server.tool("brain_memory_operations_apply",
  "Get apply GUIDANCE for an approved memory operation (NO write — sub-03 §11.1).\n\nQUANDO USARLO: dopo aver `approved` un'operazione (via brain_memory_operations_patch), per ottenere `next_action.tool` (es. mcp__pir__create_task) + args + `must_include_in_tags` (es. `brain_memory_op:abc123`). L'apply NON scrive l'artifact — ritorna istruzioni. L'agente esegue il `next_action.tool` con i tag richiesti per stabilire la chain di audit.\nQUANDO NON USARLO: per scrivere direttamente l'artifact — questo endpoint ritorna SOLO guidance. Pattern: PATCH → apply (guidance) → call next_action.tool con must_include_in_tags. Non usarlo prima di approval_state='approved' (409 precondition).\nRESTITUISCE: {operation_id, next_action:{tool, args, must_include_in_tags}, operation_summary}.",
  {
    operation_id: z.string().min(1),
  },
  async ({ operation_id }) =>
    json(await post(`/api/v1/brain/memory-operations/${encodeURIComponent(operation_id)}/apply`, {}))
);

// --- Brain — Sub-04 Learn Findings ---

server.tool("brain_findings",
  "List Brain Learn findings (sub-04 §11.2) — conclusioni approvabili dal ciclo.\n\nQUANDO USARLO: trovare findings aperti (default `approval_state=['open']`) per drain del Triage Queue. Filtri: finding_type, severity_min, confidence_min (low|medium|high — TIER, non float), recurrence_min, regression_only, applied. Default agent: `cycle_key='latest', approval_state=['open'], severity_min='low'`. CE2 recency_factor disponibile read-time (se decay enabled in settings).\nQUANDO NON USARLO: per drift signals — usa brain_drift. Per proposte di azione (intermedio) — usa brain_memory_operations. Per applicare: vedi brain_findings_apply (GUIDANCE-only). NEVER multiplica confidence × severity in score composito (F10/FR1 anti-pattern).\nRESTITUISCE: {items:[Finding|FindingRedacted], cycle_key, run_id, redacted_count, redacted_evidence_count, total_returned, next_cursor}.",
  {
    cycle_key: z.string().optional(),
    run_id: z.string().optional(),
    scope_type: z.enum(["company", "program", "project"]).optional(),
    scope_key: z.string().optional(),
    finding_type: z.array(z.string()).optional(),
    severity_min: z.enum(["low", "medium", "high", "critical"]).optional().default("low"),
    confidence_min: z.enum(["low", "medium", "high"]).optional().default("low"),
    approval_state: z.array(z.string()).optional(),
    include_terminal: z.boolean().optional().default(false),
    recurrence_min: z.number().int().min(1).optional().default(1),
    regression_only: z.boolean().optional().default(false),
    applied: z.boolean().optional(),
    created_after: z.string().optional(),
    owner_user_id: z.string().optional(),
    cursor: z.string().optional(),
    limit: z.number().int().min(1).max(200).optional().default(50),
  },
  async (input) => json(await get(`/api/v1/brain/findings${listQs(input)}`))
);

server.tool("brain_findings_get",
  "Fetch a single finding by finding_id.\n\nQUANDO USARLO: hai un finding_id (da brain_findings o WS payload) e vuoi vedere proposta completa: title, summary, why_now, evidence, suggested_artifact, owner_hint, closure_condition (drift_signal_clears|memory_op_applied|artifact_exists|manual_attest), regression_of_finding_id. Necessario prima di brain_findings_apply per leggere il guidance contract.\nQUANDO NON USARLO: per ricerca semantica — usa brain_findings con filtri. Per modificare lo stato — usa brain_findings_patch.\nRESTITUISCE: Finding completo (o FindingRedacted se cross-scope con progetto invisibile).",
  {
    finding_id: z.string().min(1),
  },
  async ({ finding_id }) =>
    json(await get(`/api/v1/brain/findings/${encodeURIComponent(finding_id)}`))
);

server.tool("brain_findings_patch",
  "Lifecycle action on a finding (approve / dismiss / resolve).\n\nQUANDO USARLO: operator gate per gestire la coda 'Da decidere'. `approve` segna approval_state=approved MA NON crea artifact (sub-04 F1: GUIDANCE-only). `dismiss` = non utile. `resolve` = osservato comportamento atteso (richiede reason se closure_condition.kind=manual_attest). Idempotency-Key obbligatoria.\nQUANDO NON USARLO: per creare artifact dal finding — pattern e' PATCH approve → brain_findings_apply (guidance) → call next_action.tool. Per bulk: brain_findings_bulk_patch.\nRESTITUISCE: Finding aggiornato con nuovo approval_state + approved_by/approved_at o applied_artifact_ref.",
  {
    finding_id: z.string().min(1),
    approval_state: z.enum(["approved", "dismissed", "resolved"]),
    reason: z.string().max(500).optional(),
    applied_artifact_ref: z.string().max(500).optional(),
    idempotency_key: z.string().optional(),
  },
  async ({ finding_id, approval_state, reason, applied_artifact_ref, idempotency_key }) =>
    json(await patchWithIdempotency(
      `/api/v1/brain/findings/${encodeURIComponent(finding_id)}`,
      { approval_state, reason, applied_artifact_ref },
      idempotency_key,
    ))
);

server.tool("brain_findings_bulk_patch",
  "Bulk lifecycle patch on findings (cap 25).\n\nQUANDO USARLO: drain massivo (es. dismiss una classe di rumore). Max 25 finding_id per request (oltre → 413). Conflict invariant: one bad transition fails the whole batch (409, NO partial commit).\nQUANDO NON USARLO: per >25 — splitta. Per gestione individuale con motivazione — usa brain_findings_patch.\nRESTITUISCE: {results:[{finding_id, status}], applied_count, skipped_count}.",
  {
    finding_ids: z.array(z.string().min(1)).min(1).max(25),
    approval_state: z.enum(["approved", "dismissed", "resolved"]),
    reason: z.string().max(500).optional(),
    idempotency_key: z.string().optional(),
  },
  async ({ finding_ids, approval_state, reason, idempotency_key }) =>
    json(await patchWithIdempotency(
      "/api/v1/brain/findings:bulk",
      { finding_ids, approval_state, reason },
      idempotency_key,
    ))
);

server.tool("brain_findings_apply",
  "Get apply GUIDANCE for an approved finding (NO write — sub-04 F1).\n\nQUANDO USARLO: dopo aver `approved` una finding, per ottenere `next_action.tool` (es. mcp__pir__create_task con title/description/tags pre-compilato) + `must_include_in_tags=brain_finding:{id}` per audit chain. L'apply NON scrive l'artifact — ritorna istruzioni. L'agente esegue il next_action.tool con i tag richiesti.\nQUANDO NON USARLO: per scrivere artifact direttamente — pattern e' GUIDANCE-only. Non usarlo prima di approval_state='approved' (409). Non chiamarlo in loop 'ricomputa finche' la finding X appare': se non emerge, e' un problema del producer rule — file un learning via mcp__pir__create_learning.\nRESTITUISCE: {operation_id (=finding_id), next_action:{tool, args, must_include_in_tags}, operation_summary}.",
  {
    finding_id: z.string().min(1),
  },
  async ({ finding_id }) =>
    json(await post(`/api/v1/brain/findings/${encodeURIComponent(finding_id)}/apply`, {}))
);

// --- Brain — Sub-05 Surfaces (cycles recompute / capabilities) ---

server.tool("brain_cycles_recompute",
  "Force manual recompute of a Brain cycle (operator+, sub-01 §6.D4).\n\nQUANDO USARLO: dopo bug fix in un producer (collector/drift/memory_ops/learn), dopo backfill di dati upstream, o per debug `dry_run=true` (stima eventi senza scrivere). `sources=None` ricomputa tutto; `sources=['drift']` ricomputa solo drift (digest resta, findings citano i nuovi signal_id). Idempotency-Key obbligatoria. Concurrent calls sullo stesso ciclo serializzano via lease: il secondo caller ritorna 202 con il run_id dell'in-flight.\nQUANDO NON USARLO: come scheduler — il batch giornaliero gira da solo dopo brain_cutoff_hour_utc. Per cicli >30 giorni vecchi: il server rifiuta con `cycle_too_old` (force=true sblocca, audit log la marca come override). Mai chiamarlo in loop 'ricomputa finche' X appare'.\nRESTITUISCE: {status, cycle_key, run_id, event_count, journal_count, duration_ms, mode, dry_run}.",
  {
    cycle_key: z.string().min(1).meta({
      description: "YYYY-MM-DD cycle to recompute.",
      examples: ["2026-05-15"],
    }),
    sources: z.array(z.enum(["digest", "drift", "memory_ops", "learn"])).optional().meta({
      description: "Sub-phases to recompute (null=all).",
    }),
    force: z.boolean().optional().default(false).meta({
      description: "Bypass max_age guard (recompute_max_age_days).",
    }),
    dry_run: z.boolean().optional().default(false),
    idempotency_key: z.string().min(1).meta({
      description: "Required: Idempotency-Key header.",
    }),
  },
  async ({ cycle_key, sources, force, dry_run, idempotency_key }) =>
    json(await postWithIdempotency(
      `/api/v1/brain/cycles/${encodeURIComponent(cycle_key)}/recompute`,
      { sources, force, dry_run },
      idempotency_key,
    ))
);

server.tool("brain_capabilities",
  "Discover Brain schema metadata (enums + glyphs + node kinds) — sub-05 OD-11.\n\nQUANDO USARLO: cold-start dell'agente — leggere i Literal enum esposti dal Brain (event_types, signal_types, knowledge_forms, operation_types, finding_types, severities, confidence_tiers, drift_axes, approval_states, signal_states, run_statuses, closure_condition_kinds, knowledge_glyphs). Pattern equivalente di mcp__pir__graph_capabilities. Evita hardcoding constants. Schema_version aumenta quando i Literal cambiano.\nQUANDO NON USARLO: per dati live del ciclo — usa brain_runs/brain_events/etc. Non e' un health check.\nRESTITUISCE: {schema_version, event_types[], source_systems[], signal_types[], knowledge_forms[], operation_types[], finding_types[], severities[], confidence_tiers[], drift_axes[], approval_states[], finding_approval_states[], signal_states[], run_statuses[], run_triggers[], scope_types[], suggested_artifacts[], closure_condition_kinds[], knowledge_glyphs:{form: glyph}}.",
  {},
  async () => json(await get("/api/v1/brain/capabilities"))
);

// --- KG PR-Impact query tools (sub-02) ---
//
// Three read-side tools that mirror the REST surface in
// api/routers/pr_impact.py. Category A per kb/mcp-naming-policy.md
// (stateless queries — graph_ prefix).

server.tool("graph_pr_impact",
  "Get the full PR impact bundle: modified functions + transitive impact (depth-1) + branch metadata + populator status. Use to render the Codex lens for a specific PR or to answer 'what does this PR touch?'.\n\nRESTITUISCE: {pr_id, pr_metadata, modified_functions[], transitive_impact[], involved_projects[], visibility, next_offset, total_estimate, schema_version}.",
  {
    pr_id: z.string()
      .regex(/^pr:artifact:[0-9a-f-]{36}$/)
      .max(64)
      .describe(
        "Canonical PR node id, e.g. 'pr:artifact:9b2309e0-ed7d-4963-985a-e26e36837468'"
      ),
    depth: z.number().int().min(0).max(4).optional().default(1).describe(
      "Transitive BFS depth (0 = modified functions only). MVP supports up to 1; deeper depths land in v1.1."
    ),
    offset: z.number().int().min(0).optional().default(0),
    limit: z.number().int().min(1).max(200).optional().default(50),
    include_all: z.boolean().optional().default(false).describe(
      "Bypass the function_cap_default ceiling (slow on PRs touching 800+ functions)"
    ),
  },
  async ({ pr_id, depth, offset, limit, include_all }) =>
    json(await get(`/api/v1/graph/pr-impact/${encodeURIComponent(pr_id)}${qs({ depth, offset, limit, include_all })}`))
);

server.tool("graph_branches",
  "List branches with their open-PR rollup + freshness flag. Use as the data source for the SidebarActivity branch tree in the Codex lens, or to answer 'which branches have open PRs touching this codebase?'.\n\nRESTITUISCE: {branches: [{name, head_sha, head_commit_at, is_main, is_stale, open_pr_ids[], age_days}], main_head, main_head_at, next_offset, total_estimate, schema_version}.",
  {
    state: z.enum(["active", "stale", "all"]).optional().default("active").describe(
      "active = draft/open/merging PRs; stale = open PRs older than KG_BRANCH_STALE_DAYS; all = no state filter"
    ),
    project: z.string().max(64).optional(),
    offset: z.number().int().min(0).optional().default(0),
    limit: z.number().int().min(1).max(200).optional().default(50),
  },
  async ({ state, project, offset, limit }) =>
    json(await get(`/api/v1/graph/branches${qs({ state, project, offset, limit })}`))
);

server.tool("graph_conflicts",
  "Detect shared touched functions across 2-5 PRs (modifies edges intersection). Use when answering 'which of these PRs will collide if we merge them in parallel?'.\n\nRESTITUISCE: {conflicts: [{pr_ids, shared_function_id, shared_qualified_name, touch_kinds[]}], pr_ids_examined[], total, schema_version}.",
  {
    pr_ids: z.array(z.string().max(64))
      .min(2)
      .max(5)
      .describe(
        "2-5 PR identifiers — either bare task UUIDs ('9b2309e0-...') or canonical 'pr:artifact:<uuid>' form."
      ),
    project: z.string().max(64).optional(),
  },
  async ({ pr_ids, project }) => {
    // Build the repeated `pr_ids=X&pr_ids=Y` query param shape.
    const baseParams = project ? `project=${encodeURIComponent(project)}` : "";
    const idParams = pr_ids.map((p) => `pr_ids=${encodeURIComponent(p)}`).join("&");
    const sep = baseParams ? "&" : "";
    const url = `/api/v1/graph/conflicts?${baseParams}${sep}${idParams}`;
    return json(await get(url));
  }
);


server.tool("graph_semantic_modules",
  "List Brain-ratified semantic module clusters (sub-04 DORMANT v1). Returns an empty bundle while Brain v1 sub-03 Memory Ops is not in production — the response carries `backend_status: 'dormant'` so callers can no-op gracefully without a 5xx. Once live, the same endpoint will return ratified cluster names + the function_node_ids that belong to each cluster.\n\nRESTITUISCE: {semantic_modules: [{operation_id, cluster_name, paths[], function_node_ids[], ratified_at, ratified_by_display, aliases[]}], next_cursor, total_estimate, redacted_count, redacted_evidence_count, cycle_key, run_id, as_of, schema_version, backend_status}.",
  {
    project: z.string().max(64).optional(),
    cursor: z.string().max(512).optional(),
    limit: z.number().int().min(1).max(200).optional().default(50),
  },
  async ({ project, cursor, limit }) =>
    json(await get(`/api/v1/graph/semantic-modules${qs({ project, cursor, limit })}`))
);


// --- KG PR-Impact admin tools (sub-01 D5) ---
//
// Three operational tools (Category B per kb/mcp-naming-policy.md):
//   pr_impact_backfill   — enqueue a populator job for a PR
//   pr_impact_status     — list webhook deliveries + their job state
//   pr_impact_dlq_replay — re-enqueue a dead-letter delivery
//
// All three require operator role (admin token); replay is admin-only.

server.tool("pr_impact_backfill",
  "Enqueue a populator job for a PR. Use when the webhook missed a push or when you want to force a re-attribution after a code refactor. Honors PR_IMPACT_ENABLED='off' by returning status='skipped'.\n\nRESTITUISCE: {job_id, status: 'queued'|'skipped', reason?}.",
  {
    pr_task_id: z.string().min(36).max(36).describe(
      "Canonical PR task UUID (pull_requests.task_id), e.g. '9b2309e0-ed7d-4963-985a-e26e36837468'"
    ),
    force: z.boolean().optional().default(false).describe(
      "(reserved) bypass the incremental hash gate when the populator implements it"
    ),
    incremental: z.boolean().optional().default(true).describe(
      "Skip files whose content hash is unchanged from the last populator run"
    ),
  },
  async ({ pr_task_id, force, incremental }) =>
    json(await post("/api/v1/admin/pr-impact/backfill", { pr_task_id, force, incremental }))
);

server.tool("pr_impact_status",
  "List recent webhook deliveries with their pipeline state (status, retry_count, error_summary, job_count). Use for ops triage after a webhook flake or when investigating why a PR didn't get populated.\n\nRESTITUISCE: {items: [{delivery_id, source, event_type, pr_id, status, received_at, processed_at, retry_count, error_summary, job_count}], total}.",
  {
    pr_id: z.string().max(128).optional().describe(
      "Filter by pull_requests.task_id (the canonical pr_task_id passed in webhook payloads)"
    ),
    status: z.string().max(32).optional().describe(
      "Filter by delivery status: pending | processed | failed | skipped | dead"
    ),
    limit: z.number().int().min(1).max(200).optional().default(50),
  },
  async ({ pr_id, status, limit }) =>
    json(await get(`/api/v1/admin/pr-impact/deliveries${qs({ pr_id, status, limit })}`))
);

server.tool("pr_impact_dlq_replay",
  "Re-enqueue a dead-letter delivery. Resets the linked pr_impact_jobs rows (attempts=0, last_error=NULL) and flips the webhook_deliveries row back to status='pending' so the next sweep tick + freshly-queued job rerun the populator.\n\nUse only after diagnosing the failure that put the delivery in DLQ.\nRESTITUISCE: {delivery_id, job_id, reset_attempts: true}.",
  {
    delivery_id: z.string().min(1).max(128).describe(
      "X-GitHub-Delivery header value of the original webhook event"
    ),
  },
  async ({ delivery_id }) =>
    json(await post("/api/v1/admin/pr-impact/dlq/replay", { delivery_id }))
);

const transport = new StdioServerTransport();
await server.connect(transport);
