#!/usr/bin/env python3
# v2.1.0 - 2026-04-15 - KG Phase 1: --incremental <paths> + --handle-delete + file_state hash gate
# v2.0.0 - 2026-04-15 - KG Fase 2.y+2.z: cross-program pilot (4) + .claude/ infra-indexing (hook|skill|command|plugin)
# v1.0.0 - 2026-04-14 - KG Fase 2: cross-project populator (orchestrator + 3 inner fns)
"""Populate cross-project KG edges for Fase 2.

## Scope

Indexes the projects discovered at runtime (this instance's hub project plus
any deployment-configured programs/pilot slugs) and emits the 5 new Fase-2 edge
types on top of the existing Fase-1 code/artifact graph:

- `depends_on` — code symbol → code symbol (AST imports + URL literal matches)
- `mentions`   — .md/context/handoff → `project:artifact:{slug}` node
- `refers_to`  — .md → file node (on-demand, id = file:artifact:sha256(path)[:12])
- `cites`      — handoff → handoff/solution/learning cross-project
- `shares_tag` — .md ↔ .md (symmetric, cap top-K=20/source, generic-tag filter)
- `similar_to` — .md ↔ .md (symmetric, provider-calibrated cosine threshold,
                 top-N=5, per-source transaction with DELETE+UPSERT so the set
                 stays coherent)

The orchestrator is ~1 file with 1 entrypoint `populate_cross_project()` and 3
inner workers (`_extract_depends_on`, `_extract_prose_edges`, `_compute_similar_to`)
following SIMP-1 from plan v2.

## Single-writer contract (PAT-2)

This populator is **standalone**. It opens a plain `sqlite3.connect()` sync
connection and uses `BEGIN IMMEDIATE` per write chunk, reusing the helpers in
`scripts/_graph_writer.py` (PAT-3). Run it only when `pir-api.service` is
stopped or when you're sure no writer is active — the API pool is read-only
(`PRAGMA query_only=ON`) so concurrent reads never conflict with writes.

## Security (DI-A3)

File references to paths matching `SECRET_PATH_BLACKLIST` are skipped before
the file node is created — no `file:artifact:*` node is emitted for `.env`,
`*.key`, `*secret*`, `credentials.json`, `*.pem`. This is defense-in-depth
against a doc accidentally pointing at a secret: we don't want the graph to
surface secret filenames in search results.

## Performance contract

Target wall clock on 18 projects: <60s (gate). Strategy:
- ProcessPoolExecutor for .md parsing (PERF-3, flat file list)
- Single-pass regex with named groups (PERF-2, trie-style alternation)
- Inverted index for shares_tag (PERF-1, O(T+P²) with cap)
- sqlite-vec KNN for similar_to (PERF-4)
- CSafeLoader + mtime cache for frontmatter (PERF-8)

## Invocation

    python -m core.scripts.populate_cross_project                # auto-resolve DB
    python -m core.scripts.populate_cross_project --db /tmp/x.db
    python -m core.scripts.populate_cross_project --projects-root /tmp/fake-projects
    python -m core.scripts.populate_cross_project --skip-similar-to  # when no vec embeddings
    python -m core.scripts.populate_cross_project --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

# Phase 1 incremental helpers are defined in populate_artifacts (single source
# of truth for routing + hash gate + soft-delete). We re-use the pure ones
# here to keep both populators in lockstep.
from core.scripts.populate_artifacts import (
    _file_sha256,
    _file_state_forget,
    _file_state_record,
    _file_state_unchanged,
    _route_metadata_path,
)

import yaml

from core.scripts._frontmatter import parse_frontmatter
from core.scripts._graph_writer import (
    chunked_upsert_edges,
    chunked_upsert_nodes,
)

logger = logging.getLogger("populate_cross_project")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROJECTS_ROOT = Path("/data/projects")


# --- Configuration constants -------------------------------------------------

# Discovery is dynamic — projects are found by scanning the filesystem, never
# from a hardcoded name list (a stale list scanned the wrong projects in early
# prod runs).
#
# Discovery rule (in order of precedence):
#   1. env ACTIVE_SLUGS_OVERRIDE="slug1,slug2,..." → use exactly those (testing)
#   2. CORE_HUB_SLUGS always included (this instance's own hub project, plus any
#      slugs in env CROSS_PROJECT_HUB_SLUGS)
#   3. Any project whose `project.yaml` declares a `program` listed in env
#      CROSS_PROJECT_PROGRAMS (comma-separated), or whose slug matches a regex
#      in env CROSS_PROJECT_NAME_PATTERNS (comma-separated; back-compat naming)
#
# All program/name/pilot literals are deployment-specific and supplied via env;
# defaults are name-free so the shipped code carries no customer slugs.


def _env_set(name: str) -> frozenset[str]:
    raw = os.environ.get(name, "").strip()
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


# Hub project(s) always in scope: this instance's own slug + optional env extras.
CORE_HUB_SLUGS: frozenset[str] = frozenset({"marvisx"}) | _env_set("CROSS_PROJECT_HUB_SLUGS")

# Program values (project.yaml `program:`) that opt a project into cross-project
# indexing. Empty by default — set CROSS_PROJECT_PROGRAMS per deployment.
_CROSS_PROJECT_PROGRAMS: frozenset[str] = frozenset(
    p.lower() for p in _env_set("CROSS_PROJECT_PROGRAMS")
)

# Back-compat slug-name patterns (regex). Empty by default — set
# CROSS_PROJECT_NAME_PATTERNS (comma-separated regexes) per deployment.
_CI_PROJECT_NAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in _env_set("CROSS_PROJECT_NAME_PATTERNS")
)

# Explicit cross-program pilot slugs. Empty by default (deployment-specific);
# set CROSS_PROGRAM_PILOT_SLUGS per deployment. Each entry is still validated by
# `_filter_by_code_files`, so unknown/empty entries are skipped safely.
CROSS_PROGRAM_PILOT: frozenset[str] = _env_set("CROSS_PROGRAM_PILOT_SLUGS")


def _discover_active_slugs(projects_root: Path) -> frozenset[str]:
    """Discovery dinamica progetti in scope Fase 2.

    Override esplicito via env per testing/sandbox. Altrimenti scansiona
    `projects_root` per CORE_HUB_SLUGS + dir con `program` in
    CROSS_PROJECT_PROGRAMS + dir matching CROSS_PROJECT_NAME_PATTERNS.
    """
    import os
    override = os.environ.get("ACTIVE_SLUGS_OVERRIDE", "").strip()
    if override:
        return frozenset(s.strip() for s in override.split(",") if s.strip())

    discovered: set[str] = set(CORE_HUB_SLUGS)

    if not projects_root.exists():
        logger.warning("projects_root %s does not exist — only CORE_HUB_SLUGS",
                       projects_root)
        return frozenset(discovered)

    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        slug = project_dir.name
        if slug in discovered:
            continue
        yaml_path = project_dir / "project.yaml"
        if not yaml_path.exists():
            continue
        # Primary: project.yaml `program` listed in CROSS_PROJECT_PROGRAMS
        program_in_scope = False
        try:
            py = yaml.load(yaml_path.read_text(encoding="utf-8"),
                           Loader=yaml.CSafeLoader) or {}
            program = (py.get("program") or "").strip().lower()
            program_in_scope = bool(program) and program in _CROSS_PROJECT_PROGRAMS
        except (yaml.YAMLError, OSError):
            pass
        # Fallback: name pattern (back-compat for projects without explicit program)
        name_matches = any(p.match(slug) for p in _CI_PROJECT_NAME_PATTERNS)
        if program_in_scope or name_matches:
            discovered.add(slug)

    return frozenset(discovered)


def _filter_by_code_files(
    projects_root: Path,
    candidate_slugs: frozenset[str],
) -> frozenset[str]:
    """PERF-2: filtro secondario — solo slug con repo_path valido AND > 0 code file.

    Per ciascuno slug candidato:
      1. Legge `repo_path` da project.yaml (fallback: project_dir/repo)
      2. Verifica che la dir esista e sia un git repo
      3. Conta `git ls-files **/*.{py,ts,tsx}` > 0
    Restituisce solo gli slug che superano tutti e 3 i check.

    Tollera mancanze (project.yaml assente, repo_path inesistente, no code) →
    semplicemente esclude il candidato. Nessuna eccezione propagata: discovery
    deve essere robusta a stato filesystem inconsistente.
    """
    import os as _os
    import subprocess as _subprocess

    valid: set[str] = set()
    for slug in candidate_slugs:
        project_dir = projects_root / slug
        if not project_dir.is_dir():
            continue
        yaml_path = project_dir / "project.yaml"
        if not yaml_path.exists():
            continue
        try:
            py = yaml.load(yaml_path.read_text(encoding="utf-8"),
                           Loader=yaml.CSafeLoader) or {}
        except (yaml.YAMLError, OSError):
            continue
        repo_path_raw = py.get("repo_path")
        if not repo_path_raw or not isinstance(repo_path_raw, str):
            continue
        # Expand ~ and env vars per safety
        repo_path = Path(_os.path.expandvars(_os.path.expanduser(repo_path_raw)))
        if not repo_path.is_dir() or not (repo_path / ".git").exists():
            continue
        try:
            out = _subprocess.check_output(
                ["git", "ls-files", "**/*.py", "**/*.ts", "**/*.tsx"],
                cwd=repo_path, text=True,
                stderr=_subprocess.DEVNULL, timeout=10,
            )
        except (_subprocess.CalledProcessError, _subprocess.TimeoutExpired,
                FileNotFoundError):
            continue
        if any(line.strip() for line in out.split("\n")):
            valid.add(slug)
    return frozenset(valid)


def _discover_all_slugs(projects_root: Path) -> frozenset[str]:
    """Fase 2.y v2.0.0 (PAT AM-05): unified discovery.

    Returns: CI_SLUGS ∪ CROSS_PROGRAM_PILOT_filtered ∪ CORE_HUB_SLUGS
    where:
      - CI_SLUGS = _discover_active_slugs(projects_root) (esistente, c&i pattern)
      - CROSS_PROGRAM_PILOT_filtered = CROSS_PROGRAM_PILOT ∩ _filter_by_code_files(...)
        (PERF-2: skip slugs whose repo_path is invalid o senza code)
      - CORE_HUB_SLUGS sempre presente (safety net)

    Per testing: rispetta ACTIVE_SLUGS_OVERRIDE come _discover_active_slugs.
    """
    import os as _os
    override = _os.environ.get("ACTIVE_SLUGS_OVERRIDE", "").strip()
    if override:
        return frozenset(s.strip() for s in override.split(",") if s.strip())

    ci = _discover_active_slugs(projects_root)
    pilot_filtered = _filter_by_code_files(projects_root, CROSS_PROGRAM_PILOT)
    if pilot_filtered != CROSS_PROGRAM_PILOT:
        skipped = CROSS_PROGRAM_PILOT - pilot_filtered
        logger.info("cross-program pilot: %d/%d included, skipped (no valid repo_path or 0 code files): %s",
                    len(pilot_filtered), len(CROSS_PROGRAM_PILOT), sorted(skipped))
    return ci | pilot_filtered | CORE_HUB_SLUGS


# Backward-compat module-level ACTIVE_SLUGS: ora wraps unified discovery
# (CI + cross-program pilot + core hub) con default path /data/projects/.
# Tests should monkey-patch _discover_all_slugs or set ACTIVE_SLUGS_OVERRIDE
# env per fixture deterministica.
# Safe at import: discovery returns at least CORE_HUB_SLUGS anche se path missing.
try:
    ACTIVE_SLUGS: frozenset[str] = _discover_all_slugs(Path("/data/projects"))
except Exception as e:  # pragma: no cover (defensive: filesystem errors)
    logger.warning("ACTIVE_SLUGS discovery failed: %s — fallback to CORE_HUB_SLUGS", e)
    ACTIVE_SLUGS = CORE_HUB_SLUGS

# DI-A4: tags that are too generic to form meaningful shares_tag edges. A file
# tagged just "project" or "code" would link to ~everything and dilute the
# signal. The populator ALSO filters per-tag prevalence < 30% (i.e. tags that
# appear on a majority of docs are skipped) on top of this list.
GENERIC_TAGS_EXCLUDE: frozenset[str] = frozenset({
    "project", "code", "done", "active", "pending", "draft",
    "in_progress", "completed", "todo", "note", "doc", "index",
    # Phase 4.5 follow-up: parole comuni che diventano false positive ora
    # che _PROSE_RE accetta uppercase iniziale (es. "Test" in "## Test plan",
    # "Plan" in "## Plan v2"). NB: il filtro confronta lowercase, quindi
    # "Plan" diventa "plan" e matcha qui.
    "test", "plan", "fix", "use", "data", "name", "type", "user", "role",
    "step", "phase", "main", "info", "true", "false", "new", "old",
})

# DI-A3: path patterns we refuse to create `file:artifact:*` nodes for.
# Matches against the basename of the path (not the full path) so a reference
# to `foo/.env` is caught just like a reference to `.env`.
SECRET_PATH_BLACKLIST: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\.env(\..+)?$"),
    re.compile(r".*\.key$"),
    re.compile(r".*secret.*", re.IGNORECASE),
    re.compile(r"^credentials\.json$"),
    re.compile(r".*\.pem$"),
    re.compile(r".*\.crt$"),
    re.compile(r"^id_(rsa|ed25519|ecdsa|dsa)$"),
)

# DI-A6: absolute paths we won't create file nodes for even if they "exist".
# System/temporary directories referenced in a doc are almost certainly not
# meaningful graph edges.
# Fase 2.y v2.0.0 (PERF-3): added `.claude/worktrees/` to skip duplicate
# scans of feature-branch worktrees that mirror main repo structure (would
# inflate node count + create stale duplicate file refs).
FILESYSTEM_EXCLUDES: tuple[str, ...] = (
    "/var/", "/tmp/", "/usr/", "/etc/", "/proc/", "/sys/", "/dev/",
    "/boot/", "/root/", "/run/",
    ".claude/worktrees/",  # Fase 2.y PERF-3: skip worktree duplicates
)

# Cap: per-source limit on shares_tag edges. Without this cap a doc with 5
# popular tags could emit hundreds of edges and swamp the signal (DI-A4).
SHARES_TAG_MAX_EDGES_PER_SOURCE: int = 20

# Cap: top-N similar docs per source (PERF-4).
SIMILAR_TO_TOP_N: int = 5
SIMILAR_TO_THRESHOLD: float = 0.85  # active-provider cosine similarity floor

# The remote backend's cosine geometry calibrates to a lower floor (0.75) than
# the local Granite engine (0.85). The threshold value is unchanged from the
# pre-carve-out config — only the env-var/label names are now backend-agnostic.
_SIMILAR_TO_THRESHOLD_DEFAULTS: dict[str, float] = {
    "KG_SIMILAR_TO_THRESHOLD_DEFAULT": 0.85,
    "KG_SIMILAR_TO_THRESHOLD_REMOTE": 0.75,
    "KG_SIMILAR_TO_THRESHOLD_GRANITE_97M": 0.85,
    "KG_SIMILAR_TO_THRESHOLD_GRANITE_311M": 0.80,
}

_SIMILAR_TO_THRESHOLD_ENV_BY_MODE: dict[str, str] = {
    "remote": "KG_SIMILAR_TO_THRESHOLD_REMOTE",
    "dual": "KG_SIMILAR_TO_THRESHOLD_REMOTE",
    "granite_local": "KG_SIMILAR_TO_THRESHOLD_GRANITE_97M",
    "granite_97m": "KG_SIMILAR_TO_THRESHOLD_GRANITE_97M",
    "granite-97m": "KG_SIMILAR_TO_THRESHOLD_GRANITE_97M",
    "granite_remote": "KG_SIMILAR_TO_THRESHOLD_GRANITE_311M",
    "granite_311m": "KG_SIMILAR_TO_THRESHOLD_GRANITE_311M",
    "granite-311m": "KG_SIMILAR_TO_THRESHOLD_GRANITE_311M",
}

_SIMILAR_TO_PROVIDER_BY_MODE: dict[str, str] = {
    "remote": "remote",
    "dual": "remote",
    "granite_local": "granite_97m",
    "granite_97m": "granite_97m",
    "granite-97m": "granite_97m",
    "granite_remote": "granite_311m",
    "granite_311m": "granite_311m",
    "granite-311m": "granite_311m",
}

# Hard cap (defense in depth): if a single run would emit more edges than this
# we abort before writing so a misconfiguration (e.g. pointing at 1M .md files)
# doesn't explode the graph.
MAX_TOTAL_EDGES_PER_RUN: int = 100_000

# Known domains → project slug map. Populated at runtime from each project's
# `project.yaml.deploy.{api_url,url,console_url}` — see `_discover_projects`.
# Used by `_extract_depends_on` to match URL literals in code.
_URL_DOMAINS_TO_SLUG: dict[str, str] = {}


def _env_float(name: str) -> float:
    """Read a float env var with a local default and warning-only fallback."""
    default = _SIMILAR_TO_THRESHOLD_DEFAULTS[name]
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a float; using %.2f", name, raw, default)
        return default


def _resolve_similar_to_threshold(embedding_mode: str) -> float:
    """Resolve provider-calibrated `similar_to` threshold from EMBEDDING_MODE."""
    mode = (embedding_mode or "").strip().lower()
    env_name = _SIMILAR_TO_THRESHOLD_ENV_BY_MODE.get(
        mode, "KG_SIMILAR_TO_THRESHOLD_DEFAULT"
    )
    return _env_float(env_name)


def _resolve_similar_to_provider(embedding_mode: str) -> str:
    """Normalize EMBEDDING_MODE to the provider label stored on new KG edges."""
    mode = (embedding_mode or "").strip().lower()
    return _SIMILAR_TO_PROVIDER_BY_MODE.get(mode, mode or "unknown")


def _graph_edges_has_provider_column(conn: sqlite3.Connection) -> bool:
    return any(
        row[1] == "provider" for row in conn.execute("PRAGMA table_info(graph_edges)")
    )


# --- Frontmatter cache (PERF-8) ----------------------------------------------


def _load_frontmatter_cached(path: Path, cache: dict[str, Any]) -> dict[str, Any] | None:
    """Parse frontmatter with mtime cache.

    The cache dict is passed in by the orchestrator so it's shared across calls
    within a single run. Key format: `str(path)` → `{"mtime": float, "data": dict|None}`.
    On cache miss or stale mtime, re-parses via `parse_frontmatter`.
    """
    try:
        st = path.stat()
    except OSError:
        return None
    key = str(path)
    entry = cache.get(key)
    if entry is not None and entry["mtime"] == st.st_mtime:
        return entry["data"]
    data, _body = parse_frontmatter(path)
    cache[key] = {"mtime": st.st_mtime, "data": data}
    return data


# --- Node ID helpers ---------------------------------------------------------


def _project_node_id(slug: str) -> str:
    """`project:artifact:{safe-slug}` — target of `mentions` edges."""
    safe = re.sub(r"[^a-zA-Z0-9_\-.]", "_", slug)
    return f"project:artifact:{safe}"


def _file_node_id(abs_path: str) -> str:
    """PAT-8: `file:artifact:{sha256(abs_path)[:12]}`.

    Using a hash lets us reference paths containing non-ASCII, spaces, or
    other characters banned by NODE_ID_PATTERN (the 1h regex allows only
    `[a-zA-Z0-9_\\-.]` in the slug segment). 12 hex chars = 48 bits, collision
    probability is negligible for <10k file nodes per graph.
    """
    digest = hashlib.sha256(abs_path.encode("utf-8")).hexdigest()[:12]
    return f"file:artifact:{digest}"


def _handoff_node_id_from_filename(filename: str) -> str:
    """Match the convention used by populate_artifacts._handoff_id."""
    stem = filename
    if stem.endswith(".md"):
        stem = stem[:-3]
    if stem.startswith("handoff-"):
        stem = stem[len("handoff-"):]
    safe = re.sub(r"[^a-zA-Z0-9_\-.]+", "_", stem).strip("_") or "_unknown"
    return f"handoff:artifact:{safe}"


# --- Secret filter (DI-A3) ---------------------------------------------------


def _is_secret_path(path_str: str) -> bool:
    """True if the path basename matches SECRET_PATH_BLACKLIST.

    We check basename not full path so `scripts/foo.env` is caught, not just
    top-level `.env`. This accepts false positives (anything with "secret" in
    the name) which is fine — we'd rather skip a legitimate file than leak
    a secret reference into the graph.
    """
    base = Path(path_str).name
    return any(pat.match(base) for pat in SECRET_PATH_BLACKLIST)


def _is_excluded_system_path(path_str: str) -> bool:
    """True if the path begins with a known system/temp prefix (DI-A6)."""
    return any(path_str.startswith(prefix) for prefix in FILESYSTEM_EXCLUDES)


# --- Discovery --------------------------------------------------------------


def _discover_all_metadata_slugs(projects_root: Path) -> frozenset[str]:
    """Phase 3 KG full-rebuild: discovery permissivo per scope notturno.

    Restituisce TUTTI gli slug che hanno una directory sotto `projects_root`
    con `project.yaml` parse-able (anche minimo). Non filtra per program=c&i,
    non filtra per repo_path/code files. Usato dal full-rebuild notturno per
    coprire i 68 progetti metadata (vs i 24 del default scope).

    Project.yaml malformato → logger.warning + skip (NO silent skip, learning
    `673a3e04` family). Tags non interpretati: la rebuild scansiona solo
    metadata path (handoff/solution/audit/...), non ha bisogno di repo_path.
    """
    if not projects_root.exists():
        logger.warning("projects_root %s does not exist — empty discovery", projects_root)
        return frozenset()
    discovered: set[str] = set()
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        yaml_path = project_dir / "project.yaml"
        if not yaml_path.exists():
            continue
        try:
            py = yaml.load(yaml_path.read_text(encoding="utf-8"),
                           Loader=yaml.CSafeLoader) or {}
        except (yaml.YAMLError, OSError) as e:
            logger.warning("project.yaml malformato per %s — skip: %s",
                           project_dir.name, e)
            continue
        # Accetta qualsiasi project.yaml che non sia esplicitamente vuoto.
        # `slug` derivato da nome dir (autoritativo, project.yaml `slug` puo'
        # essere fuori sync).
        if isinstance(py, dict):
            discovered.add(project_dir.name)
        else:
            logger.warning("project.yaml not a dict per %s — skip", project_dir.name)
    return frozenset(discovered)


def _discover_projects(
    projects_root: Path,
    include_all_projects: bool = False,
) -> dict[str, dict[str, Any]]:
    """Load project.yaml for each active slug under `projects_root`.

    Default (`include_all_projects=False`): slug list = CI_SLUGS ∪
    CROSS_PROGRAM_PILOT_filtered ∪ CORE_HUB_SLUGS via `_discover_all_slugs`.
    24 progetti circa, backward-compat scope Fase 2.

    `include_all_projects=True` (Phase 3 full-rebuild): slug list = TUTTI gli
    slug con project.yaml valido sotto projects_root (~68 progetti).
    Override via ACTIVE_SLUGS_OVERRIDE env per testing applica a entrambi i
    branch.

    Returns `{slug: {path, project_yaml, docs_paths, memory_path, repo_path}}`.
    """
    if include_all_projects:
        active_slugs = _discover_all_metadata_slugs(projects_root)
        logger.info("[--include-all-projects] scope estesso: %d progetti", len(active_slugs))
    else:
        active_slugs = _discover_all_slugs(projects_root)
    logger.info("active slugs in scope: %d (%s)",
                len(active_slugs), ", ".join(sorted(active_slugs)))
    result: dict[str, dict[str, Any]] = {}
    for slug in active_slugs:
        project_dir = projects_root / slug
        if not project_dir.exists():
            logger.info("project dir missing: %s — skipping", project_dir)
            continue
        yaml_path = project_dir / "project.yaml"
        py: dict[str, Any] = {}
        if yaml_path.exists():
            try:
                py = yaml.load(yaml_path.read_text(encoding="utf-8"),
                               Loader=yaml.CSafeLoader) or {}
            except yaml.YAMLError as e:
                logger.warning("project.yaml parse error for %s: %s", slug, e)
                py = {}
        # Fase 2.y: resolve repo_path se presente (cross-program scan target).
        # repo_path è una stringa in project.yaml (POSIX absolute) → Path.
        # None se progetto non ha codice (doc-only) o repo_path mancante.
        repo_path_raw = py.get("repo_path") if isinstance(py.get("repo_path"), str) else None
        repo_path: Path | None = None
        if repo_path_raw:
            import os as _os
            candidate = Path(_os.path.expandvars(_os.path.expanduser(repo_path_raw)))
            if candidate.is_dir():
                repo_path = candidate
        result[slug] = {
            "slug": slug,
            "path": project_dir,
            "project_yaml": py,
            "docs_path": project_dir / "docs",
            "memory_path": project_dir / "memory",
            "context_path": project_dir / "context.md",
            "repo_path": repo_path,  # Fase 2.y: None for doc-only projects
        }
        # Harvest deploy domains for URL-regex matching (_extract_depends_on)
        deploy = py.get("deploy") or {}
        for key in ("api_url", "url", "console_url", "dashboard_url"):
            url = deploy.get(key)
            if not url or not isinstance(url, str):
                continue
            # Extract host portion: strip scheme + trailing path.
            m = re.match(r"^https?://([^/]+)", url.strip())
            if m:
                host = m.group(1).lower()
                _URL_DOMAINS_TO_SLUG[host] = slug
    logger.info("discovered %d projects, %d url domains",
                len(result), len(_URL_DOMAINS_TO_SLUG))
    return result


# --- Edge extractor 1/3: depends_on (AST imports + URL regex code) ----------


def _extract_depends_on(
    conn: sqlite3.Connection,
    projects: dict[str, dict[str, Any]],
) -> int:
    """Emit `depends_on` edges from code → code / code → project.

    Three sources:
    1. AST imports already emitted by Fase 1a (relation='imports') — we don't
       duplicate them; instead we detect imports where source and target nodes
       live in different project_ids and promote the relationship by emitting
       a parallel `depends_on` edge between the two. This keeps
       `imports` semantics (Python-level) while adding an application-level
       `depends_on` that's easy to query.
    2. URL literal regex over code files already indexed in `graph_nodes`
       (marvisx + any future cross-program AST run): match `https?://<host>/...`
       against `_URL_DOMAINS_TO_SLUG`. If host maps to a known project, emit
       `{source_code_node} depends_on {project_node}` with source='ast'.
    3. Direct URL scan on pilot repo code files (ROI day, task 96ed65fb).
       For every project with `repo_path is not None and slug != 'marvisx'`
       we `git ls-files` the repo, read each .py/.ts/.tsx/.js/.mjs, run the
       same URL regex, and emit edges anchored on on-demand `file:artifact:`
       nodes. Required because pilot repos are NOT currently AST-indexed
       into `graph_nodes` (populate_graph_chunked only scans REPO_ROOT =
       the hub project). Without Source 3, edges from external pilot repos
       toward the hub project are lost silently.

    Source=1 adds zero new scanning — all the work is a single SQL join.
    Source=2 walks `graph_nodes WHERE type IN ('function','file','module')`
    and reads the file contents once to search for URL literals. For MarvisX
    this is ~5k files and <5s.
    Source=3 walks pilot repo_paths (6 projects currently, <2k code files
    total); runtime <3s on ROI day dataset.

    Returns the number of edges written.
    """
    edges: list[dict[str, Any]] = []

    # ---- Source 1: promote cross-project imports to depends_on ----
    # Any existing `imports` edge where source and target have different
    # project_ids becomes `depends_on` (the original `imports` edge stays).
    cur = conn.execute(
        """
        SELECT DISTINCT e.source_id, e.target_id,
               ns.project_id AS src_proj, nt.project_id AS tgt_proj,
               e.source_file, e.source_line
          FROM graph_edges e
          JOIN graph_nodes ns ON ns.id = e.source_id
          JOIN graph_nodes nt ON nt.id = e.target_id
         WHERE e.relation = 'imports'
           AND ns.project_id IS NOT NULL
           AND nt.project_id IS NOT NULL
           AND ns.project_id != nt.project_id
        """
    )
    for row in cur.fetchall():
        src_id, tgt_id, src_proj, _tgt_proj, source_file, source_line = row
        edges.append({
            "source_id": src_id,
            "target_id": tgt_id,
            "relation": "depends_on",
            "confidence": 1.0,
            "source": "ast",
            "source_file": source_file,
            "source_line": source_line,
            "metadata": {"promoted_from": "imports"},
            "project_id": src_proj,
        })

    # ---- Source 2: URL literal regex on marvisx code files ----
    if _URL_DOMAINS_TO_SLUG:
        # Build a single alternation over known domains — escape each host for
        # safe regex inclusion, preserve longest-first ordering so `api.x.com`
        # wins over `x.com` for URL literals that'd match both.
        sorted_hosts = sorted(_URL_DOMAINS_TO_SLUG.keys(), key=len, reverse=True)
        host_alt = "|".join(re.escape(h) for h in sorted_hosts)
        url_re = re.compile(
            r"https?://(" + host_alt + r")(?:[/:?#\s\"']|$)",
            re.IGNORECASE,
        )
    else:
        url_re = None

    if url_re is not None:
        # Fase 2.y v2.0.0: scan code files in TUTTI i progetti (non solo l'hub).
        # Pre-Fase-2.y, lo scan era ristretto al progetto hub (solo source di
        # edges cross-project). Ora che indicizziamo cross-program, anche file
        # di repo esterni possono puntare all'hub (esempio: un api/main.py che
        # contiene un URL verso l'API dell'hub).
        cur = conn.execute(
            """
            SELECT id, file_path, project_id
              FROM graph_nodes
             WHERE type IN ('function','file','module')
               AND file_path IS NOT NULL
               AND deprecated_at IS NULL
            """
        )
        # Bucket by file_path — we read each file once, not once per node.
        by_path: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for row in cur.fetchall():
            nid, fpath, proj_id = row
            by_path[fpath].append((nid, proj_id))

        for fpath, nodes_for_path in by_path.items():
            abs_path = REPO_ROOT / fpath if not fpath.startswith("/") else Path(fpath)
            try:
                text = abs_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            # Project_id of the source file = project_id of its first node
            # (every node for a path has the same project_id by construction).
            src_project = nodes_for_path[0][1] if nodes_for_path else "marvisx"
            hits: set[str] = set()  # project slugs referenced in this file
            for m in url_re.finditer(text):
                host = m.group(1).lower()
                target_slug = _URL_DOMAINS_TO_SLUG.get(host)
                # Skip self-references: a marvisx file containing its own domain
                # shouldn't produce a self-edge.
                if target_slug and target_slug != src_project:
                    hits.add(target_slug)
            if not hits:
                continue
            # Pick the file-level node to anchor the edge (not every function
            # in the file, to avoid N^2 edges). If no file node exists, use
            # the first node as-is.
            file_node = next(
                (nid for (nid, _p) in nodes_for_path if nid.startswith(("py:file:", "ts:file:"))),
                nodes_for_path[0][0],
            )
            for slug in hits:
                edges.append({
                    "source_id": file_node,
                    "target_id": _project_node_id(slug),
                    "relation": "depends_on",
                    "confidence": 0.8,  # URL regex: less certain than AST
                    "source": "ast",
                    "source_file": fpath,
                    "metadata": {"detected_via": "url_literal", "domain": _first_matching_domain(slug)},
                    "project_id": src_project,
                })

    # ---- Source 3: direct URL scan on pilot repo code files --------------
    # ROI day fix (task 96ed65fb): Source 2 reads from `graph_nodes`, but
    # `populate_graph_chunked` (ast_parser) only indexes the hub project's
    # files (REPO_ROOT = the hub). External pilot repos therefore have zero
    # code nodes, so Source 2 skips them entirely even when they contain real
    # cross-program URL references (e.g. an external service's router emitting
    # an HTTP call toward the hub's API).
    #
    # Workaround until Fase 2.y.2 wires full cross-program AST indexing:
    # walk each pilot's `repo_path` directly, grep for known domains, and
    # emit depends_on edges anchored on on-demand `file:artifact:<sha>`
    # nodes. This mirrors the pattern already used by `_extract_prose_edges`
    # for cross-project .md references. ~6 pilots * <200 code files each =
    # <1k regex matches total, negligible runtime.
    if url_re is not None:
        file_nodes_to_upsert: list[dict[str, Any]] = []
        for slug, info in projects.items():
            repo_path: Path | None = info.get("repo_path")
            if repo_path is None or slug == "marvisx":
                # Skip marvisx (handled by Source 2 which already reads its
                # indexed graph_nodes) + projects without a resolved repo.
                continue
            # Use git ls-files to respect .gitignore and stay bounded.
            try:
                import subprocess as _subprocess
                out = _subprocess.check_output(
                    ["git", "ls-files",
                     "**/*.py", "**/*.ts", "**/*.tsx", "**/*.js", "**/*.mjs"],
                    cwd=repo_path, text=True,
                    stderr=_subprocess.DEVNULL, timeout=15,
                )
            except (OSError, _subprocess.CalledProcessError,
                    _subprocess.TimeoutExpired):
                continue
            for rel_line in out.split("\n"):
                rel = rel_line.strip()
                if not rel:
                    continue
                # Skip .claude/worktrees (PERF-3) + secret paths (DI-A3)
                if ".claude/worktrees/" in rel:
                    continue
                if _is_secret_path(rel):
                    continue
                abs_path = repo_path / rel
                try:
                    text = abs_path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                hits: set[str] = set()
                for m in url_re.finditer(text):
                    host = m.group(1).lower()
                    target_slug = _URL_DOMAINS_TO_SLUG.get(host)
                    if target_slug and target_slug != slug:
                        hits.add(target_slug)
                if not hits:
                    continue
                abs_str = str(abs_path.resolve())
                file_id = _file_node_id(abs_str)
                file_nodes_to_upsert.append({
                    "id": file_id,
                    "type": "file",
                    "name": abs_path.name[:60],
                    "qualified_name": f"file.{file_id.split(':')[-1]}",
                    "file_path": abs_str,
                    "line_number": None,
                    "metadata": {
                        "abs_path": abs_str,
                        "resolved": True,
                        "discovered_via": "cross_program_url_scan",
                    },
                    "project_id": slug,
                })
                for target_slug in hits:
                    edges.append({
                        "source_id": file_id,
                        "target_id": _project_node_id(target_slug),
                        "relation": "depends_on",
                        "confidence": 0.8,  # URL regex (same tier as Source 2)
                        "source": "ast",
                        "source_file": abs_str,
                        "metadata": {
                            "detected_via": "url_literal_cross_program",
                            "domain": _first_matching_domain(target_slug),
                        },
                        "project_id": slug,
                    })
        if file_nodes_to_upsert:
            chunked_upsert_nodes(conn, file_nodes_to_upsert)

    return chunked_upsert_edges(conn, edges)


def _first_matching_domain(slug: str) -> str | None:
    for host, s in _URL_DOMAINS_TO_SLUG.items():
        if s == slug:
            return host
    return None


# --- Edge extractor 2/3: prose edges (mentions + refers_to + cites + shares_tag)
# ----------------------------------------------------------------------------

# Regex used by `_parse_md_prose`. Multi-alternation single-pass (PERF-2):
# - `mention`  → bare project slug surrounded by word boundaries. We accept
#                `a-z0-9&\-` which covers canonical project slugs (incl. `&`).
# - `filepath` → path-like string with a known source extension.
# - `handoff`  → `handoff-YYYY-MM-DD-*.md` handoff filename.
# Named groups let us dispatch in Python without re-running the pattern.
_PROSE_RE = re.compile(
    r"""
    (?:
        (?P<handoff>handoff-\d{4}-\d{2}-\d{2}[a-zA-Z0-9_\-]*\.md)
        |
        (?P<filepath>(?:[A-Za-z0-9_./\-&]*?/)?[A-Za-z0-9_\-&]+\.(?:env|py|ts|tsx|js|mjs|md|yaml|yml|json|sql|sh))
        |
        (?<![a-zA-Z0-9])(?P<mention>[A-Za-z][A-Za-z0-9&\-]{2,40})(?![a-zA-Z0-9])
    )
    """,
    re.VERBOSE,
)


def _extract_prose_edges(
    conn: sqlite3.Connection,
    projects: dict[str, dict[str, Any]],
    workers: int = 4,
) -> int:
    """Scan every .md across `projects` and emit 4 edge families:
    `mentions`, `refers_to`, `cites`, `shares_tag`.

    Returns the total number of edges written.
    """
    # Flat file list (PERF-3): every .md under projects[*].docs_path +
    # memory_path + context.md root + any top-level .md.
    md_files: list[tuple[str, str]] = []  # (project_slug, abs_path)
    for slug, info in projects.items():
        for root in (info["docs_path"], info["memory_path"], info["path"]):
            if not root.exists():
                continue
            if root.is_file() and root.suffix == ".md":
                md_files.append((slug, str(root)))
                continue
            for p in root.rglob("*.md"):
                if p.is_symlink():
                    continue
                md_files.append((slug, str(p)))

    logger.info("parsing %d .md files across %d projects", len(md_files), len(projects))

    # Bug fix sessione 137: derivare active_slugs da projects.keys() invece del
    # ACTIVE_SLUGS module-global. Quando l'orchestrator gira con
    # --include-all-projects, projects.keys() contiene ~68 slug; ACTIVE_SLUGS
    # invece e' settato all'import a ~24 (default scope). Usare il global
    # global filtrava silently le mentions verso i progetti del scope esteso.
    active_slugs_dynamic: frozenset[str] = frozenset(projects.keys())

    # Parse in parallel. For each file we need: frontmatter dict, prose matches,
    # slug (inherit from owning project).
    frontmatter_cache: dict[str, Any] = {}
    parsed: list[dict[str, Any]] = []
    for (slug, abs_path) in md_files:
        data = _load_frontmatter_cached(Path(abs_path), frontmatter_cache)
        tags = set()
        if data:
            raw_tags = data.get("tags") or []
            if isinstance(raw_tags, list):
                tags = {str(t).strip().lower() for t in raw_tags if t}
            elif isinstance(raw_tags, str):
                tags = {t.strip().lower() for t in raw_tags.split(",") if t.strip()}
        parsed.append({
            "project_slug": slug,
            "path": abs_path,
            "frontmatter": data or {},
            "tags": tags,
        })

    # Collect raw text (after frontmatter close) for prose regex. We parse
    # text-only in main proc (not pickled via ProcessPool) because the regex
    # pass is already fast and avoiding IPC overhead keeps the orchestrator
    # simple. workers param is retained for future switch-over.
    _ = workers  # reserved

    # Pre-compute the list of valid handoff node ids so `cites` only emits
    # edges to handoffs we actually know about (FK safety).
    valid_handoff_ids = {
        row[0]
        for row in conn.execute("SELECT id FROM graph_nodes WHERE type='handoff'").fetchall()
    }
    edges: list[dict[str, Any]] = []
    new_project_nodes: list[dict[str, Any]] = []
    new_file_nodes: list[dict[str, Any]] = []
    new_handoff_source_nodes: list[dict[str, Any]] = []

    # Shared: build project nodes once (targets of mentions).
    seen_project_slugs: set[str] = set()
    for slug in projects.keys():
        new_project_nodes.append({
            "id": _project_node_id(slug),
            "type": "project",
            "name": slug,
            "qualified_name": f"project.{slug}",
            "file_path": None,
            "line_number": None,
            "metadata": {"slug": slug},
            "project_id": slug,
        })
        seen_project_slugs.add(slug)

    # Inverted index for shares_tag (PERF-1): tag → {md_node_id: project_slug}
    # We populate this in the main loop and fold into edges afterwards.
    tag_to_docs: dict[str, dict[str, str]] = defaultdict(dict)
    # Also track per-doc tag set for cap calculation.
    doc_tags: dict[str, tuple[set[str], str]] = {}

    # Resolve the "source md node" for each parsed file: a .md either already
    # has a handoff/solution node id in graph_nodes, or we create an on-demand
    # file node. We key by abs_path for a quick lookup.
    cur = conn.execute(
        "SELECT id, file_path FROM graph_nodes WHERE type IN ('handoff','solution','audit','spike','analysis','research','rubric','guide','mockup') "
        "AND file_path IS NOT NULL"
    )
    path_to_existing_id: dict[str, str] = {}
    for row in cur.fetchall():
        if row[1]:
            path_to_existing_id[str(row[1])] = row[0]

    for item in parsed:
        slug = item["project_slug"]
        abs_path = item["path"]
        tags = item["tags"]

        # Determine the source-side md node id. Prefer an existing handoff/
        # solution node (match on file_path — which is relative to repo root
        # for marvisx, absolute otherwise). Fallback: file:artifact:<hash>
        # node (created on demand, skipping secrets).
        #
        # path_to_existing_id is keyed by `file_path` as stored by
        # populate_artifacts (relative for marvisx handoffs, otherwise abs).
        # We try both.
        src_id: str | None = None
        # Check relative-to-projects-root form: /data/projects/marvisx/memory/x.md
        # populate_artifacts stored as projects/marvisx/memory/x.md for marvisx.
        for candidate in (
            abs_path,
            abs_path.replace("/data/projects/", "projects/"),
            str(Path(abs_path).relative_to(DEFAULT_PROJECTS_ROOT.parent))
                if str(abs_path).startswith(str(DEFAULT_PROJECTS_ROOT.parent))
                else abs_path,
        ):
            if candidate in path_to_existing_id:
                src_id = path_to_existing_id[candidate]
                break

        if src_id is None:
            # Skip secrets entirely (DI-A3). For SOURCE (the .md we're
            # parsing) we only screen the secret blacklist — the
            # system-path exclude (DI-A6) applies only to TARGETS of
            # refers_to edges (we trust our caller-provided projects_root,
            # even if it points into /tmp for tests).
            if _is_secret_path(abs_path):
                continue
            src_id = _file_node_id(abs_path)
            # Record the file node for UPSERT (we'll dedup later).
            new_file_nodes.append({
                "id": src_id,
                "type": "file",
                "name": Path(abs_path).name[:60],
                "qualified_name": f"file.{_file_node_id(abs_path).split(':')[-1]}",
                "file_path": abs_path,
                "line_number": None,
                "metadata": {
                    "abs_path": abs_path,
                    "resolved": True,
                    "discovered_via": "cross_project_md_scan",
                },
                "project_id": slug,
            })

        # Register in doc_tags for shares_tag later.
        if tags:
            relevant_tags = {t for t in tags if t not in GENERIC_TAGS_EXCLUDE}
            if relevant_tags:
                doc_tags[src_id] = (relevant_tags, slug)
                for t in relevant_tags:
                    tag_to_docs[t][src_id] = slug

        # Read text body once for prose regex.
        try:
            text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        # Strip frontmatter block before regexing so we don't double-match
        # slugs inside `tags: [marvisx]` etc.
        if text.startswith("---"):
            parts = text.split("---", 2)
            if len(parts) >= 3:
                text = parts[2]

        mentions_emitted: set[str] = set()
        refers_emitted: set[str] = set()
        cites_emitted: set[str] = set()

        for m in _PROSE_RE.finditer(text):
            if m.group("mention"):
                mention = m.group("mention").lower()
                # DI-A10: only emit if the mention is an EXACT slug of a
                # known active project, and never a self-reference.
                # Use dynamic active_slugs (=projects.keys()) to honour
                # --include-all-projects scope esteso (~68 vs ~24 default).
                if mention in active_slugs_dynamic and mention != slug:
                    tgt = _project_node_id(mention)
                    if tgt in mentions_emitted:
                        continue
                    mentions_emitted.add(tgt)
                    edges.append({
                        "source_id": src_id,
                        "target_id": tgt,
                        "relation": "mentions",
                        "confidence": 0.9,
                        "source": "frontmatter",  # prose, but 'source' enum is constrained
                        "source_file": abs_path,
                        "metadata": {"mention": mention},
                        "project_id": slug,
                    })
            elif m.group("filepath"):
                fpath = m.group("filepath")
                # DI-A3: ALWAYS refuse secret paths up-front. The system-path
                # check (DI-A6) runs AFTER resolution because a legitimate
                # reference to /tmp/<projects_root>/<slug>/... (test fixture
                # or custom deployment) is not a system file — we rely on the
                # "in_known_project" check after resolution to distinguish.
                if _is_secret_path(fpath):
                    continue
                # Try to resolve: absolute or relative to project root.
                candidate_paths = [Path(fpath)]
                if not fpath.startswith("/"):
                    candidate_paths.append(projects[slug]["path"] / fpath)
                    candidate_paths.append(REPO_ROOT / fpath)
                resolved_path: Path | None = None
                for cp in candidate_paths:
                    try:
                        if cp.exists() and cp.is_file():
                            resolved_path = cp.resolve()
                            break
                    except OSError:
                        continue
                if resolved_path is None:
                    continue
                resolved_str = str(resolved_path)
                # Secret paths are ALWAYS refused (DI-A3), independent of
                # which project owns them.
                if _is_secret_path(resolved_str):
                    continue
                # System paths (DI-A6) are refused UNLESS the file lives
                # inside a known project root — a test fixture may point
                # projects_root at /tmp/... and the excluder would otherwise
                # refuse every legitimate target. We therefore check only
                # targets that don't fall under any project.path.
                in_known_project = any(
                    resolved_str.startswith(str(pj["path"]))
                    for pj in projects.values()
                )
                if not in_known_project and _is_excluded_system_path(resolved_str):
                    continue
                tgt_id = _file_node_id(resolved_str)
                if tgt_id in refers_emitted or tgt_id == src_id:
                    continue
                refers_emitted.add(tgt_id)
                # Determine project_id of target from path prefix.
                tgt_project = "marvisx"
                for candidate_slug in projects.keys():
                    pdir = projects[candidate_slug]["path"]
                    try:
                        if str(resolved_path).startswith(str(pdir)):
                            tgt_project = candidate_slug
                            break
                    except ValueError:
                        pass
                new_file_nodes.append({
                    "id": tgt_id,
                    "type": "file",
                    "name": resolved_path.name[:60],
                    "qualified_name": f"file.{tgt_id.split(':')[-1]}",
                    "file_path": resolved_str,
                    "line_number": None,
                    "metadata": {
                        "abs_path": resolved_str,
                        "resolved": True,
                        "discovered_via": "refers_to_md_scan",
                    },
                    "project_id": tgt_project,
                })
                edges.append({
                    "source_id": src_id,
                    "target_id": tgt_id,
                    "relation": "refers_to",
                    "confidence": 0.85,
                    "source": "frontmatter",
                    "source_file": abs_path,
                    "metadata": {"matched_path": fpath},
                    "project_id": slug,
                })
            elif m.group("handoff"):
                handoff_filename = m.group("handoff")
                handoff_id = _handoff_node_id_from_filename(handoff_filename)
                if handoff_id in cites_emitted or handoff_id == src_id:
                    continue
                if handoff_id not in valid_handoff_ids:
                    continue  # FK safety: only cite known handoffs
                cites_emitted.add(handoff_id)
                edges.append({
                    "source_id": src_id,
                    "target_id": handoff_id,
                    "relation": "cites",
                    "confidence": 0.85,
                    "source": "frontmatter",
                    "source_file": abs_path,
                    "metadata": {"cited_filename": handoff_filename},
                    "project_id": slug,
                })

    # ---- shares_tag (inverted index + cap + filters, DI-A4/PERF-1) ----
    total_docs = len(doc_tags)
    # Filter tags: skip any tag that's on >30% of docs (too broad to be useful).
    min_prevalence = max(1, int(0.01 * total_docs))  # at least 1 doc
    max_prevalence = max(2, int(0.30 * total_docs))  # up to 30% of docs

    per_source_counts: dict[str, int] = defaultdict(int)
    shares_emitted: set[tuple[str, str]] = set()  # (src, tgt) ordered
    for tag, doc_map in tag_to_docs.items():
        if len(doc_map) < min_prevalence or len(doc_map) > max_prevalence:
            continue
        if len(doc_map) < 2:
            continue
        # Pair docs sharing this tag. itertools.combinations enumerates each
        # pair once — we emit a single directed edge (a,b) for storage, the
        # router surfaces both sides via undirected_neighbors (ARCH-02).
        doc_ids = sorted(doc_map.keys())
        for a, b in itertools.combinations(doc_ids, 2):
            # Cap-aware emission: stop adding for a doc when it hits the cap.
            if per_source_counts[a] >= SHARES_TAG_MAX_EDGES_PER_SOURCE:
                break
            if (a, b) in shares_emitted:
                continue
            shares_emitted.add((a, b))
            per_source_counts[a] += 1
            # Also count on b so incoming-ish symmetry stays fair.
            per_source_counts[b] += 1
            edges.append({
                "source_id": a,
                "target_id": b,
                "relation": "shares_tag",
                "confidence": 0.7,
                "source": "frontmatter",
                "metadata": {
                    "shared_tag": tag,
                    "symmetric": True,  # ARCH-02
                },
                "project_id": doc_map[a],
            })

    # ---- write nodes first (FK safety), then edges ----
    # Dedup new_file_nodes and new_project_nodes by id (same id can appear
    # multiple times across parses).
    def _dedup_by_id(lst: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for n in lst:
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            out.append(n)
        return out

    all_new_nodes = _dedup_by_id(new_project_nodes + new_file_nodes + new_handoff_source_nodes)
    chunked_upsert_nodes(conn, all_new_nodes)

    # Safety gate on edge count.
    if len(edges) > MAX_TOTAL_EDGES_PER_RUN:
        logger.warning(
            "populator emitted %d edges (> hard cap %d) — truncating for safety",
            len(edges), MAX_TOTAL_EDGES_PER_RUN,
        )
        edges = edges[:MAX_TOTAL_EDGES_PER_RUN]

    n_edges = chunked_upsert_edges(conn, edges)
    logger.info(
        "prose edges: %d edges, %d new project nodes, %d new file nodes",
        n_edges, len(new_project_nodes), len(new_file_nodes),
    )
    return n_edges


# --- Edge extractor 3/3: similar_to via active provider/sqlite-vec (PERF-4) ---


_VEC0_EXTENSION_PATH: str = "/data/pir/lib/vec0"


def _compute_similar_to(
    conn: sqlite3.Connection,
    projects: dict[str, dict[str, Any]],
    threshold: float | None = None,
    top_n: int = SIMILAR_TO_TOP_N,
    vec0_path: str = _VEC0_EXTENSION_PATH,
) -> int:
    """Emit `similar_to` edges using the active embedding provider (vec0 KNN).

    Bridge `graph_nodes` (string ids, `file_path` column) with the semantic
    search store introduced in migration 040:
      - `documents(id INTEGER PK, file_path UNIQUE, project, ...)`
      - `vec_documents(doc_id INTEGER PK, embedding float[512])` — vec0 vtable
    Mapping: `graph_nodes.file_path` → `documents.file_path` → `documents.id`
    (= `vec_documents.doc_id`).

    Uses the vec0 native KNN syntax (`WHERE embedding MATCH ? AND k = ?
    ORDER BY distance`) — `vec_distance_cosine()` does NOT exist in vec0.
    Per-source transaction (DI-A2): DELETE existing `similar_to` edges first,
    then UPSERT the top-N neighbours with cosine > threshold. On crash the
    source keeps its old edge set (rollback) or its new set (commit), never
    a half-populated state.

    Returns the number of edges written. Returns 0 with a WARNING (not an
    error) when vec0 is unavailable or `vec_documents`/`documents` are empty
    — `shares_tag` still provides tag-level connectivity.
    """
    embedding_mode = os.environ.get("EMBEDDING_MODE", "remote")
    active_threshold = (
        _resolve_similar_to_threshold(embedding_mode)
        if threshold is None
        else threshold
    )
    provider = _resolve_similar_to_provider(embedding_mode)
    has_provider_column = _graph_edges_has_provider_column(conn)

    # Guard 1: documents table present? (migration 040 always ships it,
    # but an old checkout may lag.)
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='documents'"
    ).fetchone():
        logger.warning(
            "documents table missing — similar_to edges DISABLED. "
            "Apply migration 040_semantic_search.sql first."
        )
        return 0

    # Guard 2: load vec0 extension. Without this the virtual table
    # `vec_documents` can be queried for row existence via sqlite_master but
    # ANY SELECT/INSERT against it fails with `no such module: vec0`.
    try:
        conn.enable_load_extension(True)
    except AttributeError:
        logger.warning(
            "Python sqlite3 built without --enable-loadable-sqlite-extensions "
            "— similar_to edges DISABLED. Rebuild Python with extension support."
        )
        return 0
    try:
        try:
            conn.load_extension(vec0_path)
        except sqlite3.OperationalError as e:
            logger.warning(
                "vec0 extension not available at %s: %s — similar_to edges DISABLED. "
                "shares_tag still provides tag-level connectivity.",
                vec0_path, e,
            )
            return 0
    finally:
        conn.enable_load_extension(False)

    # Guard 3: vec_documents virtual table present? (Created at runtime by
    # the API in `api/db.py` / `api/services/embedding_service.py`; an agent
    # run before the API has booted once would hit an empty DB.)
    if not conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='vec_documents'"
    ).fetchone():
        logger.warning(
            "vec_documents table missing — similar_to edges DISABLED. "
            "Boot the API once so it CREATEs the vec0 virtual table."
        )
        return 0

    # Build node↔doc mapping. We need graph_node.id (string) ↔ doc_id (INTEGER)
    # via the file_path join — `graph_nodes.file_path` stores the absolute path
    # to the markdown file, matching `documents.file_path`.
    md_types = (
        "handoff", "solution", "learning", "audit", "spike", "analysis",
        "research", "rubric", "guide", "mockup",
    )
    type_placeholders = ",".join(["?"] * len(md_types))
    cur = conn.execute(
        f"""
        SELECT n.id AS node_id, n.project_id AS project_id,
               n.file_path AS file_path
          FROM graph_nodes n
         WHERE n.type IN ({type_placeholders})
           AND n.deprecated_at IS NULL
           AND n.file_path IS NOT NULL
        """,
        md_types,
    )
    candidates = cur.fetchall()

    node_to_doc: dict[str, int] = {}
    node_meta: dict[str, str | None] = {}  # node_id → project_id
    doc_to_node: dict[int, str] = {}
    # Bug fix sessione 137: graph_nodes.file_path e' RELATIVO al parent del
    # metadata path ('projects/marvisx/memory/handoff-X.md' per marvisx,
    # path ASSOLUTO per altri progetti — vedi populate_artifacts:_rel_file_path).
    # documents.file_path invece e' SEMPRE ASSOLUTO ('/data/projects/...').
    # Tentiamo entrambe le forme per il lookup.
    for row in candidates:
        node_id, project_id, file_path = row[0], row[1], row[2]
        if not file_path:
            continue
        candidate_paths = [file_path]
        if file_path.startswith("projects/"):
            # Convert relative 'projects/X/...' → absolute '/data/projects/X/...'
            candidate_paths.append("/data/" + file_path)
        doc = None
        for cp in candidate_paths:
            doc = conn.execute(
                "SELECT id FROM documents WHERE file_path = ?", (cp,)
            ).fetchone()
            if doc:
                break
        if not doc:
            continue
        doc_id = int(doc[0])
        node_to_doc[node_id] = doc_id
        node_meta[node_id] = project_id
        doc_to_node[doc_id] = node_id

    if not node_to_doc:
        logger.warning(
            "no graph_nodes↔documents mapping found — similar_to edges DISABLED. "
            "Either no .md graph nodes have file_path set, or no matching rows "
            "in documents (run a semantic search reindex first)."
        )
        return 0

    logger.info(
        "similar_to: scanning %d md-type nodes with doc_id mapping "
        "(mode=%s provider=%s threshold=%.2f)",
        len(node_to_doc), embedding_mode, provider, active_threshold,
    )

    n_edges = 0
    # k = top_n + 1 because vec0's KNN always includes the query vector itself
    # as the 0-distance match; we filter it out below.
    knn_k = top_n + 1

    for src_id, src_doc_id in node_to_doc.items():
        # Fetch source embedding. vec0 stores embedding as a BLOB under the
        # `embedding` virtual column, readable the same way as a regular row.
        emb_row = conn.execute(
            "SELECT embedding FROM vec_documents WHERE doc_id = ?",
            (src_doc_id,),
        ).fetchone()
        if not emb_row or emb_row[0] is None:
            continue
        src_embedding = emb_row[0]

        # DI-A2: per-source transaction. The DELETE is scoped to `source_id=?`
        # so we never orphan OTHER sources' similar_to edges.
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "DELETE FROM graph_edges WHERE source_id = ? AND relation = 'similar_to'",
                (src_id,),
            )
            try:
                neighbors = conn.execute(
                    """
                    SELECT doc_id, distance
                      FROM vec_documents
                     WHERE embedding MATCH ?
                       AND k = ?
                     ORDER BY distance
                    """,
                    (src_embedding, knn_k),
                ).fetchall()
            except sqlite3.OperationalError as e:
                logger.debug("vec0 KNN query failed for doc_id=%s: %s", src_doc_id, e)
                neighbors = []

            new_edges: list[dict[str, Any]] = []
            for tgt_doc_id, distance in neighbors:
                if tgt_doc_id == src_doc_id:
                    continue  # skip self
                if distance is None:
                    continue
                # vec0 returns squared L2 distance for unit-norm vectors.
                # Embedding clients normalize vectors before storage:
                #   ||a - b||² = 2 - 2·cos(a,b)  →  cosine = 1 - dist/2
                cosine = 1.0 - (float(distance) / 2.0)
                if cosine < active_threshold:
                    continue
                tgt_node_id = doc_to_node.get(int(tgt_doc_id))
                if not tgt_node_id:
                    continue  # target doc has no graph_node counterpart
                metadata = {
                    "cosine": round(cosine, 4),
                    "embedding_mode": embedding_mode,
                    "symmetric": True,
                    "method": "vec0-knn",
                    "provider": provider,
                    "threshold": active_threshold,
                }
                new_edges.append({
                    "source_id": src_id,
                    "target_id": tgt_node_id,
                    "relation": "similar_to",
                    "confidence": cosine,
                    "source": "rem",
                    "metadata": metadata,
                    "provider": provider,
                    "project_id": node_meta.get(src_id),
                })

            if new_edges:
                # NOTE: we don't call chunked_upsert_edges here because it
                # opens its own BEGIN IMMEDIATE — we're already in one. Inline
                # the UPSERT and commit as part of this per-source txn.
                rows = [
                    (
                        e["source_id"], e["target_id"], e["relation"],
                        float(e["confidence"]), e["source"],
                        json.dumps(
                            e["metadata"], sort_keys=True, separators=(",", ":"),
                        ),
                        None, None,
                        e.get("project_id"),
                        e.get("provider"),
                    )
                    for e in new_edges
                ]
                if has_provider_column:
                    conn.executemany(
                        """
                        INSERT INTO graph_edges
                            (source_id, target_id, relation, confidence, source,
                             metadata, source_file, source_line,
                             first_seen_at, last_seen_at, project_id, provider)
                        VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?,
                            datetime('now'), datetime('now'), ?, ?
                        )
                        ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                            confidence = MAX(graph_edges.confidence, excluded.confidence),
                            metadata = excluded.metadata,
                            last_seen_at = datetime('now'),
                            project_id = COALESCE(graph_edges.project_id, excluded.project_id),
                            provider = COALESCE(excluded.provider, graph_edges.provider)
                        """,
                        rows,
                    )
                else:
                    conn.executemany(
                        """
                        INSERT INTO graph_edges
                            (source_id, target_id, relation, confidence, source,
                             metadata, source_file, source_line,
                             first_seen_at, last_seen_at, project_id)
                        VALUES (
                            ?, ?, ?, ?, ?, ?, ?, ?,
                            datetime('now'), datetime('now'), ?
                        )
                        ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                            confidence = MAX(graph_edges.confidence, excluded.confidence),
                            metadata = excluded.metadata,
                            last_seen_at = datetime('now'),
                            project_id = COALESCE(graph_edges.project_id, excluded.project_id)
                        """,
                        [row[:-1] for row in rows],
                    )
                n_edges += len(new_edges)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.exception("similar_to write failed for %s", src_id)

    logger.info("similar_to: wrote %d edges", n_edges)
    return n_edges


# --- Edge extractor 4/4: .claude/ infra-indexing (Fase 2.z, SIMP-3 dispatcher)
# ----------------------------------------------------------------------------
#
# PAT AM-03: 4 nuovi node types (kind=`artifact`) con deterministic slug
# (filename stem o YAML name) — non sha256, sono stabili e query-friendly.
# Esempi:
#   hook:artifact:quality-gate     ← .claude/hooks/quality-gate.sh
#   skill:artifact:interface-design ← .claude/skills/interface-design/SKILL.md
#   command:artifact:vision-audit  ← .claude/commands/vision-audit.md
#   plugin:artifact:compound-engineering ← derivato da .claude/settings.json
#
# Tutti i nodi infra hanno project_id='marvisx' (sono infrastruttura del
# monorepo MarvisX). Single-writer: questa funzione è invocata dentro
# l'orchestrator → riusa la conn aperta, no nuove connessioni.

# Sanitize per NODE_ID_PATTERN [a-zA-Z0-9_\-.]+ (PAT AM-03)
_INFRA_SLUG_RE = re.compile(r"[^a-zA-Z0-9_\-.]+")

# PERF-7: skills scan limited to 1 livello (`*/SKILL.md`) — no `**` ricorsivo
# per evitare scansioni di subdir nested negli skill packages (references/).


def _sanitize_infra_slug(name: str) -> str:
    """Make a string safe for the slug segment of NODE_ID_PATTERN.

    Replaces any char outside [a-zA-Z0-9_\\-.] with `_`, strips outer underscores,
    fallback `_unknown` se vuoto.
    """
    safe = _INFRA_SLUG_RE.sub("_", name).strip("_")
    return safe or "_unknown"


def _node_from_file(
    path: Path,
    node_type: str,
    project_id: str = "marvisx",
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SIMP-3 helper: build a graph_nodes row from a file path.

    PAT AM-03: id = `{node_type}:artifact:{sanitized_filename_stem}`. Use the
    file stem (no extension, no path), sanitized to NODE_ID_PATTERN charset.
    """
    slug = _sanitize_infra_slug(path.stem)
    metadata: dict[str, Any] = {
        "abs_path": str(path),
        "discovered_via": f"infra_indexing_{node_type}",
    }
    if extra_metadata:
        metadata.update(extra_metadata)
    return {
        "id": f"{node_type}:artifact:{slug}",
        "type": node_type,
        "name": path.name[:60],
        "qualified_name": f"{node_type}.{slug}",
        "file_path": str(path),
        "line_number": None,
        "metadata": metadata,
        "project_id": project_id,
    }


