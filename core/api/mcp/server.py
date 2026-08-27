# v1.0.0 - 2026-05-27 - S1 F3.0: Python MCP server skeleton (FastMCP, stdio, use_cases-direct)
"""Python MCP server — the payoff of the S1 "collapse runtime" refactor.

A single ``FastMCP("marvis")`` instance exposes the Marvis tools, each calling the
``use_cases`` layer DIRECTLY (no Node). Claude Code launches this as a stdio
subprocess exactly as it does the Node ``index.mjs`` today:

    python -m core.api.mcp.server

F3.0 ships the SKELETON + the per-tool TEMPLATE on two domains (tasks +
learnings). The remaining 79 Node tools land in later F3 batches that copy the
template in ``tools/tasks.py`` / ``tools/learnings.py``.

SDK contract used (``mcp`` >= 1.27):
  * ``FastMCP(name)`` + ``@mcp.tool()`` decorator. The function docstring becomes
    the tool ``description``; the type hints become the input JSON schema.
  * ``mcp.run()`` defaults to the stdio transport — parity 1:1 with the Node
    server.
  * ``await mcp.list_tools()`` introspects the registered tools (used by the smoke
    test for ``tools/list`` parity against the Node baseline).

Remote hosted tier: ``MARVIS_MCP_TRANSPORT=http`` builds a separate
``fastmcp.FastMCP`` instance with ``StaticTokenVerifier`` by default, or dual
StaticTokenVerifier + WorkOS AuthKit OAuth when ``WORKOS_AUTHKIT_DOMAIN`` and
``MCP_PUBLIC_BASE_URL`` are configured. It serves streamable-http on
``127.0.0.1:$MARVIS_MCP_PORT/mcp``. The module-level ``mcp`` singleton stays
stdio-only and unauthenticated so local plugin behavior does not drift.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from mcp.server.fastmcp import FastMCP as StdioFastMCP

from core.api.mcp.guidance import (
    apply_tool_metadata_to_server,
    build_instructions,
)
from core.api.mcp.tools import register_all

# Module-level singleton: the MCP process is the lifetime container (the
# `app.state` equivalent for a server with no FastAPI `app`). Tools register on
# this at import time so `from core.api.mcp.server import mcp` already carries the
# full tool set — the smoke test introspects it without launching stdio.
_INSTRUCTIONS = build_instructions()


def _apply_core_tool_meta(server) -> None:
    """Mark cold-start tools as always-loaded when the server supports metadata."""
    try:
        apply_tool_metadata_to_server(server)
    except Exception:  # pragma: no cover - FastMCP internal-shape guard
        pass


def _build_stdio_mcp():
    """Build the trusted local stdio MCP server (no auth)."""
    server = StdioFastMCP("marvis", instructions=_INSTRUCTIONS)
    register_all(server)
    _apply_core_tool_meta(server)
    return server


def _join_url_path(base_url: str, path: str) -> str:
    return f"{base_url.rstrip('/')}/{path.strip('/')}"


def _read_env_or_file(name: str) -> str:
    """Secret from env, or from the file named by ``{name}_FILE`` (IMPL §A Patch 2).

    Ports the ``*_FILE`` convention from the systemd bash wrapper into the code
    so Docker secrets work without a shell wrapper — and the token does not
    have to live in ``/proc/<pid>/environ``.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    path = os.environ.get(f"{name}_FILE", "").strip()
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"{name}_FILE={path!r} is set but unreadable: {exc}") from exc


def _oauth_protected_resource_metadata(
    *,
    public_base_url: str,
    authkit_domain: str,
    mcp_path: str = "/mcp",
    resource_name: str = "Marvis MCP",
) -> dict[str, object]:
    """Build RFC 9728 protected resource metadata for the hosted MCP endpoint."""
    return {
        "resource": _join_url_path(public_base_url, mcp_path),
        "authorization_servers": [authkit_domain.rstrip("/")],
        "bearer_methods_supported": ["header"],
        "resource_name": resource_name,
    }


