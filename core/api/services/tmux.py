# v1.6.0 - 2026-03-13 - add get_pane_cwd for session auto-registration project detection
from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from core.api.config import settings
from core.platform import current_uid

logger = logging.getLogger(__name__)

SESSION_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,28}[a-zA-Z0-9]$")
RESERVED_CHARS = set(":.${}'\"\\")
SUBPROCESS_TIMEOUT = 10
CREATE_SESSION_CONFIRM_ATTEMPTS = 20
CREATE_SESSION_CONFIRM_INTERVAL = 0.25
TMUX_HISTORY_LIMIT = 10_000
SYSTEMD_RUN_BIN = "/usr/bin/systemd-run"
TMUX_BIN = "/usr/bin/tmux"
TMUX_PROXY_BIN = "/data/pir/tmux-proxy"
AGENT_SESSION_SLICE = os.environ.get("MARVIS_AGENT_SESSION_SLICE", "agents.slice")
MARVISX_TMUX_TMPDIR_NAME = "marvisx-tmux"
RUNTIME_HOME = os.environ.get("MARVIS_RUNTIME_HOME", os.path.expanduser("~"))
RUNTIME_PATH = (
    f"{RUNTIME_HOME}/.local/bin:"
    f"{RUNTIME_HOME}/bin:"
    f"{RUNTIME_HOME}/.npm-global/bin:"
    f"{RUNTIME_HOME}/.opencode/bin:"
    "/usr/local/sbin:"
    "/usr/local/bin:"
    "/usr/sbin:"
    "/usr/bin:"
    "/snap/bin"
)
TENANT_ENV_WHITELIST_DEFAULTS = {
    "core": {
        "HOME",
        "PATH",
        "SHELL",
        "TERM",
        "LANG",
        "LC_ALL",
        "XDG_RUNTIME_DIR",
    },
    "marvis-personal": {
        "ANTHROPIC_API_KEY",
        "EXA_API_KEY",
        "LLM_GATEWAY_API_KEY",
        "LLM_GATEWAY_BASE_URL",
        "OPENAI_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_OWNER_CHAT_ID",
    },
}
USER_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

_SAFE_SESSION_NAME = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
TmuxServer = Literal["marvisx", "legacy"]


@dataclass(frozen=True, slots=True)
class ProcessSnapshot:
    pid: int
    parent_pid: int
    command: str
    cpu_pct: float
    rss_kb: float


def _validate_session_name(name: str) -> str:
    """Strict session name validation. Allows alphanumeric, dash, underscore (1-64 chars).

    Raises ValueError if invalid. Use this for all tmux command inputs to prevent
    command injection via crafted session names.
    """
    if not _SAFE_SESSION_NAME.match(name):
        raise ValueError(f"Invalid session name: {name!r}")
    return name


def validate_session_name(name: str) -> str:
    """Validate tmux session name. Raises ValueError if invalid.

    Delegates to _validate_session_name for strict alphanumeric + dash + underscore check.
    Also rejects any reserved shell characters as a defence-in-depth measure.
    """
    _validate_session_name(name)
    if any(c in name for c in RESERVED_CHARS):
        raise ValueError("Session name contains reserved characters")
    return name


def _exact_target(name: str) -> str:
    """Prefix name with = for tmux exact session matching (prevents prefix matching).

    IMPORTANT: Only use for commands that take -t target-session (has-session,
    kill-session, rename-session). Commands that take -t target-pane (capture-pane,
    display-message, send-keys) do NOT support the = prefix in tmux 3.4.
    """
    return f"={name}"


def _runtime_base_dir() -> str:
    """User-writable runtime dir for tmux sockets/state.

    ``/run/user/{uid}`` exists only on Linux with systemd-logind; on macOS
    ``/run`` is read-only, so hardcoding it made every ``/api/v1/sessions``
    call 500 for local-tier users (gh issue #15). Honor ``XDG_RUNTIME_DIR``
    when set, keep the Linux default when it actually exists, else fall back
    to a per-uid dir under the platform tmpdir (created 0700 by the caller).
    """
    env_dir = os.environ.get("XDG_RUNTIME_DIR")
    if env_dir:
        return env_dir
    uid = current_uid()
    linux_default = f"/run/user/{uid}" if uid is not None else None
    if linux_default and os.path.isdir(linux_default):
        return linux_default
    suffix = uid if uid is not None else "local"
    return os.path.join(tempfile.gettempdir(), f"marvisx-runtime-{suffix}")


