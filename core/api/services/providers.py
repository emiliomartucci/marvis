# v1.4.0 - 2026-04-02 - Add model-aware launch commands and OpenCode runtime overrides
from __future__ import annotations

import asyncio
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from core.api.config import settings

ProviderName = Literal["claude", "gemini", "codex", "opencode"]


@dataclass(frozen=True, slots=True)
class KeystrokeStep:
    key: str
    delay_after: float = 0.3


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    name: str
    binary: str
    cli_flags: str
    process_names: tuple[str, ...]
    exit_sequence: tuple[KeystrokeStep, ...]
    submit_with_double_enter: bool = False
    source_bashrc: bool = False
    launcher_path: str | None = None


_API_ROOT = Path(__file__).resolve().parents[1]
_OPENCODE_LAUNCHER = str(_API_ROOT / "bin" / "opencode-launch.sh")
_OPENCODE_TUI_CONFIGS = {
    "dark": str(_API_ROOT / "opencode-runtime" / "tui.dark.json"),
    "light": str(_API_ROOT / "opencode-runtime" / "tui.light.json"),
}


PROVIDERS: dict[str, ProviderConfig] = {
    "claude": ProviderConfig(
        name="Claude Code",
        binary="claude",
        cli_flags="--dangerously-skip-permissions",
        process_names=("claude", "node"),
        exit_sequence=(
            KeystrokeStep("C-c", 1.0),
            KeystrokeStep("C-u", 0.3),
            KeystrokeStep("/exit", 0.5),
            KeystrokeStep("Escape", 0.3),
            KeystrokeStep("Enter", 2.0),
        ),
        submit_with_double_enter=True,
    ),
    "gemini": ProviderConfig(
        name="Gemini CLI",
        binary="gemini",
        cli_flags="--yolo",
        process_names=("gemini",),
        exit_sequence=(
            KeystrokeStep("C-c", 0.5),
            KeystrokeStep("/exit", 1.0),
            KeystrokeStep("Enter", 1.0),
        ),
    ),
    "codex": ProviderConfig(
        name="Codex CLI",
        binary="codex",
        cli_flags="--dangerously-bypass-approvals-and-sandbox",
        process_names=("codex", "node"),
        exit_sequence=(
            KeystrokeStep("C-c", 0.5),
            KeystrokeStep("/exit", 1.0),
            KeystrokeStep("Enter", 1.0),
        ),
    ),
    "opencode": ProviderConfig(
        name="OpenCode",
        binary="opencode",
        cli_flags="",
        process_names=("opencode", "node"),
        exit_sequence=(
            KeystrokeStep("C-c", 0.5),
            KeystrokeStep("/exit", 0.5),
            KeystrokeStep("Enter", 1.0),
        ),
        submit_with_double_enter=True,
        launcher_path=_OPENCODE_LAUNCHER,
    ),
}

# All known process names across all providers (for quick status checks in list endpoints)
ALL_KNOWN_PROCESS_NAMES = tuple(
    sorted({pn for cfg in PROVIDERS.values() for pn in cfg.process_names})
)

def _runtime_home() -> str:
    return settings.effective_runtime_home


def _runtime_bashrc() -> str:
    return os.path.join(_runtime_home(), ".bashrc")


def _runtime_path_prefix() -> str:
    home = _runtime_home()
    return ":".join(
        (
            os.path.join(home, ".local", "bin"),
            os.path.join(home, "bin"),
            os.path.join(home, ".npm-global", "bin"),
            os.path.join(home, ".opencode", "bin"),
        )
    )


def _opencode_state_root() -> str:
    return os.path.join(
        _runtime_home(), ".local", "state", "opencode-marvisx-console"
    )


def _provider_invocation(config: ProviderConfig) -> str:
    return " ".join(part for part in (config.binary, config.cli_flags) if part)


def get_provider(name: str | None) -> ProviderConfig:
    """Get provider config. None defaults to Claude (backward compat).
    Raises ValueError for unknown provider names."""
    if name is None:
        return PROVIDERS["claude"]
    try:
        return PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"Unknown provider: {name!r}. Available: {', '.join(PROVIDERS)}"
        )


def _model_flag(config: ProviderConfig, model: str | None) -> str:
    if not model:
        return ""
    if config.binary == "codex":
        return f"-m {shlex.quote(model)}"
    return f"--model {shlex.quote(model)}"