def _normalize_protected_resource_routes(
    provider,  # noqa: ANN001 - fastmcp auth provider
    *,
    public_base_url: str,
    authkit_domain: str,
    mcp_path: str,
    resource_name: str,
) -> None:
    """Serve RFC 9728 metadata with a bare-origin ``authorization_servers``.

    fastmcp stores the authorization server as a pydantic ``AnyHttpUrl``, which
    normalizes ``https://host`` to ``https://host/``. A client that concatenates
    that issuer with ``/.well-known/oauth-authorization-server`` then requests a
    double-slash URL, gets a 308, and — when it does not follow the redirect
    during discovery — fails Dynamic Client Registration outright. The universal
    edge builds the same document by hand without the slash and registers fine,
    so this keeps the per-tenant document byte-compatible with it. Also drops an
    empty ``scopes_supported``, which advertises "no scopes" rather than
    "unscoped".
    """
    from mcp.server.auth.routes import cors_middleware
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    original_get_routes = provider.get_routes
    metadata = _oauth_protected_resource_metadata(
        public_base_url=public_base_url,
        authkit_domain=authkit_domain,
        mcp_path=mcp_path,
        resource_name=resource_name,
    )

    async def _serve_metadata(request):  # noqa: ANN001
        return JSONResponse(metadata)

    def get_routes(*args, **kwargs):  # noqa: ANN002, ANN003
        routes = original_get_routes(*args, **kwargs)
        patched = []
        for route in routes:
            if isinstance(route, Route) and route.path.startswith(
                "/.well-known/oauth-protected-resource"
            ):
                patched.append(
                    Route(
                        route.path,
                        endpoint=cors_middleware(_serve_metadata, ["GET", "OPTIONS"]),
                        methods=["GET", "OPTIONS"],
                    )
                )
            else:
                patched.append(route)
        return patched

    provider.get_routes = get_routes


def _build_oidc_auth(*, static_verifier, oidc_issuer: str, public_base_url: str, mcp_path: str):
    """Generic OIDC auth profile (Entra ID day-1) — IMPL §A.0b/§A.3.

    ``OIDCProxy`` is the DCR bridge: Claude registers via Dynamic Client
    Registration, Entra has no ``/register``, the proxy exposes one and serves
    its own RFC 9728 protected-resource metadata (``authorization_servers`` =
    this proxy, NEVER the Entra issuer — a client pointed straight at Entra
    dies on the missing DCR endpoint). The custom ``token_verifier`` replaces
    the proxy's default JWTVerifier and receives the RAW upstream JWT, so the
    ``tid`` gate and role claims reach ``current_mcp_context()`` intact.
    ``algorithm``/``required_scopes`` must live ON the verifier — passing them
    to OIDCProxy together with a custom verifier is a ValueError (fastmcp 3.4.2).

    The second MultiAuth verifier is the M2M raw-token path: client-credentials
    tokens do not pass through the proxy.
    """
    from fastmcp.server.auth import MultiAuth
    from fastmcp.server.auth.oidc_proxy import OIDCProxy

    from core.api.mcp.org_verifier import TenantScopedJWTVerifier

    if not public_base_url:
        raise RuntimeError("MCP_PUBLIC_BASE_URL is required when OIDC_ISSUER is set")
    audience = os.environ.get("OIDC_AUDIENCE", "").strip()
    if not audience:
        raise RuntimeError(
            "OIDC_AUDIENCE is required when OIDC_ISSUER is set — the App ID URI "
            "(e.g. api://marvis-mcp), NOT the client GUID (FastMCP #3729)"
        )
    client_id = os.environ.get("OIDC_CLIENT_ID", "").strip()
    if not client_id:
        raise RuntimeError("OIDC_CLIENT_ID is required when OIDC_ISSUER is set")
    client_secret = _read_env_or_file("OIDC_CLIENT_SECRET")
    if not client_secret:
        raise RuntimeError(
            "OIDC_CLIENT_SECRET (or OIDC_CLIENT_SECRET_FILE) is required when "
            "OIDC_ISSUER is set"
        )
    jwks_uri = os.environ.get("OIDC_JWKS_URI", "").strip()
    if not jwks_uri:
        raise RuntimeError(
            "OIDC_JWKS_URI is required when OIDC_ISSUER is set (issuer+JWKS are "
            "per-profile — never shared with the WorkOS path)"
        )
    expected_tenant = os.environ.get("MARVIS_TENANT_EXPECTED", "").strip()
    if not expected_tenant:
        raise RuntimeError(
            "MARVIS_TENANT_EXPECTED is required when OIDC_ISSUER is set — the IdP "
            "tenant GUID the `tid` claim must equal (fail-closed cross-tenant gate)"
        )
    config_url = (
        os.environ.get("OIDC_CONFIG_URL", "").strip()
        or f"{oidc_issuer.rstrip('/')}/.well-known/openid-configuration"
    )

    tenant_verifier = TenantScopedJWTVerifier(
        tenant_claim="tid",
        expected_value=expected_tenant,
        jwks_uri=jwks_uri,
        issuer=oidc_issuer,
        algorithm="RS256",
        # aud is ON for Entra (unlike WorkOS audience=None): the App ID URI.
        audience=audience,
    )
    oidc_proxy = OIDCProxy(
        config_url=config_url,
        client_id=client_id,
        client_secret=client_secret,
        base_url=public_base_url,
        token_verifier=tenant_verifier,
        # Entra v2 rejects the RFC 8707 `resource` param (AADSTS901002).
        forward_resource=False,
        # Without the scope override Entra mints a Graph token (wrong aud → 401).
        extra_authorize_params={
            "scope": f"openid profile email offline_access {audience}/access_as_user"
        },
    )
    auth = MultiAuth(server=oidc_proxy, verifiers=[static_verifier, tenant_verifier])
    # ONE set_mcp_path, on MultiAuth (propagates to the verifiers too) — §E.
    auth.set_mcp_path(mcp_path)
    return auth