def _tmux_scope_env() -> dict[str, str]:
    """Minimal environment for tmux launched outside pir-api.service.

    Do not forward os.environ wholesale: pir-api carries production secrets.
    Provider-specific HOME/PATH is also exported in the pane start command.
    """
    env = {
        "HOME": RUNTIME_HOME,
        "PATH": RUNTIME_PATH,
        "SHELL": "/bin/bash",
        "TERM": os.environ.get("TERM", "xterm-256color"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "XDG_RUNTIME_DIR": _runtime_base_dir(),
    }
    if "LC_ALL" in os.environ:
        env["LC_ALL"] = os.environ["LC_ALL"]
    return env


def _tenant_env_whitelist(tenant_slug: str) -> set[str]:
    """Env-var names allowed into a tenant's tmux panes.

    Tenant-agnostic: the per-tenant whitelist is NOT hardcoded by slug in core.
    It is the union of (a) the generic core defaults, (b) any generic default
    set keyed by slug that ships in core (e.g. "marvis-personal"), and (c) the
    tenant-provided list from settings.tenant_env_whitelist (env var
    TENANT_ENV_WHITELIST, set by the tenant overlay in deploy/<tenant>/).
    """
    return (
        TENANT_ENV_WHITELIST_DEFAULTS["core"]
        | TENANT_ENV_WHITELIST_DEFAULTS.get(tenant_slug, set())
        | set(settings.tenant_env_whitelist)
    )


def _build_session_env(
    tenant_slug: str | None = None,
    user_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build the sanitized environment exported into a user tmux pane."""
    tenant = tenant_slug or settings.deploy_mode
    allowed = _tenant_env_whitelist(tenant)
    env = _tmux_scope_env()

    managed_core_keys = TENANT_ENV_WHITELIST_DEFAULTS["core"]
    for key in sorted(allowed - managed_core_keys):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value

    if settings.multi_tenant_enabled:
        env["DEPLOY_MODE"] = tenant
        env["TENANT_SLUG"] = tenant

    if user_env:
        for key, value in user_env.items():
            if key not in allowed or not USER_ENV_NAME_RE.fullmatch(key):
                continue
            env[key] = str(value)

    return env


def _session_env_exports(env: dict[str, str]) -> str:
    exports = []
    core_keys = TENANT_ENV_WHITELIST_DEFAULTS["core"]
    for key in sorted(env):
        if key in core_keys:
            continue
        if not USER_ENV_NAME_RE.fullmatch(key):
            continue
        quoted = "'" + env[key].replace("'", "'\"'\"'") + "'"
        exports.append(f"export {key}={quoted};")
    return " ".join(exports) + (" " if exports else "")


def _uid_isolated_start_command(start_command: str, env: dict[str, str]) -> str:
    if not settings.uid_isolation_enabled:
        return start_command

    user_index = env.get("USER_UID_INDEX")
    try:
        index = int(user_index or "0")
    except ValueError:
        index = 0

    if index <= 0 or index > settings.uid_pool_size:
        raise ValueError("uid isolation enabled but USER_UID_INDEX is missing or invalid")

    username = f"{settings.uid_pool_prefix}-{index:02d}"
    user_home = env.get("USER_HOME", f"/data/users/{username}")
    quoted_home = shlex.quote(user_home)
    quoted_command = shlex.quote(f"cd {quoted_home} && {start_command}")
    return f"sudo -H -u {shlex.quote(username)} /bin/bash -lc {quoted_command}"


def _marvisx_tmux_tmpdir() -> str:
    return os.path.join(_runtime_base_dir(), MARVISX_TMUX_TMPDIR_NAME)


def _ensure_marvisx_tmux_tmpdir() -> None:
    path = _marvisx_tmux_tmpdir()
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError as exc:
        # Best-effort: a read-only base dir must degrade to "no sessions",
        # not crash every tmux call site (gh issue #15 on macOS).
        logger.warning("Could not create MarvisX tmux tmpdir %s: %s", path, exc)
        return
    try:
        os.chmod(path, 0o700)
    except PermissionError:
        logger.warning("Could not chmod MarvisX tmux tmpdir: %s", path)


def tmux_env_for_server(server: TmuxServer) -> dict[str, str]:
    """Return a sanitized env that routes tmux to the requested server.

    tmux gives the TMUX environment variable priority over socket discovery.
    API subprocesses should never inherit it from an operator shell because
    that would silently target the wrong server.
    """
    env = _tmux_scope_env()
    env.pop("TMUX", None)
    if server == "marvisx":
        env["TMUX_TMPDIR"] = _marvisx_tmux_tmpdir()
    else:
        env.pop("TMUX_TMPDIR", None)
    return env


def _tmux_command(*args: str) -> tuple[str, ...]:
    return (TMUX_PROXY_BIN, *args)


def tmux_command_for_server(server: TmuxServer, *args: str) -> tuple[str, ...]:
    if server == "marvisx":
        _ensure_marvisx_tmux_tmpdir()
    return _tmux_command(*args)


def _direct_tmux_command_for_server(server: TmuxServer, *args: str) -> tuple[str, ...]:
    if server == "marvisx":
        _ensure_marvisx_tmux_tmpdir()
    return (TMUX_BIN, *args)


def _scoped_tmux_new_session_command(*args: str) -> tuple[str, ...]:
    tmpdir = _marvisx_tmux_tmpdir()
    return (
        SYSTEMD_RUN_BIN,
        "--scope",
        "--quiet",
        "--collect",
        f"--slice={AGENT_SESSION_SLICE}",
        "--property=OOMPolicy=kill",
        "--property=MemorySwapMax=0",
        "--property=CPUWeight=40",
        f"--setenv=HOME={RUNTIME_HOME}",
        f"--setenv=PATH={RUNTIME_PATH}",
        "--setenv=SHELL=/bin/bash",
        f"--setenv=TERM={os.environ.get('TERM', 'xterm-256color')}",
        f"--setenv=LANG={os.environ.get('LANG', 'C.UTF-8')}",
        f"--setenv=XDG_RUNTIME_DIR={_runtime_base_dir()}",
        f"--setenv=TMUX_TMPDIR={tmpdir}",
        "--setenv=TMUX=",
        TMUX_PROXY_BIN,
        "new-session",
        *args,
    )


def _history_limit_commands(name: str) -> list[str]:
    limit = str(TMUX_HISTORY_LIMIT)
    return [
        ";",
        "set-option",
        "-g",
        "history-limit",
        limit,
        ";",
        "set-option",
        "-t",
        name,
        "history-limit",
        limit,
    ]


async def _run_tmux(server: TmuxServer, *args: str) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *tmux_command_for_server(server, *args),
            env=tmux_env_for_server(server),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        # tmux (or the tmux proxy) is not installed — normal on the local tier
        # (macOS/Windows). Callers already treat returncode != 0 as "no
        # sessions"; raising here turned /api/v1/sessions into a 500.
        return 127, b"", f"tmux unavailable: {exc}".encode()
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=SUBPROCESS_TIMEOUT
    )
    return proc.returncode, stdout, stderr


async def _run_direct_tmux(server: TmuxServer, *args: str) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *_direct_tmux_command_for_server(server, *args),
            env=tmux_env_for_server(server),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        return 127, b"", f"tmux unavailable: {exc}".encode()
    stdout, stderr = await asyncio.wait_for(
        proc.communicate(), timeout=SUBPROCESS_TIMEOUT
    )
    return proc.returncode, stdout, stderr


def _tmux_server_missing(stderr: bytes) -> bool:
    stderr_text = stderr.decode(errors="replace")
    return (
        "no server running" in stderr_text
        or "no sessions" in stderr_text
        or "tmux unavailable" in stderr_text
        or ("error connecting" in stderr_text and "No such file" in stderr_text)
    )


async def _scrub_tmux_global_env(server: TmuxServer = "marvisx") -> None:
    """Remove non-whitelisted variables from tmux global environment."""
    tenant = settings.deploy_mode if settings.multi_tenant_enabled else "core"
    allowed = _tenant_env_whitelist(tenant) | {"TMUX", "TMUX_TMPDIR"}
    try:
        returncode, stdout, stderr = await _run_tmux(server, "show-environment", "-g")
    except (asyncio.TimeoutError, OSError):
        logger.warning("Failed to read tmux global environment for %s", server)
        return
    if returncode != 0:
        if _tmux_server_missing(stderr):
            return
        logger.warning(
            "tmux show-environment failed for %s: %s",
            server,
            stderr.decode(errors="replace").strip(),
        )
        return

    for line in stdout.decode(errors="replace").splitlines():
        if line.startswith("-") or "=" not in line:
            continue
        key = line.split("=", 1)[0]
        if key in allowed:
            continue
        try:
            await _run_tmux(server, "set-environment", "-g", "-u", key)
        except (asyncio.TimeoutError, OSError):
            logger.warning("Failed to unset tmux global env var %s", key)


async def _set_history_limit(server: TmuxServer, *args: str) -> bool:
    try:
        returncode, _, stderr = await _run_direct_tmux(
            server,
            "set-option",
            *args,
            "history-limit",
            str(TMUX_HISTORY_LIMIT),
        )
    except asyncio.TimeoutError:
        logger.warning("tmux history-limit configuration timed out for %s", server)
        return False
    except OSError as exc:
        logger.warning("tmux history-limit configuration failed for %s: %s", server, exc)
        return False

    if returncode == 0 or _tmux_server_missing(stderr):
        return True

    logger.warning(
        "tmux history-limit configuration failed for %s: %s",
        server,
        stderr.decode(errors="replace").strip(),
    )
    return False


async def configure_history_limits() -> None:
    """Best-effort cap for existing tmux servers and already-open sessions.

    MUST NOT start a server. A bare ``set-option -g`` on a dead server boots a
    fresh tmux server inside whatever cgroup the caller runs in — at API startup
    that is ``pir-api.service`` — and every pane that server later forks inherits
    that cgroup. That is the path that put agent workers (and their MCP subtrees)
    under ``pir-api.service``, where a routine pir-api restart kills them. So
    configure only servers that already have sessions; freshly spawned sessions
    receive their history-limit from the new-session command itself (see
    ``_history_limit_commands``), which runs inside the per-session scope.
    """
    for server in ("marvisx", "legacy"):
        sessions = await _list_sessions_for_server(server)
        if not sessions:
            # Server down or empty → never boot one with set-option -g.
            continue
        await _set_history_limit(server, "-g")
        for session in sessions:
            try:
                name = validate_session_name(session["name"])
            except ValueError:
                logger.warning(
                    "Skipping tmux history-limit cap for invalid session name: %r",
                    session["name"],
                )
                continue
            await _set_history_limit(server, "-t", name)


async def _list_sessions_for_server(server: TmuxServer) -> list[dict]:
    try:
        returncode, stdout, stderr = await _run_tmux(
            server,
            "list-sessions",
            "-F",
            "#{session_name}\t#{session_created}\t#{session_attached}",
        )
    except asyncio.TimeoutError:
        logger.error("tmux list-sessions timed out for %s", server)
        return []

    if returncode != 0:
        # tmux server not running = no sessions
        if _tmux_server_missing(stderr):
            return []
        logger.error(
            "tmux list-sessions failed for %s: %s",
            server,
            stderr.decode(errors="replace").strip(),
        )
        return []

    sessions = []
    for line in stdout.decode().strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            sessions.append(
                {
                    "name": parts[0],
                    "created_epoch": parts[1],
                    "attached": parts[2] != "0",
                    "tmux_server": server,
                }
            )
    return sessions


async def list_sessions() -> list[dict]:
    """List all tmux sessions across MarvisX and legacy tmux servers.

    New sessions live on the MarvisX server. Legacy sessions may remain on the
    default tmux server for weeks, so list is a union and never mutates either
    server.
    """
    sessions: list[dict] = []
    seen: set[str] = set()
    for server in ("marvisx", "legacy"):
        for session in await _list_sessions_for_server(server):
            name = session["name"]
            if name in seen:
                logger.warning(
                    "Duplicate tmux session name %s across servers; keeping first", name
                )
                continue
            seen.add(name)
            sessions.append(session)
    return sessions


async def session_exists_on_server(name: str, server: TmuxServer) -> bool:
    name = validate_session_name(name)
    try:
        returncode, _, _ = await _run_tmux(
            server, "has-session", "-t", _exact_target(name)
        )
        return returncode == 0
    except asyncio.TimeoutError:
        return False


async def resolve_session_server(name: str) -> TmuxServer | None:
    """Find the tmux server that owns a session without moving it."""
    name = validate_session_name(name)
    for server in ("marvisx", "legacy"):
        if await session_exists_on_server(name, server):
            return server
    return None


async def session_exists(name: str) -> bool:
    """Check if a tmux session exists."""
    name = validate_session_name(name)
    return await resolve_session_server(name) is not None


async def _marvisx_session_exists_after_create_attempt(name: str) -> bool:
    for attempt in range(CREATE_SESSION_CONFIRM_ATTEMPTS):
        if await session_exists_on_server(name, "marvisx"):
            return True
        if attempt < CREATE_SESSION_CONFIRM_ATTEMPTS - 1:
            await asyncio.sleep(CREATE_SESSION_CONFIRM_INTERVAL)
    return False


async def create_session(
    name: str,
    start_dir: str = "",
    start_command: str | None = None,
    tenant_slug: str | None = None,
    user_env: dict[str, str] | None = None,
) -> bool:
    """Create a new tmux session. Returns True on success."""
    name = validate_session_name(name)
    if await session_exists(name):
        logger.error(
            "tmux session already exists on a MarvisX or legacy server: %s", name
        )
        return False
    try:
        _ensure_marvisx_tmux_tmpdir()
        args = ["-d", "-s", name]
        if start_dir:
            args.extend(["-c", start_dir])
        # Export TMUX_SESSION_NAME so MarvisX state hooks (Claude Code,
        # OpenCode plugin, future Codex bridge) can resolve the session name
        # without spawning `tmux display` (plan 2026-04-26 §M9). `name` is
        # already validated against the strict regex above; safe in single
        # quotes.
        session_env = _build_session_env(tenant_slug, user_env)
        session_env["TMUX_SESSION_NAME"] = name
        env_prelude = _session_env_exports(session_env)
        if start_command:
            start_command = _uid_isolated_start_command(start_command, session_env)
            # Start the CLI directly with the session so tmux does not visibly
            # type the launcher command into the pane via send-keys.
            args.extend(["/bin/bash", "-lc", env_prelude + start_command])
        else:
            # No start_command → still export the env in the default shell.
            args.extend(["/bin/bash", "-lc", env_prelude + "exec ${SHELL:-/bin/bash}"])
        args.extend(_history_limit_commands(name))
        proc = await asyncio.create_subprocess_exec(
            *_scoped_tmux_new_session_command(*args),
            env=tmux_env_for_server("marvisx"),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT
        )
    except asyncio.TimeoutError:
        if await _marvisx_session_exists_after_create_attempt(name):
            logger.warning(
                "tmux new-session timed out for %s but session exists on "
                "MarvisX server; treating as created",
                name,
            )
            return True
        logger.error(
            "tmux new-session timed out for %s and session was not created", name
        )
        return False

    if proc.returncode != 0:
        if await _marvisx_session_exists_after_create_attempt(name):
            logger.warning(
                "tmux new-session returned %s for %s but session exists on "
                "MarvisX server; treating as created: %s",
                proc.returncode,
                name,
                stderr.decode(errors="replace").strip(),
            )
            return True
        logger.error(
            "tmux new-session failed for %s: %s",
            name,
            stderr.decode(errors="replace").strip(),
        )
        return False

    logger.info("Created tmux session on MarvisX tmux server: %s", name)
    return True


async def kill_session(name: str) -> bool:
    """Kill a tmux session. Returns True on success."""
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        logger.error("tmux kill-session failed for %s: session not found", name)
        return False
    try:
        returncode, _, stderr = await _run_tmux(
            server, "kill-session", "-t", _exact_target(name)
        )
    except asyncio.TimeoutError:
        logger.error("tmux kill-session timed out for %s", name)
        return False

    if returncode != 0:
        logger.error(
            "tmux kill-session failed for %s: %s", name, stderr.decode().strip()
        )
        return False

    logger.info("Killed tmux session on %s server: %s", server, name)
    return True


async def rename_session(old_name: str, new_name: str) -> bool:
    """Rename a tmux session. Returns True on success."""
    old_name = validate_session_name(old_name)
    new_name = validate_session_name(new_name)
    server = await resolve_session_server(old_name)
    if server is None:
        logger.error("tmux rename-session failed: %s not found", old_name)
        return False
    if await session_exists(new_name):
        logger.error("tmux rename-session failed: %s already exists", new_name)
        return False
    try:
        returncode, _, stderr = await _run_tmux(
            server, "rename-session", "-t", _exact_target(old_name), new_name
        )
    except asyncio.TimeoutError:
        logger.error("tmux rename-session timed out for %s -> %s", old_name, new_name)
        return False

    if returncode != 0:
        logger.error("tmux rename-session failed: %s", stderr.decode().strip())
        return False

    logger.info(
        "Renamed tmux session on %s server: %s -> %s", server, old_name, new_name
    )
    return True


async def get_pane_size(name: str) -> tuple[int, int] | None:
    """Get pane dimensions (width, height). Returns None on error."""
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return None
    try:
        returncode, stdout, _ = await _run_tmux(
            server,
            "list-panes",
            "-t",
            name,
            "-F",
            "#{pane_width}\t#{pane_height}",
        )
        if returncode == 0:
            parts = stdout.decode().strip().split("\n")[0].split("\t")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        return None
    except (asyncio.TimeoutError, OSError, ValueError):
        return None


async def get_session_status(name: str) -> str | None:
    """Get the active process running in a session's current pane."""
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return None
    try:
        returncode, stdout, _ = await _run_tmux(
            server,
            "list-panes",
            "-t",
            name,
            "-F",
            "#{pane_current_command}",
        )
        if returncode == 0:
            cmd = stdout.decode().strip().split("\n")[0]
            return cmd if cmd else None
        return None
    except (asyncio.TimeoutError, OSError):
        return None


async def get_all_session_statuses() -> dict[str, str | None]:
    """Get active process for all sessions in one call."""
    result: dict[str, str | None] = {}
    for server in ("marvisx", "legacy"):
        result.update(await _get_all_session_statuses_for_server(server, result.keys()))
    return result


async def _get_all_session_statuses_for_server(
    server: TmuxServer, existing_names: Iterable[str]
) -> dict[str, str | None]:
    try:
        returncode, stdout, _ = await _run_tmux(
            server,
            "list-windows",
            "-a",
            "-F",
            "#{session_name}\t#{pane_current_command}",
        )
        if returncode != 0:
            return {}
        result: dict[str, str | None] = {}
        seen = set(existing_names)
        for line in stdout.decode().strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) >= 2 and parts[0] not in result and parts[0] not in seen:
                result[parts[0]] = parts[1] if parts[1] else None
        return result
    except (asyncio.TimeoutError, OSError):
        return {}


async def get_all_session_pane_pids() -> dict[str, int]:
    """Get active pane PID for all sessions in one tmux call."""
    result: dict[str, int] = {}
    for server in ("marvisx", "legacy"):
        result.update(
            await _get_all_session_pane_pids_for_server(server, result.keys())
        )
    return result


async def _get_all_session_pane_pids_for_server(
    server: TmuxServer, existing_names: Iterable[str]
) -> dict[str, int]:
    try:
        returncode, stdout, _ = await _run_tmux(
            server,
            "list-windows",
            "-a",
            "-F",
            "#{session_name}\t#{pane_pid}",
        )
        if returncode != 0:
            return {}
        result: dict[str, int] = {}
        seen = set(existing_names)
        for line in stdout.decode().strip().split("\n"):
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) != 2 or parts[0] in result or parts[0] in seen:
                continue
            try:
                result[parts[0]] = int(parts[1])
            except ValueError:
                continue
        return result
    except (asyncio.TimeoutError, OSError):
        return {}


