from __future__ import annotations

import os
import time as _t
from pathlib import Path

# Workspace root: override via MARVIS_WORKSPACE_ROOT, else default to ~/workspace
# (prod runs as the service user whose HOME holds the workspace, so the default
# preserves the historical hardcoded path behavior).
WORKSPACE_ROOT = os.environ.get("MARVIS_WORKSPACE_ROOT", str(Path.home() / "workspace"))


def resolve_project_path(project_slug: str | None) -> str:
    """Resolve a project slug to its repo or metadata path.

    Falls back to the MarvisX workspace when the slug is missing or unknown.
    Always returns an absolute, expanded path suitable for Claude JSONL lookup.
    """
    if not project_slug:
        return WORKSPACE_ROOT

    from core.api.routers.projects import _INDEX_TTL, _build_project_index, _index_built_at, _project_index

    if _t.monotonic() - _index_built_at > _INDEX_TTL:
        _build_project_index()

    entry = _project_index.get(project_slug)
    if entry and entry.repo_path:
        return os.path.abspath(str(entry.repo_path))
    if entry:
        return os.path.abspath(str(entry.metadata_path.resolve()))
    return WORKSPACE_ROOT


def resolve_project_access_paths(project_slug: str | None) -> tuple[str, ...]:
    """Return repo and metadata paths for a project slug.

    Session launch now happens from the shared workspace for every provider, but
    selected projects still need to be reachable via provider-specific extra
    directory flags where supported.
    """
    if not project_slug:
        return ()

    from core.api.routers.projects import _INDEX_TTL, _build_project_index, _index_built_at, _project_index

    if _t.monotonic() - _index_built_at > _INDEX_TTL:
        _build_project_index()

    entry = _project_index.get(project_slug)
    if not entry:
        return ()

    paths: list[str] = []
    if entry.repo_path:
        paths.append(os.path.abspath(str(entry.repo_path)))

    metadata_path = os.path.abspath(str(entry.metadata_path.resolve()))
    if metadata_path not in paths:
        paths.append(metadata_path)

    return tuple(paths)


def candidate_project_paths(project_slug: str | None) -> tuple[str, ...]:
    """Return the preferred lookup path plus the legacy workspace fallback."""
    primary = resolve_project_path(project_slug)
    if primary == WORKSPACE_ROOT:
        return (primary,)
    return (primary, WORKSPACE_ROOT)