def _build_http_mcp():
    """Build the remote MCP server with per-tenant Bearer and optional OAuth."""
    from fastmcp import FastMCP as HttpFastMCP
    from fastmcp.server.auth import MultiAuth
    from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
    from starlette.responses import JSONResponse

    token = _read_env_or_file("TENANT_BEARER_TOKEN")
    if not token:
        raise RuntimeError(
            "TENANT_BEARER_TOKEN (or TENANT_BEARER_TOKEN_FILE) is required when "
            "MARVIS_MCP_TRANSPORT=http"
        )

    tenant_id = os.environ.get("TENANT_ID", "tenant").strip() or "tenant"
    workspace_id = os.environ.get("MARVIS_MCP_WORKSPACE_ID", "").strip()
    if not workspace_id:
        raise RuntimeError(
            "MARVIS_MCP_WORKSPACE_ID is required when MARVIS_MCP_TRANSPORT=http"
        )
    static_verifier = StaticTokenVerifier(
        tokens={
            token: {
                "client_id": tenant_id,
                "scopes": ["read:data", "write:data"],
                "workspace_id": workspace_id,
            }
        },
        required_scopes=["read:data"],
    )
    authkit_domain = os.environ.get("WORKOS_AUTHKIT_DOMAIN", "").strip()
    oidc_issuer = os.environ.get("OIDC_ISSUER", "").strip()
    # S1 presence-switch (IMPL §E): the REAL WorkOS gate is WORKOS_AUTHKIT_DOMAIN
    # (org_id is only an inner modifier). A leftover authkit domain next to an
    # OIDC_ISSUER would silently run the tenant in WorkOS mode on a US IdP with
    # the OIDC config ignored — refuse the ambiguity at boot.
    if authkit_domain and oidc_issuer:
        raise RuntimeError(
            "WORKOS_AUTHKIT_DOMAIN and OIDC_ISSUER are both set — the WorkOS AuthKit "
            "and generic OIDC auth profiles are mutually exclusive (S1). Remove one; "
            "refusing to start in an ambiguous auth state."
        )
    public_base_url = os.environ.get("MCP_PUBLIC_BASE_URL", "").strip()
    mcp_path = os.environ.get("MARVIS_MCP_PATH", "/mcp").strip() or "/mcp"
    org_id = os.environ.get("MARVIS_WORKOS_ORG_ID", "").strip()
    if org_id and not org_id.startswith("org_"):
        raise RuntimeError(
            "MARVIS_WORKOS_ORG_ID is set but malformed (expected 'org_...'); "
            "refusing to start in an ambiguous auth state"
        )
    auth = static_verifier

    if oidc_issuer:
        auth = _build_oidc_auth(
            static_verifier=static_verifier,
            oidc_issuer=oidc_issuer,
            public_base_url=public_base_url,
            mcp_path=mcp_path,
        )
    elif authkit_domain:
        if not public_base_url:
            raise RuntimeError(
                "MCP_PUBLIC_BASE_URL is required when WORKOS_AUTHKIT_DOMAIN is set"
            )
        from fastmcp.server.auth.providers.workos import AuthKitProvider

        _authkit_kwargs = dict(
            authkit_domain=authkit_domain,
            base_url=public_base_url,
            # WorkOS AuthKit managed clients reject custom MCP scopes unless RBAC
            # is configured. AuthKit security is aud/resource + allowlist here.
            required_scopes=[],
            scopes_supported=[],
            resource_name=f"Marvis brain - {tenant_id}",
        )
        if org_id:
            # Org-based tenant isolation (no dashboard Resource Indicator):
            # validate org_id server-side, drop the per-tenant aud. Passing a
            # token_verifier skips AuthKit aud auto-bind (fastmcp 3.4.2). Plan
            # 2026-07-01-feat-oauth-org-based-tenant-isolation-plan.md.
            from core.api.mcp.org_verifier import OrgScopedJWTVerifier

            # Accept both issuers with the shared JWKS: interactive AuthKit
            # OAuth (iss=authkit_domain, used by MCP clients) AND User
            # Management password-grant (iss=api.workos.com/user_management/
            # <client_id>, used for headless tests). Same signing key + org
            # gate isolate either way. WORKOS_CLIENT_ID is public (not a secret).
            _um_client = os.environ.get("WORKOS_CLIENT_ID", "").strip()
            _issuers = [authkit_domain]
            if _um_client:
                _issuers.append(
                    f"https://api.workos.com/user_management/{_um_client}"
                )
            _authkit_kwargs["token_verifier"] = OrgScopedJWTVerifier(
                expected_org=org_id,
                jwks_uri=f"{authkit_domain}/oauth2/jwks",
                issuer=_issuers,
                algorithm="RS256",
                audience=None,
            )
        authkit_provider = AuthKitProvider(**_authkit_kwargs)
        authkit_provider.set_mcp_path(mcp_path)
        _normalize_protected_resource_routes(
            authkit_provider,
            public_base_url=public_base_url,
            authkit_domain=authkit_domain,
            mcp_path=mcp_path,
            resource_name=f"Marvis brain - {tenant_id}",
        )
        auth = MultiAuth(server=authkit_provider, verifiers=[static_verifier])
        auth.set_mcp_path(mcp_path)

    server = HttpFastMCP(name="marvis", instructions=_INSTRUCTIONS, auth=auth)

    @server.custom_route("/api/v1/ingest/webhook", methods=["POST"])
    async def ingest_webhook(request):  # noqa: ANN001
        from fastapi import HTTPException

        from core.api.routers.ingest_triage import handle_signed_webhook_request

        try:
            result = await handle_signed_webhook_request(request)
        except HTTPException as exc:
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return JSONResponse(result.model_dump(mode="json"), status_code=202)

    @server.custom_route("/api/v1/bug-reports", methods=["GET", "POST"])
    async def bug_report_collection(request):  # noqa: ANN001
        from core.api.mcp._adapter import acquire_db, acquire_write_db
        from core.api.services import bug_reports_store as store

        try:
            reporter = store.authenticate(
                request.headers.get("x-bug-ingest-tenant"),
                request.headers.get("x-bug-ingest-token"),
            )
            if request.method == "POST":
                try:
                    body = await request.json()
                except Exception:  # noqa: BLE001
                    return JSONResponse({"detail": "invalid json"}, status_code=400)
                async with acquire_write_db(label="mcp.bug_report_create") as db:
                    result = await store.create_report(
                        db,
                        reporter,
                        title=body.get("title"),
                        description=body.get("description"),
                        environment=body.get("environment"),
                        kind_hint=body.get("kind_hint"),
                        severity_hint=body.get("severity_hint"),
                    )
                return JSONResponse(result, status_code=201)
            qp = request.query_params
            try:
                limit = int(qp.get("limit", "50"))
            except (TypeError, ValueError):
                limit = 50
            async with acquire_db() as db:
                rows = await store.list_reports(
                    db, reporter, status=qp.get("status"), since=qp.get("since"), limit=limit
                )
            return JSONResponse(rows)
        except store.BugIngestError as exc:
            headers = None
            if exc.status_code == 429:
                headers = {"Retry-After": str(max(1, (exc.retry_after_ms or 0) // 1000))}
            return JSONResponse(
                {"detail": exc.detail, "code": exc.code}, status_code=exc.status_code, headers=headers
            )

    @server.custom_route("/api/v1/bug-reports/{report_id}", methods=["GET"])
    async def bug_report_item(request):  # noqa: ANN001
        from core.api.mcp._adapter import acquire_db
        from core.api.services import bug_reports_store as store

        try:
            reporter = store.authenticate(
                request.headers.get("x-bug-ingest-tenant"),
                request.headers.get("x-bug-ingest-token"),
            )
            async with acquire_db() as db:
                row = await store.get_report(db, reporter, str(request.path_params.get("report_id", "")))
        except store.BugIngestError as exc:
            return JSONResponse({"detail": exc.detail, "code": exc.code}, status_code=exc.status_code)
        if row is None:
            return JSONResponse({"detail": "bug report not found"}, status_code=404)
        return JSONResponse(row)

    def _internal_request_authorized(request) -> bool:  # noqa: ANN001
        """Loopback-only worker auth (RBAC F3): the exact tenant bearer.

        These /internal paths are NEVER in the Caddy allow-list (public = 404);
        the in-handler check is defense in depth for the loopback port.
        """
        import hmac

        header = request.headers.get("authorization", "")
        presented = header.removeprefix("Bearer ").strip()
        return bool(presented) and hmac.compare_digest(presented, token)

    @server.custom_route("/internal/user-provisioning/pending", methods=["GET"])
    async def user_provisioning_pending(request):  # noqa: ANN001
        from core.api.mcp._adapter import acquire_write_db
        from core.api.services import user_provisioning as upq

        if not _internal_request_authorized(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        # Write handle: the pending read runs the tenant-side sweep/re-check
        # transitions (single-writer covenant — the root worker never writes).
        async with acquire_write_db(label="mcp.upq_pending") as db:
            items = await upq.pending_batch(db, workspace_id=workspace_id)
        return JSONResponse({"items": items})

    @server.custom_route(
        "/internal/user-provisioning/{request_id}/complete", methods=["POST"]
    )
    async def user_provisioning_complete(request):  # noqa: ANN001
        from core.api.mcp._adapter import acquire_write_db
        from core.api.services import user_provisioning as upq
        from core.api.use_cases._errors import ServiceError

        if not _internal_request_authorized(request):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"detail": "invalid json"}, status_code=400)
        try:
            async with acquire_write_db(label="mcp.upq_complete") as db:
                result = await upq.complete_request(
                    db,
                    request_id=str(request.path_params.get("request_id", "")),
                    outcome=str(body.get("outcome", "")),
                    workos_user_id=body.get("workos_user_id"),
                    error=body.get("error"),
                    workspace_id=workspace_id,
                )
        except ServiceError as exc:
            return JSONResponse({"detail": exc.message, "code": exc.code}, status_code=400)
        return JSONResponse(result)

    @server.custom_route("/healthz", methods=["GET"])
    async def healthz(_request):  # noqa: ANN001
        from core.api.services.access_grants import grants_query_error_count

        return JSONResponse(
            {
                "status": "ok",
                "service": "marvis-mcp",
                "transport": "http",
                "tenant": tenant_id,
                "db_path": os.environ.get("MARVIS_DB_PATH")
                or os.environ.get("PIR_DB_PATH"),
                "projects_root": os.environ.get("MARVIS_PROJECTS_ROOT"),
                "grants_query_error_count": grants_query_error_count(),
            }
        )

    if authkit_domain:

        @server.custom_route(
            "/.well-known/oauth-protected-resource", methods=["GET"]
        )
        async def oauth_protected_resource(_request):  # noqa: ANN001
            return JSONResponse(
                _oauth_protected_resource_metadata(
                    public_base_url=public_base_url,
                    authkit_domain=authkit_domain,
                    mcp_path=mcp_path,
                    resource_name=f"Marvis brain - {tenant_id}",
                )
            )

    register_all(server)
    _apply_core_tool_meta(server)
    # P3 tool profiles RBAC: wire the per-role exposure gate + usage counter.
    # DELIBERATELY outside the metadata try/except — a wiring failure MUST raise
    # at boot (fail-closed), never start the server unprotected. HTTP-only: the
    # stdio server is a trusted local surface with no middleware layer.
    from core.api.mcp.tool_profiles import apply_tool_profiles
    from core.api.mcp.tool_usage import apply_tool_usage

    try:
        from core.api.mcp.product_events import apply_product_events
    except ImportError:
        apply_product_events = None

    apply_tool_profiles(server)
    if apply_product_events is not None:
        apply_product_events(server)
    # Durable per-tool usage counter (measure for data-driven pruning / OQ5 split).
    # Best-effort + non-blocking; records {tool, actor, ts} only — never arguments.
    apply_tool_usage(server)
    return server


mcp = _build_stdio_mcp()


def _transport_from_env() -> Literal["stdio", "http"]:
    raw = os.environ.get("MARVIS_MCP_TRANSPORT", "stdio").strip().lower()
    if raw in {"", "stdio"}:
        return "stdio"
    if raw in {"http", "streamable-http", "streamable_http"}:
        return "http"
    raise SystemExit(
        "Unsupported MARVIS_MCP_TRANSPORT="
        f"{raw!r}; expected 'stdio' or 'http'"
    )


def _http_port_from_env() -> int:
    raw = os.environ.get("MARVIS_MCP_PORT", "8100").strip()
    try:
        port = int(raw)
    except ValueError as exc:
        raise SystemExit(f"MARVIS_MCP_PORT must be an integer, got {raw!r}") from exc
    if not 1 <= port <= 65535:
        raise SystemExit(f"MARVIS_MCP_PORT must be between 1 and 65535, got {port}")
    return port


def _http_host_from_env() -> str:
    """Bind host for the HTTP transport (IMPL §A Patch 1).

    Default loopback: the systemd fleet sits behind Caddy on the same host.
    Containers set ``MARVIS_MCP_BIND_HOST=0.0.0.0`` and rely on the container
    network for isolation — the port is NOT published. Before this patch the
    host was hardcoded to 127.0.0.1 and the env name was dead code, making any
    containerized server unreachable.
    """
    return os.environ.get("MARVIS_MCP_BIND_HOST", "127.0.0.1").strip() or "127.0.0.1"


def _enforce_db_path_env_invariant() -> None:
    """S2 (IMPL §E): keep ``MARVIS_DB_PATH`` == the settings-resolved db_path.

    ``_db_system_role`` — the DB fallback that keeps interactive OAuth logins
    from collapsing to viewer (d8d9af95) — reads ``MARVIS_DB_PATH`` from env,
    while the server resolves its database from ``settings.yaml`` (the env
    alone is ignored, learning 10e5c09f). Unset → export the resolved path so
    the fallback reads the SAME database the server serves. Set to something
    else → refuse to boot: a mismatched env silently reads the wrong database
    and every interactive login resolves to viewer fleet-wide.
    """
    from core.api.config import settings

    resolved = str(settings.db_path)
    env_path = os.environ.get("MARVIS_DB_PATH", "").strip()
    if env_path:
        try:
            same = Path(env_path).resolve() == Path(resolved).resolve()
        except OSError:
            same = False
        if not same:
            raise RuntimeError(
                f"MARVIS_DB_PATH={env_path!r} does not match the settings-resolved "
                f"db_path {resolved!r} (S2): the OAuth role DB-fallback would read "
                "the wrong database and every interactive login would resolve to "
                "viewer. Fix or unset MARVIS_DB_PATH."
            )
    os.environ["MARVIS_DB_PATH"] = resolved


def _http_stateless_from_env() -> bool:
    """Use stateless Streamable HTTP for hosted tenants by default.

    Hosted MCP clients can keep a stale ``Mcp-Session-Id`` after a tenant restart.
    FastMCP's stateful transport returns ``Session not found`` before it processes
    a fresh initialize request, which makes the connector look permanently
    broken. Stateless HTTP creates a fresh transport per request and ignores stale
    client session IDs. Keep self-hosted/local defaults unchanged unless an env
    override is explicit.
    """
    for name in ("MARVIS_MCP_STATELESS_HTTP", "FASTMCP_STATELESS_HTTP"):
        raw = os.environ.get(name)
        if raw is not None:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
    return os.environ.get("DEPLOY_MODE", "").strip() == "hosted-tenant"


class _DropStatelessTerminateNone(logging.Filter):
    """Hide FastMCP's per-request stateless cleanup log, keep real terminations."""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.getMessage() != "Terminating session: None"


def _suppress_stateless_terminate_none_log() -> None:
    logger = logging.getLogger("mcp.server.streamable_http")
    if any(isinstance(f, _DropStatelessTerminateNone) for f in logger.filters):
        return
    logger.addFilter(_DropStatelessTerminateNone())


def main(transport: Literal["stdio", "http"] | None = None) -> None:
    """Run the MCP server.

    Mirror the user's ``~/.marvis/settings.yaml`` onto the API ``settings``
    singleton + project-index roots BEFORE serving any tool, so ``search`` /
    ``graph_*`` reach the SAME SQLite file the ``marvis`` CLI uses (instead of the
    bare ``db_path='console.db'`` default). Best-effort: no settings file → the
    API defaults / ``$PIR_DB_PATH`` env stand (parity with the CLI runtime).

    Then open the DB the SAME way the FastAPI lifespan does — ``init_pool()``
    creates the read-only pool AND the single dedicated writer. This is NOT
    optional for write tools: ``acquire_db()`` has a no-pool fallback (so reads
    answer even if the pool is absent), but ``acquire_write_db()`` raises
    ``"DB not initialized — call init_pool() first"`` when the writer is None.
    Without this, every mutator (``create_task``, ``update_task``, ...) failed —
    the agent could read the brain via MCP but never write it back.

    init + serve + close run in ONE event loop: aiosqlite connections are bound
    to the running loop, so opening the pool in a separate ``asyncio.run()`` pass
    before ``mcp.run()`` would leave the writer attached to an already-closed loop.

    Default transport stays stdio. HTTP is opt-in via
    ``MARVIS_MCP_TRANSPORT=http`` (or the caller passing ``transport="http"``) and
    binds a streamable-http endpoint to ``127.0.0.1:$MARVIS_MCP_PORT/mcp`` with
    per-tenant Bearer auth from ``TENANT_BEARER_TOKEN``.
    """
    import asyncio

    from core.api.config import settings
    from core.api.db import close_pool, init_pool
    from core.api.mcp import _adapter as mcp_adapter
    from core.api.runtime_settings import apply_marvis_settings

    apply_marvis_settings()
    _enforce_db_path_env_invariant()
    selected_transport = transport or _transport_from_env()
    mcp_adapter.set_mcp_transport_mode(selected_transport)
    mcp_adapter.set_tool_error_runtime(
        "mcp" if selected_transport == "stdio" else "fastmcp"
    )

    async def _serve() -> None:
        await init_pool(size=settings.db_pool_size)
        try:
            if selected_transport == "stdio":
                await mcp.run_stdio_async()
            else:
                http_mcp = _build_http_mcp()
                stateless_http = _http_stateless_from_env()
                if stateless_http:
                    _suppress_stateless_terminate_none_log()
                await http_mcp.run_http_async(
                    show_banner=False,
                    transport="streamable-http",
                    host=_http_host_from_env(),
                    port=_http_port_from_env(),
                    path="/mcp",
                    stateless_http=stateless_http,
                )
        finally:
            await close_pool()

    asyncio.run(_serve())


if __name__ == "__main__":
    main()
