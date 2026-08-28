# v1.0.0 - 2026-05-27 - S2 F1: thin marvis CLI runtime (subcommands over use_cases)
"""``marvis`` runtime subcommands — terminal adapters over the S1 use_cases.

Registered onto the SAME Typer ``app`` as ``marvis init`` (one entrypoint). Each
command builds the local single-user context, opens a DB context, calls a pure
async use_case, and serializes the result (Rich table for humans / ``--json`` for
pipes). No HTTP, no token.

Command groups (see ``rich_help_panel``):
- **Runtime**: ``status``, ``brief``
- **Projects**: ``project list``, ``project create``, ``project import``
- **Triage**: ``triage``, ``approve``
- **Audit**: ``audit``

Heavy imports (use_cases, embedding, KG lens) are LAZY — done inside command
bodies — so ``marvis --help`` / ``marvis status`` stay fast and never trigger a
model load (learning a09b8754).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import typer
import yaml
from rich.table import Table

from core.cli._runtime_ctx import (
    LOCAL_ADMIN_CTX,
    LOCAL_CTX,
    console,
    emit,
    err_console,
    handle_service_error,
    run_async,
    with_db,
    with_write_db,
)

_PANEL_RUNTIME = "Runtime"
_PANEL_PROJECTS = "Projects"
_PANEL_TRIAGE = "Triage"
_PANEL_AUDIT = "Audit"

def _read_template_text() -> str | None:
    """Read the bundled ``projects/_template/project.yaml`` text, wheel-safe.

    The template ships as ``projects._template`` package-data; read it via
    ``importlib.resources`` so it resolves from an installed wheel (where the old
    ``Path(__file__)`` walk-up silently returned a non-existent path and the CLI
    fell back to a minimal seed). Fall back to the repo-relative path for an
    editable/source checkout (learning 9e527cfa).
    """
    try:
        import importlib.resources as _res

        ref = _res.files("projects._template").joinpath("project.yaml")
        if ref.is_file():
            return ref.read_text(encoding="utf-8")
    except (ModuleNotFoundError, FileNotFoundError, TypeError, AttributeError):
        pass
    fallback = (
        Path(__file__).resolve().parent.parent.parent
        / "projects"
        / "_template"
        / "project.yaml"
    )
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8")
    return None


def register(app: typer.Typer) -> None:
    """Attach all runtime commands + the ``project`` group onto an existing app."""
    app.command("status", rich_help_panel=_PANEL_RUNTIME)(status_cmd)
    app.command("brief", rich_help_panel=_PANEL_RUNTIME)(brief_cmd)
    app.command("triage", rich_help_panel=_PANEL_TRIAGE)(triage_cmd)
    app.command("approve", rich_help_panel=_PANEL_TRIAGE)(approve_cmd)
    app.command("audit", rich_help_panel=_PANEL_AUDIT)(audit_cmd)
    app.add_typer(
        project_app,
        name="project",
        rich_help_panel=_PANEL_PROJECTS,
        help="Manage registered projects (list / create / import).",
    )


# ---------------------------------------------------------------------------
# marvis status
# ---------------------------------------------------------------------------


def _vault_state() -> bool:
    """True if a BYOK provider key vault exists (no decryption — just presence)."""
    vault_dir = os.environ.get("MARVIS_VAULT_DIR")
    base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
    return (base / "byok.vault").is_file()


def _settings_snapshot() -> dict[str, Any]:
    """Read ~/.marvis/settings.yaml for provider + telemetry flags (best-effort)."""
    settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
    if settings_path:
        path = Path(settings_path).expanduser()
    else:
        vault_dir = os.environ.get("MARVIS_VAULT_DIR")
        base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
        path = base / "settings.yaml"
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


@handle_service_error
def status_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """One-shot "sto bene?": runtime up, project + task counts, BYOK + telemetry."""

    async def _gather() -> dict[str, Any]:
        from core.api.use_cases import projects as projects_uc
        from core.api.use_cases import tasks as tasks_uc

        async with with_db() as db:
            summary = await tasks_uc.get_tasks_summary(LOCAL_CTX, db)
            programs = await projects_uc.list_programs(LOCAL_CTX, db)
        n_projects = sum(len(p.projects) for p in programs)
        return {
            "summary": summary.model_dump(mode="json"),
            "n_projects": n_projects,
        }

    db_ok = True
    payload: dict[str, Any]
    try:
        gathered = run_async(_gather())
        payload = {
            "db_ok": True,
            "n_projects": gathered["n_projects"],
            "tasks": gathered["summary"],
        }
    except Exception as exc:  # noqa: BLE001 — status must always report, never crash
        db_ok = False
        payload = {"db_ok": False, "n_projects": 0, "tasks": {}, "error": str(exc)}

    settings_data = _settings_snapshot()
    llm = settings_data.get("llm") or {}
    byok = bool(_vault_state() or llm.get("provider"))
    # Real EFFECTIVE state: reuse the same precedence `marvis telemetry status`
    # uses (DO_NOT_TRACK / MARVIS_TELEMETRY env, then settings.yaml, default OFF).
    try:
        from core.telemetry.client import _enabled as _telemetry_enabled

        telemetry = bool(_telemetry_enabled())
    except Exception:  # noqa: BLE001 — status must never crash
        telemetry = bool(settings_data.get("telemetry", {}).get("enabled", False)) if isinstance(
            settings_data.get("telemetry"), dict
        ) else bool(settings_data.get("telemetry", False))

    result = {
        "db_ok": db_ok,
        "n_projects": payload["n_projects"],
        "task_counts": payload["tasks"].get("by_status", {}) if payload["tasks"] else {},
        "byok": byok,
        "telemetry": telemetry,
        "provider": llm.get("provider"),
    }

    def _render(s: dict[str, Any]) -> None:
        t = Table(title="marvis status", show_header=False)
        t.add_row("Runtime", "[green]up[/]" if s["db_ok"] else "[red]down[/]")
        t.add_row("Progetti", str(s["n_projects"]))
        by_status = s["task_counts"] or {}
        total_tasks = sum(v for v in by_status.values() if isinstance(v, int))
        t.add_row("Task (attivi)", str(total_tasks))
        if by_status:
            detail = ", ".join(
                f"{k}={v}" for k, v in by_status.items() if isinstance(v, int) and v
            )
            if detail:
                t.add_row("  per stato", detail)
        provider = s.get("provider") or ""
        t.add_row(
            "Provider BYOK",
            f"[green]{provider}[/]" if s["byok"] else "[yellow]assente[/]",
        )
        t.add_row("Telemetria", "on" if s["telemetry"] else "off")
        console.print(t)

    emit(result, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis project (group: list / create / import)
# ---------------------------------------------------------------------------

project_app = typer.Typer(add_completion=False, no_args_is_help=True)


@project_app.command("list")
@handle_service_error
def project_list_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """List registered projects (slug, type, lifecycle, program)."""

    async def _run() -> list[dict[str, Any]]:
        from core.api.use_cases import projects as projects_uc

        async with with_db() as db:
            programs = await projects_uc.list_programs(LOCAL_CTX, db)
        flat: list[dict[str, Any]] = []
        for prog in programs:
            for proj in prog.projects:
                flat.append(
                    {
                        "slug": proj.slug,
                        "type": proj.type,
                        "lifecycle": proj.lifecycle,
                        "program": proj.program,
                        "language": proj.language,
                    }
                )
        return flat

    projects = run_async(_run())

    def _render(rows: list[dict[str, Any]]) -> None:
        t = Table(title="projects")
        t.add_column("slug")
        t.add_column("type")
        t.add_column("lifecycle")
        t.add_column("program")
        for r in rows:
            t.add_row(
                r["slug"],
                r["type"] or "-",
                r["lifecycle"] or "-",
                r["program"] or "-",
            )
        console.print(t)

    emit(projects, json_out=json_out, render=_render)


def _projects_root() -> Path:
    """Resolve projects_root from settings.yaml (falls back to CLI default)."""
    settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
    if settings_path:
        path = Path(settings_path).expanduser()
    else:
        vault_dir = os.environ.get("MARVIS_VAULT_DIR")
        base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
        path = base / "settings.yaml"
    root: str | None = None
    if path.is_file():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            root = (data.get("storage") or {}).get("projects_root")
        except Exception:  # noqa: BLE001
            root = None
    if not root:
        from core.wizard.defaults import default_projects_root

        root = default_projects_root()
    return Path(root).expanduser()


def _template_yaml() -> dict[str, Any]:
    """Load the projects/_template schema as a dict (fallback to a minimal seed)."""
    text = _read_template_text()
    if text is not None:
        try:
            data = yaml.safe_load(text) or {}
            if isinstance(data, dict):
                return data
        except Exception:  # noqa: BLE001
            pass
    return {
        "project": None,
        "program": None,
        "scope": "work",
        "description": "",
        "type": "work",
        "repo_path": None,
        "lifecycle": "idea",
        "phase": "",
        "language": "none",
        "stack": [],
        "last_session": 0,
        "last_work": None,
    }


def _write_project_yaml(
    *,
    slug: str,
    project_type: str,
    name: str | None,
    language: str | None,
    repo_path: str | None,
    json_out: bool,
) -> dict[str, Any]:
    """Idempotent: write ``<projects_root>/<slug>/project.yaml`` from the template.

    Existing slug → no-op + a warning to stderr + exit 0 (NOT an error), so an
    agent re-running the command never treats it as a failure.
    """
    project_dir = _projects_root() / slug
    project_yaml = project_dir / "project.yaml"
    task_file = project_dir / ".task"

    def existing_result() -> dict[str, Any]:
        if not json_out:
            err_console.print(f"[yellow]{slug}: already exists, no-op[/]")
        return {
            "slug": slug,
            "type": project_type,
            "status": "exists",
            "metadata_path": str(project_dir.resolve()),
        }

    if project_yaml.exists():
        return existing_result()

    from core.api.use_cases.projects import project_creation_guard

    with project_creation_guard(project_dir.parent):
        if project_yaml.exists():
            return existing_result()

        data = _template_yaml()
        data["project"] = slug
        data["type"] = project_type
        data["language"] = language or data.get("language") or "none"
        if name:
            data["description"] = name
        if repo_path:
            data["repo_path"] = repo_path

        project_dir.mkdir(parents=True, exist_ok=True)
        project_yaml.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        if not task_file.exists():
            task_file.write_text(slug + "\n", encoding="utf-8")

    return {
        "slug": slug,
        "type": project_type,
        "status": "created",
        "metadata_path": str(project_dir.resolve()),
    }


def _normalize_slug(raw: str) -> str:
    """Slugify a project slug (a directory name or an explicit --slug) so import /
    create never store a non-conformant slug that `project list` then silently
    hides (issue #5). Hard-error on an empty result instead of guessing one.

    A change is reported to stderr so it stays out of a ``--json`` stdout pipe.
    """
    from core.wizard import slugify

    norm = slugify(raw)
    if not norm:
        err_console.print(
            f"[red]Cannot derive a valid slug from {raw!r}. Pass --slug explicitly.[/]"
        )
        raise typer.Exit(2)
    if norm != raw:
        err_console.print(f"[yellow]slug normalized: {raw!r} → {norm!r}[/]")
    return norm


@project_app.command("create")
@handle_service_error
def project_create_cmd(
    slug: str = typer.Argument(..., help="Project slug (kebab-case)."),
    project_type: str = typer.Option(
        ..., "--type", help="work | code | system.", case_sensitive=False
    ),
    name: str | None = typer.Option(None, "--name", help="Human-readable name / description."),
    language: str | None = typer.Option(None, "--language", help="Primary language."),
    repo_path: str | None = typer.Option(
        None, "--repo-path", help="Absolute path to git repo (code/system)."
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Create a new project (writes project.yaml from the template). Idempotent."""
    ptype = project_type.lower()
    if ptype not in ("work", "code", "system"):
        err_console.print("[red]--type must be one of: work, code, system[/]")
        raise typer.Exit(2)

    slug = _normalize_slug(slug)
    result = _write_project_yaml(
        slug=slug,
        project_type=ptype,
        name=name,
        language=language,
        repo_path=repo_path,
        json_out=json_out,
    )

    def _render(r: dict[str, Any]) -> None:
        verb = "exists" if r["status"] == "exists" else "created"
        color = "yellow" if r["status"] == "exists" else "green"
        console.print(f"[{color}]{r['slug']} ({r['type']}) — {verb}[/]")
        console.print(f"  → {r['metadata_path']}")

    emit(result, json_out=json_out, render=_render)


_CONTEXT_SEED = """\
# {slug}

> Seeded by `marvis project import --scaffold` on {date}.
> Source code lives in-place at `{source}` (read-only, never modified).

## What this is

_(Describe the project in 1-2 sentences. The transmute install-skill / an agent
fills this in with judgement — the CLI only seeds the structure.)_

## Status

- lifecycle: idea
- last reviewed: {date}
"""


def _scaffold_transmute(
    *,
    slug: str,
    project_type: str,
    src: Path,
    json_out: bool,
) -> dict[str, Any]:
    """Scaffold the Marvis structure for ``src`` into a NEW metadata dir.

    Layer 1 of RI-7 (mechanical, deterministic): writes ``project.yaml`` +
    ``docs/`` + ``memory/`` + a seed ``context.md`` + a ``.marvis-transmute.yaml``
    manifest into ``<projects_root>/<slug>`` (separate from the source code).

    Non-destructive guarantee (D3): the source is hashed BEFORE, every write is
    confined to the new dir by a write-guard, and the source is re-hashed AFTER —
    a single changed hash aborts. Idempotent: a second run diffs against the
    recorded manifest instead of clobbering.
    """
    from core.cli import _transmute as tx

    new_dir = (_projects_root() / slug).resolve()

    # The new dir must never be the source (would make the guard self-contradict
    # and risk writing into the code).
    if new_dir == src.resolve() or src.resolve() in new_dir.parents:
        err_console.print(
            "[red]Refusing to scaffold inside the source tree; "
            "metadata dir must be separate from the code.[/]"
        )
        raise typer.Exit(2)

    # (a) Hash-inventory of the source BEFORE any write (proof-of-untouched).
    before = tx.hash_inventory(src)

    existing_manifest = tx.load_manifest(new_dir)
    if existing_manifest is not None:
        # Idempotent path: do NOT clobber — report the drift vs the recorded run.
        drift = tx.manifest_drift(existing_manifest, before)
        changed = any(drift.values())
        # Source must STILL match what we recorded (or we'd index a moved tree).
        after_noop = tx.hash_inventory(src)
        if after_noop != before:
            raise tx.TransmuteError(
                "source changed mid-read; aborting (non-destructive invariant)."
            )
        return {
            "slug": slug,
            "type": project_type,
            "status": "exists",
            "scaffold": "diff",
            "metadata_path": str(new_dir),
            "source_path": str(src),
            "source_drift": drift,
            "source_changed_since_transmute": changed,
        }

    guard = tx.WriteGuard(allowed_root=new_dir, forbidden_root=src)

    # Scaffold the structure (every write goes through the guard).
    new_dir.mkdir(parents=True, exist_ok=True)
    for sub in tx._SCAFFOLD_DIRS:
        guard.mkdir(new_dir / sub)

    # project.yaml derived from the source via the shared template helper.
    data = _template_yaml()
    data["project"] = slug
    data["type"] = project_type
    if project_type in ("code", "system") and (src / ".git").exists():
        data["repo_path"] = str(src)
    guard.write_text(
        new_dir / "project.yaml",
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
    )

    from datetime import date as _date

    guard.write_text(
        new_dir / "context.md",
        _CONTEXT_SEED.format(slug=slug, source=str(src), date=_date.today().isoformat()),
    )

    task_file = new_dir / ".task"
    if not task_file.exists():
        guard.write_text(task_file, slug + "\n")

    # Manifest (path registry + idempotency baseline) — written LAST.
    manifest = tx.build_manifest(
        slug=slug, source_root=src, inventory=before, project_type=project_type
    )
    guard.write_text(
        new_dir / tx.MANIFEST_NAME,
        yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True),
    )

    # (b) Re-hash the source AFTER scaffolding: a single changed hash = fail.
    after = tx.hash_inventory(src)
    if after != before:
        drift = tx.diff_inventory(before, after)
        raise tx.TransmuteError(
            f"non-destructive invariant violated: source tree changed: {drift}"
        )

    return {
        "slug": slug,
        "type": project_type,
        "status": "created",
        "scaffold": "full",
        "metadata_path": str(new_dir),
        "source_path": str(src),
        "source_files_hashed": len(before),
        "source_verified_untouched": True,
    }


