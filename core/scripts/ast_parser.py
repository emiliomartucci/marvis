#!/usr/bin/env python3
# v1.5.0 - 2026-08-27 - Canonicalize package-prefixed internal Python imports
# v1.4.0 - 2026-04-14 - KG fix edges orfane (task 2a98db4a): DELETE stale edges per re-parsed files before UPSERT
# v1.3.0 - 2026-04-15 - KG Fase 2.y: discover_files(repo_root, patterns) refactor (PERF-1) + Fase 2.z NODE_ID_RE prefixes
# v1.2.0 - 2026-04-14 - KG Fase 1g: --incremental mode (parse only specified files)
"""
AST parser unificato per Knowledge Graph MarvisX (Fase 1a).

Sostituisce `scripts/ast_parser_spike.py` (3 file target Python) ed estende il
grafo all'intero codebase:
- Python: `git ls-files 'api/*.py'` via tree-sitter-python in ProcessPoolExecutor
- TypeScript: `git ls-files 'console/src/*.ts' 'console/src/*.tsx'` via
  tree-sitter-typescript (Python-native, zero subprocess Node)

Architettura (v2 post-deepen):
- Namespace prefix `py:` / `ts:` nei node_id per evitare collisioni cross-lang
- Qualified name completo per metodi classe (`Module.Class.method`)
- Metadata esteso: is_async, decorators, http_verb, is_component, is_hook, stub, language
- Chunked UPSERT 500 nodi per transaction BEGIN IMMEDIATE (`executemany`)
- Metadata merge Python-side (non overwrite) prima dell'INSERT
- Custom exception `ASTParserError` (pattern MarvisX, vedi `api/services/git_ops.py`)
- NODE_ID_PATTERN esteso per kebab-case file TS (dash accettati)

Invocazione standalone:
    python -m core.scripts.ast_parser                    # auto-discover db_path
    python -m core.scripts.ast_parser --db /tmp/x.db     # path custom
    python -m core.scripts.ast_parser --stats            # stampa solo stats post-run

Uso programmatico (fixture test):
    from core.scripts.ast_parser import populate_graph_chunked
    result = populate_graph_chunked(db_path="/tmp/test.db")

## Single-writer note (importante)

MarvisX enforce un pattern single-writer SQLite: tutti i path di scrittura
dall'API devono usare `api.db.write_db` / `get_write_db` / `acquire_write_db`.
Questo script e' deliberatamente **standalone** (batch/ops tool invocato fuori
dal server) quindi apre una propria connessione `sqlite3.connect(db_path)` in
modalita' `BEGIN IMMEDIATE`. Questo e' sicuro perche':
1. Lo script viene eseguito manualmente o via cron, NON dal processo API.
2. `BEGIN IMMEDIATE` prende subito il lock scrittori: se l'API sta scrivendo,
   lo script attende (busy_timeout=15000ms) o fallisce pulitamente.
3. Gli altri lettori (pir-api.service) usano la pool read-only con
   `PRAGMA query_only=ON`, incompatibile con scritture concorrenti sulla stessa
   connessione.

Quando Fase 1g introdurra' l'invocazione via hook git post-commit, questo
contratto verra' riconsiderato (probabile spostamento dentro a un endpoint
write-protected).
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any, Iterable

try:
    import tree_sitter_python as tspython
    import tree_sitter_typescript as tstypescript
    from tree_sitter import Language, Node, Parser
except ImportError as e:  # pragma: no cover
    print(f"ERROR: tree-sitter import failed: {e}", file=sys.stderr)
    print(
        "Install: pip install 'tree-sitter>=0.23.0,<0.26' "
        "'tree-sitter-python>=0.23.0,<0.25' 'tree-sitter-typescript~=0.23.2'",
        file=sys.stderr,
    )
    raise

logger = logging.getLogger("ast_parser")

REPO_ROOT = Path(__file__).resolve().parent.parent
# v1.4.0 (2026-05-15): mutable via --repo-root CLI for multi-repo indexing.
# When --project is set, all nodes inserted in this run get project_id=PROJECT_ID
# (default "marvisx"). Lets kg_full_rebuild loop over ~/repos/*/ external repos.
PROJECT_ID = "marvisx"

# Node id format (mirrors api/services/graph_service.NODE_ID_PATTERN and the
# zod regex in mcp-pir/index.mjs). Keep the three in sync.
# Fase 1a: prefix py|ts + accept dashes for kebab-case TS filenames.
# Fase 1c: extended for artifact prefixes (task|pr|commit|handoff|solution|learning)
# with `artifact` sub-type — populated by scripts/populate_artifacts.py.
# Fase 1h: added doc-type prefixes (audit|spike|analysis|research|rubric|guide|mockup)
# so populate_artifacts can index every docs/ subdir as a distinct node type.
# Fase 2: added `project` (cross-project hub nodes) and `file` (on-demand file
# nodes for `refers_to` PAT-8 schema `file:artifact:{sha256(path)[:12]}`).
# Fase 2.z: added `hook|skill|command|plugin` (.claude/ infra-indexing).
# PAT AM-03: kind=`artifact` con deterministic slug = filename stem.
# Phase 6: added `plan|brainstorm` doc-type (migration 077) per coverage
# estesa docs/plans/ e docs/brainstorms/ su 68 progetti (populate_artifacts
# --all-projects).
# Phase 7.3 (migration 090): added `inbox` prefix — saved inbox_items indexed
# via scripts/populate_inbox_nodes.py.
# Universal ingestion E4.2 (migration 096): added `xlsx` prefix and `sheet`
# kind for workbook/sheet artifact nodes.
NODE_ID_RE = re.compile(
    r"^(py|ts|task|pr|commit|handoff|solution|learning"
    r"|audit|spike|analysis|research|rubric|guide|mockup"  # spike/rubric kept for legacy queryability
    r"|project|file"
    r"|hook|skill|command|plugin"
    r"|plan|brainstorm"
    r"|inbox|xlsx"
    r"|policy|contract|transcript|record|report):"  # Phase 1.5 E5-fix9 + mig 125 business docs
    r"(function|file|module|artifact|sheet):[a-zA-Z0-9_\-.]+$"
)

BATCH_SIZE = 500  # nodes per BEGIN IMMEDIATE chunk (performance agent finding)
EDGE_BATCH_SIZE = 2000  # edges per chunk (edges are cheaper than nodes)

# HTTP verb decorators we extract (FastAPI / APIRouter style).
HTTP_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}

# Python builtins we skip when resolving a call target — too noisy.
PY_BUILTINS_SKIP = {
    "len", "str", "int", "float", "list", "dict", "set", "tuple", "bool",
    "range", "print", "isinstance", "hasattr", "getattr", "setattr",
    "super", "type", "any", "all", "enumerate", "zip", "sorted", "min",
    "max", "sum", "abs", "repr", "id", "open", "iter", "next",
}

# Same spirit for TypeScript — skip ubiquitous globals so the graph stays readable.
TS_BUILTINS_SKIP = {
    "console", "JSON", "Math", "Object", "Array", "Number", "String", "Boolean",
    "Promise", "Date", "Error", "RegExp", "Map", "Set", "Symbol", "BigInt",
    "parseInt", "parseFloat", "isNaN", "isFinite", "String",
    "encodeURIComponent", "decodeURIComponent",
    # React hooks are NOT skipped — we want is_hook metadata on them.
}


# ---------------------------------------------------------------------------
# Custom exception (pattern MarvisX: vedi `api/services/git_ops.py::GitOpsError`)
# ---------------------------------------------------------------------------


class ASTParserError(Exception):
    """Errore fatale di parsing AST (discovery, language load, DB write)."""

    pass


# ---------------------------------------------------------------------------
# Language handles — constructed lazily (ProcessPool workers rebuild them).
# ---------------------------------------------------------------------------


def _py_lang() -> Language:
    return Language(tspython.language())


def _ts_lang() -> Language:
    return Language(tstypescript.language_typescript())


def _tsx_lang() -> Language:
    return Language(tstypescript.language_tsx())


# ---------------------------------------------------------------------------
# Path / naming helpers
# ---------------------------------------------------------------------------


def _rel_to_module_py(rel_path: str) -> str:
    """'api/routers/newsletter.py' → 'api.routers.newsletter'."""
    p = rel_path.replace("\\", "/")
    if p.endswith(".py"):
        p = p[:-3]
    p = p.replace("/", ".")
    if p.endswith(".__init__"):
        p = p[: -len(".__init__")]
    return p


def _rel_to_module_ts(rel_path: str) -> str:
    """'console/src/components/Modal.tsx' → 'console.src.components.modal'.

    TypeScript doesn't enforce dotted module names like Python, but we normalize
    the file path so node ids are stable. Dashes are preserved (kebab-case).

    Next.js route groups like `(app)` and dynamic segments like `[id]` are
    sanitized (parentheses and brackets dropped) so the resulting id matches
    NODE_ID_RE which only allows [a-zA-Z0-9_\\-.].
    """
    p = rel_path.replace("\\", "/")
    for suf in (".tsx", ".ts"):
        if p.endswith(suf):
            p = p[: -len(suf)]
            break
    p = p.replace("/", ".")
    # Sanitize characters that would break NODE_ID_RE (parens, brackets, etc.)
    # We replace with nothing to keep the path readable — collisions are unlikely
    # because path segments remain distinct after removing the wrappers.
    p = re.sub(r"[()\[\]{}!$,'\"`@#%^&*+=<>?:;|/\\]", "", p)
    # Collapse duplicate dots that may result from sanitization
    p = re.sub(r"\.+", ".", p).strip(".")
    # Lowercase to stay consistent with Python module naming, since TS filenames
    # can be PascalCase (components) and that would collide with class names.
    return p.lower()


def _norm_qn(qn: str) -> str:
    """Normalize qualified name: strip + lowercase + single-dot."""
    return qn.strip().lower()


def _safe_id(prefix: str, node_type: str, qn: str) -> str:
    node_id = f"{prefix}:{node_type}:{_norm_qn(qn)}"
    if not NODE_ID_RE.match(node_id):
        # Should never happen if qn is sane. Guard for defense in depth.
        raise ASTParserError(f"Invalid node_id produced: {node_id!r}")
    return node_id


# ---------------------------------------------------------------------------
# Discovery — git ls-files (architecture agent finding)
# ---------------------------------------------------------------------------


def discover_files(
    repo_root: Path | None = None,
    patterns: tuple[str, ...] | None = None,
) -> tuple[list[Path], list[Path]]:
    """Discover in-scope Python + TypeScript files via `git ls-files`.

    Returns (py_files, ts_files) as absolute paths. Empty list is tolerated (the
    parser simply emits no nodes). Missing `git` binary or non-git repo raise
    ASTParserError.

    Fase 2.y v1.3.0 (PERF-1 CRITICAL): refactored signature to accept
    (repo_root, patterns) so the caller can scan cross-program repos with
    different layouts (propriofacile, tpla, cer-webapp, factup) instead of the
    marvisx-specific ('api/*.py', 'console/src/*.ts*'). Backward-compat: when
    `patterns` is None, default args = marvisx layout (no behaviour change for
    Fase 1 callers).

    Args:
        repo_root: git repo root (default REPO_ROOT = marvisx)
        patterns: tuple of git ls-files patterns. None → marvisx default.
                  Pass e.g. ('**/*.py', '**/*.ts', '**/*.tsx') for cross-program
                  scans; or layout-specific subtrees per-project.

    Returns:
        (py_files, ts_files) Path lists. Files are split by suffix (.py vs the
        TS/JS family .ts/.tsx/.mts/.cts/.mjs/.cjs/.js/.jsx) AFTER the git
        ls-files call, so callers can pass a single union pattern.
    """
    root = repo_root or REPO_ROOT
    if patterns is None:
        # Backward-compat: marvisx layout (Fase 1 default).
        patterns = ("api/*.py", "console/src/*.ts", "console/src/*.tsx")
    try:
        out = subprocess.check_output(
            ["git", "ls-files", *patterns],
            cwd=root,
            text=True,
            stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise ASTParserError(f"git ls-files failed: {e}") from e

    py_files: list[Path] = []
    ts_files: list[Path] = []
    for line in out.strip().split("\n"):
        if not line:
            continue
        suffix = line.rsplit(".", 1)[-1].lower() if "." in line else ""
        if suffix == "py":
            py_files.append(root / line)
        elif suffix in ("ts", "tsx", "mts", "cts", "mjs", "cjs", "js", "jsx"):
            # The tree-sitter TypeScript grammar parses the JS family too, so
            # ES-module / CommonJS JavaScript (.js/.mjs/.cjs/.mts/.cts/.jsx)
            # routes to parse_typescript_file. Hosted callers pass Python/TS-only
            # patterns, so their classification is unchanged; only callers that
            # ask for a JS pattern (the graph exporter) surface these.
            ts_files.append(root / line)
        # Other extensions silently dropped (caller passed wider pattern).
    return py_files, ts_files


# ---------------------------------------------------------------------------
# Python parser
# ---------------------------------------------------------------------------


def _py_iter_functions(root: Node, class_stack: list[str] | None = None) -> Iterable[tuple[Node, bool, str]]:
    """Yield (function_definition node, is_async, qualified_name_suffix).

    Walks the tree tracking the class-name stack so methods produce
    `Class.method` (or `Outer.Inner.method` for nested classes).
    Nested function_definitions NOT inside a class keep just the function name.
    """
    stack = class_stack or []

    def walk(n: Node, st: list[str]):
        if n.type == "class_definition":
            name_node = n.child_by_field_name("name")
            cname = name_node.text.decode() if name_node else "_anon_class"
            new_stack = st + [cname]
            body = n.child_by_field_name("body")
            if body is not None:
                for c in body.children:
                    yield from walk(c, new_stack)
            return
        if n.type == "function_definition":
            name_node = n.child_by_field_name("name")
            if name_node is None:
                return
            fname = name_node.text.decode()
            is_async = any(c.type == "async" for c in n.children)
            suffix = ".".join(st + [fname]) if st else fname
            yield n, is_async, suffix
            # Don't recurse into function body for further function defs — we
            # intentionally skip inner functions to avoid namespace explosion.
            return
        for c in n.children:
            yield from walk(c, st)

    yield from walk(root, stack)


def _py_resolve_call_target(func_expr: Node) -> str | None:
    if func_expr is None:
        return None
    if func_expr.type == "identifier":
        return func_expr.text.decode()
    if func_expr.type == "attribute":
        # For `router.post(...)` we want 'post' to expose http_verb, but for
        # calls inside function body we want the leaf for import resolution.
        attr = func_expr.child_by_field_name("attribute")
        if attr and attr.type == "identifier":
            return attr.text.decode()
    return None


def _py_iter_calls_in(fn: Node) -> Iterable[tuple[str, int]]:
    """Yield (target_name, line) for every call-like reference inside fn.

    Walks parameters + body so Depends(get_db) in defaults is attributed to the
    enclosing function. Skips nested function_definition subtrees so their
    calls don't leak upward.
    """

    def walk(n: Node):
        if n is not fn and n.type == "function_definition":
            return
        if n.type == "call":
            func = n.child_by_field_name("function")
            target = _py_resolve_call_target(func)
            if target:
                yield target, n.start_point[0] + 1
            args = n.child_by_field_name("arguments")
            if args is not None:
                for arg_child in args.children:
                    if arg_child.type == "identifier":
                        yield arg_child.text.decode(), arg_child.start_point[0] + 1
                    elif arg_child.type == "keyword_argument":
                        val = arg_child.child_by_field_name("value")
                        if val and val.type == "identifier":
                            yield val.text.decode(), val.start_point[0] + 1
                        elif val is not None:
                            yield from walk(val)
                    else:
                        yield from walk(arg_child)
            return
        for c in n.children:
            yield from walk(c)

    yield from walk(fn)


def _py_collect_imports(root: Node) -> dict[str, str]:
    """alias → qualified_name (same semantics as spike parser)."""
    table: dict[str, str] = {}

    def text(n: Node) -> str:
        return n.text.decode()

    def visit(n: Node):
        if n.type == "import_statement":
            for c in n.children:
                if c.type == "dotted_name":
                    name = text(c)
                    alias = name.split(".")[-1]
                    table.setdefault(alias, name)
                elif c.type == "aliased_import":
                    original = c.child_by_field_name("name")
                    alias = c.child_by_field_name("alias")
                    if original and alias:
                        table.setdefault(text(alias), text(original))
            return
        if n.type == "import_from_statement":
            module_name_node = n.child_by_field_name("module_name")
            if module_name_node:
                module_name = text(module_name_node)
                seen_import_kw = False
                for c in n.children:
                    if c.type == "import":
                        seen_import_kw = True
                        continue
                    if not seen_import_kw:
                        continue
                    if c.type == "dotted_name":
                        leaf = text(c).split(".")[-1]
                        table.setdefault(leaf, f"{module_name}.{leaf}")
                    elif c.type == "aliased_import":
                        original = c.child_by_field_name("name")
                        alias = c.child_by_field_name("alias")
                        if original and alias:
                            table.setdefault(text(alias), f"{module_name}.{text(original)}")
            return
        for c in n.children:
            visit(c)

    visit(root)
    return table


def _py_normalize_internal_import_qn(qualified_name: str) -> str:
    """Match package-qualified imports to definitions rooted at REPO_ROOT.

    The bundled runtime indexes ``core/`` as its Python root, so a definition
    in ``api/db.py`` is ``api.db.get_db`` while application imports use
    ``core.api.db``.  Strip the repository-package prefix only when the first
    remaining component exists locally; third-party packages stay untouched.
    """
    prefix = f"{REPO_ROOT.name}."
    if not qualified_name.startswith(prefix):
        return qualified_name
    candidate = qualified_name[len(prefix):]
    first_component = candidate.split(".", 1)[0]
    if not first_component or not (REPO_ROOT / first_component).exists():
        return qualified_name
    return candidate


def _py_extract_decorators(fn_node: Node, src: bytes) -> tuple[list[str], str | None]:
    """Return (decorators_list, http_verb_or_None).

    A decorated function_definition is the *child* of a `decorated_definition`
    in tree-sitter-python. We expect callers to pass `decorated_definition`
    directly OR fall back to the fn_node's parent.
    """
    decorators: list[str] = []
    http_verb: str | None = None

    parent = fn_node.parent
    if parent is None or parent.type != "decorated_definition":
        return decorators, http_verb

    for c in parent.children:
        if c.type != "decorator":
            continue
        # Skip leading '@'
        expr_children = [ch for ch in c.children if ch.type != "@"]
        if not expr_children:
            continue
        dec_expr = expr_children[0]
        # decorator text (raw, without '@')
        dec_text = dec_expr.text.decode().strip()
        decorators.append(dec_text)
        # Extract http_verb from `@router.post(...)` style
        if dec_expr.type == "call":
            func = dec_expr.child_by_field_name("function")
            if func is not None and func.type == "attribute":
                attr_node = func.child_by_field_name("attribute")
                if attr_node is not None:
                    verb = attr_node.text.decode().lower()
                    if verb in HTTP_VERBS:
                        http_verb = verb.upper()
        elif dec_expr.type == "attribute":
            attr_node = dec_expr.child_by_field_name("attribute")
            if attr_node is not None:
                verb = attr_node.text.decode().lower()
                if verb in HTTP_VERBS:
                    http_verb = verb.upper()

    return decorators, http_verb


def parse_python_file(path_str: str) -> tuple[list[dict], list[dict]]:
    """Parse a Python source file, returning (nodes, edges) namespace `py:`.

    Top-level function: importable as module function so ProcessPoolExecutor
    can pickle it (bound methods can't be pickled cleanly).
    """
    path = Path(path_str)
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    if not path.exists():
        return [], []

    try:
        rel_path = str(path.relative_to(REPO_ROOT))
    except ValueError:
        # Outside repo root — use the name as a last resort (shouldn't happen
        # under git ls-files, but keeps the parser robust for ad-hoc calls).
        rel_path = path.name
    module = _rel_to_module_py(rel_path)
    src = path.read_bytes()

    parser = Parser(_py_lang())

    file_node = {
        "id": _safe_id("py", "file", module),
        "type": "file",
        "name": path.name,
        "qualified_name": _norm_qn(module),
        "file_path": rel_path,
        "line_number": 1,
        "metadata": {"language": "python"},
    }
    # Phase 7.3: emit a companion module node per file so the Phase 7.2 bridge
    # sweep (scripts/populate_module_file_bridge.py) finds a module<->file pair
    # for every indexed file (not just those that another file imports by full
    # path). Pre-7.3, py:module:<qn> existed only as an import-target stub, so
    # files like `api/db.py` — imported as `from api.db import get_db`, giving
    # stub id `py:module:api.db.get_db` — never had a `py:module:api.db`
    # sibling to bridge from. The internal flag lets graph callers tell the
    # companion apart from third-party import stubs. `_dedupe_nodes` and
    # `_merge_metadata` preserve `stub=False` + merge `internal=True` even if
    # another file imports this module as a full-path stub in the same run.
    internal_module_node = {
        "id": _safe_id("py", "module", module),
        "type": "module",
        "name": module.split(".")[-1],
        "qualified_name": _norm_qn(module),
        "file_path": rel_path,
        "line_number": 1,
        "metadata": {"stub": False, "internal": True, "language": "python"},
    }
    nodes: list[dict] = [file_node, internal_module_node]
    edges: list[dict] = []

    if not src.strip():
        return nodes, edges

    tree = parser.parse(src)
    root = tree.root_node
    if root.has_error:
        logger.warning("Parse error in %s — best-effort extraction", rel_path)

    imports = {
        alias: _py_normalize_internal_import_qn(qualified_name)
        for alias, qualified_name in _py_collect_imports(root).items()
    }

    # First pass: function defs (with Class.method qualified names)
    func_defs: list[tuple[Node, bool, str]] = list(_py_iter_functions(root))
    module_func_qns: set[str] = set()

    for fn_node, is_async, qn_suffix in func_defs:
        qn = _norm_qn(f"{module}.{qn_suffix}")
        module_func_qns.add(qn)
        decorators, http_verb = _py_extract_decorators(fn_node, src)
        metadata: dict[str, Any] = {
            "language": "python",
            "is_async": is_async,
            "stub": False,
        }
        if decorators:
            metadata["decorators"] = decorators
        if http_verb:
            metadata["http_verb"] = http_verb
        name_node = fn_node.child_by_field_name("name")
        fname = name_node.text.decode() if name_node else qn_suffix.split(".")[-1]
        line = fn_node.start_point[0] + 1
        node_id = _safe_id("py", "function", qn)
        nodes.append({
            "id": node_id,
            "type": "function",
            "name": fname,
            "qualified_name": qn,
            "file_path": rel_path,
            "line_number": line,
            "metadata": metadata,
        })
        edges.append({
            "source_id": file_node["id"],
            "target_id": node_id,
            "relation": "defines",
            "source_file": rel_path,
            "source_line": line,
        })

    # Second pass: calls inside each function
    emitted_call_targets: dict[str, dict] = {}
    for fn_node, _is_async, qn_suffix in func_defs:
        src_qn = _norm_qn(f"{module}.{qn_suffix}")
        src_id = _safe_id("py", "function", src_qn)

        seen_edges_in_fn: set[tuple[str, str]] = set()
        for target_name, line in _py_iter_calls_in(fn_node):
            if target_name in PY_BUILTINS_SKIP:
                continue
            # Resolve target
            local_qn = _norm_qn(f"{module}.{target_name}")
            if local_qn in module_func_qns:
                target_qn = local_qn
            elif target_name in imports:
                target_qn = _norm_qn(imports[target_name])
            else:
                target_qn = _norm_qn(target_name)

            target_id = _safe_id("py", "function", target_qn)
            edge_key = (target_id, "calls")
            if edge_key in seen_edges_in_fn:
                continue
            seen_edges_in_fn.add(edge_key)

            # Emit stub target if not in-module and not already emitted.
            if target_id not in emitted_call_targets and target_qn not in module_func_qns:
                emitted_call_targets[target_id] = {
                    "id": target_id,
                    "type": "function",
                    "name": target_qn.split(".")[-1],
                    "qualified_name": target_qn,
                    "file_path": None,
                    "line_number": None,
                    "metadata": {"stub": True, "language": "python"},
                }
            edges.append({
                "source_id": src_id,
                "target_id": target_id,
                "relation": "calls",
                "source_file": rel_path,
                "source_line": line,
            })

    nodes.extend(emitted_call_targets.values())

    # Imports → file --imports--> module (stubs namespace py:)
    seen_module_stubs: set[str] = set()
    for alias, qn in imports.items():
        qn_norm = _norm_qn(qn)
        # Imports outside the repo (site-packages, stdlib) still get stubs — they
        # are queryable but flagged `stub: true`. Language is python regardless.
        target_module_id = _safe_id("py", "module", qn_norm)
        nodes.append({
            "id": target_module_id,
            "type": "module",
            "name": qn_norm.split(".")[-1],
            "qualified_name": qn_norm,
            "file_path": None,
            "line_number": None,
            "metadata": {"stub": True, "alias": alias, "language": "python"},
        })
        edges.append({
            "source_id": file_node["id"],
            "target_id": target_module_id,
            "relation": "imports",
            "source_file": rel_path,
            "source_line": None,
        })

        # Phase 7.4: for `from X import Y` (qn="X.Y"), also emit a module-level
        # stub `py:module:X` so the bridge sweep can match it to `py:file:X` via
        # qualified_name equality. Without this, symbol-level stubs (qn="X.Y")
        # never bridge because no file node has qn="X.Y".
        # `_dedupe_nodes` ensures stub=False (from the Phase 7.3 internal
        # companion) wins over stub=True emitted here.
        dot = qn_norm.rfind(".")
        if dot > 0:
            parent_qn = qn_norm[:dot]
            if parent_qn not in seen_module_stubs:
                seen_module_stubs.add(parent_qn)
                parent_module_id = _safe_id("py", "module", parent_qn)
                nodes.append({
                    "id": parent_module_id,
                    "type": "module",
                    "name": parent_qn.split(".")[-1],
                    "qualified_name": parent_qn,
                    "file_path": None,
                    "line_number": None,
                    "metadata": {"stub": True, "language": "python"},
                })
                edges.append({
                    "source_id": file_node["id"],
                    "target_id": parent_module_id,
                    "relation": "imports",
                    "source_file": rel_path,
                    "source_line": None,
                })

    return nodes, edges


# ---------------------------------------------------------------------------
# TypeScript parser
# ---------------------------------------------------------------------------


_HOOK_NAME_RE = re.compile(r"^use[A-Z]")


def _ts_iter_functions(root: Node, is_tsx: bool) -> Iterable[tuple[Node, bool, str, str, bool, bool]]:
    """Yield (fn_node, is_async, qualified_suffix, leaf_name, is_component, is_hook).

    Handles:
      function foo() {}
      async function foo() {}
      export function foo() {}
      export default function foo() {}
      const foo = () => {}
      const foo = async () => {}
      const Foo = (props) => <div/>     (component, tsx only)
      class Foo { bar() {} }            (method → Foo.bar)

    We deliberately don't descend into nested functions — too noisy for a v1.
    """
    results: list[tuple[Node, bool, str, str, bool, bool]] = []

    def classify(name: str, fn_node: Node) -> tuple[bool, bool]:
        """Return (is_component, is_hook) based on naming + JSX return."""
        is_hook = bool(_HOOK_NAME_RE.match(name))
        is_component = False
        if is_tsx and name and name[0].isupper():
            # Treat any uppercased function in tsx as a component candidate.
            # We could additionally verify JSX return, but tree-sitter-typescript
            # JSX detection is expensive and the naming heuristic is the
            # community norm (React eslint rules rely on it too).
            is_component = True
        return is_component, is_hook

    def handle_function_decl(n: Node, class_stack: list[str]):
        name_node = n.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode()
        is_async = any(c.type == "async" for c in n.children)
        is_component, is_hook = classify(name, n)
        suffix = ".".join(class_stack + [name]) if class_stack else name
        results.append((n, is_async, suffix, name, is_component, is_hook))

    def handle_method(n: Node, class_stack: list[str]):
        name_node = n.child_by_field_name("name")
        if name_node is None:
            return
        name = name_node.text.decode()
        # tree-sitter-typescript marks async as a modifier inside method_definition
        is_async = any(c.type == "async" for c in n.children)
        is_component, is_hook = classify(name, n)
        suffix = ".".join(class_stack + [name]) if class_stack else name
        results.append((n, is_async, suffix, name, is_component, is_hook))

    def handle_variable_declarator(n: Node, class_stack: list[str]):
        # const foo = () => ... / const Foo = async () => <div/>
        name_node = n.child_by_field_name("name")
        value_node = n.child_by_field_name("value")
        if name_node is None or value_node is None:
            return
        if name_node.type != "identifier":
            return
        name = name_node.text.decode()
        # Peel away wrappers (as-expression, parenthesized, satisfies)
        v = value_node
        while v is not None and v.type in ("as_expression", "parenthesized_expression", "satisfies_expression"):
            inner = v.child_by_field_name("expression") or (v.children[0] if v.children else None)
            if inner is None or inner is v:
                break
            v = inner
        if v is None or v.type not in ("arrow_function", "function_expression", "function"):
            return
        is_async = any(c.type == "async" for c in v.children)
        is_component, is_hook = classify(name, v)
        suffix = ".".join(class_stack + [name]) if class_stack else name
        results.append((v, is_async, suffix, name, is_component, is_hook))

    def walk(n: Node, class_stack: list[str]):
        if n.type in ("function_declaration", "generator_function_declaration"):
            handle_function_decl(n, class_stack)
            return  # do not descend into body
        if n.type == "class_declaration":
            name_node = n.child_by_field_name("name")
            cname = name_node.text.decode() if name_node else "_anon_class"
            body = n.child_by_field_name("body")
            if body is not None:
                for c in body.children:
                    if c.type in ("method_definition",):
                        handle_method(c, class_stack + [cname])
            return
        if n.type == "variable_declarator":
            handle_variable_declarator(n, class_stack)
            return
        # Walk into top-level, export statements, lexical decl, etc.
        for c in n.children:
            walk(c, class_stack)

    walk(root, [])
    yield from results


def _ts_collect_imports(root: Node) -> dict[str, str]:
    """alias/identifier → source module path (as-written, e.g. './utils', 'react').

    TypeScript imports:
        import X from 'react'              → X → 'react'
        import { a, b as c } from './x'    → a → './x' (leaf=a), c → './x' (leaf=b)
        import * as ns from './x'          → ns → './x'
    We don't resolve path aliases or tsconfig baseUrl here — the stub module
    node carries the raw source so it stays queryable.
    """
    table: dict[str, str] = {}

    def text(n: Node) -> str:
        return n.text.decode().strip("'\"`")

    def visit(n: Node):
        if n.type == "import_statement":
            source_node = n.child_by_field_name("source")
            if source_node is None:
                # Fall back: last child of type string
                for c in reversed(n.children):
                    if c.type == "string":
                        source_node = c
                        break
            if source_node is None:
                return
            src = text(source_node)
            import_clause = None
            for c in n.children:
                if c.type == "import_clause":
                    import_clause = c
                    break
            if import_clause is None:
                return
            for c in import_clause.children:
                if c.type == "identifier":
                    # default import: `import Foo from '...'`
                    table.setdefault(text(c), src)
                elif c.type == "namespace_import":
                    # import * as ns from '...'
                    for sub in c.children:
                        if sub.type == "identifier":
                            table.setdefault(text(sub), src)
                elif c.type == "named_imports":
                    for spec in c.children:
                        if spec.type == "import_specifier":
                            alias_node = spec.child_by_field_name("alias")
                            name_node = spec.child_by_field_name("name")
                            if alias_node and alias_node.type == "identifier":
                                table.setdefault(text(alias_node), src)
                            elif name_node and name_node.type == "identifier":
                                table.setdefault(text(name_node), src)
            return
        for c in n.children:
            visit(c)

    visit(root)
    return table


def _ts_iter_calls_in(fn: Node) -> Iterable[tuple[str, int]]:
    """Yield (target_name, line) for call expressions inside fn.

    We stop at nested function boundaries so inner callbacks don't leak up —
    mirrors the Python walker behaviour.
    """
    nested_fn_types = {
        "function_declaration", "generator_function_declaration",
        "function_expression", "arrow_function", "method_definition",
    }

    def resolve(func_expr: Node) -> str | None:
        if func_expr is None:
            return None
        if func_expr.type == "identifier":
            return func_expr.text.decode()
        if func_expr.type == "member_expression":
            prop = func_expr.child_by_field_name("property")
            if prop is not None and prop.type in ("property_identifier", "identifier"):
                return prop.text.decode()
        return None

    def walk(n: Node):
        if n is not fn and n.type in nested_fn_types:
            return
        if n.type == "call_expression":
            func = n.child_by_field_name("function")
            target = resolve(func)
            if target:
                yield target, n.start_point[0] + 1
        for c in n.children:
            yield from walk(c)

    yield from walk(fn)


def parse_typescript_file(path_str: str) -> tuple[list[dict], list[dict]]:
    """Parse a TS/TSX file with tree-sitter-typescript. Namespace `ts:`."""
    path = Path(path_str)
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    if not path.exists():
        return [], []

    try:
        rel_path = str(path.relative_to(REPO_ROOT))
    except ValueError:
        rel_path = path.name
    module = _rel_to_module_ts(rel_path)
    src = path.read_bytes()
    is_tsx = path.suffix == ".tsx"

    parser = Parser(_tsx_lang() if is_tsx else _ts_lang())

    file_node = {
        "id": _safe_id("ts", "file", module),
        "type": "file",
        "name": path.name,
        "qualified_name": _norm_qn(module),
        "file_path": rel_path,
        "line_number": 1,
        "metadata": {"language": "typescript", "is_tsx": is_tsx},
    }
    # Phase 7.3: emit a companion module node per file so the bridge sweep finds
    # a `ts:module:<qn>` sibling for every file. Without this, TS bridge
    # coverage is ~1/258 because TS imports collapse their source to the
    # filename tail (`./utils` -> `utils`) via `_ts_import_source_to_qn`, so the
    # stub qualified_name rarely matches the file qualified_name (which keeps
    # the full `console.src.components.modal` path). See Python parser for
    # parallel logic + dedupe/metadata-merge semantics.
    internal_module_node = {
        "id": _safe_id("ts", "module", module),
        "type": "module",
        "name": module.split(".")[-1],
        "qualified_name": _norm_qn(module),
        "file_path": rel_path,
        "line_number": 1,
        "metadata": {
            "stub": False,
            "internal": True,
            "language": "typescript",
            "is_tsx": is_tsx,
        },
    }
    nodes: list[dict] = [file_node, internal_module_node]
    edges: list[dict] = []

    if not src.strip():
        return nodes, edges

    tree = parser.parse(src)
    root = tree.root_node
    if root.has_error:
        logger.warning("Parse error in %s — best-effort extraction", rel_path)

    imports = _ts_collect_imports(root)

    fns = list(_ts_iter_functions(root, is_tsx=is_tsx))
    module_func_qns: set[str] = set()

    for fn_node, is_async, suffix, leaf_name, is_component, is_hook in fns:
        qn = _norm_qn(f"{module}.{suffix}")
        module_func_qns.add(qn)
        line = fn_node.start_point[0] + 1
        metadata: dict[str, Any] = {
            "language": "typescript",
            "is_async": is_async,
            "stub": False,
        }
        if is_tsx:
            metadata["is_tsx"] = True
        if is_component:
            metadata["is_component"] = True
        if is_hook:
            metadata["is_hook"] = True
        node_id = _safe_id("ts", "function", qn)
        nodes.append({
            "id": node_id,
            "type": "function",
            "name": leaf_name,
            "qualified_name": qn,
            "file_path": rel_path,
            "line_number": line,
            "metadata": metadata,
        })
        edges.append({
            "source_id": file_node["id"],
            "target_id": node_id,
            "relation": "defines",
            "source_file": rel_path,
            "source_line": line,
        })

    # Calls
    emitted_call_targets: dict[str, dict] = {}
    for fn_node, _is_async, suffix, _leaf, _is_comp, _is_hook in fns:
        src_qn = _norm_qn(f"{module}.{suffix}")
        src_id = _safe_id("ts", "function", src_qn)
        seen_in_fn: set[tuple[str, str]] = set()

        for target_name, line in _ts_iter_calls_in(fn_node):
            if target_name in TS_BUILTINS_SKIP:
                continue
            local_qn = _norm_qn(f"{module}.{target_name}")
            if local_qn in module_func_qns:
                target_qn = local_qn
            elif target_name in imports:
                # `react` / './utils' — we point at a module-like qualified name
                # by normalizing the import source path to a dotted form.
                src_path = imports[target_name]
                # Use the raw module source so cross-file resolution is at least
                # queryable; `_safe_id` will lower-case + normalize it.
                target_qn = _norm_qn(_ts_import_source_to_qn(src_path) + "." + target_name)
            else:
                target_qn = _norm_qn(target_name)

            target_id = _safe_id("ts", "function", target_qn)
            edge_key = (target_id, "calls")
            if edge_key in seen_in_fn:
                continue
            seen_in_fn.add(edge_key)

            if target_id not in emitted_call_targets and target_qn not in module_func_qns:
                name_leaf = target_qn.split(".")[-1]
                emitted_call_targets[target_id] = {
                    "id": target_id,
                    "type": "function",
                    "name": name_leaf,
                    "qualified_name": target_qn,
                    "file_path": None,
                    "line_number": None,
                    "metadata": {
                        "stub": True,
                        "language": "typescript",
                        "is_hook": bool(_HOOK_NAME_RE.match(name_leaf)),
                    },
                }
            edges.append({
                "source_id": src_id,
                "target_id": target_id,
                "relation": "calls",
                "source_file": rel_path,
                "source_line": line,
            })

    nodes.extend(emitted_call_targets.values())

    # Imports
    for alias, src_path in imports.items():
        mod_qn = _ts_import_source_to_qn(src_path)
        target_id = _safe_id("ts", "module", mod_qn)
        nodes.append({
            "id": target_id,
            "type": "module",
            "name": mod_qn.split(".")[-1],
            "qualified_name": _norm_qn(mod_qn),
            "file_path": None,
            "line_number": None,
            "metadata": {"stub": True, "alias": alias, "language": "typescript", "source": src_path},
        })
        edges.append({
            "source_id": file_node["id"],
            "target_id": target_id,
            "relation": "imports",
            "source_file": rel_path,
            "source_line": None,
        })

    return nodes, edges


def _ts_import_source_to_qn(src: str) -> str:
    """Normalize an import source string to a qualified-name-safe dotted form.

    Examples:
        'react'                 → 'react'
        './utils'               → 'utils'
        '../hooks/useX'         → 'hooks.usex'
        '@/components/Modal'    → 'components.modal'
    """
    s = src.strip()
    # Strip alias prefix '@/' used by Next.js path aliases
    if s.startswith("@/"):
        s = s[2:]
    # Drop leading './' and '../' segments — they don't help cross-file linking
    # and would produce invalid node_ids (`.` repeated).
    while s.startswith("./") or s.startswith("../"):
        if s.startswith("./"):
            s = s[2:]
        else:
            s = s[3:]
    # Drop extensions if present
    for ext in (".tsx", ".ts", ".js", ".jsx", ".mjs"):
        if s.endswith(ext):
            s = s[: -len(ext)]
            break
    s = s.replace("/", ".")
    # Collapse any leading dots
    s = s.lstrip(".")
    if not s:
        s = "_unknown_module"
    # Sanitize any character not allowed by NODE_ID_RE (parens/brackets/etc.)
    s = re.sub(r"[()\[\]{}!$,'\"`@#%^&*+=<>?:;|/\\]", "", s)
    s = re.sub(r"\.+", ".", s).strip(".")
    if not s:
        s = "_unknown_module"
    return s.lower()


# ---------------------------------------------------------------------------
# Persistence — chunked UPSERT with metadata merge
# ---------------------------------------------------------------------------


def _merge_metadata(old_json: str | None, new_meta: dict) -> str:
    """Merge existing metadata JSON with new dict. New keys win, but
    `stub: True` never overwrites `stub: False` (definitions trump stubs).
    """
    try:
        old = json.loads(old_json) if old_json else {}
    except (TypeError, ValueError):
        old = {}
    merged = dict(old)
    for k, v in new_meta.items():
        if k == "stub":
            # A concrete definition (stub=False) must never be demoted by a stub ref.
            if merged.get("stub") is False and v is True:
                continue
        merged[k] = v
    return json.dumps(merged, sort_keys=True, separators=(",", ":"))


def _dedupe_nodes(nodes: Iterable[dict]) -> list[dict]:
    """Collapse duplicates by id, merging metadata across duplicates.

    Later concrete definitions beat earlier stubs (same logic as _merge_metadata).
    """
    merged: dict[str, dict] = {}
    for n in nodes:
        nid = n["id"]
        if nid not in merged:
            merged[nid] = dict(n)
            continue
        cur = merged[nid]
        # Prefer non-null file_path/line_number (concrete definition)
        if not cur.get("file_path") and n.get("file_path"):
            cur["file_path"] = n["file_path"]
        if not cur.get("line_number") and n.get("line_number"):
            cur["line_number"] = n["line_number"]
        # Merge metadata
        cur_meta = cur.get("metadata") or {}
        new_meta = n.get("metadata") or {}
        merged_meta = dict(cur_meta)
        for k, v in new_meta.items():
            if k == "stub":
                if merged_meta.get("stub") is False and v is True:
                    continue
            merged_meta[k] = v
        cur["metadata"] = merged_meta
    return list(merged.values())


def _chunked(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _upsert_nodes_chunked(
    conn: sqlite3.Connection, nodes: list[dict], batch_size: int = BATCH_SIZE
) -> int:
    """Chunked UPSERT with metadata merge.

    Strategy: for each chunk, SELECT existing metadata for the ids in the
    chunk, merge in Python, then executemany INSERT/ON CONFLICT. This keeps
    each transaction under `batch_size` rows which is the right ballpark for
    SQLite on a disk DB.
    """
    total = 0
    for chunk in _chunked(nodes, batch_size):
        ids = [n["id"] for n in chunk]
        placeholders = ",".join(["?"] * len(ids))
        cur = conn.execute(
            f"SELECT id, metadata FROM graph_nodes WHERE id IN ({placeholders})",
            ids,
        )
        existing_meta = {row[0]: row[1] for row in cur.fetchall()}

        rows_to_write = []
        for n in chunk:
            merged_meta_json = _merge_metadata(existing_meta.get(n["id"]), n.get("metadata") or {})
            # Fase 2: ast_parser indexes the MarvisX monorepo. Default
            # project_id='marvisx' so new nodes created post-migration-073
            # don't end up with NULL project_id (breaks scope filters).
            rows_to_write.append((
                n["id"],
                n["type"],
                n["name"],
                n["qualified_name"],
                n.get("file_path"),
                n.get("line_number"),
                merged_meta_json,
                n.get("project_id", PROJECT_ID),
            ))

        conn.execute("BEGIN IMMEDIATE")
        try:
            # Fase 1d: set last_seen_at on every insert + update so the stale
            # detection pass can tell "still in the codebase" from "vanished".
            # Fase 2: project_id written on insert, preserved on conflict.
            conn.executemany(
                """
                INSERT INTO graph_nodes
                    (id, type, name, qualified_name, file_path, line_number,
                     metadata, last_seen_at, project_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?)
                ON CONFLICT(id) DO UPDATE SET
                    type = excluded.type,
                    name = excluded.name,
                    qualified_name = excluded.qualified_name,
                    file_path = COALESCE(excluded.file_path, graph_nodes.file_path),
                    line_number = COALESCE(excluded.line_number, graph_nodes.line_number),
                    metadata = excluded.metadata,
                    deprecated_at = NULL,
                    last_seen_at = datetime('now'),
                    updated_at = datetime('now'),
                    project_id = COALESCE(graph_nodes.project_id, excluded.project_id)
                """,
                rows_to_write,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        total += len(rows_to_write)
    return total


def _delete_stale_edges_for_files(
    conn: sqlite3.Connection, parsed_file_paths: list[str]
) -> int:
    """Delete edges whose source_file is in the re-parsed set.

    Called BEFORE `_upsert_edges_chunked` to clear edges that may have vanished
    from the new AST (e.g., a `Depends(get_db)` that became `Depends(get_write_db)`
    after a refactor: the old `calls get_db` edge had no path to be overwritten by
    a pure INSERT..ON CONFLICT UPSERT since the source_id/target_id pair differs).

    Edges with `source_file IS NULL` (artifacts edges, doc chain, etc.) are
    preserved: `NULL IN (...)` is always falsy in SQL.

    Returns: total number of rows deleted across all chunks.

    Task: 2a98db4a (fix edges orfane after single-writer refactor burst).
    """
    if not parsed_file_paths:
        return 0
    total = 0
    # Chunked DELETE to avoid huge IN clause (SQLite parameter limit ~999).
    # Use the same paranoid "normalized absolute path" form the INSERT side uses:
    # edge rows store `source_file` exactly as emitted by the parser, which for
    # real runs is the absolute string returned by `Path.resolve()`. Tests may
    # inject plain relative strings — we pass the list through as-is and let
    # the caller decide the canonical form.
    chunk_size = 500
    for i in range(0, len(parsed_file_paths), chunk_size):
        chunk = parsed_file_paths[i:i + chunk_size]
        placeholders = ",".join("?" * len(chunk))
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                f"DELETE FROM graph_edges WHERE source_file IN ({placeholders})",
                chunk,
            )
            total += cur.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return total


def _upsert_edges_chunked(
    conn: sqlite3.Connection, edges: list[dict], batch_size: int = EDGE_BATCH_SIZE
) -> int:
    """Chunked UPSERT for edges. ON CONFLICT preserves source_line and bumps confidence.

    Fase 1d: first_seen_at is set only on insert (COALESCE keeps the original
    value on conflict), last_seen_at is refreshed on every pass.
    """
    total = 0
    for chunk in _chunked(edges, batch_size):
        rows = [
            (
                e["source_id"],
                e["target_id"],
                e["relation"],
                e.get("source_file"),
                e.get("source_line"),
                e.get("project_id", PROJECT_ID),
            )
            for e in chunk
        ]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                """
                INSERT INTO graph_edges
                    (source_id, target_id, relation, source_file, source_line,
                     first_seen_at, last_seen_at, project_id)
                VALUES (?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?)
                ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                    source_line = COALESCE(excluded.source_line, graph_edges.source_line),
                    confidence = MAX(graph_edges.confidence, excluded.confidence),
                    last_seen_at = datetime('now'),
                    project_id = COALESCE(graph_edges.project_id, excluded.project_id)
                """,
                rows,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        total += len(rows)
    return total


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _resolve_db_path(explicit: str | None = None) -> str:
    """Resolve which DB to populate.

    Priority:
    1. Explicit --db argument
    2. /data/pir/console.db if it exists (prod)
    3. {repo_root}/console.db (dev)
    """
    if explicit:
        return explicit
    prod = Path("/data/pir/console.db")
    if prod.exists():
        return str(prod)
    return str(REPO_ROOT / "console.db")


def populate_graph_chunked(
    db_path: str | None = None,
    python_workers: int = 4,
    skip_python: bool = False,
    skip_typescript: bool = False,
    run_stale_check: bool = False,
    stale_days: int = 14,
    files: list[Path] | None = None,
    scan_patterns: tuple[str, ...] | None = None,
    repo_root: Path | None = None,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Full-codebase or incremental populate. Returns measurements dict.

    Args:
        db_path: explicit SQLite path (else resolve_db_path)
        python_workers: ProcessPoolExecutor size for Python parse (set 0 to skip pool)
        skip_python / skip_typescript: test escape hatch.
        run_stale_check: if True, call populate_temporal.mark_stale_nodes after
            the upsert to soft-delete nodes whose `last_seen_at` is older than
            `stale_days`. Default off so the default path stays pure parser —
            temporal is driven by `core.scripts.populate_temporal` (cron).
        stale_days: passed to mark_stale_nodes when run_stale_check is True.
        files: if provided (Fase 1g incremental mode), parse ONLY these files
            instead of running `git ls-files` discovery. Non-existent paths are
            silently dropped (incremental runs after deletes are a no-op). Files
            are split by extension: `.py` -> Python parser, `.ts`/`.tsx` ->
            TypeScript parser, others ignored. Empty list is a valid no-op.
        repo_root: RI-7 layer 2 — index an ARBITRARY repo in-place. When set, the
            parser computes node ids / `rel_path` relative to this root and tags
            every node/edge with ``project_id`` instead of the marvisx default.
            The globals are restored after the run so concurrent callers (and
            the next CLI invocation) are unaffected. Used with file-list mode
            (the manifest-driven `source_roots[]` walk supplies the files) and
            ``python_workers=0`` so the in-process global override is honored.
        project_id: tag inserted nodes/edges with this project. Required when
            ``repo_root`` points at a non-marvisx tree (else they'd be filed
            under ``marvisx`` and corrupt scope filters).
    """
    global REPO_ROOT, PROJECT_ID  # noqa: PLW0603 — scoped override, restored in finally
    _saved_repo_root, _saved_project_id = REPO_ROOT, PROJECT_ID
    if repo_root is not None:
        REPO_ROOT = repo_root.resolve()
    if project_id is not None:
        PROJECT_ID = project_id
    try:
        return _populate_graph_chunked_impl(
            db_path=db_path,
            python_workers=python_workers,
            skip_python=skip_python,
            skip_typescript=skip_typescript,
            run_stale_check=run_stale_check,
            stale_days=stale_days,
            files=files,
            scan_patterns=scan_patterns,
        )
    finally:
        REPO_ROOT, PROJECT_ID = _saved_repo_root, _saved_project_id


def _populate_graph_chunked_impl(
    db_path: str | None = None,
    python_workers: int = 4,
    skip_python: bool = False,
    skip_typescript: bool = False,
    run_stale_check: bool = False,
    stale_days: int = 14,
    files: list[Path] | None = None,
    scan_patterns: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Inner body of :func:`populate_graph_chunked` (globals already set)."""
    db = _resolve_db_path(db_path)
    if files is None:
        # v1.4.1: pass scan_patterns through so external repos with non-marvisx
        # layouts (e.g. marvis-mac queue-gateway/, services/) can be scanned.
        py_files, ts_files = discover_files(patterns=scan_patterns)
    else:
        py_files = []
        ts_files = []
        for f in files:
            p = Path(f) if not isinstance(f, Path) else f
            if not p.is_absolute():
                p = (REPO_ROOT / p).resolve()
            if not p.exists():
                continue
            suffix = p.suffix.lower()
            if suffix == ".py":
                py_files.append(p)
            elif suffix in (".ts", ".tsx"):
                ts_files.append(p)
    if skip_python:
        py_files = []
    if skip_typescript:
        ts_files = []

    # ---- Parse Python with ProcessPool ----
    t_py0 = time.perf_counter()
    py_results: list[tuple[list[dict], list[dict]]] = []
    if py_files:
        path_strs = [str(p) for p in py_files]
        if python_workers > 0 and len(path_strs) > python_workers:
            with ProcessPoolExecutor(max_workers=python_workers) as pool:
                py_results = list(pool.map(parse_python_file, path_strs))
        else:
            py_results = [parse_python_file(p) for p in path_strs]
    py_parse_ms = (time.perf_counter() - t_py0) * 1000

    # ---- Parse TypeScript sequentially (tree-sitter native is already fast) ----
    t_ts0 = time.perf_counter()
    ts_results: list[tuple[list[dict], list[dict]]] = []
    for f in ts_files:
        try:
            ts_results.append(parse_typescript_file(str(f)))
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("TS parse failed for %s: %s", f, e)
            ts_results.append(([], []))
    ts_parse_ms = (time.perf_counter() - t_ts0) * 1000

    all_nodes = [n for r in py_results + ts_results for n in r[0]]
    all_edges = [e for r in py_results + ts_results for e in r[1]]

    nodes_unique = _dedupe_nodes(all_nodes)

    # ---- Write (chunked UPSERT) ----
    t_w0 = time.perf_counter()
    conn = sqlite3.connect(db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        n_nodes_written = _upsert_nodes_chunked(conn, nodes_unique)
        # Fix edges orfane (task 2a98db4a): DELETE edges whose source_file is
        # in the set of re-parsed files before re-inserting the fresh set. This
        # clears edges that vanished from the new AST (e.g., a Depends(get_db)
        # replaced by Depends(get_write_db)). Artifacts edges (source_file
        # IS NULL) are NOT touched.
        #
        # The parser stores `source_file` as a repo-relative string (see
        # `parse_python_file` / `parse_typescript_file`: rel_path =
        # str(path.relative_to(REPO_ROOT))). We must match that exact form.
        parsed_paths: list[str] = []
        for p in (py_files + ts_files):
            abs_p = p if p.is_absolute() else (REPO_ROOT / p).resolve()
            try:
                parsed_paths.append(str(abs_p.relative_to(REPO_ROOT)))
            except ValueError:
                parsed_paths.append(abs_p.name)
        n_edges_deleted = _delete_stale_edges_for_files(conn, parsed_paths)
        n_edges_written = _upsert_edges_chunked(conn, all_edges)
    finally:
        conn.close()
    write_ms = (time.perf_counter() - t_w0) * 1000

    stale_result: dict[str, Any] | None = None
    if run_stale_check:
        # Lazy import to keep ast_parser usable even when populate_temporal
        # module is absent (e.g. in a downgrade state).
        try:
            from core.scripts.populate_temporal import mark_stale_nodes
        except ImportError:
            mark_stale_nodes = None
        if mark_stale_nodes is not None:
            try:
                stale_result = mark_stale_nodes(db_path=db, stale_days=stale_days)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("stale-check skipped: %s", e)

    out = {
        "db_path": db,
        "python_files": len(py_files),
        "typescript_files": len(ts_files),
        "python_parse_ms": round(py_parse_ms, 2),
        "typescript_parse_ms": round(ts_parse_ms, 2),
        "write_ms": round(write_ms, 2),
        "n_nodes": n_nodes_written,
        "n_edges_deleted_stale": n_edges_deleted,
        "n_edges": n_edges_written,
    }
    if stale_result is not None:
        out["stale"] = stale_result
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main() -> int:
    ap = argparse.ArgumentParser(description="KG Fase 1a AST parser (Python + TypeScript)")
    ap.add_argument("--db", default=None, help="Path to SQLite DB (default: auto-resolve)")
    ap.add_argument("--workers", type=int, default=4, help="Python ProcessPool workers (0 = no pool)")
    ap.add_argument("--skip-python", action="store_true")
    ap.add_argument("--skip-typescript", action="store_true")
    ap.add_argument(
        "--run-stale-check",
        action="store_true",
        help="Fase 1d: after the upsert, soft-delete nodes not seen in --stale-days",
    )
    ap.add_argument("--stale-days", type=int, default=14)
    ap.add_argument(
        "--incremental",
        action="store_true",
        help="Fase 1g: parse only the files listed as positional args (post-commit hook mode)",
    )
    ap.add_argument(
        "files",
        nargs="*",
        help="Files to parse in --incremental mode (relative or absolute). Ignored otherwise.",
    )
    ap.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Override REPO_ROOT to scan a different git repo (e.g. ~/repos/<repo-name>). When set, --project should also be passed.",
    )
    ap.add_argument(
        "--project",
        type=str,
        default=None,
        help="Override default project_id ('marvisx') for nodes/edges inserted in this run. Required for --repo-root != marvisx.",
    )
    ap.add_argument(
        "--scan-patterns",
        nargs="+",
        default=None,
        metavar="GLOB",
        help="git ls-files patterns to discover .py/.ts/.tsx files (default: marvisx-specific 'api/*.py' 'console/src/*.ts' 'console/src/*.tsx'). For external repos use generic '**/*.py' '**/*.ts' '**/*.tsx'.",
    )
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    # v1.4.0: multi-repo support — mutate module globals when overrides supplied.
    global REPO_ROOT, PROJECT_ID  # noqa: PLW0603 — intentional CLI-driven override
    if args.repo_root is not None:
        REPO_ROOT = args.repo_root.resolve()
    if args.project is not None:
        PROJECT_ID = args.project

    files_arg: list[Path] | None = None
    if args.incremental:
        if not args.files:
            # Empty incremental call is a no-op — exit clean so the hook never fails.
            print(json.dumps({"incremental": True, "files": 0, "n_nodes": 0, "n_edges": 0}))
            return 0
        files_arg = [Path(f) for f in args.files]

    scan_patterns = tuple(args.scan_patterns) if args.scan_patterns else None
    out = populate_graph_chunked(
        db_path=args.db,
        python_workers=args.workers,
        skip_python=args.skip_python,
        skip_typescript=args.skip_typescript,
        run_stale_check=args.run_stale_check,
        stale_days=args.stale_days,
        files=files_arg,
        scan_patterns=scan_patterns,
    )
    if args.incremental:
        out["incremental"] = True
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(_main())
