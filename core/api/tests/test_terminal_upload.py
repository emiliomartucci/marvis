from pathlib import Path

import core.api.routers.sessions as sessions_router

from core.api.terminal import FALLBACK_UPLOAD_DIR, _resolve_upload_target


def test_resolve_upload_target_prefers_known_project_slug():
    project_slug, upload_dir = _resolve_upload_target(
        "marvisx", "/var/marvisx/workspace"
    )

    assert project_slug == "marvisx"
    assert upload_dir == Path("/data/projects/marvisx/input")


def test_resolve_upload_target_recovers_project_from_workspace_cwd(monkeypatch):
    monkeypatch.setattr(
        sessions_router,
        "_detect_project_from_path",
        lambda cwd: "marvisx" if cwd == "/var/marvisx/workspace" else None,
    )

    project_slug, upload_dir = _resolve_upload_target(None, "/var/marvisx/workspace")

    assert project_slug == "marvisx"
    assert upload_dir == Path("/data/projects/marvisx/input")


def test_resolve_upload_target_falls_back_when_project_is_unknown():
    project_slug, upload_dir = _resolve_upload_target(None, "/tmp/random-session-dir")

    assert project_slug is None
    assert upload_dir == FALLBACK_UPLOAD_DIR
