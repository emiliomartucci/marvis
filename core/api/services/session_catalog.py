from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from core.api.services.providers import ProviderName

LaunchRoot = Literal["project", "workspace"]

_HOME = str(Path.home())
WORKSPACE_ROOT = os.environ.get("MARVIS_WORKSPACE_ROOT", f"{_HOME}/workspace")


@dataclass(frozen=True, slots=True)
class SessionModelDefinition:
    id: str
    label: str
    cli_model: str | None
    description: str
    context_window: int | None = None
    supports_1m: bool = False
    recommended: bool = False
    experimental: bool = False
    note: str | None = None
    launch_args: tuple[str, ...] = ()


_BLANK_MODEL = SessionModelDefinition(
    id="",
    label="Blank",
    cli_model=None,
    description="Skip the model flag and let the CLI choose.",
)


@dataclass(frozen=True, slots=True)
class SessionPermissionPresetDefinition:
    id: str
    label: str
    badge: str
    description: str
    config_override: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SessionProviderDefinition:
    id: ProviderName
    label: str
    default_model: str
    models: tuple[SessionModelDefinition, ...]
    permission_presets: tuple[SessionPermissionPresetDefinition, ...] = ()
    launch_root: LaunchRoot = "workspace"
    note: str | None = None


_OPENCODE_DEFAULT_PRESET = SessionPermissionPresetDefinition(
    id="default",
    label="Trusted",
    badge="ext-dir",
    description="Allow trusted external dirs and skip doom-loop prompts.",
    config_override={
        "permission": {
            "doom_loop": "allow",
            "external_directory": {
                "/data/projects/**": "allow",
                f"{_HOME}/repos/**": "allow",
                f"{_HOME}/dev/**": "allow",
                f"{_HOME}/workspace/**": "allow",
            },
        },
    },
)

_OPENCODE_YOLO_PRESET = SessionPermissionPresetDefinition(
    id="yolo",
    label="YOLO",
    badge="allow",
    description="Allow all OpenCode permissions without prompting.",
    config_override={"permission": "allow"},
)


