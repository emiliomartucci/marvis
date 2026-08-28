from __future__ import annotations

from core.api.services import providers, session_catalog, session_ops
from core.api.services.session_catalog import (
    WORKSPACE_ROOT,
    get_model_definition,
    get_permission_preset_definition,
    get_provider_definition,
    resolve_launch_directory,
)
from core.api.services.session_ops import build_session_start_spec


def test_all_providers_use_workspace_launch_root():
    for provider_name in ("claude", "gemini", "codex", "opencode"):
        provider = get_provider_definition(provider_name)
        assert provider.launch_root == "workspace"
        assert (
            resolve_launch_directory(provider_name, "/data/projects/marvisx")
            == WORKSPACE_ROOT
        )


def test_claude_default_model_and_1m_variant_are_available():
    provider = get_provider_definition("claude")
    default_model = get_model_definition("claude", None)
    long_model = get_model_definition("claude", "sonnet[1m]")

    assert provider.default_model == "claude-opus-4-7"
    assert default_model.cli_model == "claude-opus-4-7"
    assert long_model.supports_1m is True
    assert long_model.context_window == 1_000_000


def test_opencode_yolo_permission_preset_is_full_allow():
    preset = get_permission_preset_definition("opencode", "yolo")

    assert preset is not None
    assert preset.config_override == {"permission": "allow"}


def test_opencode_default_model_is_gpt54():
    provider = get_provider_definition("opencode")
    default_model = get_model_definition("opencode", None)
    launch = build_session_start_spec(
        "opencode",
        "marvisx",
        session_name="console-theme-dark",
        theme_mode="dark",
    )

    assert provider.default_model == "openai/gpt-5.4"
    assert default_model.cli_model == "openai/gpt-5.4"
    assert default_model.recommended is True
    assert "--model openai/gpt-5.4" in launch.start_command
    assert "tui.dark.json" in launch.start_command
    assert "OPENCODE_STATE_DIR=" in launch.start_command


def test_blank_model_is_available_for_all_providers():
    for provider_name in ("claude", "gemini", "codex", "opencode"):
        blank_model = get_model_definition(provider_name, "")
        launch = build_session_start_spec(provider_name, "marvisx", "")

        assert blank_model.id == ""
        assert blank_model.cli_model is None
        assert "--model" not in launch.start_command
        assert "-m " not in launch.start_command


def test_opencode_resume_start_spec_preserves_model_and_permission(monkeypatch):
    monkeypatch.setattr(session_catalog, "WORKSPACE_ROOT", "/var/marvisx/workspace")
    monkeypatch.setattr(providers.settings, "runtime_home", "/var/marvisx")
    launch = build_session_start_spec(
        "opencode",
        "marvisx",
        "openai/gpt-5.4",
        "yolo",
        resume_session_id="ses_296333d27ffeWpQ1ckV3VVikAy",
        session_name="console-theme-light",
        theme_mode="light",
    )

    assert "OPENCODE_CONFIG_CONTENT='{" in launch.start_command
    assert '"permission":"allow"' in launch.start_command
    assert "--model openai/gpt-5.4" in launch.start_command
    assert "--session ses_296333d27ffeWpQ1ckV3VVikAy" in launch.start_command
    assert "tui.light.json" in launch.start_command
    assert (
        "/var/marvisx/.local/state/opencode-marvisx-console/console-theme-light"
        in launch.start_command
    )


def test_codex_default_start_spec_enables_1m_context(monkeypatch):
    monkeypatch.setattr(session_catalog, "WORKSPACE_ROOT", "/var/marvisx/workspace")
    monkeypatch.setattr(providers.settings, "runtime_home", "/var/marvisx")
    launch = build_session_start_spec("codex", "marvisx")

    assert launch.model_id == "gpt-5.5"
    assert launch.cli_model == "gpt-5.5"
    assert "--dangerously-bypass-approvals-and-sandbox" in launch.start_command
    assert "-m gpt-5.5" in launch.start_command
    assert "-c 'model_reasoning_effort=\"xhigh\"'" in launch.start_command
    assert "-c 'service_tier=\"fast\"'" in launch.start_command
    assert "-c model_context_window=1000000" in launch.start_command
    assert "-c model_auto_compact_token_limit=950000" in launch.start_command
    assert "-C /var/marvisx/workspace" in launch.start_command


def test_workspace_launch_still_adds_project_access_dirs(monkeypatch):
    monkeypatch.setattr(session_catalog, "WORKSPACE_ROOT", "/var/marvisx/workspace")
    monkeypatch.setattr(providers.settings, "runtime_home", "/var/marvisx")
    monkeypatch.setattr(
        session_ops,
        "resolve_project_path",
        lambda _slug: "/var/marvisx/repos/propriofacile",
    )
    monkeypatch.setattr(
        session_ops,
        "resolve_project_access_paths",
        lambda _slug: (
            "/var/marvisx/repos/propriofacile",
            "/data/projects/propriofacile",
        ),
    )

    claude_launch = build_session_start_spec("claude", "propriofacile")
    gemini_launch = build_session_start_spec("gemini", "propriofacile")
    codex_launch = build_session_start_spec("codex", "propriofacile")

    assert (
        "cd /var/marvisx/workspace && claude --dangerously-skip-permissions"
        in claude_launch.start_command
    )
    assert "--add-dir /var/marvisx/repos/propriofacile" in claude_launch.start_command
    assert "--add-dir /data/projects/propriofacile" in claude_launch.start_command

    assert "cd /var/marvisx/workspace && gemini --yolo" in gemini_launch.start_command
    assert (
        "--include-directories /var/marvisx/repos/propriofacile"
        in gemini_launch.start_command
    )
    assert (
        "--include-directories /data/projects/propriofacile"
        in gemini_launch.start_command
    )

    assert "-C /var/marvisx/workspace" in codex_launch.start_command
    assert "--add-dir /var/marvisx/repos/propriofacile" in codex_launch.start_command
    assert "--add-dir /data/projects/propriofacile" in codex_launch.start_command
