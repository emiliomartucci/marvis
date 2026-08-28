# v1.0.0 - 2026-05-28 - S-Transmute RI-7 layer 2: in-place KG index of an arbitrary repo
"""In-place KG indexing of a transmuted project's source code (RI-7 layer 2).

Layer 1 (``core/cli/_transmute.py``) scaffolds the Marvis metadata dir and writes
a ``.marvis-transmute.yaml`` manifest whose ``source_roots[]`` point — in-place,
never copied — at the real source code. This layer reads that manifest and feeds
the source into the existing tree-sitter populator (``scripts/ast_parser``) so a
*foreign* repo gets the same calls/imports/defines graph the marvisx monorepo has,
plus a chunk-per-symbol code embedding.

Two hard guarantees carried over from D3 (regola ferrea, non-distruttiva):

1. **READ-ONLY on the source.** Nothing in this module ever opens a source file
   for writing. The only writes are KG rows in the Marvis DB. We re-hash the
   source roots before and after the index pass and abort if a single byte moved.
2. **File-discovery security gate** (RI-7): a secrets blocklist (always on, never
   trusts only ``.gitignore``), ``follow_symlinks=False``, ``.gitignore`` respect
   with a ``.marvisindex`` override, binary skip (extension + null-byte sniff),
   noise-dir skip, a per-file size cap and a file-count cap (both env-overridable)
   so a 100k-file repo can't exhaust RAM at embed time.

The id scheme is inherited from the populator: ``{py|ts}:{kind}:{qualified_name}``
tagged with ``project_id=<slug>``. Ids are content/qualified-name addressed, NOT
absolute paths, so moving a file inside the repo does not orphan its nodes — the
qualified name (module path) is the same and ``project_id`` keeps the scope.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

logger = logging.getLogger("marvisx.index_source")

# ---------------------------------------------------------------------------
# Security gate — file discovery
# ---------------------------------------------------------------------------

# Secrets blocklist: matched by name (case-insensitive) and a few glob-ish
# suffixes. ALWAYS active — we never rely on `.gitignore` alone to keep a key
# out of the index (the OSS leak post-mortem, learning fa749d38: allowlist green
# while real leaks merely exempted).
_SECRET_EXACT = frozenset(
    {
        ".env",
        ".env.local",
        ".env.production",
        ".env.development",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".htpasswd",
        "credentials",
        "credentials.json",
        "secrets.yaml",
        "secrets.yml",
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
    }
)
_SECRET_PREFIXES = ("id_", "credentials", ".env", "secret")
_SECRET_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".jks",
    ".crt",
    ".cer",
    ".der",
    ".asc",
    ".gpg",
)

# Noise directories never walked (build artifacts, caches, deps, VCS).
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "env",
        "dist",
        "build",
        "target",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
        ".next",
        ".cache",
        "coverage",
        ".gradle",
    }
)

# Source extensions we currently parse (tree-sitter Python + TypeScript).
_SOURCE_SUFFIXES = (".py", ".ts", ".tsx")

# Binary extensions skipped outright (the null-byte sniff is the real backstop).
_BINARY_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp", ".svg",
        ".pdf", ".zip", ".gz", ".tar", ".tgz", ".bz2", ".xz", ".7z", ".rar",
        ".so", ".dylib", ".dll", ".a", ".o", ".obj", ".class", ".pyc", ".pyo",
        ".wasm", ".bin", ".exe", ".woff", ".woff2", ".ttf", ".eot", ".otf",
        ".mp3", ".mp4", ".mov", ".avi", ".wav", ".flac", ".ogg",
        ".db", ".sqlite", ".sqlite3",
    }
)

_DEFAULT_MAX_FILE_BYTES = 1_000_000  # ~1MB per-file cap (RI-7)
_DEFAULT_MAX_FILES = 20_000          # file-count cap; a huge repo can't blow up RAM
_NULL_SNIFF_BYTES = 8192             # first 8KB scanned for a NUL byte


def _max_file_bytes() -> int:
    return _int_env("MARVIS_INDEX_MAX_FILE_BYTES", _DEFAULT_MAX_FILE_BYTES)


def _max_files() -> int:
    return _int_env("MARVIS_INDEX_MAX_FILES", _DEFAULT_MAX_FILES)


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def is_secret_name(name: str) -> bool:
    """True if a filename looks like a secret (blocklist, case-insensitive)."""
    lower = name.lower()
    if lower in _SECRET_EXACT:
        return True
    if any(lower.endswith(suf) for suf in _SECRET_SUFFIXES):
        return True
    if any(lower.startswith(pre) for pre in _SECRET_PREFIXES):
        return True
    return False


def _looks_binary(path: Path) -> bool:
    """Extension check first, then a null-byte sniff of the first 8KB."""
    if path.suffix.lower() in _BINARY_SUFFIXES:
        return True
    try:
        with path.open("rb") as fh:
            return b"\x00" in fh.read(_NULL_SNIFF_BYTES)
    except OSError:
        return True  # unreadable → treat as binary (skip), never crash


def _gitignored(root: Path) -> set[str]:
    """Return repo-relative POSIX paths git would ignore under ``root``.

    Best-effort: if ``root`` is not a git repo (or git is absent) we return an
    empty set — the secrets blocklist + noise-dir skip still apply, so we never
    *fail open* on the things that matter. ``.marvisindex`` (if present, listing
    one repo-relative path per line) overrides the ignore for generated code that
    is genuinely useful to index.
    """
    if not (root / ".git").exists():
        return set()
    try:
        out = subprocess.check_output(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return set()
    ignored = {p for p in out.split("\0") if p}
    override = root / ".marvisindex"
    if override.is_file():
        try:
            for line in override.read_text(encoding="utf-8").splitlines():
                rel = line.strip()
                if rel and not rel.startswith("#"):
                    ignored.discard(rel)
        except OSError:
            pass
    return ignored


@dataclass(slots=True)
class DiscoveryStats:
    """Counters describing why files were kept or dropped (observability)."""

    walked: int = 0
    kept: int = 0
    skipped_secret: int = 0
    skipped_binary: int = 0
    skipped_symlink: int = 0
    skipped_too_large: int = 0
    skipped_not_source: int = 0
    skipped_gitignored: int = 0
    hit_file_cap: bool = False


def iter_source_files(
    root: Path,
    *,
    exclude: Sequence[str] = (),
    stats: DiscoveryStats | None = None,
) -> Iterator[Path]:
    """Yield in-scope source files under ``root`` (the RI-7 security gate).

    Read-only: only ``stat``/``open('rb')`` for sniffing. ``follow_symlinks`` is
    False everywhere — a symlink (file OR dir) is never traversed, so a link
    escaping the root can't smuggle outside files into the index. Yields absolute
    paths in a deterministic (sorted) order.
    """
    root = root.resolve()
    st = stats if stats is not None else DiscoveryStats()
    extra_skip = {e.strip("/").split("/")[-1] for e in exclude if e}
    skip_dirs = _SKIP_DIRS | extra_skip
    ignored = _gitignored(root)
    max_bytes = _max_file_bytes()
    cap = _max_files()

    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name, reverse=True)
        except (PermissionError, OSError):
            continue
        for entry in entries:
            # follow_symlinks=False: a symlink is dropped, dir or file.
            if entry.is_symlink():
                st.skipped_symlink += 1
                continue
            if entry.is_dir():
                if entry.name in skip_dirs:
                    continue
                stack.append(entry)
                continue
            if not entry.is_file():
                continue
            st.walked += 1
            name = entry.name
            if is_secret_name(name):
                st.skipped_secret += 1
                continue
            if entry.suffix.lower() not in _SOURCE_SUFFIXES:
                st.skipped_not_source += 1
                continue
            try:
                rel = entry.resolve().relative_to(root).as_posix()
            except ValueError:
                rel = name
            if rel in ignored:
                st.skipped_gitignored += 1
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                continue
            if size > max_bytes:
                st.skipped_too_large += 1
                continue
            if _looks_binary(entry):
                st.skipped_binary += 1
                continue
            if st.kept >= cap:
                st.hit_file_cap = True
                logger.warning(
                    "file-count cap (%d) reached under %s; remaining files skipped "
                    "(raise MARVIS_INDEX_MAX_FILES to index more).",
                    cap,
                    root,
                )
                return
            st.kept += 1
            yield entry


# ---------------------------------------------------------------------------
# Per-symbol code embeddings (chunk-per-symbol, AST boundaries)
# ---------------------------------------------------------------------------


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def build_symbol_text(node: dict, lines: Sequence[str], *, max_lines: int = 200) -> str:
    """Assemble the embedding text for one function/method node.

    Chunk-per-symbol (NOT a blind sliding window): the populator already gives us
    the symbol's start line; we take the body up to the next symbol or
    ``max_lines`` (cAST-style truncation budget). The qualified name + signature
    line are PREPENDED as context enrichment, which lifts small-model retrieval.
    """
    qn = node.get("qualified_name") or node.get("name") or ""
    start = (node.get("line_number") or 1) - 1
    end = min(len(lines), start + max_lines)
    body = "\n".join(lines[start:end]) if 0 <= start < len(lines) else ""
    header = f"# symbol: {qn}"
    return f"{header}\n{body}".strip()


# An embedder is any callable: list[str] -> list[list[float]]. The default uses
# the in-process Granite client; tests inject a fake to avoid a model load.
Embedder = Callable[[list[str]], list[list[float]]]


def _default_embedder() -> Embedder:
    """Lazily build a Granite-backed embedder (never loaded at import time)."""
    from core.api.services.embedding_internal import GraniteEmbeddingClient

    client = GraniteEmbeddingClient()

    def _embed(texts: list[str]) -> list[list[float]]:
        return client.embed_texts(texts, input_type="document")

    return _embed


def _pack_vector(vec: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vec)}f", *(float(x) for x in vec))


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class IndexResult:
    slug: str
    db_path: str
    roots: list[str] = field(default_factory=list)
    files_indexed: int = 0
    n_nodes: int = 0
    n_edges: int = 0
    n_embeddings: int = 0
    discovery: dict[str, Any] = field(default_factory=dict)
    source_verified_untouched: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "db_path": self.db_path,
            "roots": self.roots,
            "files_indexed": self.files_indexed,
            "n_nodes": self.n_nodes,
            "n_edges": self.n_edges,
            "n_embeddings": self.n_embeddings,
            "discovery": self.discovery,
            "source_verified_untouched": self.source_verified_untouched,
        }


class IndexSourceError(RuntimeError):
    """A manifest/source invariant was violated (read-only, missing roots, ...)."""


def _load_manifest_roots(metadata_dir: Path) -> list[dict[str, Any]]:
    """Read ``source_roots[]`` from the project's ``.marvis-transmute.yaml``."""
    from core.cli import _transmute as tx

    manifest = tx.load_manifest(metadata_dir)
    if manifest is None:
        raise IndexSourceError(
            f"no {tx.MANIFEST_NAME} in {metadata_dir} — run "
            f"`marvis project import <path> --scaffold` (layer 1) first."
        )
    roots = manifest.get("source_roots") or []
    if not roots:
        raise IndexSourceError(f"{tx.MANIFEST_NAME} has empty source_roots[].")
    return roots


