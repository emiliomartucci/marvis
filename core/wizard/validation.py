"""Per-step validators. Pure functions — defensive filesystem checks only.

Returns a list of ValidationError so a single call surfaces every problem
in one round-trip (UX over short-circuit). Callers treat empty list as ok.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .state import (
    FirstProjectPayload,
    LlmProvider,
    LlmProviderPayload,
    StoragePayload,
    WelcomePayload,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")


class ValidationError(ValueError):
    """Field-scoped validation failure. `.field` + `.message` are the wire shape."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


def _is_writable(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.W_OK)
    parent = path.parent
    return parent.exists() and os.access(parent, os.W_OK)


def validate_welcome(payload: WelcomePayload) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if not payload.bsl_accepted:
        errors.append(
            ValidationError(
                "bsl_accepted",
                "You must accept the BSL license to continue",
            )
        )
    return errors


def validate_storage(payload: StoragePayload) -> list[ValidationError]:
    errors: list[ValidationError] = []

    if not payload.projects_root:
        errors.append(ValidationError("projects_root", "Path cannot be empty"))
    else:
        projects_root = Path(payload.projects_root).expanduser()
        if not projects_root.is_absolute():
            errors.append(
                ValidationError("projects_root", "Must be an absolute path")
            )
        elif not _is_writable(projects_root):
            errors.append(
                ValidationError("projects_root", "Directory not writable")
            )

    if payload.db_backend.value == "sqlite":
        if not payload.db_path:
            errors.append(ValidationError("db_path", "SQLite path required"))
    elif payload.db_backend.value == "postgres":
        if not payload.postgres_dsn:
            errors.append(
                ValidationError("postgres_dsn", "Postgres DSN required")
            )
        elif not payload.postgres_dsn.startswith(
            ("postgresql://", "postgres://")
        ):
            errors.append(
                ValidationError(
                    "postgres_dsn", "DSN must start with postgresql://"
                )
            )

    return errors


def validate_llm_provider(
    payload: LlmProviderPayload, *, allow_empty: bool = False
) -> list[ValidationError]:
    """Validate provider + key. `allow_empty=True` lets a fully blank payload pass (skip path)."""
    if allow_empty and payload.provider is None and not payload.api_key:
        return []

    errors: list[ValidationError] = []
    if payload.provider is None:
        errors.append(ValidationError("provider", "Provider required"))
        return errors

    if payload.provider == LlmProvider.mac_gateway:
        if not payload.base_url:
            errors.append(
                ValidationError("base_url", "Mac Gateway base_url required")
            )
        if not payload.api_key:
            errors.append(ValidationError("api_key", "Virtual key required"))
    else:
        if not payload.api_key:
            errors.append(ValidationError("api_key", "API key required"))

    return errors


def validate_first_project(
    payload: FirstProjectPayload,
) -> list[ValidationError]:
    errors: list[ValidationError] = []
    if not payload.name or not payload.name.strip():
        errors.append(ValidationError("name", "Project name required"))
    if not payload.slug:
        errors.append(ValidationError("slug", "Slug required"))
    elif not SLUG_PATTERN.match(payload.slug):
        errors.append(
            ValidationError(
                "slug",
                "Slug must match ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$",
            )
        )
    return errors


def slugify(name: str) -> str:
    """Best-effort slug. May return empty; caller validates via SLUG_PATTERN."""
    base = name.lower().strip()
    base = re.sub(r"[^a-z0-9]+", "-", base)
    return base.strip("-")[:64]