async def send_keys(name: str, keys: str, double_enter: bool = True) -> bool:
    """Send keys to a tmux session with Enter. Returns True on success.

    When double_enter=True (default, needed for Claude Code), sends two Enters:
    the first creates a newline in the multiline input, the second submits.
    When double_enter=False (Gemini, Codex), a single Enter suffices.
    """
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return False
    try:
        # First: type the text + Enter
        returncode, _, _ = await _run_tmux(
            server,
            "send-keys",
            "-t",
            name,
            keys,
            "Enter",
        )
        if returncode != 0:
            return False
        if double_enter:
            # Wait for TUI to register the newline state
            await asyncio.sleep(0.15)
            # Second Enter: submit the message (cursor is now on empty line)
            returncode, _, _ = await _run_tmux(
                server,
                "send-keys",
                "-t",
                name,
                "Enter",
            )
            return returncode == 0
        return True
    except (asyncio.TimeoutError, OSError):
        return False


async def send_keys_raw(name: str, *keys: str) -> bool:
    """Send raw keys to a tmux session (no automatic Enter). Returns True on success."""
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return False
    try:
        returncode, _, _ = await _run_tmux(
            server,
            "send-keys",
            "-t",
            name,
            *keys,
        )
        return returncode == 0
    except (asyncio.TimeoutError, OSError):
        return False


