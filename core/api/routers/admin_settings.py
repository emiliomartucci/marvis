# v1.0.0 - 2026-05-19 - Smoke test post-deploy + env-check endpoint
"""Admin readonly endpoint to verify critical ENV vars are loaded.

Driven by incident 3e9c40a (2026-05-19 CORS 25h outage) where
`cors_origins_prod` defaulted to `[]` because the .env did not export
CORS_ORIGINS_PROD after the sentinel-clean refactor. Learning
f47b9d0a captures the prevention rule: a post-deploy smoke must
verify env loading, not just process liveness.
"""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from core.api.config import ALLOWED_REPO_PARENTS, settings
from core.api.models import UserInfo
from core.api.rbac import require_role

router = APIRouter(
    prefix="/api/v1/admin/settings",
    tags=["admin", "settings"],
)


CRITICAL_ENV_VARS: tuple[tuple[str, str, bool], ...] = (
    # (env_var_name, settings_attr, is_secret)
    ("CORS_ORIGINS_PROD", "cors_origins_prod", False),
    ("FINDER_SYMLINK_WHITELIST", "finder_symlink_whitelist", False),
    ("BRAIN_LLM_GATEWAY_BASE_URL", "brain_llm_gateway_base_url", False),
    ("BRAIN_LLM_GATEWAY_API_KEY", "brain_llm_gateway_api_key", True),
    ("BRAIN_LLM_POLISH_ENABLED", "brain_llm_polish_enabled", False),
    ("ALLOWED_REPO_PARENTS", None, False),
)


class EnvVarStatus(BaseModel):
    loaded: bool = Field(
        ..., description="True if the runtime value is non-empty / non-default-empty."
    )
    source: str = Field(
        ..., description="'env' if present in os.environ, else 'default'."
    )
    value_redacted: str = Field(
        ..., description="Truncated / masked representation of the active value."
    )
    is_empty_default: bool = Field(
        ...,
        description="True if value is an empty list/string and no env override is present.",
    )


class EnvCheckSummary(BaseModel):
    total: int
    warnings: int
    all_critical_loaded: bool


class EnvCheckResponse(BaseModel):
    critical_vars: dict[str, EnvVarStatus]
    warnings: list[str]
    summary: EnvCheckSummary


def _redact(value: Any, *, is_secret: bool) -> str:
    if value is None:
        return ""
    if hasattr(value, "get_secret_value"):
        raw = value.get_secret_value()
        if not raw:
            return ""
        return f"***{raw[-4:]}" if len(raw) > 4 else "***"
    if is_secret:
        raw = str(value)
        if not raw:
            return ""
        return f"***{raw[-4:]}" if len(raw) > 4 else "***"
    if isinstance(value, list):
        if not value:
            return "[]"
        preview = ", ".join(str(v) for v in value[:3])
        suffix = f" (+{len(value) - 3} more)" if len(value) > 3 else ""
        return f"[{preview}]{suffix}"
    text = str(value)
    return text if len(text) <= 200 else text[:197] + "..."


def _is_empty(value: Any) -> bool:
    if value is None:
        return True
    if hasattr(value, "get_secret_value"):
        return not value.get_secret_value()
    if isinstance(value, (list, tuple, dict, str)):
        return len(value) == 0
    return False


@router.get("/env-check", response_model=EnvCheckResponse)
async def env_check(
    _: UserInfo = Depends(require_role("admin", "super_admin")),
) -> EnvCheckResponse:
    """Readonly snapshot of critical ENV vars and their effective values.

    Used by deploy-api.sh smoke step to detect:
    - missing .env entries leaving Pydantic defaults active (CORS_ORIGINS_PROD=[]),
    - secret keys not exported (gateway API key empty),
    - feature switches stuck at default (BRAIN_LLM_POLISH_ENABLED).
    """
    result: dict[str, EnvVarStatus] = {}
    warnings: list[str] = []

    for env_name, attr, is_secret in CRITICAL_ENV_VARS:
        env_present = env_name in os.environ and os.environ[env_name] != ""

        if attr is None:
            # ALLOWED_REPO_PARENTS is parsed outside Pydantic Settings.
            value: Any = [str(p) for p in ALLOWED_REPO_PARENTS]
        else:
            value = getattr(settings, attr)

        empty = _is_empty(value)
        status = EnvVarStatus(
            loaded=not empty,
            source="env" if env_present else "default",
            value_redacted=_redact(value, is_secret=is_secret),
            is_empty_default=empty and not env_present,
        )
        result[env_name] = status

        if status.is_empty_default:
            warnings.append(
                f"{env_name} is empty and not overridden in environment "
                f"-- likely missing from .env (regression risk: CORS, polish, finder)."
            )

    summary = EnvCheckSummary(
        total=len(result),
        warnings=len(warnings),
        all_critical_loaded=all(s.loaded for s in result.values()),
    )
    return EnvCheckResponse(
        critical_vars=result,
        warnings=warnings,
        summary=summary,
    )
