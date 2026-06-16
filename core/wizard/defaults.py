"""Sensible defaults per step. Env-var override allowed for power users + CI."""

from __future__ import annotations

import os

from core.platform import db_default_path, projects_root_default

from .state import (
    DbBackend,
    FirstProjectPayload,
    LlmProvider,
    LlmProviderPayload,
    ProjectType,
    StoragePayload,
)


def default_projects_root() -> str:
    # Delegates to the single cross-OS resolver (env > platformdirs default). The
    # old /data/projects candidate + .exists()/.parent.exists() probe returned
    # C:\data\projects on Windows because C:\ (parent of C:\data) always exists.
    return str(projects_root_default())


def default_db_path() -> str:
    return str(db_default_path())


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