async def exit_copy_mode(name: str) -> None:
    """Exit tmux copy mode if active. No-op if not in copy mode."""
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return
    try:
        await _run_tmux(
            server,
            "send-keys",
            "-t",
            name,
            "-X",
            "cancel",
        )
    except (asyncio.TimeoutError, OSError):
        pass


_NEEDS_INPUT_PATTERNS = [
    re.compile(r"Allow\s+\w+", re.IGNORECASE),  # "Allow Read", "Allow Bash"
    re.compile(r"\(y/n\)", re.IGNORECASE),  # (y/n) prompts
    re.compile(r"\(Y/n\)"),  # (Y/n) prompts
    re.compile(r"Yes\s*/\s*No", re.IGNORECASE),  # Yes / No
    re.compile(r"Enter to select"),  # selection menu
    re.compile(r"approve", re.IGNORECASE),  # approval prompts
    re.compile(r"\? \[Y/n\]"),  # ? [Y/n]
    re.compile(r"Do you want to"),  # "Do you want to proceed?"
]

_IDLE_PATTERNS = [
    re.compile(r"^❯\s*$"),  # bare prompt
    re.compile(r"^❯ "),  # prompt with text
    re.compile(r"^> "),  # fallback prompt
    re.compile(r'Try "'),  # suggestion text
]