def _parse_md_frontmatter_safe(path: Path) -> dict[str, Any] | None:
    """SIMP-3 helper: thin wrapper su parse_frontmatter che non solleva.

    Return None se file unreadable o no frontmatter; dict altrimenti. Usato
    da _extract_skills/_extract_commands per leggere title/description.
    """
    try:
        data, _body = parse_frontmatter(path)
        return data
    except Exception:  # pragma: no cover (defensive)
        return None


def _extract_hooks(hooks_dir: Path) -> list[dict[str, Any]]:
    """Scan `.claude/hooks/*.sh` → 1 node per hook script (kind=artifact).

    Skip non-.sh files, skip _config.sh (è config interno, non hook eseguibile).
    """
    if not hooks_dir.is_dir():
        return []
    nodes: list[dict[str, Any]] = []
    for p in sorted(hooks_dir.glob("*.sh")):
        if p.name.startswith("_"):
            continue  # skip _config.sh and similar internals
        # Lettura prime righe per estrarre version banner se presente
        version = None
        try:
            head = p.read_text(encoding="utf-8", errors="ignore").splitlines()[:3]
            for line in head:
                m = re.search(r"v(\d+\.\d+\.\d+)", line)
                if m:
                    version = m.group(1)
                    break
        except OSError:
            pass
        nodes.append(_node_from_file(
            p, "hook",
            extra_metadata={"hook_type": "shell", "version": version} if version
            else {"hook_type": "shell"},
        ))
    return nodes


