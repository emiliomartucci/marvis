"""MCP tools — fleet ``report_bug`` (transport C client side).

Every tenant's LLM gets these. ``report_bug`` redacts + caps locally (never let a
secret leave the process un-redacted, even though the operator re-redacts), then
POSTs to the operator's loopback ingest endpoint with this tenant's HMAC ingest
token. ``bug_status`` / ``list_bug_reports`` read back the tenant's OWN reports
over the same transport (the operator scopes by the token's tenant → another
tenant's id is a 404).

Config (per-tenant env, injected by provisioning): ``MARVIS_BUGREPORT_INGEST_URL``
(operator base, e.g. http://127.0.0.1:8111), ``MARVIS_BUGREPORT_INGEST_TOKEN``
(this tenant's derived token), plus ``TENANT_ID``. Unconfigured tenants get a
clean ``not_configured`` envelope, never a crash.
"""
from __future__ import annotations

import os
from typing import Annotated, Any

from pydantic import Field

from core.api.config import settings
from core.api.services import bug_reports_core as brc
from core.api.services import bug_reports_store as brc_store
from core.api.mcp._adapter import acquire_db, current_mcp_context

_TIMEOUT_S = 8.0


def _config() -> tuple[str, str, str] | None:
    """Return (base_url, tenant, token) or None when the feature is unconfigured."""
    base = (getattr(settings, "bugreport_ingest_url", "") or "").rstrip("/")
    token = getattr(settings, "bugreport_ingest_token", "") or ""
    tenant = os.environ.get("TENANT_ID", "") or ""
    if not base or not token or not tenant:
        return None
    return base, tenant, token


def _not_configured() -> dict[str, Any]:
    return {
        "ok": False,
        "code": "not_configured",
        "hint": "bug reporting is not enabled for this tenant (missing ingest url/token)",
    }