_SPINNER_RE = re.compile(
    r"^[◐◑◒◓✽✻]"
)  # active spinners only (● = completed marker, excluded)
_COMPLETED_SPINNER_RE = re.compile(
    r"^[◐◑◒◓✽✻●]\s+(Cogitated|Thought|Took)\s",
    re.IGNORECASE,
)  # past-tense indicators: spinner char + "Cogitated for 51s" etc.

_WORKING_PATTERNS = [
    re.compile(r"Crystallizing", re.IGNORECASE),
    re.compile(r"Running[…\.]+\s*\("),  # "Running… (14s · timeout 5m)"
    re.compile(r"thinking with", re.IGNORECASE),  # "thinking with max effort"
    re.compile(r"[✶✷]\s+\S.*[…\.]{2,}"),  # "✶ Building and testing…"
    re.compile(r"·\s+\S.*[…\.]{2,}\s*\("),  # "· Crystallizing… (5m 7s..."
]

_STATUS_BAR_RE = re.compile(r"^[─━═┄┅┈┉]|^\s*\[.*\]\s*#+|^\s*⏵|^\s*Contex")

_SEPARATOR_RE = re.compile(r"^[─━═┄┅┈┉]+$")


def _strip_status_bar(lines: list[str]) -> list[str]:
    """Remove Claude Code status bar lines (separator + model/context/cost lines).

    Also strips trailing empty/whitespace lines first — Claude Code v4.6+
    appends a blank line after the status bar which blocked detection.
    """
    result = list(lines)
    # Strip trailing empty lines first
    while result and not result[-1].strip():
        result.pop()
    # Strip status bar lines (separator, model info, bypass indicator)
    while result and _STATUS_BAR_RE.match(result[-1].strip()):
        result.pop()
    return result