def _extract_skills(skills_dir: Path) -> list[dict[str, Any]]:
    """Scan `.claude/skills/*/SKILL.md` → 1 node per skill.

    PERF-7: solo 1 livello (`*/SKILL.md`), NO `**/SKILL.md` ricorsivo.
    Slug = parent dir name (skill package name). Frontmatter (se presente)
    aggiunge title/description in metadata.
    """
    if not skills_dir.is_dir():
        return []
    nodes: list[dict[str, Any]] = []
    for skill_md in sorted(skills_dir.glob("*/SKILL.md")):  # PERF-7: 1-level
        skill_pkg = skill_md.parent
        slug = _sanitize_infra_slug(skill_pkg.name)
        fm = _parse_md_frontmatter_safe(skill_md) or {}
        metadata: dict[str, Any] = {
            "abs_path": str(skill_md),
            "discovered_via": "infra_indexing_skill",
            "skill_package": skill_pkg.name,
        }
        # Optional fields da frontmatter (tolerant to missing)
        for fk in ("title", "description", "version"):
            if fk in fm and fm[fk] is not None:
                metadata[fk] = str(fm[fk])[:200]
        nodes.append({
            "id": f"skill:artifact:{slug}",
            "type": "skill",
            "name": skill_pkg.name[:60],
            "qualified_name": f"skill.{slug}",
            "file_path": str(skill_md),
            "line_number": None,
            "metadata": metadata,
            "project_id": "marvisx",
        })
    return nodes