def _hash_root(root: Path) -> dict[str, str]:
    """SHA-256 inventory of a source root (read-only proof-of-untouched)."""
    from core.cli import _transmute as tx

    return tx.hash_inventory(root)


def index_project_source(
    metadata_dir: Path,
    *,
    slug: str,
    db_path: str,
    embedder: Embedder | None = None,
    embed: bool = True,
) -> IndexResult:
    """Index every ``source_roots[]`` of a transmuted project into the KG.

    Reuses ``scripts/ast_parser.populate_graph_chunked`` parametrized on the root
    + ``project_id=slug`` (content-addressed ids, NOT absolute paths), then writes
    chunk-per-symbol code embeddings. READ-ONLY on the source: every source root
    is hashed before and after; a single changed byte aborts (D3).
    """
    from core.scripts.ast_parser import populate_graph_chunked

    roots = _load_manifest_roots(metadata_dir)
    result = IndexResult(slug=slug, db_path=db_path)
    agg_stats = DiscoveryStats()

    for entry in roots:
        raw_path = entry.get("path")
        if not raw_path:
            continue
        root = Path(raw_path).expanduser().resolve()
        if not root.is_dir():
            logger.warning("source_root %s is not a directory; skipping.", root)
            continue
        result.roots.append(str(root))
        exclude = entry.get("exclude") or []

        before = _hash_root(root)

        files = list(iter_source_files(root, exclude=exclude, stats=agg_stats))
        result.files_indexed += len(files)

        if files:
            pop = populate_graph_chunked(
                db_path=db_path,
                python_workers=0,  # in-process so the repo_root global override holds
                files=files,
                repo_root=root,
                project_id=slug,
            )
            result.n_nodes += int(pop.get("n_nodes", 0))
            result.n_edges += int(pop.get("n_edges", 0))

            if embed:
                result.n_embeddings += _embed_symbols(
                    db_path=db_path,
                    slug=slug,
                    root=root,
                    files=files,
                    embedder=embedder,
                )

        after = _hash_root(root)
        if after != before:
            result.source_verified_untouched = False
            raise IndexSourceError(
                f"non-destructive invariant violated: source changed under {root}"
            )

    result.discovery = {
        "walked": agg_stats.walked,
        "kept": agg_stats.kept,
        "skipped_secret": agg_stats.skipped_secret,
        "skipped_binary": agg_stats.skipped_binary,
        "skipped_symlink": agg_stats.skipped_symlink,
        "skipped_too_large": agg_stats.skipped_too_large,
        "skipped_not_source": agg_stats.skipped_not_source,
        "skipped_gitignored": agg_stats.skipped_gitignored,
        "hit_file_cap": agg_stats.hit_file_cap,
    }
    return result