SESSION_PROVIDERS: dict[ProviderName, SessionProviderDefinition] = {
    "claude": SessionProviderDefinition(
        id="claude",
        label="Claude Code",
        default_model="claude-opus-4-7",
        note="Opus 4.7 tokenizer may increase token usage 1.0–1.35× vs 4.6.",
        models=(
            SessionModelDefinition(
                id="claude-opus-4-7",
                label="Opus 4.7",
                cli_model="claude-opus-4-7",
                description="Latest Opus — stronger SWE and agentic workflows.",
                recommended=True,
            ),
            SessionModelDefinition(
                id="claude-opus-4-7[1m]",
                label="Opus 4.7 1M",
                cli_model="claude-opus-4-7[1m]",
                description="Long-session Opus 4.7 with 1M context.",
                context_window=1_000_000,
                supports_1m=True,
            ),
            SessionModelDefinition(
                id="sonnet",
                label="Sonnet 4.6",
                cli_model="sonnet",
                description="Balanced Claude fallback for daily coding.",
            ),
            SessionModelDefinition(
                id="sonnet[1m]",
                label="Sonnet 4.6 1M",
                cli_model="sonnet[1m]",
                description="Long-session Sonnet with 1M context.",
                context_window=1_000_000,
                supports_1m=True,
            ),
            SessionModelDefinition(
                id="opus",
                label="Opus 4.6",
                cli_model="opus",
                description="Previous Opus — stable alias.",
            ),
            SessionModelDefinition(
                id="opus[1m]",
                label="Opus 4.6 1M",
                cli_model="opus[1m]",
                description="Long-session Opus 4.6 with 1M context.",
                context_window=1_000_000,
                supports_1m=True,
            ),
            SessionModelDefinition(
                id="opusplan",
                label="OpusPlan",
                cli_model="opusplan",
                description="Claude-only preset: Opus in plan, Sonnet in execution.",
            ),
            SessionModelDefinition(
                id="haiku",
                label="Haiku 4.5",
                cli_model="haiku",
                description="Fast low-cost fallback.",
            ),
        ),
    ),
    "gemini": SessionProviderDefinition(
        id="gemini",
        label="Gemini CLI",
        default_model="gemini-2.5-pro",
        models=(
            SessionModelDefinition(
                id="gemini-2.5-pro",
                label="Gemini 2.5 Pro",
                cli_model="gemini-2.5-pro",
                description="State-of-the-art Gemini model for code and long context.",
                context_window=1_048_576,
                supports_1m=True,
                recommended=True,
            ),
            SessionModelDefinition(
                id="gemini-2.5-flash",
                label="Gemini 2.5 Flash",
                cli_model="gemini-2.5-flash",
                description="Fast 1M-context Gemini for higher-volume runs.",
                context_window=1_048_576,
                supports_1m=True,
            ),
            SessionModelDefinition(
                id="gemini-3.1-pro-preview",
                label="Gemini 3.1 Pro Preview",
                cli_model="gemini-3.1-pro-preview",
                description="Preview model optimized for software engineering workflows.",
                context_window=1_048_576,
                supports_1m=True,
                experimental=True,
            ),
        ),
    ),
    "codex": SessionProviderDefinition(
        id="codex",
        label="Codex CLI",
        default_model="gpt-5.5",
        note="Defaults to GPT-5.5 with xhigh reasoning and fast service tier.",
        models=(
            SessionModelDefinition(
                id="gpt-5.5",
                label="GPT-5.5",
                cli_model="gpt-5.5",
                description="Latest GPT-5.5 coding model for Codex.",
                context_window=1_000_000,
                supports_1m=True,
                recommended=True,
                note="Launches with xhigh reasoning, fast service tier, and 1M compaction window.",
                launch_args=(
                    "-c 'model_reasoning_effort=\"xhigh\"'",
                    "-c 'service_tier=\"fast\"'",
                    "-c model_context_window=1000000",
                    "-c model_auto_compact_token_limit=950000",
                ),
            ),
            SessionModelDefinition(
                id="gpt-5.4",
                label="GPT-5.4",
                cli_model="gpt-5.4",
                description="Frontier OpenAI coding and professional-work model.",
                context_window=1_050_000,
                supports_1m=True,
                note="OpenAI charges higher long-context rates above 272K input tokens.",
                launch_args=(
                    "-c model_context_window=1000000",
                    "-c model_auto_compact_token_limit=950000",
                ),
            ),
            SessionModelDefinition(
                id="gpt-5.4-mini",
                label="GPT-5.4 Mini",
                cli_model="gpt-5.4-mini",
                description="Fast GPT-5.4 variant for lighter loops.",
            ),
            SessionModelDefinition(
                id="gpt-5.3-codex",
                label="GPT-5.3 Codex",
                cli_model="gpt-5.3-codex",
                description="Codex-tuned model for implementation-heavy work.",
            ),
            SessionModelDefinition(
                id="gpt-5.3-codex-spark",
                label="GPT-5.3 Codex Spark",
                cli_model="gpt-5.3-codex-spark",
                description="Ultra-fast Codex option for short loops.",
            ),
            SessionModelDefinition(
                id="gpt-5.2-codex",
                label="GPT-5.2 Codex",
                cli_model="gpt-5.2-codex",
                description="Stable fallback for Codex-tuned behavior.",
            ),
        ),
    ),
    "opencode": SessionProviderDefinition(
        id="opencode",
        label="OpenCode",
        default_model="openai/gpt-5.4",
        note="Defaults to GPT-5.4 with xhigh reasoning; permissions are controlled via OpenCode config, not a dedicated --yolo flag.",
        permission_presets=(
            _OPENCODE_DEFAULT_PRESET,
            _OPENCODE_YOLO_PRESET,
        ),
        models=(
            SessionModelDefinition(
                id="openai/gpt-5.4",
                label="GPT-5.4",
                cli_model="openai/gpt-5.4",
                description="Default OpenCode model with xhigh reasoning.",
                context_window=1_050_000,
                supports_1m=True,
                recommended=True,
                note="OpenAI long-context pricing changes above 272K input tokens.",
            ),
            SessionModelDefinition(
                id="anthropic/claude-sonnet-4-6",
                label="Claude Sonnet 4.6",
                cli_model="anthropic/claude-sonnet-4-6",
                description="Balanced Anthropic alternative inside OpenCode.",
                context_window=1_000_000,
                supports_1m=True,
            ),
            SessionModelDefinition(
                id="anthropic/claude-opus-4-7",
                label="Claude Opus 4.7",
                cli_model="anthropic/claude-opus-4-7",
                description="Latest Opus — stronger SWE and agentic workflows in OpenCode.",
                context_window=1_000_000,
                supports_1m=True,
            ),
            SessionModelDefinition(
                id="anthropic/claude-opus-4-6",
                label="Claude Opus 4.6",
                cli_model="anthropic/claude-opus-4-6",
                description="Previous Opus — stable fallback in OpenCode.",
                context_window=1_000_000,
                supports_1m=True,
            ),
            SessionModelDefinition(
                id="google/gemini-2.5-pro",
                label="Gemini 2.5 Pro",
                cli_model="google/gemini-2.5-pro",
                description="1M-context Gemini option in OpenCode.",
                context_window=1_048_576,
                supports_1m=True,
            ),
            SessionModelDefinition(
                id="google/gemini-2.5-flash",
                label="Gemini 2.5 Flash",
                cli_model="google/gemini-2.5-flash",
                description="Fast 1M-context Gemini option in OpenCode.",
                context_window=1_048_576,
                supports_1m=True,
            ),
            SessionModelDefinition(
                id="google/gemini-3.1-pro-preview",
                label="Gemini 3.1 Pro Preview",
                cli_model="google/gemini-3.1-pro-preview",
                description="Preview Gemini model for agentic software work.",
                context_window=1_048_576,
                supports_1m=True,
                experimental=True,
            ),
            SessionModelDefinition(
                id="groq/qwen/qwen3-32b",
                label="Qwen3 32B",
                cli_model="groq/qwen/qwen3-32b",
                description="Recommended open-model default.",
            ),
            SessionModelDefinition(
                id="groq/llama-3.3-70b-versatile",
                label="Llama 3.3 70B",
                cli_model="groq/llama-3.3-70b-versatile",
                description="Open fallback for broader generation tasks.",
            ),
        ),
    ),
}


