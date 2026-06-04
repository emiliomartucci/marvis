"""Runtime path helpers for top-level and core/ package layouts."""
from __future__ import annotations

from pathlib import Path


def api_package_root(current_file: str | Path) -> Path:
    """Return the api package directory for a module file."""
    path = Path(current_file).resolve()
    for parent in path.parents:
        if parent.name == "api":
            return parent
    raise RuntimeError(f"could not locate api package root from {current_file}")


def repo_root(current_file: str | Path) -> Path:
    """Return the runtime repository root for api/ or core/api/ modules."""
    api_root = api_package_root(current_file)
    if api_root.parent.name == "core":
        return api_root.parent.parent
    return api_root.parent


def repo_path(current_file: str | Path, *parts: str) -> Path:
    """Return a path under the runtime repository root."""
    return repo_root(current_file).joinpath(*parts)