@project_app.command("import")
@handle_service_error
def project_import_cmd(
    path: str = typer.Argument(..., help="Path to an existing dir / git repo."),
    project_type: str | None = typer.Option(
        None, "--type", help="Override deduced type (work | code | system)."
    ),
    slug: str | None = typer.Option(None, "--slug", help="Override slug (default = dir name)."),
    scaffold: bool = typer.Option(
        False,
        "--scaffold",
        help="Transmute: scaffold the full Marvis structure (docs/, memory/, "
        "context.md, manifest) into a NEW metadata dir. Non-destructive: the "
        "source is hash-verified untouched.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Register an EXISTING dir/repo: deduce type=code + repo_path from .git.

    With ``--scaffold`` it transmutes the project into a new metadata dir
    (docs/ + memory/ + context.md + ``.marvis-transmute.yaml``), guaranteeing the
    source tree is never modified (SHA-256 inventory pre/post + write-guard).
    """
    src = Path(path).expanduser().resolve()
    if not src.is_dir():
        err_console.print(f"[red]Not a directory: {src}[/]")
        raise typer.Exit(2)

    has_git = (src / ".git").exists()
    if project_type:
        ptype = project_type.lower()
        if ptype not in ("work", "code", "system"):
            err_console.print("[red]--type must be one of: work, code, system[/]")
            raise typer.Exit(2)
    else:
        ptype = "code" if has_git else "work"

    resolved_slug = _normalize_slug(slug or src.name)
    deduced_repo = str(src) if (ptype in ("code", "system") and has_git) else None

    if scaffold:
        result = _scaffold_transmute(
            slug=resolved_slug, project_type=ptype, src=src, json_out=json_out
        )
    else:
        result = _write_project_yaml(
            slug=resolved_slug,
            project_type=ptype,
            name=None,
            language=None,
            repo_path=deduced_repo,
            json_out=json_out,
        )
    result["source_path"] = str(src)
    result.setdefault("repo_path", deduced_repo)

    def _render(r: dict[str, Any]) -> None:
        verb = "exists" if r["status"] == "exists" else "imported"
        if r.get("scaffold") == "full":
            verb = "transmuted"
        elif r.get("scaffold") == "diff":
            verb = "exists (diff)"
        color = "yellow" if r["status"] == "exists" else "green"
        console.print(f"[{color}]{r['slug']} ({r['type']}) — {verb}[/]")
        console.print(f"  source → {r['source_path']}")
        if r.get("repo_path"):
            console.print(f"  repo   → {r['repo_path']}")
        console.print(f"  meta   → {r['metadata_path']}")
        # Index hint (issue #8): import only registers metadata — the knowledge
        # graph stays empty until the code is indexed, which needs a scaffolded
        # project (.marvis-transmute.yaml).
        if r.get("scaffold") == "full":
            console.print(f"  → index the code: marvis project index {r['slug']}")
        elif r.get("repo_path"):
            console.print(
                "  → to index the code into the knowledge graph, re-run with "
                f"--scaffold, then: marvis project index {r['slug']}"
            )
        if r.get("source_verified_untouched"):
            console.print(
                f"  ✓ source untouched ({r['source_files_hashed']} files hashed)"
            )
        if r.get("scaffold") == "diff":
            drift = r.get("source_drift") or {}
            if r.get("source_changed_since_transmute"):
                console.print(
                    f"  [yellow]source changed since transmute: "
                    f"+{len(drift.get('added', []))} "
                    f"-{len(drift.get('removed', []))} "
                    f"~{len(drift.get('modified', []))}[/]"
                )
            else:
                console.print("  ✓ source identical to last transmute")

    emit(result, json_out=json_out, render=_render)


def _resolve_db_path() -> str:
    """Resolve the configured SQLite path the runtime writes to (post-settings)."""
    from core.cli._runtime_ctx import _apply_settings

    _apply_settings()
    from core.api.config import settings

    return str(Path(settings.db_path).expanduser())


@project_app.command("index")
@handle_service_error
def project_index_cmd(
    slug: str = typer.Argument(..., help="Project slug (must be transmuted/scaffolded)."),
    embed: bool = typer.Option(
        False,
        "--embed",
        help="Also compute per-symbol code embeddings (opt-in). They are stored "
        "for future semantic code search but are NOT queried by `search` yet, so "
        "by default indexing builds only the structural graph (calls/imports/"
        "defines) — which is what the graph_* tools use today. --embed adds the "
        "heavier embedding pass (loads the local model; OOM-bounded by the "
        "token-budget batcher).",
    ),
    no_embed: bool = typer.Option(
        False,
        "--no-embed",
        hidden=True,
        help="Deprecated no-op: embeddings are off by default now; kept for "
        "back-compat. Use --embed to opt in.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Index a transmuted project's source code in-place into the KG (RI-7 layer 2).

    Reads ``<projects_root>/<slug>/.marvis-transmute.yaml``, walks every
    ``source_roots[]`` through the file-discovery security gate (secrets/binary/
    symlink/size/count), populates calls/imports/defines (and, with ``--embed``, a
    chunk-per-symbol code embedding), and proves the source tree is byte-for-byte
    untouched (D3).
    """
    from core.cli._index_source import IndexSourceError, index_project_source

    metadata_dir = (_projects_root() / slug).resolve()
    if not metadata_dir.is_dir():
        err_console.print(f"[red]Unknown project: {slug} ({metadata_dir} not found)[/]")
        raise typer.Exit(2)

    db_path = _resolve_db_path()
    try:
        result = index_project_source(
            metadata_dir,
            slug=slug,
            db_path=db_path,
            embed=embed and not no_embed,
        )
    except IndexSourceError as exc:
        err_console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    payload = result.as_dict()

    def _render(r: dict[str, Any]) -> None:
        console.print(f"[green]{r['slug']} — indexed[/]")
        for root in r["roots"]:
            console.print(f"  root → {root}")
        console.print(
            f"  files={r['files_indexed']} nodes={r['n_nodes']} "
            f"edges={r['n_edges']} embeddings={r['n_embeddings']}"
        )
        d = r.get("discovery") or {}
        console.print(
            f"  gate: kept={d.get('kept', 0)} "
            f"secrets={d.get('skipped_secret', 0)} "
            f"binary={d.get('skipped_binary', 0)} "
            f"symlink={d.get('skipped_symlink', 0)} "
            f"too_large={d.get('skipped_too_large', 0)}"
        )
        if d.get("hit_file_cap"):
            console.print(
                "  [yellow]file-count cap hit; raise MARVIS_INDEX_MAX_FILES.[/]"
            )
        if r.get("source_verified_untouched"):
            console.print("  ✓ source untouched")

    emit(payload, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis triage
# ---------------------------------------------------------------------------


@handle_service_error
def triage_cmd(
    status: str = typer.Option(
        "awaiting_triage",
        "--status",
        help="queued | awaiting_triage | all.",
    ),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """List the ingest triage queue (items waiting for approval). Read-only."""
    status_filter: str | None
    if status == "all":
        status_filter = None
    elif status in ("queued", "awaiting_triage"):
        status_filter = status
    else:
        err_console.print("[red]--status must be one of: queued, awaiting_triage, all[/]")
        raise typer.Exit(2)

    async def _run() -> list[dict[str, Any]]:
        from core.api.use_cases import ingest_triage as ingest_uc

        async with with_db() as db:
            items = await ingest_uc.list_pending(LOCAL_CTX, db, status=status_filter)
        return [i.model_dump(mode="json") for i in items]

    items = run_async(_run())

    def _render(rows: list[dict[str, Any]]) -> None:
        t = Table(title=f"ingest triage ({status})")
        t.add_column("id")
        t.add_column("project")
        t.add_column("status")
        t.add_column("file")
        t.add_column("target")
        for r in rows:
            target = "/".join(
                x for x in (r.get("target_folder"), r.get("target_filename")) if x
            )
            t.add_row(
                str(r.get("id", ""))[:12],
                r.get("project_slug", "-"),
                r.get("status", "-"),
                Path(r.get("file_path", "")).name or "-",
                target or "-",
            )
        console.print(t)

    emit(items, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis approve (WRITE)
# ---------------------------------------------------------------------------


@handle_service_error
def approve_cmd(
    ingest_id: str = typer.Argument(..., help="ingest_pending row id to approve."),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Approve an already-classified triage item (file → project). WRITE.

    Pure thin adapter: it calls ``ingest_triage.approve`` on the item as-is — no
    hand-rolled SQL (no-fork: surfaces are adapters over use_cases, never raw
    queries). In the local single-user runtime there is no human-only gate
    (``is_human_session=True``), so no second permission model is needed.

    DEFERRED: overriding the routing (target_folder/target_filename) before
    approving belongs to a future ``marvis patch`` command backed by a real
    ``ingest_triage.patch`` use_case — the MCP ``patch_ingest_pending`` tool
    needs the same extraction, so it is one shared follow-up, not CLI SQL here.
    """

    async def _run() -> dict[str, Any]:
        from core.api.use_cases import ingest_triage as ingest_uc

        async with with_write_db() as db:
            response, project_slug = await ingest_uc.approve(
                LOCAL_CTX, db, ingest_id=ingest_id
            )
        return {**response.model_dump(mode="json"), "project_slug": project_slug}

    result = run_async(_run())

    def _render(r: dict[str, Any]) -> None:
        console.print(
            f"[green]approved[/] {r['id']} → {r.get('project_slug', '?')} ({r['status']})"
        )

    emit(result, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis audit
# ---------------------------------------------------------------------------


@handle_service_error
def audit_cmd(
    limit: int = typer.Option(50, "--limit", help="Max entries (newest first)."),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Local action trail (newest first). Read-only.

    Uses the local-admin context: the local single user is the sole operator, so it
    legitimately reads its own complete trail (the operator-narrowed audit slice
    would otherwise hide everything but learnings).
    """

    async def _run() -> list[dict[str, Any]]:
        from core.api.use_cases import audit as audit_uc

        async with with_db() as db:
            entries = await audit_uc.list_audit_entries(LOCAL_ADMIN_CTX, db, limit=limit)
        return [e.model_dump(mode="json") for e in entries]

    entries = run_async(_run())

    def _render(rows: list[dict[str, Any]]) -> None:
        t = Table(title="audit log")
        t.add_column("timestamp")
        t.add_column("action")
        t.add_column("user")
        t.add_column("resource")
        for r in rows:
            t.add_row(
                str(r.get("timestamp", ""))[:19],
                r.get("action", "-"),
                r.get("user", "-"),
                f"{r.get('resource_type', '-')}:{str(r.get('resource_id', ''))[:12]}",
            )
        console.print(t)

    emit(entries, json_out=json_out, render=_render)


# ---------------------------------------------------------------------------
# marvis brief
# ---------------------------------------------------------------------------


@handle_service_error
def brief_cmd(
    slug: str = typer.Argument(..., help="Project slug."),
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Cold-start bundle: project + open tasks + latest handoff + learnings + KG."""

    async def _run() -> dict[str, Any]:
        from core.api.use_cases import projects as projects_uc

        async with with_db() as db:
            return await projects_uc.get_session_brief(LOCAL_CTX, db, slug=slug)

    bundle = run_async(_run())

    def _render(b: dict[str, Any]) -> None:
        project = b.get("project") or {}
        t = Table(title=f"brief — {slug}", show_header=False)
        t.add_row("project", project.get("slug") or slug)
        t.add_row("type", str(project.get("type") or "-"))
        t.add_row("lifecycle", str(project.get("lifecycle") or "-"))
        t.add_row("open tasks", str(len(b.get("open_tasks", []))))
        latest = b.get("latest_handoff")
        t.add_row("latest handoff", (latest or {}).get("title", "-") if latest else "-")
        t.add_row("recent learnings", str(len(b.get("recent_learnings", []))))
        console.print(t)
        for task in b.get("open_tasks", [])[:10]:
            console.print(f"  · [{task.get('status')}] {task.get('title')}")

    emit(bundle, json_out=json_out, render=_render)