def _function_nodes_for_files(
    files: Sequence[Path], root: Path, slug: str
) -> dict[str, list[dict]]:
    """Parse files (no DB) and group function nodes by their source file.

    Reuses the populator's own parse functions so symbol boundaries / ids match
    exactly what was written to the graph — single source of truth for ids.
    """
    from core.scripts import ast_parser as ap

    # The parse functions read REPO_ROOT to compute rel_path / ids; set it for
    # this pass and restore it (the populator already does the same internally).
    saved_root, saved_pid = ap.REPO_ROOT, ap.PROJECT_ID
    ap.REPO_ROOT, ap.PROJECT_ID = root.resolve(), slug
    grouped: dict[str, list[dict]] = {}
    try:
        for f in files:
            suffix = f.suffix.lower()
            if suffix == ".py":
                nodes, _ = ap.parse_python_file(str(f))
            elif suffix in (".ts", ".tsx"):
                nodes, _ = ap.parse_typescript_file(str(f))
            else:
                continue
            for n in nodes:
                if n.get("type") != "function" or n.get("metadata", {}).get("stub"):
                    continue
                fp = n.get("file_path")
                if not fp:
                    continue
                grouped.setdefault(fp, []).append(n)
    finally:
        ap.REPO_ROOT, ap.PROJECT_ID = saved_root, saved_pid
    return grouped


