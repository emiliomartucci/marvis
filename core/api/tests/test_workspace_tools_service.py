from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core.api.services import workspace_tools as ws
from core.api.use_cases._errors import (
    AuthorizationError,
    NotFoundError,
    ServiceUnavailableError,
    ValidationError,
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def test_read_file_returns_content_hash_and_freshness(tmp_path: Path) -> None:
    path = tmp_path / "marvisx" / "context.md"
    path.parent.mkdir()
    path.write_text("hello\n", encoding="utf-8")

    out = ws.read_file("marvisx/context.md", projects_root=tmp_path)

    assert out["content"] == "hello\n"
    assert out["path"] == "marvisx/context.md"
    assert len(out["sha256"]) == 64
    assert out["freshness"]["current_sha256"] == out["sha256"]
    assert out["freshness"]["freshness_status"] == "freshness_unavailable"


def test_read_file_supports_explicit_projects_prefix(tmp_path: Path) -> None:
    path = tmp_path / "marvisx" / "context.md"
    path.parent.mkdir()
    path.write_text("hello\n", encoding="utf-8")

    out = ws.read_file("projects/marvisx/context.md", projects_root=tmp_path)

    assert out["content"] == "hello\n"
    assert out["path"] == "projects/marvisx/context.md"


def test_read_file_supports_repos_virtual_root(tmp_path: Path) -> None:
    tenant_root = tmp_path / "tenant"
    projects_root = tenant_root / "projects"
    repo_file = tenant_root / "repos" / "marvisx" / "README.md"
    projects_root.mkdir(parents=True)
    repo_file.parent.mkdir(parents=True)
    repo_file.write_text("repo\n", encoding="utf-8")

    out = ws.read_file("repos/marvisx/README.md", projects_root=projects_root)

    assert out["content"] == "repo\n"
    assert out["path"] == "repos/marvisx/README.md"


@pytest.mark.parametrize(
    "bad_path",
    ["/etc/passwd", "../outside.md", "a/../b", "a/\x00/b", ".env", "p/.ssh/config", "p/key.pem", "p/.git/config"],
)
def test_resolver_rejects_escape_secret_and_null_paths(tmp_path: Path, bad_path: str) -> None:
    with pytest.raises((ValidationError, AuthorizationError)):
        ws.read_file(bad_path, projects_root=tmp_path)


def test_resolver_rejects_symlink_before_file_io(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-workspace.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.md"
    link.symlink_to(outside)

    with pytest.raises(AuthorizationError) as exc:
        ws.read_file("link.md", projects_root=tmp_path)

    assert exc.value.code == "workspace_symlink_denied"


def test_write_file_requires_hash_for_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("old", encoding="utf-8")

    with pytest.raises(ws.WorkspaceError):
        ws.write_file("doc.md", "new", projects_root=tmp_path)

    assert path.read_text(encoding="utf-8") == "old"


def test_write_file_rejects_stale_hash_and_preserves_file(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("old", encoding="utf-8")

    with pytest.raises(ws.WorkspaceError) as exc:
        ws.write_file(
            "doc.md",
            "new",
            projects_root=tmp_path,
            if_match_sha256="0" * 64,
        )

    assert exc.value.code == "conflict"
    assert "current_sha256" in exc.value.message
    assert path.read_text(encoding="utf-8") == "old"


def test_write_file_blocks_when_storage_full(tmp_path: Path) -> None:
    guard = ws.WorkspaceStorageGuard(
        quota_mode="record-only",
        used_bytes=95,
        quota_bytes=100,
        backpressure_percent=90,
        full_percent=95,
    )

    with pytest.raises(ws.WorkspaceError) as exc:
        ws.write_file("doc.md", "new", projects_root=tmp_path, storage_guard=guard)

    assert exc.value.code == "storage_full"
    assert not (tmp_path / "doc.md").exists()


def test_write_file_blocks_expansion_when_storage_backpressure(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("hello", encoding="utf-8")
    current = ws.read_file("doc.md", projects_root=tmp_path)["sha256"]
    guard = ws.WorkspaceStorageGuard(
        quota_mode="record-only",
        used_bytes=90,
        quota_bytes=100,
        backpressure_percent=90,
        full_percent=95,
    )

    with pytest.raises(ws.WorkspaceError) as exc:
        ws.write_file(
            "doc.md",
            "hello!!!",
            projects_root=tmp_path,
            if_match_sha256=current,
            storage_guard=guard,
        )

    assert exc.value.code == "storage_backpressure"
    assert path.read_text(encoding="utf-8") == "hello"


def test_edit_allows_shrink_when_storage_backpressure(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("hello world", encoding="utf-8")
    current = ws.read_file("doc.md", projects_root=tmp_path)["sha256"]
    guard = ws.WorkspaceStorageGuard(
        quota_mode="record-only",
        used_bytes=90,
        quota_bytes=100,
        backpressure_percent=90,
        full_percent=95,
    )

    out = ws.edit(
        "doc.md",
        " world",
        "",
        projects_root=tmp_path,
        if_match_sha256=current,
        storage_guard=guard,
    )

    assert out["ok"] is True
    assert path.read_text(encoding="utf-8") == "hello"


def test_read_file_ignores_storage_guard_env_full(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_root = tmp_path / "tenant"
    projects_root = tenant_root / "projects"
    projects_root.mkdir(parents=True)
    (projects_root / "doc.md").write_text("hello", encoding="utf-8")
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "tenants": {
                    "emilio": {
                        "storage": {
                            "quota_mode": "record-only",
                            "last_usage_bytes": 95,
                            "quota_bytes": 100,
                            "backpressure_percent": 90,
                            "full_percent": 95,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TENANT_ID", "emilio")
    monkeypatch.setenv("MARVIS_TENANT_REGISTRY_PATH", str(registry))

    out = ws.read_file("doc.md", projects_root=projects_root)

    assert out["content"] == "hello"


def test_storage_guard_from_env_reads_tenant_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_root = tmp_path / "tenant"
    projects_root = tenant_root / "projects"
    projects_root.mkdir(parents=True)
    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "tenants": {
                    "emilio": {
                        "storage": {
                            "quota_mode": "record-only",
                            "last_usage_bytes": 12,
                            "last_usage_snapshot_at": _iso(datetime.now(UTC)),
                            "quota_bytes": 100,
                            "warn_percent": 75,
                            "backpressure_percent": 88,
                            "full_percent": 96,
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TENANT_ID", "emilio")
    monkeypatch.setenv("MARVIS_TENANT_REGISTRY_PATH", str(registry))

    guard = ws.storage_guard_from_env(projects_root)

    assert guard == ws.WorkspaceStorageGuard(
        quota_mode="record-only",
        used_bytes=12,
        quota_bytes=100,
        snapshot_at=guard.snapshot_at,
        snapshot_age_seconds=guard.snapshot_age_seconds,
        snapshot_stale=False,
        warn_percent=75,
        backpressure_percent=88,
        full_percent=96,
        source=f"registry:{registry}:emilio:{tenant_root}",
    )
    assert guard.snapshot_at is not None
    assert guard.snapshot_age_seconds is not None
    assert guard.snapshot_age_seconds < 10


def test_write_file_fails_closed_when_storage_registry_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TENANT_ID", "emilio")
    monkeypatch.setenv("MARVIS_TENANT_REGISTRY_PATH", str(tmp_path / "missing.json"))

    with pytest.raises(ws.WorkspaceError) as exc:
        ws.write_file("doc.md", "hello", projects_root=tmp_path)

    assert exc.value.code == "storage_guard_unavailable"
    assert not (tmp_path / "doc.md").exists()


def test_write_file_blocks_when_storage_snapshot_missing(tmp_path: Path) -> None:
    guard = ws.WorkspaceStorageGuard(
        quota_mode="record-only",
        used_bytes=None,
        quota_bytes=100,
    )

    with pytest.raises(ws.WorkspaceError) as exc:
        ws.write_file("doc.md", "hello", projects_root=tmp_path, storage_guard=guard)

    assert exc.value.code == "storage_usage_unknown"
    assert not (tmp_path / "doc.md").exists()


def test_write_file_blocks_when_storage_snapshot_stale(tmp_path: Path) -> None:
    stale_at = _iso(datetime.now(UTC) - timedelta(days=2))
    guard = ws.WorkspaceStorageGuard(
        quota_mode="record-only",
        used_bytes=12,
        quota_bytes=100,
        snapshot_at=stale_at,
        snapshot_age_seconds=2 * 24 * 60 * 60,
        snapshot_stale=True,
    )

    with pytest.raises(ws.WorkspaceError) as exc:
        ws.write_file("doc.md", "hello", projects_root=tmp_path, storage_guard=guard)

    assert exc.value.code == "storage_usage_stale"
    assert not (tmp_path / "doc.md").exists()


def test_write_file_new_parent_requires_create_parent(tmp_path: Path) -> None:
    with pytest.raises(NotFoundError):
        ws.write_file("new/doc.md", "hello", projects_root=tmp_path)

    out = ws.write_file(
        "new/doc.md",
        "hello",
        projects_root=tmp_path,
        create_parent=True,
    )

    assert out["created"] is True
    assert (tmp_path / "new" / "doc.md").read_text(encoding="utf-8") == "hello"


def test_edit_blocks_ambiguous_match(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("x one x two", encoding="utf-8")

    with pytest.raises(ws.WorkspaceError) as exc:
        ws.edit("doc.md", "x", "y", projects_root=tmp_path)

    assert exc.value.code == "ambiguous_edit"
    assert path.read_text(encoding="utf-8") == "x one x two"


def test_edit_single_match_updates_atomically(tmp_path: Path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("alpha beta", encoding="utf-8")
    original = ws.read_file("doc.md", projects_root=tmp_path)["sha256"]

    out = ws.edit(
        "doc.md",
        "beta",
        "gamma",
        projects_root=tmp_path,
        if_match_sha256=original,
    )

    assert out["replacements"] == 1
    assert path.read_text(encoding="utf-8") == "alpha gamma"


def test_directory_tree_caps_entries_and_skips_denied(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.md").write_text("1", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1", encoding="utf-8")
    (tmp_path / "b").mkdir()

    out = ws.directory_tree("", projects_root=tmp_path, max_depth=2, max_entries=2)

    assert out["truncated"] is True
    assert all(".env" not in item["path"] for item in out["entries"])


def test_directory_tree_lists_repos_virtual_root(tmp_path: Path) -> None:
    tenant_root = tmp_path / "tenant"
    projects_root = tenant_root / "projects"
    repo = tenant_root / "repos" / "marvisx"
    projects_root.mkdir(parents=True)
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("repo\n", encoding="utf-8")

    out = ws.directory_tree("repos", projects_root=projects_root, max_depth=2)

    assert out["path"] == "repos"
    assert any(item["path"] == "repos/marvisx" for item in out["entries"])
    assert any(item["path"] == "repos/marvisx/README.md" for item in out["entries"])


def test_repos_virtual_root_requires_existing_repos_dir(tmp_path: Path) -> None:
    projects_root = tmp_path / "tenant" / "projects"
    projects_root.mkdir(parents=True)

    with pytest.raises(ServiceUnavailableError) as exc:
        ws.directory_tree("repos", projects_root=projects_root)

    assert exc.value.code == "repos_root_unavailable"


def test_grep_missing_rg_is_clear(tmp_path: Path) -> None:
    with pytest.raises(ServiceUnavailableError) as exc:
        ws.grep("hello", projects_root=tmp_path, rg_path="")

    assert exc.value.code == "grep_backend_unavailable"


def test_grep_parses_matches_from_runner(tmp_path: Path) -> None:
    target = tmp_path / "proj" / "a.md"
    target.parent.mkdir()
    target.write_text("hello\n", encoding="utf-8")

    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=f"{target}:1:hello\n",
            stderr="",
        )

    out = ws.grep(
        "hello",
        projects_root=tmp_path,
        rg_path="/usr/bin/rg",
        runner=fake_runner,
    )

    assert out["matched"] is True
    assert out["matches"] == [{"path": "proj/a.md", "line": 1, "text": "hello"}]


def test_write_audit_required_blocks_without_sink(tmp_path: Path) -> None:
    policy = ws.WorkspacePolicy(require_audit_for_writes=True)

    with pytest.raises(ServiceUnavailableError) as exc:
        ws.write_file("doc.md", "hello", projects_root=tmp_path, policy=policy)

    assert exc.value.code == "audit_unavailable"


def test_run_bash_requires_shell_policy(tmp_path: Path) -> None:
    with pytest.raises(AuthorizationError) as exc:
        ws.run_bash("pwd", projects_root=tmp_path)

    assert exc.value.code == "workspace_shell_disabled"


def test_run_bash_executes_guarded_command_in_workspace_cwd(tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    calls = {}

    def fake_runner(*args, **kwargs):
        calls["args"] = args[0]
        calls["cwd"] = kwargs["cwd"]
        calls["env"] = kwargs["env"]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    out = ws.run_bash(
        "printf ok",
        cwd="proj",
        projects_root=tmp_path,
        policy=ws.WorkspacePolicy(can_shell=True),
        runner=fake_runner,
    )

    assert out["ok"] is True
    assert out["returncode"] == 0
    assert out["stdout"] == "ok\n"
    assert out["cwd"] == "proj"
    assert out["safety"]["filesystem_jail"] is False
    assert calls["args"] == ["/bin/bash", "-lc", "printf ok"]
    assert calls["cwd"] == str(cwd)
    assert calls["env"]["HOME"] == str(tmp_path)
    assert calls["env"]["PWD"] == str(cwd)


def test_run_bash_executes_inside_repos_virtual_root(tmp_path: Path) -> None:
    tenant_root = tmp_path / "tenant"
    projects_root = tenant_root / "projects"
    repo = tenant_root / "repos" / "marvisx"
    projects_root.mkdir(parents=True)
    repo.mkdir(parents=True)
    calls = {}

    def fake_runner(*args, **kwargs):
        calls["cwd"] = kwargs["cwd"]
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="repo\n",
            stderr="",
        )

    out = ws.run_bash(
        "git status",
        cwd="repos/marvisx",
        projects_root=projects_root,
        policy=ws.WorkspacePolicy(can_shell=True),
        runner=fake_runner,
    )

    assert out["ok"] is True
    assert out["cwd"] == "repos/marvisx"
    assert calls["cwd"] == str(repo)


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/passwd",
        "cat ../secret.txt",
        "echo $HOME",
        "echo ok && echo bad",
        "python3 -c 'print(1)'",
        "ssh example.com",
        "FOO=bar pytest",
    ],
)
def test_run_bash_rejects_unsafe_commands(tmp_path: Path, command: str) -> None:
    with pytest.raises((AuthorizationError, ValidationError)):
        ws.run_bash(
            command,
            projects_root=tmp_path,
            policy=ws.WorkspacePolicy(can_shell=True),
        )


def test_run_bash_returns_nonzero_without_tool_error(tmp_path: Path) -> None:
    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=2,
            stdout="",
            stderr="failed\n",
        )

    out = ws.run_bash(
        "git status",
        projects_root=tmp_path,
        policy=ws.WorkspacePolicy(can_shell=True),
        runner=fake_runner,
    )

    assert out["ok"] is False
    assert out["returncode"] == 2
    assert out["stderr"] == "failed\n"


def test_run_bash_timeout_returns_bounded_result(tmp_path: Path) -> None:
    def fake_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], timeout=0.1, output="partial", stderr=None)

    out = ws.run_bash(
        "git status",
        projects_root=tmp_path,
        policy=ws.WorkspacePolicy(can_shell=True),
        runner=fake_runner,
        timeout_ms=100,
    )

    assert out["ok"] is False
    assert out["timed_out"] is True
    assert out["returncode"] is None
    assert out["stdout"] == "partial"
    assert "timed out" in out["stderr"]


def test_run_bash_caps_output(tmp_path: Path) -> None:
    def fake_runner(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout="abcdef",
            stderr="123456",
        )

    out = ws.run_bash(
        "printf ok",
        projects_root=tmp_path,
        policy=ws.WorkspacePolicy(can_shell=True),
        runner=fake_runner,
        max_output_bytes=3,
    )

    assert out["stdout"] == "abc"
    assert out["stderr"] == "123"
    assert out["stdout_truncated"] is True
    assert out["stderr_truncated"] is True