def _extract_commands(commands_dir: Path) -> list[dict[str, Any]]:
    """Scan `.claude/commands/*.md` → 1 node per slash-command.

    Slug = filename stem (es. `vision-audit.md` → slug `vision-audit`).
    Frontmatter opzionale (se presente) → title/description in metadata.
    """
    if not commands_dir.is_dir():
        return []
    nodes: list[dict[str, Any]] = []
    for p in sorted(commands_dir.glob("*.md")):
        fm = _parse_md_frontmatter_safe(p) or {}
        extra: dict[str, Any] = {"command_type": "slash"}
        for fk in ("title", "description"):
            if fk in fm and fm[fk] is not None:
                extra[fk] = str(fm[fk])[:200]
        nodes.append(_node_from_file(p, "command", extra_metadata=extra))
    return nodes


def _extract_plugins(settings_json_path: Path) -> list[dict[str, Any]]:
    """Parse `.claude/settings.json` → 1 node per plugin/hook-binding.

    Strategy: derive un node per "command" referenced via PreToolUse hooks
    (chi consuma uno script .sh esterno); il settings.json di MarvisX non ha
    una sezione `plugins:` esplicita, quindi al momento estraiamo 1 node
    aggregato che cattura il fatto che il file esiste + le configurazioni.

    Robusto a struttura assente: ritorna [] se settings.json non esiste o non
    parse-able.
    """
    if not settings_json_path.is_file():
        return []
    try:
        data = json.loads(settings_json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    nodes: list[dict[str, Any]] = []
    # Hook config plugin (è il "plugin" piu' rappresentativo nel settings)
    hooks_section = data.get("hooks") or {}
    if hooks_section:
        nodes.append({
            "id": "plugin:artifact:hooks-config",
            "type": "plugin",
            "name": "hooks-config",
            "qualified_name": "plugin.hooks-config",
            "file_path": str(settings_json_path),
            "line_number": None,
            "metadata": {
                "abs_path": str(settings_json_path),
                "discovered_via": "infra_indexing_plugin",
                "plugin_kind": "hooks",
                "phases": sorted(hooks_section.keys()),
            },
            "project_id": "marvisx",
        })
    # Estrazione opzionale di altri plugins se appaiono in futuro
    # (es. compound-engineering, repomix-mcp). Non assumiamo schema fisso.
    plugins_section = data.get("plugins") or {}
    if isinstance(plugins_section, dict):
        for plugin_name in plugins_section.keys():
            slug = _sanitize_infra_slug(str(plugin_name))
            nodes.append({
                "id": f"plugin:artifact:{slug}",
                "type": "plugin",
                "name": plugin_name[:60],
                "qualified_name": f"plugin.{slug}",
                "file_path": str(settings_json_path),
                "line_number": None,
                "metadata": {
                    "abs_path": str(settings_json_path),
                    "discovered_via": "infra_indexing_plugin",
                    "plugin_kind": "settings_plugin",
                },
                "project_id": "marvisx",
            })
    return nodes


def _extract_infra_nodes(
    conn: sqlite3.Connection,
    marvisx_repo_root: Path,
) -> int:
    """Fase 2.z dispatcher (SIMP-3): estrae 4 type infra da .claude/.

    Scans:
      - `.claude/hooks/*.sh`            → type=hook
      - `.claude/skills/*/SKILL.md`     → type=skill (PERF-7: 1-level)
      - `.claude/commands/*.md`         → type=command
      - `.claude/settings.json`         → type=plugin (1+ derived nodes)

    Tutti i nodi vanno in project_id='marvisx' (infra del monorepo).
    Single UPSERT batch via chunked_upsert_nodes.

    Returns count nodi inseriti/aggiornati.
    """
    claude_root = marvisx_repo_root / ".claude"
    all_nodes: list[dict[str, Any]] = []
    all_nodes.extend(_extract_hooks(claude_root / "hooks"))
    all_nodes.extend(_extract_skills(claude_root / "skills"))
    all_nodes.extend(_extract_commands(claude_root / "commands"))
    all_nodes.extend(_extract_plugins(claude_root / "settings.json"))
    if not all_nodes:
        logger.info("infra-indexing: no .claude/ artifacts found at %s", claude_root)
        return 0
    n = chunked_upsert_nodes(conn, all_nodes)
    logger.info("infra-indexing: %d nodes (hooks/skills/commands/plugins) at %s",
                n, claude_root)
    return n


# --- Orchestrator -----------------------------------------------------------


def populate_cross_project(
    db_path: str | None = None,
    projects_root: Path | None = None,
    skip_depends_on: bool = False,
    skip_prose: bool = False,
    skip_similar_to: bool = False,
    skip_infra: bool = False,
    dry_run: bool = False,
    workers: int = 4,
    marvisx_repo_root: Path | None = None,
    include_all_projects: bool = False,
) -> dict[str, Any]:
    """Fase 2 orchestrator.

    Returns a measurements dict `{db_path, projects, elapsed_ms, results}`.

    The four sub-extractors run inside a single sqlite3.connect() session
    with PRAGMA foreign_keys=ON. Each extractor writes its own
    BEGIN IMMEDIATE chunks via _graph_writer helpers, so the DB remains
    consistent even if the orchestrator crashes between passes.

    `include_all_projects` (Phase 3): se True usa scope discovery permissivo
    (~68 progetti con project.yaml valido) invece del default (~24, c&i pilot
    + cross-program). Usato dal full-rebuild notturno per coprire i metadata
    path che oggi restano fuori dal scope Fase 2.
    """
    db = db_path or _resolve_db_path()
    base = projects_root or DEFAULT_PROJECTS_ROOT
    t0 = time.perf_counter()

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")

        projects = _discover_projects(base, include_all_projects=include_all_projects)

        # Always UPSERT project nodes (targets of mentions). This is safe even
        # when skip_prose=True — the node set is a handful of rows.
        initial_project_nodes = [
            {
                "id": _project_node_id(slug),
                "type": "project",
                "name": slug,
                "qualified_name": f"project.{slug}",
                "file_path": None,
                "line_number": None,
                "metadata": {"slug": slug},
                "project_id": slug,
            }
            for slug in projects
        ]
        if not dry_run and initial_project_nodes:
            chunked_upsert_nodes(conn, initial_project_nodes)

        results: dict[str, Any] = {}

        if skip_depends_on:
            results["depends_on"] = {"n_edges": 0, "skipped": True}
        elif dry_run:
            results["depends_on"] = {"n_edges": 0, "dry_run": True}
        else:
            n = _extract_depends_on(conn, projects)
            results["depends_on"] = {"n_edges": n}

        if skip_prose:
            results["prose"] = {"n_edges": 0, "skipped": True}
        elif dry_run:
            results["prose"] = {"n_edges": 0, "dry_run": True}
        else:
            n = _extract_prose_edges(conn, projects, workers=workers)
            results["prose"] = {"n_edges": n}

        if skip_similar_to:
            results["similar_to"] = {"n_edges": 0, "skipped": True}
        elif dry_run:
            results["similar_to"] = {"n_edges": 0, "dry_run": True}
        else:
            n = _compute_similar_to(conn, projects)
            results["similar_to"] = {"n_edges": n}

        # Fase 2.z: infra-indexing (.claude/ hooks/skills/commands/plugins).
        # Default repo_root = REPO_ROOT (marvisx). Caller può sovrascrivere
        # via marvisx_repo_root (used by tests with tmp marvisx-like fixtures).
        if skip_infra:
            results["infra"] = {"n_nodes": 0, "skipped": True}
        elif dry_run:
            results["infra"] = {"n_nodes": 0, "dry_run": True}
        else:
            mx_root = marvisx_repo_root or REPO_ROOT
            n_infra = _extract_infra_nodes(conn, mx_root)
            results["infra"] = {"n_nodes": n_infra}

        # Sanity: no project_id should be NULL post-migration + populator.
        null_nodes = conn.execute(
            "SELECT COUNT(*) FROM graph_nodes WHERE project_id IS NULL"
        ).fetchone()[0]
        null_edges = conn.execute(
            "SELECT COUNT(*) FROM graph_edges WHERE project_id IS NULL"
        ).fetchone()[0]
        if null_nodes or null_edges:
            logger.warning(
                "POST-POPULATE SANITY FAIL: %d nodes + %d edges with project_id NULL",
                null_nodes, null_edges,
            )
        results["sanity"] = {
            "null_project_nodes": null_nodes,
            "null_project_edges": null_edges,
        }
    finally:
        conn.close()

    elapsed_ms = (time.perf_counter() - t0) * 1000
    return {
        "db_path": db,
        "projects_scanned": len(projects) if not dry_run else 0,
        "elapsed_ms": round(elapsed_ms, 2),
        "dry_run": dry_run,
        "results": results,
    }


def _resolve_db_path(explicit: str | None = None) -> str:
    if explicit:
        return explicit
    prod = Path("/data/pir/console.db")
    if prod.exists():
        return str(prod)
    return str(REPO_ROOT / "console.db")


# ---- Phase 1 incremental support -------------------------------------------
#
# Il Phase-1 path scritto qui emette solo gli edge types PER-FILE:
#   - mentions  (.md → project node)
#   - refers_to (.md → file node on-demand)
#   - cites     (.md → handoff node)
#
# Edge types "globali" sono DEFER al full-rebuild notturno (Phase 3):
#   - shares_tag  (richiede inverted index su tutti i doc con tag overlap)
#   - similar_to  (richiede embedding full scan)
#   - depends_on  (richiede scan repo AST Source 3)
#
# Rationale: target <1s per file. shares_tag O(P*tag_overlap_peers) potrebbe
# scalare male sotto burst; full-rebuild ricalcola comunque entro 24h.


def _resolve_md_src_id(
    conn: sqlite3.Connection,
    abs_path: str,
) -> tuple[str, dict[str, Any] | None]:
    """Risolvi il node_id per un .md incrementale.

    Preferenza: existing handoff/doc/... node (populate_artifacts ha gia'
    indicizzato). Fallback: file:artifact:<hash> on-demand (creato anche
    come node dedicato).

    Returns (node_id, new_node_dict_or_None). Il dict e' non-None solo
    quando e' stato creato un file:artifact on-demand da UPSERT.
    """
    # Try to match existing node by file_path (handoff/doc types).
    # populate_artifacts salva file_path in 3 forme possibili:
    #   - relative "projects/marvisx/memory/x.md" (marvisx legacy)
    #   - relative ad altro parent
    #   - absolute "/data/projects/<slug>/..."
    cur = conn.execute(
        "SELECT id FROM graph_nodes "
        "WHERE file_path=? AND type IN "
        "  ('handoff','solution','audit','spike','analysis','research','rubric','guide','mockup') "
        "LIMIT 1",
        (abs_path,),
    )
    row = cur.fetchone()
    if row:
        return row[0], None

    rel_candidates = [
        abs_path.replace("/data/projects/", "projects/"),
    ]
    try:
        as_rel = str(Path(abs_path).relative_to(DEFAULT_PROJECTS_ROOT.parent))
        rel_candidates.append(as_rel)
    except ValueError:
        pass

    for candidate in rel_candidates:
        row = conn.execute(
            "SELECT id FROM graph_nodes WHERE file_path=? LIMIT 1",
            (candidate,),
        ).fetchone()
        if row:
            return row[0], None

    # Fallback: file:artifact on-demand. Screen secret paths (DI-A3).
    if _is_secret_path(abs_path):
        return "", None

    file_id = _file_node_id(abs_path)
    # Determine owning project_id from path prefix.
    project_id = "marvisx"
    try:
        parts = Path(abs_path).resolve().relative_to(DEFAULT_PROJECTS_ROOT).parts
        if parts:
            project_id = parts[0]
    except (ValueError, OSError):
        pass

    new_node = {
        "id": file_id,
        "type": "file",
        "name": Path(abs_path).name[:60],
        "qualified_name": f"file.{file_id.split(':')[-1]}",
        "file_path": abs_path,
        "line_number": None,
        "metadata": {
            "abs_path": abs_path,
            "resolved": True,
            "discovered_via": "cross_project_incremental",
        },
        "project_id": project_id,
    }
    return file_id, new_node


def _extract_prose_edges_for_file(
    conn: sqlite3.Connection,
    abs_path: str,
    project_slug: str,
    projects: dict[str, dict[str, Any]],
    active_slugs: frozenset[str],
    valid_handoff_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Estrai mentions/refers_to/cites edges per un singolo .md.

    Mirror del body di `_extract_prose_edges` per 1 file. Ritorna
    `(edges, new_file_nodes)`. Il caller UPSERT-a nodes + edges.
    """
    try:
        text = Path(abs_path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return [], []

    src_id, src_new_node = _resolve_md_src_id(conn, abs_path)
    if not src_id:
        return [], []

    new_file_nodes: list[dict[str, Any]] = []
    if src_new_node is not None:
        new_file_nodes.append(src_new_node)

    # Strip frontmatter before regex
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            text = parts[2]

    edges: list[dict[str, Any]] = []
    mentions_emitted: set[str] = set()
    refers_emitted: set[str] = set()
    cites_emitted: set[str] = set()

    for m in _PROSE_RE.finditer(text):
        if m.group("mention"):
            mention = m.group("mention").lower()
            if mention in active_slugs and mention != project_slug:
                tgt = _project_node_id(mention)
                if tgt in mentions_emitted:
                    continue
                mentions_emitted.add(tgt)
                edges.append({
                    "source_id": src_id,
                    "target_id": tgt,
                    "relation": "mentions",
                    "confidence": 0.9,
                    "source": "frontmatter",
                    "source_file": abs_path,
                    "metadata": {"mention": mention},
                    "project_id": project_slug,
                })
        elif m.group("filepath"):
            fpath = m.group("filepath")
            if _is_secret_path(fpath):
                continue
            candidate_paths = [Path(fpath)]
            if not fpath.startswith("/"):
                if project_slug in projects:
                    candidate_paths.append(projects[project_slug]["path"] / fpath)
                candidate_paths.append(REPO_ROOT / fpath)
            resolved_path: Path | None = None
            for cp in candidate_paths:
                try:
                    if cp.exists() and cp.is_file():
                        resolved_path = cp.resolve()
                        break
                except OSError:
                    continue
            if resolved_path is None:
                continue
            resolved_str = str(resolved_path)
            if _is_secret_path(resolved_str):
                continue
            in_known_project = any(
                resolved_str.startswith(str(pj["path"]))
                for pj in projects.values()
            )
            if not in_known_project and _is_excluded_system_path(resolved_str):
                continue
            tgt_id = _file_node_id(resolved_str)
            if tgt_id in refers_emitted or tgt_id == src_id:
                continue
            refers_emitted.add(tgt_id)
            tgt_project = "marvisx"
            for candidate_slug, pj in projects.items():
                try:
                    if str(resolved_path).startswith(str(pj["path"])):
                        tgt_project = candidate_slug
                        break
                except ValueError:
                    pass
            new_file_nodes.append({
                "id": tgt_id,
                "type": "file",
                "name": resolved_path.name[:60],
                "qualified_name": f"file.{tgt_id.split(':')[-1]}",
                "file_path": resolved_str,
                "line_number": None,
                "metadata": {
                    "abs_path": resolved_str,
                    "resolved": True,
                    "discovered_via": "refers_to_incremental",
                },
                "project_id": tgt_project,
            })
            edges.append({
                "source_id": src_id,
                "target_id": tgt_id,
                "relation": "refers_to",
                "confidence": 0.85,
                "source": "frontmatter",
                "source_file": abs_path,
                "metadata": {"matched_path": fpath},
                "project_id": project_slug,
            })
        elif m.group("handoff"):
            handoff_filename = m.group("handoff")
            handoff_id = _handoff_node_id_from_filename(handoff_filename)
            if handoff_id in cites_emitted or handoff_id == src_id:
                continue
            if handoff_id not in valid_handoff_ids:
                continue
            cites_emitted.add(handoff_id)
            edges.append({
                "source_id": src_id,
                "target_id": handoff_id,
                "relation": "cites",
                "confidence": 0.85,
                "source": "frontmatter",
                "source_file": abs_path,
                "metadata": {"cited_filename": handoff_filename},
                "project_id": project_slug,
            })

    return edges, new_file_nodes


def _resolve_cross_project_src_id(
    conn: sqlite3.Connection,
    abs_path: str,
    kind: str | None = None,
) -> str | None:
    """Returna node_id esistente per il .md (handoff/solution/file:artifact).

    Usato per --handle-delete: se il file non esiste piu' non possiamo leggere
    frontmatter, quindi derivare id dal path. Priorita':
      1. Se `kind` e' noto, usa il canonical node-id (piu' affidabile di
         lookup su file_path, che puo' essere relativo in forme diverse).
      2. Match file_path su varianti (absolute, projects/ prefix).
      3. Fallback file:artifact sha256-based id.
    """
    # Priority 1: canonical id from kind + filename
    if kind == "handoff":
        from core.scripts.populate_artifacts import _handoff_id
        candidate_id = _handoff_id(Path(abs_path).name)
        row = conn.execute(
            "SELECT id FROM graph_nodes WHERE id=? LIMIT 1",
            (candidate_id,),
        ).fetchone()
        if row:
            return row[0]
    elif kind == "doc":
        # Doc type e' perso con il file: prova tutti i valori di DOC_TYPE_DIR_MAP
        from core.scripts.populate_artifacts import _doc_id, DOC_TYPE_DIR_MAP
        for doc_type in DOC_TYPE_DIR_MAP.keys():
            candidate_id = _doc_id(doc_type, Path(abs_path).name)
            row = conn.execute(
                "SELECT id FROM graph_nodes WHERE id=? LIMIT 1",
                (candidate_id,),
            ).fetchone()
            if row:
                return row[0]

    # Priority 2: file_path match (for files indexed via non-canonical paths)
    for candidate in (
        abs_path,
        abs_path.replace("/data/projects/", "projects/"),
    ):
        row = conn.execute(
            "SELECT id FROM graph_nodes WHERE file_path=? LIMIT 1",
            (candidate,),
        ).fetchone()
        if row:
            return row[0]

    # Priority 3: file:artifact hash-based id sopravvive anche a delete
    file_id = _file_node_id(abs_path)
    row = conn.execute(
        "SELECT id FROM graph_nodes WHERE id=? LIMIT 1",
        (file_id,),
    ).fetchone()
    if row:
        return row[0]
    return None


def populate_cross_project_incremental(
    paths: list[Path],
    db_path: str | None = None,
    handle_delete: bool = False,
    skip_hash_gate: bool = False,
    projects_root: Path | None = None,
) -> dict[str, Any]:
    """Phase 1 cross-project per-file edge extraction.

    Emette mentions/refers_to/cites per ogni path. shares_tag/similar_to/
    depends_on NON sono ricalcolati qui (deferred al full-rebuild notturno).

    DELETE esistenti edges con source_id=node_id PRIMA di emettere i nuovi
    → il refresh non lascia edges "stali" da vecchie versioni del file.
    """
    db = db_path or _resolve_db_path()
    base = projects_root or DEFAULT_PROJECTS_ROOT
    t0 = time.perf_counter()

    results: dict[str, Any] = {
        "files_processed": 0,
        "files_skipped_hash_unchanged": 0,
        "files_skipped_unroutable": 0,
        "files_deleted": 0,
        "edges_written": 0,
        "nodes_written": 0,
        "skipped": [],
    }

    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")

        # Discovery progetti: una volta per batch (non per path)
        projects = _discover_projects(base)
        active_slugs = frozenset(projects.keys())

        valid_handoff_ids = {
            row[0]
            for row in conn.execute(
                "SELECT id FROM graph_nodes WHERE type='handoff'"
            ).fetchall()
        }

        # UPSERT project nodes once (targets of mentions; cheap)
        project_nodes = [
            {
                "id": _project_node_id(slug),
                "type": "project",
                "name": slug,
                "qualified_name": f"project.{slug}",
                "file_path": None,
                "line_number": None,
                "metadata": {"slug": slug},
                "project_id": slug,
            }
            for slug in projects
        ]
        chunked_upsert_nodes(conn, project_nodes)

        for raw in paths:
            p = Path(raw)
            route = _route_metadata_path(p)
            if route is None:
                results["files_skipped_unroutable"] += 1
                results["skipped"].append({
                    "file": str(p),
                    "reason": "unroutable",
                    "fix_hint": "path must be under /data/projects/<slug>/",
                })
                continue
            slug, kind, _metadata_path = route
            abs_path = str(p.resolve() if p.exists() else p.absolute())

            if handle_delete or not p.exists():
                node_id = _resolve_cross_project_src_id(conn, abs_path, kind=kind)
                if node_id:
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        cur = conn.execute(
                            "DELETE FROM graph_edges "
                            "WHERE source_id=? OR target_id=?",
                            (node_id, node_id),
                        )
                        results["edges_written"] += cur.rowcount
                        conn.commit()
                    except Exception:
                        conn.rollback()
                        raise
                results["files_deleted"] += 1
                _file_state_forget(conn, str(p), populator=None)
                continue

            if not skip_hash_gate:
                sha = _file_sha256(p)
                if _file_state_unchanged(conn, str(p), sha, populator="cross_project"):
                    results["files_skipped_hash_unchanged"] += 1
                    continue
            else:
                sha = None

            edges, new_nodes = _extract_prose_edges_for_file(
                conn,
                abs_path=abs_path,
                project_slug=slug,
                projects=projects,
                active_slugs=active_slugs,
                valid_handoff_ids=valid_handoff_ids,
            )

            # Resolve src_id to DELETE its outgoing mentions/refers_to/cites
            # before UPSERT (same-refresh semantics as incremental artifacts).
            src_id, _ = _resolve_md_src_id(conn, abs_path)
            if src_id:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    conn.execute(
                        "DELETE FROM graph_edges "
                        "WHERE source_id=? "
                        "  AND relation IN ('mentions','refers_to','cites')",
                        (src_id,),
                    )
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise

            # Dedup new_nodes by id
            seen: set[str] = set()
            deduped: list[dict[str, Any]] = []
            for n in new_nodes:
                if n["id"] in seen:
                    continue
                seen.add(n["id"])
                deduped.append(n)

            n_nodes = chunked_upsert_nodes(conn, deduped)
            n_edges = chunked_upsert_edges(conn, edges)

            results["files_processed"] += 1
            results["nodes_written"] += n_nodes
            results["edges_written"] += n_edges

            if sha is not None:
                _file_state_record(conn, str(p), sha, populator="cross_project")

    finally:
        conn.close()

    results["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 2)
    results["note"] = (
        "Phase 1 incremental emits mentions/refers_to/cites only. "
        "shares_tag, similar_to, depends_on are deferred to full-rebuild."
    )
    return results


def _main() -> int:
    ap = argparse.ArgumentParser(
        description="KG Fase 2 cross-project populator (depends_on/mentions/refers_to/cites/shares_tag/similar_to)"
    )
    ap.add_argument("--db", default=None, help="Path to SQLite DB (default: auto-resolve)")
    ap.add_argument("--projects-root", default=None,
                    help=f"Root path for project dirs (default: {DEFAULT_PROJECTS_ROOT})")
    ap.add_argument("--skip-depends-on", action="store_true")
    ap.add_argument("--skip-prose", action="store_true")
    ap.add_argument("--skip-similar-to", action="store_true")
    ap.add_argument("--skip-infra", action="store_true",
                    help="Skip Fase 2.z .claude/ infra-indexing pass.")
    ap.add_argument("--marvisx-repo-root", default=None,
                    help="Override marvisx repo root (where .claude/ lives). "
                         "Default: scripts/.. (repo root inferred at import).")
    ap.add_argument("--dry-run", action="store_true",
                    help="Discover projects but don't write. For smoke tests.")
    ap.add_argument("--workers", type=int, default=4)
    # Phase 1 incremental flags
    ap.add_argument(
        "--incremental",
        nargs="+",
        metavar="PATH",
        help="Process N .md paths per-file (emits mentions/refers_to/cites only). "
             "shares_tag/similar_to/depends_on are deferred to full-rebuild.",
    )
    ap.add_argument(
        "--handle-delete",
        action="store_true",
        help="DELETE edges (src OR dst) for --incremental paths. Use when files "
             "have been removed from disk.",
    )
    ap.add_argument(
        "--skip-hash-gate",
        action="store_true",
        help="Bypass file_state content-hash skip.",
    )
    # Phase 3 full-rebuild scope flag
    ap.add_argument(
        "--include-all-projects",
        action="store_true",
        help="Phase 3: scope esteso (~68 progetti con project.yaml valido) "
             "invece del default Fase 2 (~24 c&i + cross-program pilot). "
             "Usato dal full-rebuild notturno per coprire tutti i metadata path.",
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )

    # Banner: remind the human that this script requires pir-api.service to be
    # stopped for single-writer safety when writing to the prod DB.
    db_path = args.db or _resolve_db_path()
    if db_path.startswith("/data/pir/"):
        logger.warning(
            "writing to PROD DB %s — make sure pir-api.service is stopped "
            "and KG_HOOK_DISABLED=1 is set in the environment",
            db_path,
        )

    if args.incremental:
        out = populate_cross_project_incremental(
            paths=[Path(p) for p in args.incremental],
            db_path=args.db,
            handle_delete=args.handle_delete,
            skip_hash_gate=args.skip_hash_gate,
            projects_root=Path(args.projects_root) if args.projects_root else None,
        )
        if out.get("skipped"):
            print(
                json.dumps({"skipped": out["skipped"]}, indent=2, default=str),
                file=sys.stderr,
            )
        print(json.dumps(out, indent=2, default=str))
        return 0

    out = populate_cross_project(
        db_path=args.db,
        projects_root=Path(args.projects_root) if args.projects_root else None,
        skip_depends_on=args.skip_depends_on,
        skip_prose=args.skip_prose,
        skip_similar_to=args.skip_similar_to,
        skip_infra=args.skip_infra,
        dry_run=args.dry_run,
        workers=args.workers,
        marvisx_repo_root=Path(args.marvisx_repo_root) if args.marvisx_repo_root else None,
        include_all_projects=args.include_all_projects,
    )
    print(json.dumps(out, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
