"""Regression coverage for Windows-safe shared lock imports."""
from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

import core.platform.locking as locking


def test_cli_shared_modules_import_when_fcntl_is_unavailable() -> None:
    """Windows has no ``fcntl``; importing CLI-reachable modules must still work."""
    code = r'''
import builtins

real_import = builtins.__import__

def guarded_import(name, globals=None, locals=None, fromlist=(), level=0):
    if name == "fcntl":
        raise ModuleNotFoundError("No module named 'fcntl'")
    return real_import(name, globals, locals, fromlist, level)

builtins.__import__ = guarded_import
import core.api.db
import core.api.use_cases.projects
'''
    result = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_windows_lock_backend_acquires_without_fcntl(monkeypatch, tmp_path) -> None:
    calls: list[object] = []

    class StubFileLock:
        def __init__(self, path, *, mode, timeout, thread_local):
            calls.append((path, mode, timeout, thread_local))

        def acquire(self):
            calls.append("acquire")

        def release(self):
            calls.append("release")

    monkeypatch.setattr(locking.os, "name", "nt")
    monkeypatch.setitem(
        sys.modules,
        "filelock",
        SimpleNamespace(FileLock=StubFileLock),
    )

    lock_path = tmp_path / "portable.lock"
    with locking.exclusive_file_lock(lock_path, mode=0o600, nofollow=True):
        calls.append("body")

    assert calls == [
        (str(lock_path), 0o600, -1, True),
        "acquire",
        "body",
        "release",
    ]


def test_posix_lock_keeps_nofollow_hardening(tmp_path) -> None:
    if locking.os.name != "posix":
        pytest.skip("POSIX-specific nofollow contract")
    target = tmp_path / "target"
    target.touch()
    symlink = tmp_path / "portable.lock"
    symlink.symlink_to(target)

    with pytest.raises(locking.LockUnavailableError):
        with locking.exclusive_file_lock(symlink, nofollow=True):
            pass