def register(mcp) -> None:
    """Register the bug-report tool group on the shared FastMCP instance."""

    @mcp.tool()
    async def report_bug(
        title: Annotated[str, Field(min_length=1, max_length=400, description="Concise bug title")],
        description: Annotated[str, Field(min_length=1, description="What went wrong, where, and the impact")],
        tool_or_area: Annotated[str | None, Field(default=None, max_length=512, description="MCP tool or product area involved")] = None,
        error_code: Annotated[str | None, Field(default=None, max_length=512, description="Error code / message class if any")] = None,
        repro: Annotated[str | None, Field(default=None, max_length=512, description="Minimal steps to reproduce")] = None,
        client: Annotated[str | None, Field(default=None, max_length=512, description="Client/agent that hit it (e.g. Claude Code)")] = None,
        kind_hint: Annotated[str | None, Field(default=None, max_length=32, description="Optional hint: bug|regression|papercut")] = None,
        severity_hint: Annotated[str | None, Field(default=None, max_length=32, description="Optional hint: low|medium|high")] = None,
    ) -> dict[str, Any]:
        """Report a bug you hit while working, so the operator's QA can triage it.

        QUANDO USARLO: quando un tool MCP o una superficie del prodotto si rompe o si comporta male (errore inatteso, 500, risultato palesemente sbagliato). Prima controlla list_bug_reports per non ri-segnalare lo stesso.
        QUANDO NON USARLO: NOT per richieste di feature o dubbi d'uso; NOT per errori attesi (validazione/permessi). NON incollare segreti: il testo viene comunque redatto.
        PROVA: ritorna {report_id, status:"logged"}; se deduplicated=true era gia' segnalato.
        RESTITUISCE: {ok, report_id, status, deduplicated} — usa report_id con bug_status."""
        cfg = _config()
        if cfg is None:
            return _not_configured()
        base, tenant, token = cfg

        # Redact + cap BEFORE anything leaves the process.
        red_title, _ = brc.redact(brc.cap(title, brc.TITLE_CAP))
        red_desc, _ = brc.redact(brc.cap(description, brc.DESCRIPTION_CAP))
        env: dict[str, str] = {}
        for name, val in (("tool_or_area", tool_or_area), ("error_code", error_code), ("repro", repro), ("client", client)):
            if val:
                r, _ = brc.redact(brc.cap(val, brc.ENV_FIELD_CAP))
                env[name] = r

        payload: dict[str, Any] = {"title": red_title, "description": red_desc}
        if env:
            payload["environment"] = env
        if kind_hint:
            payload["kind_hint"] = kind_hint
        if severity_hint:
            payload["severity_hint"] = severity_hint

        import httpx

        headers = {"X-Bug-Ingest-Tenant": tenant, "X-Bug-Ingest-Token": token}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client_http:
                resp = await client_http.post(f"{base}/api/v1/bug-reports", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            return {"ok": False, "code": "transport_error", "retryable": True, "hint": str(exc)[:200]}

        if resp.status_code in (200, 201):
            data = resp.json()
            return {
                "ok": True,
                "report_id": data.get("report_id"),
                "status": data.get("status", "logged"),
                "deduplicated": bool(data.get("deduplicated", False)),
            }
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            return {
                "ok": False,
                "code": "rate_limited",
                "retryable": True,
                "retry_after_ms": (int(retry_after) * 1000) if (retry_after and retry_after.isdigit()) else None,
                "hint": "too many bug reports this hour — back off and retry later",
            }
        return {"ok": False, "code": "ingest_error", "status_code": resp.status_code, "hint": resp.text[:200]}

    @mcp.tool()
    async def bug_status(
        report_id: Annotated[str, Field(min_length=1, description="The report_id returned by report_bug")],
    ) -> dict[str, Any]:
        """Check the triage status of a bug report YOU filed.

        QUANDO USARLO: per sapere se un tuo report e' ancora pending o e' stato preso in carico/risolto.
        QUANDO NON USARLO: NOT per report di altri (ritorna not_found).
        RESTITUISCE: {ok, report_id, status, title, created_at} oppure {ok:false, code:not_found}."""
        cfg = _config()
        if cfg is None:
            return _not_configured()
        base, tenant, token = cfg
        import httpx

        headers = {"X-Bug-Ingest-Tenant": tenant, "X-Bug-Ingest-Token": token}
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client_http:
                resp = await client_http.get(f"{base}/api/v1/bug-reports/{report_id}", headers=headers)
        except httpx.HTTPError as exc:
            return {"ok": False, "code": "transport_error", "retryable": True, "hint": str(exc)[:200]}
        if resp.status_code == 200:
            data = resp.json()
            return {"ok": True, **data}
        if resp.status_code == 404:
            return {"ok": False, "code": "not_found", "hint": "no such report for this tenant"}
        return {"ok": False, "code": "read_error", "status_code": resp.status_code, "hint": resp.text[:200]}

    @mcp.tool()
    async def list_bug_reports(
        status: Annotated[str | None, Field(default=None, description="Filter by status, e.g. pending")] = None,
        since: Annotated[str | None, Field(default=None, description="ISO date/datetime lower bound on created_at")] = None,
        limit: Annotated[int, Field(default=50, ge=1, le=200, description="Max rows")] = 50,
    ) -> dict[str, Any]:
        """List the bug reports YOU filed (this tenant only).

        QUANDO USARLO: PRIMA di segnalare, per non duplicare; per rivedere i tuoi report dopo un /clear che ti ha fatto perdere il report_id.
        RESTITUISCE: {ok, reports:[{report_id, status, title, created_at}]}."""
        cfg = _config()
        if cfg is None:
            return _not_configured()
        base, tenant, token = cfg
        import httpx

        headers = {"X-Bug-Ingest-Tenant": tenant, "X-Bug-Ingest-Token": token}
        params: dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if since:
            params["since"] = since
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client_http:
                resp = await client_http.get(f"{base}/api/v1/bug-reports", headers=headers, params=params)
        except httpx.HTTPError as exc:
            return {"ok": False, "code": "transport_error", "retryable": True, "hint": str(exc)[:200]}
        if resp.status_code == 200:
            return {"ok": True, "reports": resp.json()}
        return {"ok": False, "code": "read_error", "status_code": resp.status_code, "hint": resp.text[:200]}


    @mcp.tool()
    async def list_bug_reports_admin(
        workspace: Annotated[str | None, Field(default=None, description="Filter to one reporter tenant, e.g. acme")] = None,
        status: Annotated[str | None, Field(default=None, description="Filter by status, e.g. pending")] = None,
        since: Annotated[str | None, Field(default=None, description="ISO date/datetime lower bound on created_at")] = None,
        limit: Annotated[int, Field(default=100, ge=1, le=500, description="Max rows")] = 100,
    ) -> dict[str, Any]:
        """[admin] List ALL fleet bug reports across every reporter tenant (operator only).

        QUANDO USARLO: triage operatore - i report dei tenant arrivano con workspace_id=reporter e NON compaiono in list_bug_reports (che mostra solo i tuoi) ne nelle viste task default. Questo li enumera tutti.
        QUANDO NON USARLO: NOT dai tenant non-operatore (ritorna not_operator); NOT per non-admin (forbidden).
        RESTITUISCE: {ok, reports:[{report_id, workspace, status, title, created_at, updated_at}]}."""
        secret = getattr(settings, 'bugreport_ingest_secret', '') or ''
        if not secret:
            return {'ok': False, 'code': 'not_operator', 'hint': 'this tenant is not the fleet bug-report operator'}
        ctx = current_mcp_context()
        if getattr(ctx, 'system_role', None) not in ('admin', 'super_admin'):
            return {'ok': False, 'code': 'forbidden', 'hint': 'admin only: cross-tenant bug reports'}
        async with acquire_db() as db:
            reports = await brc_store.list_reports_admin(db, workspace=workspace, status=status, since=since, limit=limit)
        return {'ok': True, 'reports': reports}
