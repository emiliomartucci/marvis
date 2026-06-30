"""Project-root resolution for standalone KG scripts.

KG populators run as subprocesses, so they cannot rely on in-process
``PROJECT_DIRS`` mutations made by the API startup. Keep the precedence here
explicit and script-safe.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.platform import projects_root_default

LEGACY_MANAGED_PROJECTS_ROOT = Path("/data/projects")


def _settings_yaml_path() -> Path:
    settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
    if settings_path:
        return Path(settings_path).expanduser()
    vault_dir = os.environ.get("MARVIS_VAULT_DIR")
    base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
    return base / "settings.yaml"


def _settings_projects_root() -> Path | None:
    path = _settings_yaml_path()
    if not path.is_file():
        return None
    try:
        import yaml

        data: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    storage = data.get("storage") or {}
    if not isinstance(storage, dict):
        return None
    root = storage.get("projects_root")
    if not root:
        return None
    return Path(str(root)).expanduser()


def resolve_projects_root(current_default: Path | None = None) -> Path:
    """Resolve the metadata projects root for standalone scripts.

    Precedence:
      1. Explicit module override (tests monkeypatch ``PROJECTS_ROOT``).
      2. ``MARVIS_PROJECTS_ROOT``.
      3. ``settings.yaml`` ``storage.projects_root``.
      4. Managed-deploy compatibility root ``/data/projects`` when present.
      5. Cross-platform CLI default from ``core.platform``.
    """
    if current_default is not None:
        current = Path(current_default).expanduser()
        if current != LEGACY_MANAGED_PROJECTS_ROOT:
            return current.resolve()

    env_root = os.environ.get("MARVIS_PROJECTS_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    settings_root = _settings_projects_root()
    if settings_root is not None:
        return settings_root.resolve()

    if LEGACY_MANAGED_PROJECTS_ROOT.exists():
        return LEGACY_MANAGED_PROJECTS_ROOT.resolve()

    return projects_root_default().expanduser().resolve()
