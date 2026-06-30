"""Hosted storage MCP tools."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from core.api.mcp._adapter import raise_mcp_error
from core.api.use_cases._errors import NotFoundError, ServiceError, ValidationError
from core.scripts import hosted_quota

DEFAULT_REGISTRY_PATH = Path("/var/lib/marvis/tenants/registry.json")


def _tenant_id() -> str:
    tenant = (os.environ.get("TENANT_ID") or os.environ.get("MARVIS_TENANT_ID") or "").strip()
    if not tenant:
        raise ValidationError(
            code="tenant_id_missing",
            message="TENANT_ID/MARVIS_TENANT_ID is not configured",
        )
    return tenant


def _registry_path() -> Path:
    return Path(os.environ.get("MARVIS_TENANT_REGISTRY_PATH", str(DEFAULT_REGISTRY_PATH)))


def _load_registry(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NotFoundError(
            code="tenant_registry_not_found",
            message=f"tenant registry not found: {path}",
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(
            code="tenant_registry_invalid",
            message=f"tenant registry is not valid JSON: {path}",
        ) from exc
    if not isinstance(data, dict):
        raise ValidationError(
            code="tenant_registry_invalid",
            message="tenant registry root must be an object",
        )
    return data


def _tenant_meta(registry: dict[str, Any], tenant: str) -> dict[str, Any]:
    tenants = registry.get("tenants")
    meta = tenants.get(tenant) if isinstance(tenants, dict) else None
    if not isinstance(meta, dict):
        raise NotFoundError(
            code="tenant_not_found",
            message=f"tenant not found in registry: {tenant}",
        )
    return meta


def _tenant_root(meta: dict[str, Any], *, tenant: str, registry_path: Path) -> Path:
    projects_root = meta.get("projects_root")
    if isinstance(projects_root, str) and projects_root.strip():
        return Path(projects_root).expanduser().resolve().parent
    return (registry_path.parent / tenant).resolve()


def storage_usage_payload(*, deep_scan: bool = False) -> dict[str, Any]:
    tenant = _tenant_id()
    registry_path = _registry_path()
    registry = _load_registry(registry_path)
    meta = _tenant_meta(registry, tenant)
    storage = meta.get("storage") if isinstance(meta.get("storage"), dict) else None
    report = hosted_quota.usage_report(
        tenant=tenant,
        tenant_root=_tenant_root(meta, tenant=tenant, registry_path=registry_path),
        storage=storage,
        deep_scan=deep_scan,
    )
    report["registry_path"] = str(registry_path)
    report["registry_updated_at"] = meta.get("updated_at")
    return report


def register(mcp) -> None:
    """Register hosted storage tools on the shared FastMCP instance."""

    @mcp.tool()
    async def storage_usage(
        deep_scan: Annotated[
            bool,
            Field(description="When true, compute current usage without persisting it."),
        ] = False,
    ) -> dict[str, Any]:
        """Read hosted tenant storage quota/usage state.

        QUANDO USARLO: capire stato storage del tenant hosted via MCP: quota_mode, enforced, used_percent, snapshot_stale e soglie.
        QUANDO NON USARLO: NOT per applicare quota kernel o aggiornare il registry; deep_scan calcola uso corrente ma non persiste.
        RESTITUISCE: {tenant, quota_mode, enforced, state, used_bytes, quota_bytes, used_percent, snapshot_stale, thresholds}."""
        try:
            return storage_usage_payload(deep_scan=deep_scan)
        except ServiceError as e:
            raise_mcp_error(e)