def list_provider_definitions() -> tuple[SessionProviderDefinition, ...]:
    return tuple(SESSION_PROVIDERS.values())


def get_provider_definition(provider: ProviderName) -> SessionProviderDefinition:
    try:
        return SESSION_PROVIDERS[provider]
    except KeyError as exc:
        raise ValueError(f"Unknown provider catalog: {provider}") from exc


def get_model_definition(
    provider: ProviderName, model_id: str | None
) -> SessionModelDefinition:
    provider_def = get_provider_definition(provider)
    if model_id is None:
        target = provider_def.default_model
    elif model_id == _BLANK_MODEL.id:
        return _BLANK_MODEL
    else:
        target = model_id
    for model in provider_def.models:
        if model.id == target:
            return model
    raise ValueError(
        f"Unknown model {target!r} for provider {provider}. "
        f"Available: {', '.join(model.id for model in provider_def.models)}"
    )


def list_catalog_models(provider: ProviderName) -> tuple[SessionModelDefinition, ...]:
    provider_def = get_provider_definition(provider)
    return (_BLANK_MODEL, *provider_def.models)


def get_permission_preset_definition(
    provider: ProviderName,
    preset_id: str | None,
) -> SessionPermissionPresetDefinition | None:
    provider_def = get_provider_definition(provider)
    if not provider_def.permission_presets:
        return None
    target = preset_id or provider_def.permission_presets[0].id
    for preset in provider_def.permission_presets:
        if preset.id == target:
            return preset
    raise ValueError(
        f"Unknown permission preset {target!r} for provider {provider}. "
        f"Available: {', '.join(preset.id for preset in provider_def.permission_presets)}"
    )


def resolve_launch_directory(provider: ProviderName, project_path: str) -> str:
    provider_def = get_provider_definition(provider)
    if provider_def.launch_root == "workspace":
        return WORKSPACE_ROOT
    return project_path
