# v1.0.0 - 2026-05-26 - M1 CAPTURE U5 — per-function LLM config + provider keys
"""Pydantic models for BYOK provider keys and per-function LLM configuration.

The function_name enum (classify | embedding | brain) and the provider enum are a
prerequisite contract that M4 (reflect/brain) and M8.2 (wizard) consume as-designed.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

LLMFunction = Literal["classify", "embedding", "brain"]
LLMProvider = Literal["openai", "anthropic", "ollama", "openai_compatible", "mac_gateway"]

# Providers that need an API key (others authenticate via base_url / local).
KEYED_PROVIDERS = frozenset({"openai", "anthropic", "openai_compatible"})


class ProviderKeyCreateRequest(BaseModel):
    provider: LLMProvider
    label: str | None = Field(default=None, max_length=128)
    api_key: str | None = Field(
        default=None,
        description="Plaintext provider key — encrypted at rest, never stored or returned in plaintext. "
        "Optional for keyless providers (ollama / mac_gateway).",
    )
    base_url: str | None = Field(default=None, max_length=512)


class ProviderKeyResponse(BaseModel):
    id: str
    provider: LLMProvider
    label: str | None = None
    base_url: str | None = None
    has_key: bool = False
    key_prefix: str | None = None  # first chars only; None if no key or unreadable
    key_status: Literal["none", "set", "unreadable"] = "none"
    created_at: str
    updated_at: str


class LLMFunctionConfigUpdate(BaseModel):
    provider_key_id: str | None = None
    model: str | None = Field(default=None, max_length=128)
    enabled: bool = False


class LLMFunctionConfigItem(BaseModel):
    function_name: LLMFunction
    provider_key_id: str | None = None
    provider: LLMProvider | None = None
    model: str | None = None
    enabled: bool = False
    # configured = a usable provider is wired; disabled_no_provider = auto-run off.
    status: Literal["configured", "disabled_no_provider"] = "disabled_no_provider"
