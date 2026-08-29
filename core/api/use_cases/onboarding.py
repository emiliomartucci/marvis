from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from core.api.models.onboarding import (
    DemoSeedResponse,
    DemoTeardownResponse,
    ScanWorkdirCandidate,
    ScanWorkdirResponse,
    SetupReadResponse,
)
from core.api.models.tasks import TaskCreateRequest
from core.api.models.todos import TodoCreateRequest
from core.api.services.project_lifecycle import guarded_project_file_write
from core.api.use_cases import projects as projects_uc
from core.api.use_cases import tasks as tasks_uc
from core.api.use_cases import todos as todos_uc
from core.api.use_cases._context import (
    CallerContext,
    require_role_ctx,
    require_workspace_ctx,
)
from core.api.use_cases._errors import ConflictError, ValidationError

AUTHORED_SETUP_SECTIONS = (
    "Identità",
    "Sorgenti",
    "Ritmo",
    "Fonti del brain",
)

_SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".hg", ".svn", ".venv", "venv"}
_MAX_SCAN_DEPTH = 3
_DEMO_PROJECT = "casa-lorenzi"
_DEMO_TAG = "demo"
_DEMO_SOURCE_PREFIX = "demo:casa-lorenzi:"
_SETUP_HEADER_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_CHECKBOX_RE = re.compile(r"^(\s*-\s+\[)([ xX])(\]\s+.+?)$", re.MULTILINE)


@dataclass(frozen=True)
class _DemoTask:
    key: str
    title: str
    description: str
    priority: str


@dataclass(frozen=True)
class _DemoTodo:
    key: str
    text: str
    type: str
    doer: str