def _embed_symbols(
    *,
    db_path: str,
    slug: str,
    root: Path,
    files: Sequence[Path],
    embedder: Embedder | None,
) -> int:
    """Compute + persist chunk-per-symbol code embeddings (DELETE+INSERT per file).

    Incremental: every source file's prior embeddings are deleted before the
    fresh batch is inserted, so a re-index after an edit never leaves stale
    vectors. Skips re-embedding a symbol whose body hash is unchanged.
    """
    grouped = _function_nodes_for_files(files, root, slug)
    if not grouped:
        return 0

    embed_fn = embedder or _default_embedder()
    model_id = os.environ.get("MARVIS_EMBED_MODEL", "granite-embedding-97m")

    conn = sqlite3.connect(db_path)
    written = 0
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        for f in files:
            rel = _rel_for(f, root)
            nodes = grouped.get(rel) or []
            lines = _read_lines(f) if nodes else []

            # Existing hashes for this file → skip unchanged symbols.
            cur = conn.execute(
                "SELECT node_id, content_hash FROM graph_node_code_embeddings "
                "WHERE source_file = ? AND project_id = ?",
                (rel, slug),
            )
            existing = {row[0]: row[1] for row in cur.fetchall()}

            current_ids: set[str] = set()
            pending: list[tuple[str, str, str]] = []  # (node_id, text, hash)
            for n in nodes:
                text = build_symbol_text(n, lines)
                if not text:
                    continue
                current_ids.add(n["id"])
                h = _content_hash(text)
                if existing.get(n["id"]) == h:
                    continue  # body unchanged → keep the stored vector (hash-skip)
                pending.append((n["id"], text, h))

            # Incremental: delete only the symbols that vanished from this file
            # (renamed/removed) so unchanged ones keep their vector; then upsert
            # the changed/new ones. A pure DELETE-all would drop unchanged rows
            # and re-embedding-skip would leave the file empty on a no-op re-run.
            stale_ids = [nid for nid in existing if nid not in current_ids]

            conn.execute("BEGIN IMMEDIATE")
            try:
                if stale_ids:
                    placeholders = ",".join("?" * len(stale_ids))
                    conn.execute(
                        f"DELETE FROM graph_node_code_embeddings "
                        f"WHERE node_id IN ({placeholders})",
                        stale_ids,
                    )
                if pending:
                    vectors = embed_fn([t for _, t, _ in pending])
                    rows = []
                    for (node_id, _t, h), vec in zip(pending, vectors):
                        rows.append(
                            (
                                node_id,
                                slug,
                                rel,
                                len(vec),
                                _pack_vector(vec),
                                h,
                                model_id,
                            )
                        )
                    conn.executemany(
                        "INSERT INTO graph_node_code_embeddings "
                        "(node_id, project_id, source_file, dim, vector, content_hash, "
                        " model, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now')) "
                        "ON CONFLICT(node_id) DO UPDATE SET "
                        "  project_id=excluded.project_id, "
                        "  source_file=excluded.source_file, "
                        "  dim=excluded.dim, vector=excluded.vector, "
                        "  content_hash=excluded.content_hash, model=excluded.model, "
                        "  updated_at=datetime('now')",
                        rows,
                    )
                    written += len(rows)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    finally:
        conn.close()
    return written


def _rel_for(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name
