from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import HTTPException


def test_validate_path_allows_whitelisted_workspace_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.api.routers import finder

    root = tmp_path / "data"
    root.mkdir()
    workspace = tmp_path / "workspace"
    (workspace / "docs").mkdir(parents=True)
    (root / "workspace").symlink_to(workspace, target_is_directory=True)

    monkeypatch.setattr(finder.settings, "finder_root", str(root))
    monkeypatch.setattr(finder.settings, "finder_symlink_whitelist", [str(workspace)])
    monkeypatch.setattr(
        finder.settings, "finder_hidden_patterns", [".ssh", "node_modules"]
    )

    target = finder._validate_path("workspace/docs")

    assert target == (workspace / "docs").resolve()
    assert finder._rel_path(target) == "workspace/docs"


def test_validate_path_blocks_non_whitelisted_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.api.routers import finder

    root = tmp_path / "data"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (root / "workspace").symlink_to(workspace, target_is_directory=True)

    monkeypatch.setattr(finder.settings, "finder_root", str(root))
    monkeypatch.setattr(finder.settings, "finder_symlink_whitelist", [])
    monkeypatch.setattr(finder.settings, "finder_hidden_patterns", [])

    with pytest.raises(HTTPException) as exc_info:
        finder._validate_path("workspace")

    assert exc_info.value.status_code == 403


def test_validate_path_blocks_parent_traversal_through_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core.api.routers import finder

    root = tmp_path / "data"
    root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (root / "workspace").symlink_to(workspace, target_is_directory=True)

    monkeypatch.setattr(finder.settings, "finder_root", str(root))
    monkeypatch.setattr(finder.settings, "finder_symlink_whitelist", [str(workspace)])
    monkeypatch.setattr(finder.settings, "finder_hidden_patterns", [])

    with pytest.raises(HTTPException) as exc_info:
        finder._validate_path("workspace/../secrets.txt")

    assert exc_info.value.status_code == 403