def detect_activity_state(
    pane_text: str | None,
    process_status: str | None,
    provider: str | None = None,
) -> str | None:
    """Determine CLI activity state from captured pane text.

    For Claude: full TUI pattern matching (needs_input, idle, working).
    For non-Claude providers: simple alive/dead check (returns "active" or None).
    Returns: 'needs_input', 'idle', 'working', 'active', or None.
    """
    # Import here to avoid circular imports at module level
    from core.api.services.providers import ALL_KNOWN_PROCESS_NAMES

    if not process_status or process_status not in ALL_KNOWN_PROCESS_NAMES:
        return None

    # Non-Claude providers: detailed TUI parsing won't work, just report active
    if provider and provider != "claude":
        return "active"

    # Claude: if process is running but not claude/node, skip
    if process_status not in ("claude", "node"):
        return None

    if not pane_text:
        return "working"

    lines = pane_text.splitlines()
    last_lines = lines[-20:] if len(lines) >= 20 else lines
    text_block = "\n".join(last_lines)

    # Priority 1: needs_input
    for pat in _NEEDS_INPUT_PATTERNS:
        if pat.search(text_block):
            return "needs_input"

    # Priority 2: explicit working indicators (Crystallizing, Running, etc.)
    # These override prompt detection because Claude shows ❯ even while working.
    for pat in _WORKING_PATTERNS:
        if pat.search(text_block):
            return "working"

    # Strip status bar
    content_lines = _strip_status_bar(last_lines)
    if not content_lines:
        return "working"

    # Find prompt ❯ as last non-empty line
    prompt_found = False
    prompt_idx = -1
    for i in range(len(content_lines) - 1, -1, -1):
        stripped = content_lines[i].strip()
        if not stripped:
            continue
        for pat in _IDLE_PATTERNS:
            if pat.search(stripped):
                prompt_found = True
                prompt_idx = i
        break  # only check last non-empty line

    if not prompt_found:
        # No prompt → Claude is generating output → working
        return "working"

    # Prompt found. Look above: find content between nearest separator and prompt.
    # If there are active spinner bullets in the output section → still working.
    sep_idx = -1
    for i in range(prompt_idx - 1, -1, -1):
        if _SEPARATOR_RE.match(content_lines[i].strip()):
            sep_idx = i
            break

    # Check lines above separator (the output section) for active spinners
    # Only look at the last ~6 lines of output (not deep scrollback)
    output_start = max(0, sep_idx - 6) if sep_idx >= 0 else max(0, prompt_idx - 6)
    output_end = sep_idx if sep_idx >= 0 else prompt_idx
    output_section = content_lines[output_start:output_end]

    for line in output_section:
        stripped = line.strip()
        if _SPINNER_RE.match(stripped) and not _COMPLETED_SPINNER_RE.match(stripped):
            return "working"

    # Prompt visible, no active spinners → idle
    return "idle"


