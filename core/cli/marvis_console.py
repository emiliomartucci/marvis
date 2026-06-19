"""Local Console launcher + optional OS autostart.

``marvis console`` is intentionally thin: verify the packaged GUI export exists,
ensure the local API is answering on 127.0.0.1:8100, then open /ui/. The API
server process is started through this module's ``serve`` entrypoint so both
manual launch and autostart apply the same settings.yaml environment before
uvicorn imports ``core.api.main``.
"""
from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from dataclasses import dataclass
from getpass import getuser
from pathlib import Path
from typing import Any

import typer

from core.cli._runtime_ctx import console, emit, err_console
from core.platform import current_uid

_PANEL_CONSOLE = "Console"
_PANEL_AUTOSTART = "Autostart"

_HOST = "127.0.0.1"
_PORT = 8100
_UI_PATH = "/ui/"
_HEALTH_PATHS = ("/healthz", "/health")
_LABEL = "com.marvis.server"
_SYSTEMD_UNIT = "marvis.service"
_WINDOWS_TASK = "Marvis Server"


@dataclass(frozen=True)
class ApiProbe:
    listening: bool
    ours: bool
    path: str | None = None
    detail: str = ""
    brain: str | None = None


def register(app: typer.Typer) -> None:
    """Attach ``console`` and ``autostart`` onto an existing Typer app."""
    app.command("console", rich_help_panel=_PANEL_CONSOLE)(console_cmd)
    app.add_typer(
        autostart_app,
        name="autostart",
        rich_help_panel=_PANEL_AUTOSTART,
        help="Opt-in local API autostart (enable / disable / status).",
    )


autostart_app = typer.Typer(add_completion=False, no_args_is_help=True)


# ---------------------------------------------------------------------------
# Packaged GUI assets
# ---------------------------------------------------------------------------


def _console_dist_available() -> tuple[bool, str]:
    """Return whether the installed package contains the static GUI export."""
    try:
        import importlib.resources as res

        root = res.files("core.api").joinpath("console_dist")
        index = root.joinpath("index.html")
        if not index.is_file():
            return False, "core/api/console_dist/index.html is missing"
        return True, str(root)
    except Exception as exc:  # noqa: BLE001 - clear CLI degradation, no traceback
        return False, f"could not inspect core.api console_dist: {exc}"


def _print_missing_console_dist() -> None:
    err_console.print(
        "[red]GUI non inclusa in questa build.[/red] "
        "Reinstall a Marvis release that includes the local Console export."
    )


# ---------------------------------------------------------------------------
# Local API process
# ---------------------------------------------------------------------------


def _ui_url() -> str:
    return f"http://{_HOST}:{_PORT}{_UI_PATH}"


def _base_url() -> str:
    return f"http://{_HOST}:{_PORT}"


def _settings_yaml_path() -> Path:
    settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
    if settings_path:
        return Path(settings_path).expanduser()
    vault_dir = os.environ.get("MARVIS_VAULT_DIR")
    base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
    return base / "settings.yaml"


def _settings_env() -> dict[str, str]:
    """Derive API env overrides from settings.yaml without importing core.api."""
    env: dict[str, str] = {}
    path = _settings_yaml_path()
    if not path.is_file():
        return env
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return env
    if not isinstance(data, dict):
        return env
    storage = data.get("storage") or {}
    if not isinstance(storage, dict):
        return env
    db_path = storage.get("db_path")
    projects_root = storage.get("projects_root")
    if db_path:
        env["MARVIS_DB_PATH"] = str(Path(str(db_path)).expanduser())
    if projects_root:
        env["MARVIS_PROJECTS_ROOT"] = str(Path(str(projects_root)).expanduser())
    return env


def _prepare_api_environment() -> None:
    """Apply settings.yaml-derived env before uvicorn imports the API app."""
    for key, value in _settings_env().items():
        os.environ.setdefault(key, value)


def _api_command() -> list[str]:
    return [sys.executable, "-m", "core.cli.marvis_console", "serve"]


def _log_path() -> Path:
    base = Path(os.environ.get("MARVIS_VAULT_DIR", Path.home() / ".marvis")).expanduser()
    logs = base / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "console-api.log"