def _opencode_invocation(
    config: ProviderConfig,
    safe_dir: str,
    model: str | None,
    opencode_config: dict[str, object] | None,
    opencode_tui_config: str | None = None,
    opencode_state_dir: str | None = None,
    extra_cli_args: tuple[str, ...] | None = None,
) -> str:
    parts: list[str] = []
    if opencode_config:
        config_json = json.dumps(opencode_config, separators=(",", ":"))
        parts.append(f"OPENCODE_CONFIG_CONTENT={shlex.quote(config_json)}")
    if opencode_tui_config:
        parts.append(f"OPENCODE_TUI_CONFIG={shlex.quote(opencode_tui_config)}")
    if opencode_state_dir:
        parts.append(f"OPENCODE_STATE_DIR={shlex.quote(opencode_state_dir)}")
    parts.append(shlex.quote(config.launcher_path or config.binary))
    parts.append(safe_dir)
    model_flag = _model_flag(config, model)
    if model_flag:
        parts.append(model_flag)
    if extra_cli_args:
        parts.extend(extra_cli_args)
    return " ".join(parts)


def _codex_invocation(
    config: ProviderConfig,
    safe_dir: str,
    model: str | None,
    extra_cli_args: tuple[str, ...] | None = None,
) -> str:
    parts = [config.binary]
    if config.cli_flags:
        parts.append(config.cli_flags)
    model_flag = _model_flag(config, model)
    if model_flag:
        parts.append(model_flag)
    if extra_cli_args:
        parts.extend(extra_cli_args)
    parts.append(f"-C {safe_dir}")
    return " ".join(parts)


def _direct_invocation(
    config: ProviderConfig,
    model: str | None,
    extra_cli_args: tuple[str, ...] | None = None,
) -> str:
    parts = [config.binary]
    if config.cli_flags:
        parts.append(config.cli_flags)
    model_flag = _model_flag(config, model)
    if model_flag:
        parts.append(model_flag)
    if extra_cli_args:
        parts.extend(extra_cli_args)
    return " ".join(parts)


def build_start_command(
    config: ProviderConfig,
    directory: str,
    *,
    model: str | None = None,
    opencode_config: dict[str, object] | None = None,
    opencode_theme_mode: Literal["light", "dark"] | None = None,
    session_name: str | None = None,
    extra_cli_args: tuple[str, ...] | None = None,
) -> str:
    """Build shell command to start CLI in given directory.

    The systemd API service and tmux sessions do not always preserve the interactive
    shell environment. Force HOME/PATH so provider CLIs can find their auth/config.
    """
    expanded = os.path.expanduser(directory)
    safe_dir = shlex.quote(expanded)
    env_prefix = (
        f"export HOME={shlex.quote(_runtime_home())} "
        f"XDG_CONFIG_HOME={shlex.quote(os.path.join(_runtime_home(), '.config'))} "
        f"PATH={shlex.quote(_runtime_path_prefix())}:$PATH"
    )
    if config.launcher_path:
        invocation = _opencode_invocation(
            config,
            safe_dir,
            model,
            opencode_config,
            opencode_tui_config=_OPENCODE_TUI_CONFIGS.get(opencode_theme_mode)
            if opencode_theme_mode
            else None,
            opencode_state_dir=os.path.join(_opencode_state_root(), session_name)
            if session_name
            else None,
            extra_cli_args=extra_cli_args,
        )
        return f"{env_prefix} && {invocation}"
    if config.binary == "codex":
        invocation = _codex_invocation(
            config, safe_dir, model, extra_cli_args=extra_cli_args
        )
        return f"{env_prefix} && {invocation}"
    invocation = _direct_invocation(config, model, extra_cli_args=extra_cli_args)
    if config.source_bashrc:
        shell_bootstrap = shlex.quote(
            f"source {shlex.quote(_runtime_bashrc())} >/dev/null 2>&1; "
            f"cd {safe_dir} && {invocation}"
        )
        return f"{env_prefix} && bash -ic {shell_bootstrap}"
    return f"{env_prefix} && cd {safe_dir} && {invocation}"


async def is_binary_available(config: ProviderConfig) -> bool:
    """Check if the provider's CLI binary is installed and in PATH."""
    env = os.environ.copy()
    env["PATH"] = f"{_runtime_path_prefix()}:{env.get('PATH', '')}"
    proc = await asyncio.create_subprocess_exec(
        "which",
        config.binary,
        env=env,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.communicate()
    return proc.returncode == 0