async def capture_pane(name: str, last_lines: int = 25) -> str | None:
    """Capture pane content including scrollback. Returns last N non-empty lines.

    Uses -S -50 to capture scrollback (panes not actively viewed may be tiny).
    """
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return None
    try:
        returncode, stdout, _ = await _run_tmux(
            server,
            "capture-pane",
            "-t",
            name,
            "-p",
            "-S",
            "-50",
        )
        if returncode == 0:
            lines = stdout.decode().splitlines()
            # Take last N non-empty lines
            non_empty = [line for line in lines if line.strip()]
            return "\n".join(non_empty[-last_lines:])
        return None
    except (asyncio.TimeoutError, OSError):
        return None


async def get_pane_id(name: str) -> str | None:
    """Get the tmux pane ID (e.g., '%70') for a session.

    Used by statusline-based conversation detection: statusline.sh writes
    per-pane metrics to ~/.claude/pane-metrics/{pane_num}.json.
    """
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return None
    try:
        returncode, stdout, _ = await _run_tmux(
            server,
            "display-message",
            "-p",
            "-t",
            name,
            "#{pane_id}",
        )
        if returncode == 0:
            val = stdout.decode().strip()
            if val:
                return val
        return None
    except (asyncio.TimeoutError, OSError):
        return None


async def get_pane_cwd(name: str) -> str | None:
    """Get the current working directory of a session's active pane."""
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return None
    try:
        returncode, stdout, _ = await _run_tmux(
            server,
            "display-message",
            "-p",
            "-t",
            name,
            "#{pane_current_path}",
        )
        if returncode == 0:
            val = stdout.decode().strip()
            if val:
                return val
        return None
    except (asyncio.TimeoutError, OSError):
        return None


