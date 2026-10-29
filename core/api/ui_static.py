from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.api.retired_ui_contract import is_ui_request_path, retired_ui_decision

try:
    from core.hosted_control.retired_ui import RETIRED_PERSONAL_UI_HOST
except ImportError:  # Hosted-only coordinate, absent in public projection
    RETIRED_PERSONAL_UI_HOST = None

UI_DIR = Path(__file__).parent / "console_dist"
CANONICAL_CONSOLE_ORIGIN = "https://app.justaskmarvis.com"

UI_MISSING_MESSAGE = (
    "Marvis Console assets are not installed. Expected static export at "
    "core/api/console_dist/."
)

UI_CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "connect-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "frame-src 'self' blob:; "
    "frame-ancestors 'self'"
)


class SpaStaticFiles(StaticFiles):
    """StaticFiles with SPA fallback for client routes only."""

    def __init__(
        self,
        *args,
        index_path: Path | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        directory = Path(str(self.directory))
        self.index_path = index_path or directory / "index.html"

    async def get_response(self, path: str, scope) -> Response:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException:
            if Path(path).suffix:
                raise
            if not self.index_path.exists():
                return PlainTextResponse(UI_MISSING_MESSAGE, status_code=503)
            return FileResponse(self.index_path, status_code=200)


def mount_ui(app: FastAPI, ui_dir: Path = UI_DIR) -> None:
    """Mount the embedded Console export at /ui without crashing when absent."""
    mimetypes.add_type("text/javascript", ".js")
    mimetypes.add_type("application/manifest+json", ".webmanifest")

    if ui_dir.exists():
        app.mount(
            "/ui",
            SpaStaticFiles(
                directory=ui_dir,
                html=True,
                check_dir=False,
                index_path=ui_dir / "index.html",
            ),
            name="ui",
        )
        return

    async def missing_ui(_: Request) -> PlainTextResponse:
        return PlainTextResponse(UI_MISSING_MESSAGE, status_code=503)

    app.add_api_route("/ui", missing_ui, methods=["GET"], include_in_schema=False)
    app.add_api_route(
        "/ui/{path:path}",
        missing_ui,
        methods=["GET"],
        include_in_schema=False,
    )


def retired_ui_redirect(request: Request) -> Response | None:
    """Retire only the legacy personal browser entry point."""
    host = request.headers.get("host", "").lower().rstrip(".")
    if RETIRED_PERSONAL_UI_HOST is None or host != RETIRED_PERSONAL_UI_HOST:
        return None

    decision = retired_ui_decision(
        request.scope,
        target=f"{CANONICAL_CONSOLE_ORIGIN}/ui",
    )
    if decision is None:
        return None
    status, location = decision
    headers = {"Cache-Control": "no-store"}
    if status == 308 and location is not None:
        headers["Location"] = location
    elif status == 405:
        headers["Allow"] = "GET, HEAD"
    return Response(content=b"", status_code=status, headers=headers)


def apply_ui_response_headers(request: Request, response: Response) -> None:
    """Apply static-export headers that Next no longer serves for /ui."""
    path = request.url.path
    if not is_ui_request_path(path):
        return

    response.headers["Content-Security-Policy"] = UI_CONTENT_SECURITY_POLICY
    response.headers["Referrer-Policy"] = "same-origin"

    if path.endswith("/sw.js"):
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        response.headers["Service-Worker-Allowed"] = "/ui/"
        response.headers["Content-Type"] = "text/javascript; charset=utf-8"
    elif path.endswith("/manifest.webmanifest"):
        response.headers["Content-Type"] = "application/manifest+json"