_DEMO_CONTENT: dict[str, dict[str, Any]] = {
    "it": {
        "project_name": "Casa Lorenzi",
        "description": (
            "PME arredamento con aree Marketing, Vendite, Operations e Fiscale. "
            "Champion: Marco Rinaldi, Operations Manager."
        ),
        "tasks": [
            _DemoTask(
                "operations-cycle",
                "Mappare il ciclo ordini Operations",
                "Raccogli handoff su consegna, installazione e resi prima del brain notturno.",
                "high",
            ),
            _DemoTask(
                "sales-pipeline",
                "Rivedere pipeline Vendite showroom",
                "Trasforma note sparse in decisioni e prossime azioni per Marco Rinaldi.",
                "medium",
            ),
            _DemoTask(
                "marketing-brief",
                "Preparare brief campagna living autunno",
                "Allinea Marketing e Operations su promo, stock e vincoli fiscali.",
                "medium",
            ),
        ],
        "todos": [
            _DemoTodo(
                "ops-review",
                "Chiedere a Marco quali ritardi di consegna vanno nel diario Operations",
                "azione",
                "human",
            ),
            _DemoTodo(
                "fiscal-note",
                "Annotare vincoli Fiscale prima di approvare la campagna living",
                "promemoria",
                "human",
            ),
        ],
    },
    "en": {
        "project_name": "Casa Lorenzi",
        "description": (
            "Furniture SME with Marketing, Sales, Operations, and Finance areas. "
            "Champion: Marco Rinaldi, Operations Manager."
        ),
        "tasks": [
            _DemoTask(
                "operations-cycle",
                "Map the Operations order cycle",
                "Collect handoffs on delivery, installation, and returns before the nightly brain run.",
                "high",
            ),
            _DemoTask(
                "sales-pipeline",
                "Review the showroom Sales pipeline",
                "Turn scattered notes into decisions and next actions for Marco Rinaldi.",
                "medium",
            ),
            _DemoTask(
                "marketing-brief",
                "Prepare the autumn living campaign brief",
                "Align Marketing and Operations on promotions, stock, and finance constraints.",
                "medium",
            ),
        ],
        "todos": [
            _DemoTodo(
                "ops-review",
                "Ask Marco which delivery delays belong in the Operations journal",
                "azione",
                "human",
            ),
            _DemoTodo(
                "fiscal-note",
                "Capture Finance constraints before approving the living campaign",
                "promemoria",
                "human",
            ),
        ],
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_setup_content() -> str:
    return "\n\n".join(
        [
            "# Marvis setup",
            "## Identità\n- [ ] Operatore:\n- [ ] Azienda:",
            "## Sorgenti\n- [ ] Cartelle da indicizzare:\n- [ ] Esclusioni:",
            "## Ritmo\n- [ ] Ora ciclo brain notturno:\n- [ ] Giorni attivi:",
            "## Fonti del brain\n- [ ] Documenti locali\n- [ ] Repo locali",
        ]
    ) + "\n"


def _setup_path() -> Path:
    settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
    if settings_path:
        return Path(settings_path).expanduser().parent / "setup.md"

    from core.cli.marvis_init import _default_vault_dir

    return _default_vault_dir() / "setup.md"


def _reject_traversal(raw: str) -> None:
    if "\x00" in raw or ".." in Path(raw).parts:
        raise ValidationError(
            code="invalid_path",
            message="Path traversal is not allowed.",
        )


def _resolve_existing_dir(raw: str) -> Path:
    _reject_traversal(raw)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValidationError(
            code="invalid_root",
            message="root must be an absolute path.",
        )
    resolved = path.resolve()
    if not resolved.exists():
        raise ValidationError(code="root_not_found", message=f"Root does not exist: {raw}")
    if not resolved.is_dir():
        raise ValidationError(code="root_not_directory", message=f"Root is not a directory: {raw}")
    return resolved


def _normalise_exclusions(root: Path, exclusions: list[str]) -> list[Path]:
    resolved: list[Path] = []
    for raw in exclusions:
        if not raw.strip():
            continue
        _reject_traversal(raw)
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        path = candidate.resolve()
        if path == root or path.is_relative_to(root):
            resolved.append(path)
    return sorted(set(resolved), key=lambda p: str(p))


def _is_excluded(path: Path, exclusions: list[Path]) -> bool:
    return any(path == excluded or path.is_relative_to(excluded) for excluded in exclusions)


def _has_git(path: Path) -> bool:
    return (path / ".git").exists()


def scan_workdir(*, root: str, exclusions: list[str]) -> ScanWorkdirResponse:
    """Walk a workdir outside any DB lock and return deterministic project proposals."""
    base = _resolve_existing_dir(root)
    excluded = _normalise_exclusions(base, exclusions)
    proposals: list[ScanWorkdirCandidate] = []

    def visit(path: Path, depth: int) -> None:
        if depth > _MAX_SCAN_DEPTH or _is_excluded(path, excluded):
            return
        if path.name.startswith(".") or path.name in _SKIP_DIRS or path.is_symlink():
            return

        if path != base or _has_git(path):
            proposals.append(
                ScanWorkdirCandidate(
                    path=str(path),
                    name=path.name,
                    kind="code" if _has_git(path) else "no-code",
                )
            )
        if depth == _MAX_SCAN_DEPTH:
            return
        try:
            children = sorted(
                (
                    child
                    for child in path.iterdir()
                    if child.is_dir() and not child.is_symlink()
                ),
                key=lambda p: p.name.casefold(),
            )
        except OSError:
            return
        for child in children:
            visit(child.resolve(), depth + 1)

    visit(base, 0)
    unique = {item.path: item for item in proposals}
    return ScanWorkdirResponse(
        root=str(base),
        exclusions=[str(path) for path in excluded],
        proposals=[unique[path] for path in sorted(unique)],
    )


def _split_sections(content: str) -> dict[str, str]:
    matches = list(_SETUP_HEADER_RE.finditer(content))
    sections: dict[str, str] = {}
    for index, match in enumerate(matches):
        title = match.group(1).strip()
        body_start = match.end()
        body_end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        sections[title] = content[body_start:body_end].strip()
    return sections


def _extract_checkboxes(content: str) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for match in _CHECKBOX_RE.finditer(content):
        label = match.group(3)[1:].strip()
        result[label] = match.group(2).lower() == "x"
    return result


def read_setup() -> SetupReadResponse:
    path = _setup_path()
    content = path.read_text(encoding="utf-8") if path.exists() else _default_setup_content()
    return SetupReadResponse(
        path=str(path),
        content=content,
        sections={name: _split_sections(content).get(name, "") for name in AUTHORED_SETUP_SECTIONS},
        checkboxes=_extract_checkboxes(content),
    )


def _ensure_setup_file() -> tuple[Path, str]:
    path = _setup_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        content = _default_setup_content()
        path.write_text(content, encoding="utf-8")
        return path, content
    return path, path.read_text(encoding="utf-8")


def _replace_section(content: str, section: str, body: str) -> str:
    matches = list(_SETUP_HEADER_RE.finditer(content))
    for index, match in enumerate(matches):
        if match.group(1).strip() != section:
            continue
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        replacement = f"## {section}\n{body.strip()}\n\n"
        return content[:start] + replacement + content[end:].lstrip("\n")
    return content.rstrip() + f"\n\n## {section}\n{body.strip()}\n"


def _apply_checkbox_updates(content: str, updates: dict[str, bool]) -> str:
    remaining = dict(updates)

    def repl(match: re.Match[str]) -> str:
        label = match.group(3)[1:].strip()
        if label not in remaining:
            return match.group(0)
        checked = "x" if remaining.pop(label) else " "
        return f"{match.group(1)}{checked}{match.group(3)}"

    return _CHECKBOX_RE.sub(repl, content)


def _section_body(content: str, section: str) -> str:
    return _split_sections(content).get(section, "")


def write_setup(
    *,
    section: str,
    content: str | None = None,
    checkboxes: dict[str, bool] | None = None,
) -> SetupReadResponse:
    if section not in AUTHORED_SETUP_SECTIONS:
        raise ValidationError(
            code="unknown_setup_section",
            message=(
                f"Unknown setup.md section: {section}. "
                f"Allowed sections: {', '.join(AUTHORED_SETUP_SECTIONS)}"
            ),
        )
    path, existing = _ensure_setup_file()
    updated = existing
    body = content if content is not None else _section_body(updated, section)
    if checkboxes:
        body = _apply_checkbox_updates(body, checkboxes)
    if content is not None or checkboxes:
        updated = _replace_section(updated, section, body)
    if updated != existing:
        path.write_text(updated, encoding="utf-8")
    return read_setup()


async def _fetch_id_by_source_ref(
    db: aiosqlite.Connection,
    *,
    table: str,
    source_ref: str,
    workspace_id: str,
) -> str | None:
    row = await (
        await db.execute(
            f"SELECT id FROM {table} WHERE source_ref = ? AND workspace_id = ? "
            "ORDER BY created_at ASC LIMIT 1",
            (source_ref, workspace_id),
        )
    ).fetchone()
    return row["id"] if row else None


async def _noop_sync_graph(*_args: Any, **_kwargs: Any) -> bool:
    return True


def _noop_schedule_embed(**_kwargs: Any) -> None:
    return None


async def _create_demo_task(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    item: _DemoTask,
) -> str:
    workspace_id = require_workspace_ctx(ctx)
    source_ref = f"{_DEMO_SOURCE_PREFIX}task:{item.key}"
    body = TaskCreateRequest(
        title=item.title,
        description=f"{item.description}\n\nDEMO marker: Casa Lorenzi onboarding seed.",
        project=_DEMO_PROJECT,
        priority=item.priority,
        source="console",
        source_ref=source_ref,
        tags=[_DEMO_TAG, "casa-lorenzi"],
        impact=6,
        confidence=8,
        ease=7,
        delegation="agent",
        completion_mode="none",
    )
    try:
        task = await tasks_uc.create_task(
            ctx,
            db,
            body=body,
            created_by=ctx.username,
            sync_graph=_noop_sync_graph,
            schedule_embed=_noop_schedule_embed,
        )
        return task.id
    except ConflictError:
        await db.rollback()
        existing = await _fetch_id_by_source_ref(
            db,
            table="tasks",
            source_ref=source_ref,
            workspace_id=workspace_id,
        )
        if existing:
            return existing
        raise


async def _create_demo_todo(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    item: _DemoTodo,
) -> str:
    workspace_id = require_workspace_ctx(ctx)
    source_ref = f"{_DEMO_SOURCE_PREFIX}todo:{item.key}"
    body = TodoCreateRequest(
        text=item.text,
        type=item.type,
        project=_DEMO_PROJECT,
        source="agent",
        source_ref=source_ref,
        doer=item.doer,
        payload={"demo": True, "company": "Casa Lorenzi"},
    )
    try:
        todo = await todos_uc.create_todo(
            ctx,
            db,
            body=body,
            created_by=ctx.username,
            schedule_classify=False,
        )
        return todo.id
    except ConflictError:
        await db.rollback()
        existing = await _fetch_id_by_source_ref(
            db,
            table="todos",
            source_ref=source_ref,
            workspace_id=workspace_id,
        )
        if existing:
            return existing
        raise


async def _ensure_demo_project(ctx: CallerContext, db: aiosqlite.Connection, *, lang: str) -> bool:
    from core.api.routers import projects as projects_mod

    if projects_mod._find_project_entry(_DEMO_PROJECT) is not None:
        await _write_demo_marker(ctx, db)
        return False
    content = _DEMO_CONTENT[lang]
    try:
        created = await projects_uc.create_project(
            ctx,
            db,
            slug=_DEMO_PROJECT,
            name=content["project_name"],
            program="Demo",
            scope="demo",
            description=content["description"],
            lifecycle="active",
            language=lang,
            type="work",
        )
    except ConflictError:
        await db.rollback()
        projects_mod._build_project_index()
        await _write_demo_marker(ctx, db)
        return False
    projects_mod._build_project_index()
    await _write_demo_marker(ctx, db, Path(created.metadata_path))
    return True


async def _write_demo_marker(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    metadata_path: Path | None = None,
) -> None:
    from core.api.routers.projects import _find_project_entry

    entry = _find_project_entry(_DEMO_PROJECT) if metadata_path is None else None
    path = metadata_path or (entry.metadata_path if entry else None)
    if path is None:
        return
    async with guarded_project_file_write(
        ctx,
        db,
        project_slug=_DEMO_PROJECT,
        writer_kind="demo_marker",
        resource_ref=".marvis-demo.json",
        projects_root=path.parent,
    ):
        marker = path / ".marvis-demo.json"
        marker.write_text(
            json.dumps({"demo": True, "project": _DEMO_PROJECT}, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )


async def seed_demo(
    ctx: CallerContext,
    db: aiosqlite.Connection,
    *,
    lang: str = "it",
) -> DemoSeedResponse:
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    require_workspace_ctx(ctx)
    if lang not in _DEMO_CONTENT:
        raise ValidationError(code="invalid_lang", message="lang must be 'it' or 'en'.")
    content = _DEMO_CONTENT[lang]
    created_project = await _ensure_demo_project(ctx, db, lang=lang)
    task_ids = [await _create_demo_task(ctx, db, item=item) for item in content["tasks"]]
    todo_ids = [await _create_demo_todo(ctx, db, item=item) for item in content["todos"]]
    try:
        from core.api.services.first_value import hosted_tenant_id
        from core.api.services.product_events import record_product_event
    except ImportError:
        hosted_tenant_id = record_product_event = None

    tenant_id = hosted_tenant_id() if hosted_tenant_id is not None else None
    if tenant_id and record_product_event is not None:
        await record_product_event(
            db,
            subject_type="tenant",
            subject_id=tenant_id,
            tenant_id=tenant_id,
            event_name="demo_served",
            event_key="onboarding-demo-v1",
            source="tenant_onboarding",
            payload={"project_slug": _DEMO_PROJECT, "demo": True},
        )
        await db.commit()
    return DemoSeedResponse(
        project=_DEMO_PROJECT,
        created=created_project,
        tasks=task_ids,
        todos=todo_ids,
        lang=lang,
    )


async def teardown_demo(ctx: CallerContext, db: aiosqlite.Connection) -> DemoTeardownResponse:
    require_role_ctx(ctx, "operator", "admin", "super_admin")
    workspace_id = require_workspace_ctx(ctx)
    now = _now()
    task_cursor = await db.execute(
        "UPDATE tasks SET deleted_at = ?, updated_at = ? "
        "WHERE workspace_id = ? AND deleted_at IS NULL AND project = ? "
        "AND (source_ref LIKE ? OR tags LIKE ?)",
        (
            now,
            now,
            workspace_id,
            _DEMO_PROJECT,
            f"{_DEMO_SOURCE_PREFIX}%",
            '%"demo"%',
        ),
    )
    todo_cursor = await db.execute(
        "DELETE FROM todos WHERE workspace_id = ? AND project = ? "
        "AND (source_ref LIKE ? OR payload LIKE ?)",
        (
            workspace_id,
            _DEMO_PROJECT,
            f"{_DEMO_SOURCE_PREFIX}%",
            '%"demo": true%',
        ),
    )
    remaining_demo = await (
        await db.execute(
            "SELECT "
            "EXISTS(SELECT 1 FROM tasks WHERE deleted_at IS NULL AND project = ? "
            "AND (source_ref LIKE ? OR tags LIKE ?)) "
            "OR EXISTS(SELECT 1 FROM todos WHERE project = ? "
            "AND (source_ref LIKE ? OR payload LIKE ?))",
            (
                _DEMO_PROJECT,
                f"{_DEMO_SOURCE_PREFIX}%",
                '%"demo"%',
                _DEMO_PROJECT,
                f"{_DEMO_SOURCE_PREFIX}%",
                '%"demo": true%',
            ),
        )
    ).fetchone()
    await db.commit()
    project_deleted = False
    if not (remaining_demo and bool(remaining_demo[0])):
        project_deleted = await _delete_demo_project_dir(ctx, db)
    return DemoTeardownResponse(
        project=_DEMO_PROJECT,
        tasks_deleted=task_cursor.rowcount if task_cursor.rowcount != -1 else 0,
        todos_deleted=todo_cursor.rowcount if todo_cursor.rowcount != -1 else 0,
        project_deleted=project_deleted,
    )


async def _delete_demo_project_dir(
    ctx: CallerContext,
    db: aiosqlite.Connection,
) -> bool:
    from core.api.routers import projects as projects_mod

    entry = projects_mod._find_project_entry(_DEMO_PROJECT)
    if not entry:
        return False
    async with guarded_project_file_write(
        ctx,
        db,
        project_slug=_DEMO_PROJECT,
        writer_kind="demo_teardown",
        resource_ref="project-directory",
        projects_root=entry.metadata_path.parent,
    ):
        # Re-resolve under the lifecycle lock: another process may have changed
        # the index between the optimistic check and this destructive step.
        entry = projects_mod._find_project_entry(_DEMO_PROJECT)
        if not entry:
            return False
        marker = entry.metadata_path / ".marvis-demo.json"
        if not marker.exists():
            return False
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if payload.get("demo") is not True or payload.get("project") != _DEMO_PROJECT:
            return False
        shutil.rmtree(entry.metadata_path)
        projects_mod._build_project_index()
        return True