async def get_pane_start_time(name: str) -> float | None:
    """Get the creation time of a session (epoch seconds).

    Uses session_created (tmux 3.4+) since pane_start_time is unavailable.
    """
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return None
    try:
        returncode, stdout, _ = await _run_tmux(
            server,
            "display-message",
            "-p",
            "-t",
            name,
            "#{session_created}",
        )
        if returncode == 0:
            val = stdout.decode().strip()
            if val:
                return float(val)
        return None
    except (asyncio.TimeoutError, OSError, ValueError):
        return None


async def get_cli_pid(
    name: str, process_names: tuple[str, ...] = ("claude", "node")
) -> int | None:
    """Get the PID of the CLI process running in a tmux session.

    Walks pane_pid -> child processes, trying each process_name in order.
    """
    name = validate_session_name(name)
    server = await resolve_session_server(name)
    if server is None:
        return None
    try:
        returncode, stdout, _ = await _run_tmux(
            server,
            "display-message",
            "-p",
            "-t",
            name,
            "#{pane_pid}",
        )
        if returncode != 0:
            return None
        pane_pid = stdout.decode().strip()
        if not pane_pid:
            return None

        # Find child process matching any of the process names
        for pname in process_names:
            proc = await asyncio.create_subprocess_exec(
                "pgrep",
                "-P",
                pane_pid,
                pname,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=SUBPROCESS_TIMEOUT
            )
            if proc.returncode == 0:
                pid_str = stdout.decode().strip().split("\n")[0]
                if pid_str:
                    return int(pid_str)
        return None
    except (asyncio.TimeoutError, OSError, ValueError):
        return None


# Backward-compatible alias
get_claude_pid = get_cli_pid


async def get_process_metrics(pid: int) -> tuple[float, float] | None:
    """Return normalized CPU percent and RSS KiB for one already-owned PID."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    try:
        cpu_count = max(1, os.cpu_count() or 1)
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-p",
            str(pid),
            "-o",
            "%cpu=,rss=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT
        )
        if proc.returncode != 0:
            return None
        parts = stdout.decode().strip().split()
        if len(parts) < 2:
            return None
        return float(parts[0]) / cpu_count, float(parts[1])
    except (asyncio.TimeoutError, OSError, ValueError):
        return None


async def get_all_process_metrics() -> dict[int, tuple[float, float]]:
    """Single ps call to get CPU% and RSS for all processes.

    Returns {pid: (cpu_pct_normalized, rss_kb)} dict.
    cpu_pct_normalized is divided by cpu_count so 100% = all cores fully used.
    """
    try:
        cpu_count = max(1, os.cpu_count() or 1)
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-eo",
            "pid,%cpu,rss",
            "--no-headers",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT
        )
        if proc.returncode != 0:
            return {}
        result: dict[int, tuple[float, float]] = {}
        for line in stdout.decode().strip().splitlines():
            parts = line.split()
            if len(parts) >= 3:
                try:
                    pid = int(parts[0])
                    cpu = (
                        float(parts[1]) / cpu_count
                    )  # normalize to single-CPU equivalent
                    rss_kb = float(parts[2])
                    result[pid] = (cpu, rss_kb)
                except (ValueError, IndexError):
                    continue
        return result
    except (asyncio.TimeoutError, OSError):
        return {}


async def get_all_process_snapshots() -> dict[int, ProcessSnapshot]:
    """Single ps call with parent PID, command, CPU and RSS for all processes."""
    try:
        cpu_count = max(1, os.cpu_count() or 1)
        proc = await asyncio.create_subprocess_exec(
            "ps",
            "-eo",
            "pid=,ppid=,comm=,%cpu=,rss=",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT
        )
        if proc.returncode != 0:
            return {}
        result: dict[int, ProcessSnapshot] = {}
        for line in stdout.decode().strip().splitlines():
            parts = line.split()
            if len(parts) < 5:
                continue
            try:
                pid = int(parts[0])
                parent_pid = int(parts[1])
                command = parts[2]
                cpu = float(parts[3]) / cpu_count
                rss_kb = float(parts[4])
            except (ValueError, IndexError):
                continue
            result[pid] = ProcessSnapshot(
                pid=pid,
                parent_pid=parent_pid,
                command=command,
                cpu_pct=cpu,
                rss_kb=rss_kb,
            )
        return result
    except (asyncio.TimeoutError, OSError):
        return {}
