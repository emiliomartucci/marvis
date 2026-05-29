"""Sensible defaults per step. Env-var override allowed for power users + CI."""

from __future__ import annotations

import os
from pathlib import Path

from .state import (
    DbBackend,
    FirstProjectPayload,
    LlmProvider,
    LlmProviderPayload,
    ProjectType,
    StoragePayload,
)


def default_projects_root() -> str:
    candidates = [
        os.environ.get("MARVIS_PROJECTS_ROOT"),
        "/data/projects",
        str(Path.home() / "marvisx" / "projects"),
    ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.exists() or path.parent.exists():
            return str(path)
    return str(Path.home() / "marvisx" / "projects")


def default_db_path() -> str:
    return os.environ.get("MARVIS_DB_PATH") or "/data/marvisx/db/console.db"


def default_storage() -> StoragePayload:
    return StoragePayload(
        projects_root=default_projects_root(),
        db_backend=DbBackend.sqlite,
        db_path=default_db_path(),
    )


def default_llm_provider() -> LlmProviderPayload:
    env_provider = os.environ.get("MARVIS_LLM_PROVIDER", "").strip().lower()
    valid = {p.value for p in LlmProvider}
    if env_provider in valid:
        return LlmProviderPayload(provider=LlmProvider(env_provider))
    return LlmProviderPayload()


def default_first_project() -> FirstProjectPayload:
    return FirstProjectPayload(
        name="My first project",
        slug="my-first-project",
        type=ProjectType.code,
    )
