from __future__ import annotations

import json
import os
from pathlib import Path

from core.api.services import providers
from core.api.services.providers import build_start_command, get_provider


def test_opencode_provider_uses_dedicated_launcher_and_double_enter():
    config = get_provider("opencode")

    assert config.binary == "opencode"
    assert config.submit_with_double_enter is True
    assert config.launcher_path == providers._OPENCODE_LAUNCHER

    command = build_start_command(
        config,
        "/tmp/project",
        model="openai/gpt-5.4",
        opencode_config={"permission": "allow"},
        opencode_theme_mode="dark",
        session_name="console-opencode",
    )
    runtime_home = providers._runtime_home()
    assert os.path.join(runtime_home, ".opencode", "bin") in command
    assert "bash -ic" not in command
    assert providers._OPENCODE_LAUNCHER in command
    assert "OPENCODE_CONFIG_CONTENT=" in command
    assert "OPENCODE_TUI_CONFIG=" in command
    assert "OPENCODE_STATE_DIR=" in command
    assert "tui.dark.json" in command
    assert os.path.join(
        runtime_home,
        ".local",
        "state",
        "opencode-marvisx-console",
        "console-opencode",
    ) in command
    assert "--model openai/gpt-5.4" in command
    assert command.endswith(
        f"{providers._OPENCODE_LAUNCHER} /tmp/project --model openai/gpt-5.4"
    )


def test_claude_provider_keeps_direct_start_command():
    command = build_start_command(
        get_provider("claude"), "/tmp/project", model="sonnet[1m]"
    )

    assert "bash -ic" not in command
    assert (
        "cd /tmp/project && claude --dangerously-skip-permissions --model 'sonnet[1m]'"
        in command
    )


def test_codex_provider_uses_workspace_root_flag_and_yolo():
    command = build_start_command(
        get_provider("codex"),
        "/tmp/project",
        model="gpt-5.5",
        extra_cli_args=(
            "-c 'model_reasoning_effort=\"xhigh\"'",
            "-c 'service_tier=\"fast\"'",
            "-c model_context_window=1000000",
            "-c model_auto_compact_token_limit=950000",
        ),
    )

    assert "cd /tmp/project" not in command
    assert "codex --dangerously-bypass-approvals-and-sandbox -m gpt-5.5" in command
    assert "-c 'model_reasoning_effort=\"xhigh\"'" in command
    assert "-c 'service_tier=\"fast\"'" in command
    assert "-c model_context_window=1000000" in command
    assert "-c model_auto_compact_token_limit=950000" in command
    assert command.endswith("-C /tmp/project")


def test_opencode_launcher_loads_provider_and_mcp_envs():
    script = Path(providers._OPENCODE_LAUNCHER).read_text()

    for key in (
        "GROQ_API_KEY",
        "OPENAI_API_KEY",
        "GOOGLE_GENERATIVE_AI_API_KEY",
        "GEMINI_API_KEY",
        "TASKS_API_TOKEN",
        "PIR_API_URL",
        "EXA_API_KEY",
        "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET",
    ):
        assert f'load_key_from_file {key} "$file"' in script

    assert 'load_key_from_file ANTHROPIC_API_KEY "$file"' not in script

    assert 'export GOOGLE_GENERATIVE_AI_API_KEY="${GEMINI_API_KEY}"' in script
    assert 'export PIR_API_URL="${TASKS_API_URL}"' in script
    assert 'export PIR_API_URL="http://127.0.0.1:8100"' in script
    assert 'export COLORTERM="${COLORTERM:-truecolor}"' in script
    assert (
        'export OPENCODE_CONFIG_DIR="${OPENCODE_CONFIG_DIR:-$OPENCODE_RUNTIME_CONFIG_DIR}"'
        in script
    )
    assert (
        'export OPENCODE_TUI_CONFIG="${OPENCODE_TUI_CONFIG:-$OPENCODE_RUNTIME_CONFIG_DIR/tui.json}"'
        in script
    )


def test_opencode_tui_defaults_and_custom_themes_are_valid_json():
    api_root = Path(providers._OPENCODE_LAUNCHER).resolve().parents[1]
    runtime_root = api_root / "opencode-runtime"
    tui = json.loads((runtime_root / "tui.json").read_text())
    assert tui["scroll_speed"] == 3
    assert tui["scroll_acceleration"] == {"enabled": True}

    runtime_dark_tui = json.loads(
        (runtime_root / "tui.dark.json").read_text()
    )
    runtime_light_tui = json.loads(
        (runtime_root / "tui.light.json").read_text()
    )
    assert runtime_dark_tui["theme"] == "marvisx-dark"
    assert runtime_light_tui["theme"] == "marvisx-light"
    assert runtime_dark_tui["scroll_acceleration"] == {"enabled": True}
    assert runtime_light_tui["scroll_acceleration"] == {"enabled": True}

    for name in ("marvisx.json", "marvisx-dark.json", "marvisx-light.json"):
        runtime_theme = json.loads((runtime_root / "themes" / name).read_text())
        assert runtime_theme["$schema"] == "https://opencode.ai/theme.json"
        assert "theme" in runtime_theme