def _port_open(host: str = _HOST, port: int = _PORT, *, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _parse_health(body: bytes) -> dict | None:
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return None
    if isinstance(payload, dict) and payload.get("status") == "ok":
        return payload
    return None


def _probe_api(*, timeout: float = 1.0) -> ApiProbe:
    """Probe /healthz first, then /health for current API compatibility."""
    for path in _HEALTH_PATHS:
        url = f"{_base_url()}{path}"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
                body = resp.read(1024)
                payload = _parse_health(body) if 200 <= resp.status < 300 else None
                if payload is not None:
                    brain = payload.get("brain")
                    return ApiProbe(
                        True,
                        True,
                        path,
                        f"Marvis API answered {path}",
                        brain=brain if isinstance(brain, str) else None,
                    )
        except urllib.error.HTTPError:
            continue
        except (urllib.error.URLError, TimeoutError, OSError):
            continue

    if _port_open():
        return ApiProbe(
            True,
            False,
            None,
            "port 8100 is in use but /healthz and /health did not identify Marvis",
        )
    return ApiProbe(False, False, None, "port 8100 is free")


def _run_dir() -> Path:
    base = Path(os.environ.get("MARVIS_VAULT_DIR", Path.home() / ".marvis")).expanduser()
    run_dir = base / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def _pid_path() -> Path:
    return _run_dir() / "api.pid"


def write_api_pidfile() -> None:
    """Record the serve process pid so launchers can stop a stale API (gh #16)."""
    try:
        path = _pid_path()
        path.write_text(str(os.getpid()), encoding="utf-8")
        import atexit

        atexit.register(_remove_api_pidfile)
    except OSError:
        pass


def _remove_api_pidfile() -> None:
    try:
        _pid_path().unlink(missing_ok=True)
    except OSError:
        pass


def _brain_fingerprint_for(db_path: str) -> str:
    import hashlib

    resolved = str(Path(db_path).expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def _expected_brain_fingerprint() -> str | None:
    """Fingerprint of the DB this launch SHOULD serve, or None if unknown.

    Mirrors the API's /health "brain" field: sha256 of the resolved db_path.
    Postgres backends have no db_path -> no fingerprint -> no mismatch check.
    """
    db_path = _settings_env().get("MARVIS_DB_PATH") or os.environ.get("MARVIS_DB_PATH")
    if not db_path:
        try:
            from core.platform import db_default_path

            db_path = str(db_default_path())
        except Exception:
            return None
    return _brain_fingerprint_for(db_path)


def _stop_api_process(*, timeout_seconds: float = 6.0) -> tuple[bool, str]:
    """Stop the pidfile-recorded API process. Returns (stopped, detail)."""
    import signal

    path = _pid_path()
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False, "no pidfile — the API was not started by this marvis version"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_api_pidfile()
        return True, f"process {pid} already gone"
    except OSError as exc:
        return False, f"could not signal pid {pid}: {exc}"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if not _port_open():
            _remove_api_pidfile()
            return True, f"stopped pid {pid}"
        time.sleep(0.3)
    return False, f"pid {pid} did not release port {_PORT} within {timeout_seconds}s"


def _start_api_process() -> tuple[subprocess.Popen, Path]:
    log = _log_path()
    env = os.environ.copy()
    env.update(_settings_env())
    log_handle = log.open("a", encoding="utf-8")
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        _api_command(),
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=(os.name != "nt"),
        creationflags=flags,
    )
    log_handle.close()
    return proc, log


def _wait_for_api(proc: subprocess.Popen, *, timeout_seconds: float = 25.0) -> ApiProbe:
    deadline = time.monotonic() + timeout_seconds
    last = ApiProbe(False, False, None, "not probed yet")
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return ApiProbe(False, False, None, f"API process exited with {proc.returncode}")
        last = _probe_api(timeout=1.0)
        if last.ours:
            return last
        time.sleep(0.5)
    return last


def _ensure_api_running() -> tuple[bool, str | None]:
    probe = _probe_api()
    if probe.ours:
        expected = _expected_brain_fingerprint()
        if probe.brain and expected and probe.brain != expected:
            # gh issue #16: a previous API instance survived and serves a
            # DIFFERENT brain. Reusing it silently shows the wrong data.
            console.print(
                "[yellow]A Marvis API on port 8100 is serving a different brain "
                "than the current settings. Restarting it...[/yellow]"
            )
            stopped, detail = _stop_api_process()
            if not stopped:
                err_console.print(
                    f"[red]Could not stop the stale Marvis API ({detail}).[/red]\n"
                    "Stop it manually, then relaunch `marvis console`."
                )
                return False, None
        else:
            return True, None
    if probe.listening and not probe.ours:
        err_console.print(
            "[red]Port 8100 is already in use, but it is not the Marvis API.[/red]\n"
            "Marvis probes GET /healthz first, then /health. Stop the process using "
            "127.0.0.1:8100 and retry."
        )
        return False, None

    proc, log = _start_api_process()
    ready = _wait_for_api(proc)
    if ready.ours:
        return True, str(log)
    err_console.print(
        "[red]Marvis API did not become ready on 127.0.0.1:8100.[/red]\n"
        f"Log file: {log}\n"
        f"Last probe: {ready.detail}"
    )
    return False, str(log)


def console_cmd(
    no_open: bool = typer.Option(
        False,
        "--no-open",
        help="Start/verify the local API and print the URL without opening a browser.",
    ),
    stop: bool = typer.Option(
        False,
        "--stop",
        help="Stop the local API started by marvis (pidfile under ~/.marvis/run/).",
    ),
) -> None:
    """Open the packaged local GUI at http://127.0.0.1:8100/ui/."""
    if stop:
        stopped, detail = _stop_api_process()
        if stopped:
            console.print(f"Marvis API stopped ({detail}).")
            raise typer.Exit(0)
        err_console.print(f"[red]Stop failed: {detail}[/red]")
        raise typer.Exit(1)

    available, _detail = _console_dist_available()
    if not available:
        _print_missing_console_dist()
        raise typer.Exit(1)

    ok, log = _ensure_api_running()
    if not ok:
        raise typer.Exit(1)

    url = _ui_url()
    if not no_open:
        webbrowser.open(url)
    suffix = f" (API log: {log})" if log else ""
    console.print(f"Marvis Console: {url}{suffix}")


def serve_main() -> None:
    """Run the local FastAPI server in the foreground for autostart/process managers."""
    _prepare_api_environment()
    write_api_pidfile()
    import uvicorn

    uvicorn.run("core.api.main:app", host=_HOST, port=_PORT)


# ---------------------------------------------------------------------------
# Autostart artifacts
# ---------------------------------------------------------------------------


def _server_program() -> tuple[str, list[str]]:
    return sys.executable, ["-m", "core.cli.marvis_console", "serve"]


def _run(cmd: list[str], *, env: dict[str, str] | None = None) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv lists, no shell
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError as exc:
        return 127, "", str(exc)
    except Exception as exc:  # noqa: BLE001
        return 1, "", str(exc)


def _launchd_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_LABEL}.plist"


