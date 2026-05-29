# v1.0.0 - 2026-05-28 - S2: shared settings-apply so CLI + MCP server agree on db_path/projects_root
"""Apply the user's ``~/.marvis/settings.yaml`` onto the API ``settings`` singleton.

The OSS runtime has two entrypoints that must reach the SAME SQLite file and the
SAME project directory the user configured with ``marvis init``:

* the thin ``marvis`` CLI (``core.cli._runtime_ctx`` — status/brief/triage/index),
* the stdio MCP server (``core.api.mcp.server`` — search/graph/tasks/...).

Before this helper existed only the CLI mirrored ``settings.yaml`` onto
``core.api.config.settings`` (in ``_runtime_ctx._apply_settings``). The MCP server
ran with the bare defaults (``db_path='console.db'`` relative, default project
dirs), so ``search`` / ``graph_overview`` opened the WRONG (empty) database and a
transmuted project's source path tripped the ``ALLOWED_REPO_PARENTS`` warning.

This module is the single, dependency-light (no ``typer`` / ``fastapi`` / ``rich``)
implementation both entrypoints call. Best-effort: when no settings file exists the
API defaults / ``$PIR_DB_PATH`` env stand, so tests and ad-hoc runs keep working.
"""
from __future__ import annotations

import os
from pathlib import Path

_applied = False


def _settings_yaml_path() -> Path:
    """Resolve the settings.yaml location the same way ``marvis init`` writes it."""
    settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
    if settings_path:
        return Path(settings_path).expanduser()
    vault_dir = os.environ.get("MARVIS_VAULT_DIR")
    base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
    return base / "settings.yaml"


def apply_marvis_settings(*, force: bool = False) -> bool:
    """Point the runtime at the user's configured DB + projects_root (once).

    Mirrors ``storage.db_path`` / ``storage.projects_root`` from
    ``~/.marvis/settings.yaml`` (or ``$MARVIS_SETTINGS_PATH`` / ``$MARVIS_VAULT_DIR``)
    onto ``core.api.config.settings`` and the project-index roots.

    Idempotent: runs at most once per process unless ``force=True``. Returns
    ``True`` when a settings file was found and applied, ``False`` otherwise.
    """
    global _applied
    if _applied and not force:
        return False
    _applied = True

    path = _settings_yaml_path()
    if not path.is_file():
        return False

    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — malformed settings must not crash the runtime
        return False
    if not isinstance(data, dict):
        return False

    storage = data.get("storage") or {}
    db_path = storage.get("db_path")
    projects_root = storage.get("projects_root")

    applied = False

    if db_path:
        from core.api.config import settings

        settings.db_path = str(Path(db_path).expanduser())
        applied = True

    if projects_root:
        from core.api.routers.projects import _set_project_dirs

        _set_project_dirs([Path(projects_root).expanduser()])
        applied = True

    return applied
