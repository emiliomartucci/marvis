from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import Response
from starlette.requests import Request

from core.api import security
from core.api.ui_static import apply_ui_response_headers, mount_ui


def _app_with_ui(ui_dir: Path) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def ui_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        apply_ui_response_headers(request, response)
        return response

    mount_ui(app, ui_dir)
    return app


def test_spa_static_files_serves_index_for_client_route(tmp_path: Path) -> None:
    ui_dir = tmp_path / "console_dist"
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("<html>console shell</html>")
    (ui_dir / "asset.txt").write_text("asset")

    client = TestClient(_app_with_ui(ui_dir))

    response = client.get("/ui/rotta-client")

    assert response.status_code == 200
    assert response.text == "<html>console shell</html>"
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "connect-src 'self'" in csp
    assert "https://" not in csp
    assert "wss://" not in csp


def test_spa_static_files_preserves_missing_asset_404(tmp_path: Path) -> None:
    ui_dir = tmp_path / "console_dist"
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("<html>console shell</html>")

    client = TestClient(_app_with_ui(ui_dir))

    response = client.get("/ui/manca.js")

    assert response.status_code == 404


def test_ui_static_headers_force_sw_and_manifest_mime(tmp_path: Path) -> None:
    ui_dir = tmp_path / "console_dist"
    ui_dir.mkdir()
    (ui_dir / "index.html").write_text("<html>console shell</html>")
    (ui_dir / "sw.js").write_text("self.addEventListener('install', () => undefined);")
    (ui_dir / "manifest.webmanifest").write_text('{"name":"Marvis"}')

    client = TestClient(_app_with_ui(ui_dir))

    sw_response = client.get("/ui/sw.js")
    manifest_response = client.get("/ui/manifest.webmanifest")

    assert sw_response.status_code == 200
    assert sw_response.headers["Content-Type"].startswith("text/javascript")
    assert sw_response.headers["Service-Worker-Allowed"] == "/ui/"
    assert manifest_response.status_code == 200
    assert manifest_response.headers["Content-Type"] == "application/manifest+json"


def test_ui_missing_dist_returns_clear_message(tmp_path: Path) -> None:
    client = TestClient(_app_with_ui(tmp_path / "missing-console-dist"))

    response = client.get("/ui/")

    assert response.status_code == 503
    assert "Console assets are not installed" in response.text


def _request(client_host: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/me",
            "headers": [],
            "client": (client_host, 12345),
            "server": ("127.0.0.1", 8100),
            "scheme": "http",
        }
    )


@pytest.mark.anyio
async def test_local_single_user_mode_authenticates_loopback(monkeypatch) -> None:
    monkeypatch.setattr(security.settings, "pir_admin_password_hash", "")

    user = await security.get_current_user(_request("127.0.0.1"), db=None)

    assert user.username == "local"
    assert user.user_id == "local"
    assert user.system_role == "operator"
    assert user.user_type == "human"


@pytest.mark.anyio
async def test_local_single_user_mode_dual_auth_bypasses_login(monkeypatch) -> None:
    monkeypatch.setattr(security.settings, "pir_admin_password_hash", "")

    user = await security.get_current_user_or_agent(_request("127.0.0.1"), db=None)

    assert user.username == "local"
    assert user.system_role == "operator"


@pytest.mark.anyio
async def test_local_single_user_mode_rejects_non_loopback(monkeypatch) -> None:
    monkeypatch.setattr(security.settings, "pir_admin_password_hash", "")

    with pytest.raises(security.HTTPException) as exc:
        await security.get_current_user(_request("10.0.0.5"), db=None)

    assert exc.value.status_code == 403


@pytest.mark.anyio
async def test_password_configured_mode_still_requires_cookie(monkeypatch) -> None:
    monkeypatch.setattr(security.settings, "pir_admin_password_hash", "configured")

    with pytest.raises(security.HTTPException) as exc:
        await security.get_current_user(_request("127.0.0.1"), db=None)

    assert exc.value.status_code == 401