def launchd_plist_text(executable: str, args: list[str]) -> str:
    log = _log_path()
    payload = {
        "Label": _LABEL,
        "ProgramArguments": [executable, *args],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


def _launchd_enable() -> dict[str, Any]:
    executable, args = _server_program()
    plist = _launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(launchd_plist_text(executable, args), encoding="utf-8")
    domain = f"gui/{current_uid()}"
    _run(["launchctl", "bootout", domain, str(plist)])
    rc, _out, err = _run(["launchctl", "bootstrap", domain, str(plist)])
    return {
        "backend": "launchd",
        "ok": rc == 0,
        "file": str(plist),
        "remove": f"launchctl bootout {domain} {shlex.quote(str(plist))}; rm -f {shlex.quote(str(plist))}",
        "error": err.strip() if rc != 0 else "",
    }


def _launchd_disable() -> dict[str, Any]:
    plist = _launchd_plist_path()
    domain = f"gui/{current_uid()}"
    rc, _out, err = _run(["launchctl", "bootout", domain, str(plist)])
    removed = False
    if plist.exists():
        plist.unlink()
        removed = True
    return {
        "backend": "launchd",
        "ok": True,
        "file": str(plist),
        "removed": removed,
        "loader_rc": rc,
        "error": err.strip() if rc not in (0, 3, 36) else "",
    }


def _launchd_status() -> dict[str, Any]:
    plist = _launchd_plist_path()
    rc, out, err = _run(["launchctl", "print", f"gui/{current_uid()}/{_LABEL}"])
    return {
        "backend": "launchd",
        "file": str(plist),
        "file_exists": plist.exists(),
        "loader": rc == 0,
        "loader_detail": out.strip() or err.strip(),
    }


def _systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "systemd" / "user"


def _systemd_service_path() -> Path:
    return _systemd_user_dir() / _SYSTEMD_UNIT


def systemd_service_text(executable: str, args: list[str]) -> str:
    cmd = shlex.join([executable, *args])
    return (
        "[Unit]\n"
        "Description=Marvis local API server\n"
        "After=network.target\n"
        "StartLimitIntervalSec=60\n"
        "StartLimitBurst=3\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={cmd}\n"
        "Restart=on-failure\n"
        "RestartSec=10\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _systemd_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{current_uid()}")
    return env


def _systemd_enable() -> dict[str, Any]:
    executable, args = _server_program()
    service = _systemd_service_path()
    service.parent.mkdir(parents=True, exist_ok=True)
    service.write_text(systemd_service_text(executable, args), encoding="utf-8")
    env = _systemd_env()
    linger_rc, _linger_out, linger_err = _run(["loginctl", "enable-linger", getuser()], env=env)
    reload_rc, _reload_out, reload_err = _run(["systemctl", "--user", "daemon-reload"], env=env)
    enable_rc, _enable_out, enable_err = _run(
        ["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT],
        env=env,
    )
    ok = enable_rc == 0 and reload_rc == 0
    return {
        "backend": "systemd",
        "ok": ok,
        "file": str(service),
        "remove": f"systemctl --user disable --now {_SYSTEMD_UNIT}; rm -f {shlex.quote(str(service))}",
        "linger_rc": linger_rc,
        "linger_error": linger_err.strip(),
        "error": (enable_err or reload_err).strip() if not ok else "",
    }


def _systemd_disable() -> dict[str, Any]:
    service = _systemd_service_path()
    env = _systemd_env()
    rc, _out, err = _run(["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT], env=env)
    removed = False
    if service.exists():
        service.unlink()
        removed = True
    _run(["systemctl", "--user", "daemon-reload"], env=env)
    return {
        "backend": "systemd",
        "ok": rc == 0 or not removed,
        "file": str(service),
        "removed": removed,
        "loader_rc": rc,
        "error": err.strip() if rc != 0 else "",
    }


def _systemd_status() -> dict[str, Any]:
    service = _systemd_service_path()
    env = _systemd_env()
    enabled_rc, enabled_out, enabled_err = _run(
        ["systemctl", "--user", "is-enabled", _SYSTEMD_UNIT], env=env
    )
    active_rc, active_out, active_err = _run(
        ["systemctl", "--user", "is-active", _SYSTEMD_UNIT], env=env
    )
    return {
        "backend": "systemd",
        "file": str(service),
        "file_exists": service.exists(),
        "loader": enabled_rc == 0 and active_rc == 0,
        "loader_detail": "enabled=%s active=%s" % (
            (enabled_out or enabled_err).strip(),
            (active_out or active_err).strip(),
        ),
    }


def _windows_pythonw() -> str:
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    return str(exe)


def windows_register_task_script(executable: str, args: list[str]) -> str:
    arg_string = " ".join(args).replace("'", "''")
    exe = executable.replace("'", "''")
    task = _WINDOWS_TASK.replace("'", "''")
    return (
        f"$Action = New-ScheduledTaskAction -Execute '{exe}' -Argument '{arg_string}'; "
        "$Trigger = New-ScheduledTaskTrigger -AtLogOn; "
        "$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME "
        "-LogonType Interactive -RunLevel Limited; "
        "$Settings = New-ScheduledTaskSettingsSet "
        "-ExecutionTimeLimit (New-TimeSpan -Seconds 0) "
        "-RestartCount 3 -RestartInterval (New-TimeSpan -Seconds 10); "
        f"Register-ScheduledTask -TaskName '{task}' -Action $Action "
        "-Trigger $Trigger -Principal $Principal -Settings $Settings -Force"
    )


def windows_startup_lnk_script(executable: str, args: list[str]) -> str:
    arg_string = " ".join(args).replace("'", "''")
    exe = executable.replace("'", "''")
    return (
        "$Startup = [Environment]::GetFolderPath('Startup'); "
        "$Path = Join-Path $Startup 'Marvis Server.lnk'; "
        "$Shell = New-Object -ComObject WScript.Shell; "
        "$Shortcut = $Shell.CreateShortcut($Path); "
        f"$Shortcut.TargetPath = '{exe}'; "
        f"$Shortcut.Arguments = '{arg_string}'; "
        "$Shortcut.Save(); "
        "Write-Output $Path"
    )


def _powershell(script: str) -> tuple[int, str, str]:
    exe = shutil.which("powershell") or shutil.which("pwsh") or "powershell"
    return _run([exe, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script])


def _windows_enable() -> dict[str, Any]:
    executable, args = _windows_pythonw(), ["-m", "core.cli.marvis_console", "serve"]
    rc, out, err = _powershell(windows_register_task_script(executable, args))
    fallback_file = ""
    fallback_error = ""
    if rc != 0:
        lnk_rc, lnk_out, lnk_err = _powershell(windows_startup_lnk_script(executable, args))
        fallback_file = lnk_out.strip()
        fallback_error = lnk_err.strip() if lnk_rc != 0 else ""
        rc = lnk_rc
        err = fallback_error
    return {
        "backend": "windows",
        "ok": rc == 0,
        "file": fallback_file or f"Scheduled Task: {_WINDOWS_TASK}",
        "remove": (
            f"Unregister-ScheduledTask -TaskName '{_WINDOWS_TASK}' -Confirm:$false; "
            "Remove-Item \"$([Environment]::GetFolderPath('Startup'))\\Marvis Server.lnk\" -ErrorAction SilentlyContinue"
        ),
        "loader_detail": out.strip(),
        "error": err.strip() if rc != 0 else "",
    }


def _windows_disable() -> dict[str, Any]:
    script = (
        f"Unregister-ScheduledTask -TaskName '{_WINDOWS_TASK}' -Confirm:$false "
        "-ErrorAction SilentlyContinue; "
        "$Startup = [Environment]::GetFolderPath('Startup'); "
        "$Path = Join-Path $Startup 'Marvis Server.lnk'; "
        "Remove-Item $Path -ErrorAction SilentlyContinue"
    )
    rc, _out, err = _powershell(script)
    return {"backend": "windows", "ok": rc == 0, "file": f"Scheduled Task: {_WINDOWS_TASK}", "error": err.strip()}


def _windows_status() -> dict[str, Any]:
    script = (
        f"$Task = Get-ScheduledTask -TaskName '{_WINDOWS_TASK}' -ErrorAction SilentlyContinue; "
        "$Startup = [Environment]::GetFolderPath('Startup'); "
        "$Lnk = Join-Path $Startup 'Marvis Server.lnk'; "
        "if ($Task) { Write-Output $Task.State } elseif (Test-Path $Lnk) { Write-Output \"startup-link\" }"
    )
    rc, out, err = _powershell(script)
    detail = out.strip() or err.strip()
    return {
        "backend": "windows",
        "file": f"Scheduled Task: {_WINDOWS_TASK}",
        "file_exists": bool(detail),
        "loader": rc == 0 and bool(detail),
        "loader_detail": detail,
    }


def _backend() -> str:
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("win"):
        return "windows"
    return "systemd"


def _autostart_enable() -> dict[str, Any]:
    backend = _backend()
    if backend == "launchd":
        return _launchd_enable()
    if backend == "windows":
        return _windows_enable()
    return _systemd_enable()


def _autostart_disable() -> dict[str, Any]:
    backend = _backend()
    if backend == "launchd":
        return _launchd_disable()
    if backend == "windows":
        return _windows_disable()
    return _systemd_disable()


def _autostart_status() -> dict[str, Any]:
    backend = _backend()
    if backend == "launchd":
        result = _launchd_status()
    elif backend == "windows":
        result = _windows_status()
    else:
        result = _systemd_status()
    health = _probe_api()
    result["healthz"] = {
        "ok": health.ours,
        "path": health.path,
        "detail": health.detail,
    }
    return result


def _render_autostart(result: dict[str, Any]) -> None:
    status = "enabled" if result.get("ok", result.get("loader")) else "not enabled"
    console.print(f"Autostart {status} ({result.get('backend')})")
    if result.get("file"):
        console.print(f"  file: {result['file']}")
    if result.get("remove"):
        console.print(f"  remove: {result['remove']}")
    if result.get("loader_detail"):
        console.print(f"  loader: {result['loader_detail']}")
    if result.get("healthz"):
        health = result["healthz"]
        console.print(f"  health: {'ok' if health.get('ok') else 'not ready'} ({health.get('detail')})")
    if result.get("error"):
        err_console.print(f"[yellow]{result['error']}[/yellow]")


@autostart_app.command("enable")
def autostart_enable_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Enable the local Marvis API at login. Default is OFF until this is run."""
    result = _autostart_enable()
    emit(result, json_out=json_out, render=_render_autostart)
    if not result.get("ok"):
        raise typer.Exit(1)


@autostart_app.command("disable")
def autostart_disable_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Disable and remove the local API autostart artifact."""
    result = _autostart_disable()
    emit(result, json_out=json_out, render=_render_autostart)
    if not result.get("ok"):
        raise typer.Exit(1)


@autostart_app.command("status")
def autostart_status_cmd(
    json_out: bool = typer.Option(False, "--json", help="Emit pure JSON to stdout."),
) -> None:
    """Show artifact presence, loader state, and local API health."""
    result = _autostart_status()
    emit(result, json_out=json_out, render=_render_autostart)


def _module_main() -> None:
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        serve_main()
        return
    raise SystemExit("Usage: python -m core.cli.marvis_console serve")


if __name__ == "__main__":  # pragma: no cover - exercised through subprocesses
    _module_main()
