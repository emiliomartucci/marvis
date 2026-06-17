from __future__ import annotations

from pathlib import Path

import pytest

from core.api.services import tmux


class _FakeProc:
    def __init__(
        self, returncode: int = 0, stdout: bytes = b"", stderr: bytes = b""
    ) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self):
        return self._stdout, self._stderr


class _SlowProc(_FakeProc):
    async def communicate(self):
        await tmux.asyncio.sleep(1)
        return self._stdout, self._stderr


@pytest.mark.asyncio
async def test_create_session_starts_command_directly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    calls: list[tuple[str, ...]] = []
    envs: list[dict[str, str]] = []
    tmpdir = str(tmp_path / "marvisx-tmux")

    async def fake_exec(*args, **kwargs):
        envs.append(kwargs["env"])
        calls.append(tuple(args))
        if "has-session" in args:
            return _FakeProc(returncode=1, stderr=b"no sessions")
        return _FakeProc()

    monkeypatch.setattr(tmux.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tmux.os, "getuid", lambda: 1000)
    monkeypatch.setattr(tmux, "_marvisx_tmux_tmpdir", lambda: tmpdir)
    monkeypatch.setenv("PIR_JWT_SECRET", "must-not-leak")
    monkeypatch.delenv("TERM", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.delenv("LC_ALL", raising=False)

    ok = await tmux.create_session(
        "opencode-test",
        start_dir="/var/marvisx/workspace",
        start_command=(
            "export HOME=/var/marvisx && "
            "/data/pir/core/api/bin/opencode-launch.sh "
            "/var/marvisx/workspace --model openai/gpt-5.4"
        ),
    )

    assert ok is True
    assert calls[:2] == [
        ("/data/pir/tmux-proxy", "has-session", "-t", "=opencode-test"),
        ("/data/pir/tmux-proxy", "has-session", "-t", "=opencode-test"),
    ]
    assert envs[0]["TMUX_TMPDIR"] == tmpdir
    assert "TMUX_TMPDIR" not in envs[1]
    assert calls[2] == (
        "/usr/bin/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--setenv=HOME=/var/marvisx",
        f"--setenv=PATH={tmux.RUNTIME_PATH}",
        "--setenv=SHELL=/bin/bash",
        "--setenv=TERM=xterm-256color",
        "--setenv=LANG=C.UTF-8",
        "--setenv=XDG_RUNTIME_DIR=/run/user/1000",
        f"--setenv=TMUX_TMPDIR={tmpdir}",
        "--setenv=TMUX=",
        "/data/pir/tmux-proxy",
        "new-session",
        "-d",
        "-s",
        "opencode-test",
        "-c",
        "/var/marvisx/workspace",
        "/bin/bash",
        "-lc",
        "export TMUX_SESSION_NAME='opencode-test'; "
        "export HOME=/var/marvisx && "
        "/data/pir/core/api/bin/opencode-launch.sh "
        "/var/marvisx/workspace --model openai/gpt-5.4",
        ";",
        "set-option",
        "-g",
        "history-limit",
        "10000",
        ";",
        "set-option",
        "-t",
        "opencode-test",
        "history-limit",
        "10000",
    )
    assert envs[2]["TMUX_TMPDIR"] == tmpdir
    assert "PIR_JWT_SECRET" not in envs[2]


@pytest.mark.asyncio
async def test_create_session_treats_timeout_as_success_when_tmux_session_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    tmpdir = str(tmp_path / "marvisx-tmux")
    created = False

    async def fake_exec(*args, **kwargs):
        nonlocal created
        env = kwargs["env"]
        if "has-session" in args:
            is_marvisx_server = env.get("TMUX_TMPDIR") == tmpdir
            return _FakeProc(returncode=0 if created and is_marvisx_server else 1)
        if "new-session" in args:
            created = True
            return _SlowProc()
        return _FakeProc()

    monkeypatch.setattr(tmux.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tmux, "_marvisx_tmux_tmpdir", lambda: tmpdir)
    monkeypatch.setattr(tmux, "SUBPROCESS_TIMEOUT", 0.01)

    ok = await tmux.create_session(
        "timeout-ok",
        start_command="codex -m gpt-5.5",
    )

    assert ok is True


@pytest.mark.asyncio
async def test_list_sessions_includes_marvisx_and_legacy_servers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    tmpdir = str(tmp_path / "marvisx-tmux")

    async def fake_exec(*args, **kwargs):
        env = kwargs["env"]
        if env.get("TMUX_TMPDIR") == tmpdir:
            return _FakeProc(stdout=b"newone\t111\t0\n")
        return _FakeProc(stdout=b"oldone\t222\t1\n")

    monkeypatch.setattr(tmux.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tmux, "_marvisx_tmux_tmpdir", lambda: tmpdir)

    sessions = await tmux.list_sessions()

    assert sessions == [
        {
            "name": "newone",
            "created_epoch": "111",
            "attached": False,
            "tmux_server": "marvisx",
        },
        {
            "name": "oldone",
            "created_epoch": "222",
            "attached": True,
            "tmux_server": "legacy",
        },
    ]


@pytest.mark.asyncio
async def test_configure_history_limits_caps_existing_servers_and_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    tmpdir = str(tmp_path / "marvisx-tmux")
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    async def fake_exec(*args, **kwargs):
        env = kwargs["env"]
        calls.append((tuple(args), env))
        is_marvisx_server = env.get("TMUX_TMPDIR") == tmpdir
        if args[:2] == ("/data/pir/tmux-proxy", "list-sessions"):
            if is_marvisx_server:
                return _FakeProc(stdout=b"newone\t111\t0\n")
            return _FakeProc(stdout=b"oldone\t222\t1\n")
        return _FakeProc()

    monkeypatch.setattr(tmux.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tmux, "_marvisx_tmux_tmpdir", lambda: tmpdir)

    await tmux.configure_history_limits()

    direct_calls = [
        (args, env) for args, env in calls if args and args[0] == "/usr/bin/tmux"
    ]
    assert [args for args, _env in direct_calls] == [
        (
            "/usr/bin/tmux",
            "set-option",
            "-g",
            "history-limit",
            "10000",
        ),
        (
            "/usr/bin/tmux",
            "set-option",
            "-t",
            "newone",
            "history-limit",
            "10000",
        ),
        (
            "/usr/bin/tmux",
            "set-option",
            "-g",
            "history-limit",
            "10000",
        ),
        (
            "/usr/bin/tmux",
            "set-option",
            "-t",
            "oldone",
            "history-limit",
            "10000",
        ),
    ]
    assert direct_calls[0][1]["TMUX_TMPDIR"] == tmpdir
    assert direct_calls[1][1]["TMUX_TMPDIR"] == tmpdir
    assert "TMUX_TMPDIR" not in direct_calls[2][1]
    assert "TMUX_TMPDIR" not in direct_calls[3][1]


@pytest.mark.asyncio
async def test_configure_history_limits_never_starts_a_dead_server(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Regression for task 1e800849: a bare `set-option -g` on a dead server
    boots a tmux server in pir-api's cgroup. configure_history_limits must skip
    a server with no sessions and emit NO set-option call for it."""
    tmpdir = str(tmp_path / "marvisx-tmux")
    calls: list[tuple[str, ...]] = []

    async def fake_exec(*args, **kwargs):
        calls.append(tuple(args))
        if args[:2] == ("/data/pir/tmux-proxy", "list-sessions"):
            # Both servers report no sessions (down / empty).
            return _FakeProc(returncode=1, stderr=b"no server running")
        return _FakeProc()

    monkeypatch.setattr(tmux.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tmux, "_marvisx_tmux_tmpdir", lambda: tmpdir)

    await tmux.configure_history_limits()

    set_option_calls = [c for c in calls if c and c[0] == "/usr/bin/tmux"]
    assert set_option_calls == [], (
        "configure_history_limits must not run set-option on a dead server"
    )


@pytest.mark.asyncio
async def test_kill_session_targets_legacy_server_when_session_is_legacy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    tmpdir = str(tmp_path / "marvisx-tmux")
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    async def fake_exec(*args, **kwargs):
        env = kwargs["env"]
        calls.append((tuple(args), env))
        if "has-session" in args:
            exists_on_legacy = env.get("TMUX_TMPDIR") is None
            return _FakeProc(returncode=0 if exists_on_legacy else 1)
        return _FakeProc()

    monkeypatch.setattr(tmux.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tmux, "_marvisx_tmux_tmpdir", lambda: tmpdir)

    ok = await tmux.kill_session("oldone")

    assert ok is True
    assert calls[-1][0] == (
        "/data/pir/tmux-proxy",
        "kill-session",
        "-t",
        "=oldone",
    )
    assert "TMUX_TMPDIR" not in calls[-1][1]


def test_pir_api_service_does_not_kill_child_sessions_on_restart():
    unit = Path(__file__).resolve().parents[2] / "deploy" / "pir-api.service"
    text = unit.read_text()

    assert "KillMode=process" in text
    assert "KillMode=mixed" not in text
