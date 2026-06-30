# v1.1.0 - 2026-04-23 - Add opencode_pricing() for multi-provider shadow cost (PR4)
# v1.0.0 - 2026-04-22 - Extracted from claude_metrics.py for provider-agnostic metrics
"""Model registry: context windows + pricing lookup.

Centralizes model metadata previously scattered across claude_metrics.py so
OpenCode and future providers can reuse the same pricing table. Pricing is
loaded from kb/claude-pricing-<date>.json at import time (one place to update
rates without touching code).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from core.api.paths import repo_path

logger = logging.getLogger(__name__)


# Context windows per model (source: https://docs.anthropic.com/en/docs/models)
CONTEXT_WINDOWS: dict[str, int] = {
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-4-5": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
    "claude-haiku-4-5": 200_000,
    "haiku-3.5": 200_000,
}

# Most-conservative fallback when model is unknown (avoid overstating ctx%)
_DEFAULT_CONTEXT_WINDOW = 200_000


@dataclass(frozen=True)
class ModelPricing:
    """USD per 1M tokens. All fields in same unit."""

    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float

    @property
    def cache_write(self) -> float:
        """Legacy single cache_write rate (defaults to 5m TTL).

        Pre-PR2 callers use a single cache_write rate. PR2 splits 5m vs 1h.
        """
        return self.cache_write_5m


def _load_pricing_from_json() -> dict[str, ModelPricing]:
    """Load pricing from kb/claude-pricing-YYYY-MM-DD.json.

    Falls back to hard-coded table if file missing (e.g. test envs without kb/).
    """
    # kb/ is checked in at runtime root; supports both api/ and core/api/ layouts.
    kb_dir = repo_path(__file__, "kb")
    if kb_dir.is_dir():
        candidates = sorted(kb_dir.glob("claude-pricing-*.json"), reverse=True)
    else:
        candidates = []

    for pricing_path in candidates:
        try:
            with open(pricing_path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load pricing %s: %s", pricing_path, exc)
            continue

        models = data.get("models", {})
        result: dict[str, ModelPricing] = {}
        for model_id, rates in models.items():
            try:
                result[model_id] = ModelPricing(
                    input=float(rates["input"]),
                    output=float(rates["output"]),
                    cache_read=float(rates["cache_read"]),
                    cache_write_5m=float(rates["cache_write_5m"]),
                    cache_write_1h=float(rates["cache_write_1h"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning(
                    "Invalid pricing entry %s in %s: %s", model_id, pricing_path, exc
                )
        if result:
            logger.debug("Loaded pricing from %s (%d models)", pricing_path, len(result))
            return result

    # Hard-coded fallback mirrors kb/claude-pricing-2026-04-22.json
    logger.info("Using hard-coded pricing fallback (no kb/claude-pricing-*.json found)")
    return {
        "claude-opus-4-7": ModelPricing(5.0, 25.0, 0.5, 6.25, 10.0),
        "claude-opus-4-6": ModelPricing(5.0, 25.0, 0.5, 6.25, 10.0),
        "claude-sonnet-4-6": ModelPricing(3.0, 15.0, 0.3, 3.75, 6.0),
        "claude-sonnet-4-5": ModelPricing(3.0, 15.0, 0.3, 3.75, 6.0),
        "claude-haiku-4-5-20251001": ModelPricing(1.0, 5.0, 0.1, 1.25, 2.0),
        "claude-haiku-4-5": ModelPricing(1.0, 5.0, 0.1, 1.25, 2.0),
    }


CLAUDE_PRICING: dict[str, ModelPricing] = _load_pricing_from_json()

# Most-conservative fallback (haiku tier) when model unknown
_DEFAULT_PRICING_KEY = "claude-haiku-4-5-20251001"


# Legacy mirror for backward compat with claude_metrics.MODEL_PRICING shape
# (dict-of-dict with keys input/output/cache_read/cache_write). New code should
# use pricing() / CLAUDE_PRICING instead.
MODEL_PRICING: dict[str, dict[str, float]] = {
    mid: {
        "input": p.input,
        "output": p.output,
        "cache_read": p.cache_read,
        "cache_write": p.cache_write_5m,
    }
    for mid, p in CLAUDE_PRICING.items()
}


_MODEL_SUFFIX_RE = re.compile(r"\[\d+m\]$")


def normalize_model_id(model: str | None) -> str | None:
    """Strip context-window suffixes like [1m] from model IDs.

    Claude returns IDs like `claude-opus-4-7[1m]` when running in 1M-context
    mode; the pricing/context-window tables are keyed without the suffix.
    """
    if not model:
        return model
    return _MODEL_SUFFIX_RE.sub("", model)


def context_window(model: str | None) -> int:
    """Return context window size (tokens) for a model.

    Fallback is the most-conservative 200K to avoid overstating ctx% for
    unknown models. Opus 4.6+ runs at 1M in Claude Code even without the [1m]
    suffix, so we match partial 'opus' prefix for safety.
    """
    if not model:
        return _DEFAULT_CONTEXT_WINDOW
    norm = normalize_model_id(model) or ""
    if norm in CONTEXT_WINDOWS:
        return CONTEXT_WINDOWS[norm]
    # Heuristic: Opus 4.6+ always uses 1M context in Claude Code
    if "opus" in norm:
        return 1_000_000
    return _DEFAULT_CONTEXT_WINDOW


# Legacy alias — callers in claude_metrics.py + routers import this name
def get_context_window(model: str | None) -> int:
    return context_window(model)


def pricing(model: str | None) -> ModelPricing:
    """Return pricing for a model, falling back to conservative default.

    Unknown models fall back to haiku-tier (cheapest) so we don't overstate
    cost when the pricing table is stale.
    """
    if model:
        norm = normalize_model_id(model) or ""
        if norm in CLAUDE_PRICING:
            return CLAUDE_PRICING[norm]
    return CLAUDE_PRICING.get(
        _DEFAULT_PRICING_KEY,
        # Absolute last-resort if even the default key is missing
        ModelPricing(1.0, 5.0, 0.1, 1.25, 2.0),
    )


# --------------------------------------------------------------------------
# OpenCode multi-provider pricing (PR4 — shadow "cost_equivalent_usd").
# Matrix indexed by (providerID, modelID) so we can compute what a session
# WOULD cost at pay-per-token API rates even when the real `cost` is 0
# (OAuth / free tier). Fallback strategy is "skip": unknown combos return
# None so we NEVER guess a shadow cost — the UI shows only real cost in that
# case.
# --------------------------------------------------------------------------

_OPENCODE_PRICING_CACHE: dict | None = None


def _load_opencode_pricing() -> dict:
    """Lazy-load the most recent kb/opencode-pricing-*.json.

    Cached in-process after first read. Returns an empty `{"providers": {}}`
    if no file is found so callers can treat "unknown" as skip.
    """
    global _OPENCODE_PRICING_CACHE
    if _OPENCODE_PRICING_CACHE is not None:
        return _OPENCODE_PRICING_CACHE

    kb_dir = repo_path(__file__, "kb")
    if kb_dir.is_dir():
        candidates = sorted(kb_dir.glob("opencode-pricing-*.json"), reverse=True)
    else:
        candidates = []

    for path in candidates:
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Failed to load opencode pricing %s: %s", path, exc)
            continue
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            _OPENCODE_PRICING_CACHE = data
            logger.debug("Loaded opencode pricing from %s", path)
            return _OPENCODE_PRICING_CACHE

    logger.info(
        "No kb/opencode-pricing-*.json found — shadow cost disabled (fallback=skip)"
    )
    _OPENCODE_PRICING_CACHE = {"version": None, "providers": {}}
    return _OPENCODE_PRICING_CACHE


def opencode_pricing(provider_id: str | None, model_id: str | None) -> ModelPricing | None:
    """Return ModelPricing for an OpenCode (providerID, modelID) pair.

    Returns None when the pair is unknown — callers must treat "unknown" as
    "no shadow cost computed" (fallback_strategy=skip). null cache rates
    collapse to 0 so formula remains safe for providers without caching
    (Groq, Google).
    """
    if not provider_id or not model_id:
        return None
    data = _load_opencode_pricing()
    providers = data.get("providers", {})
    prov = providers.get(provider_id)
    if not isinstance(prov, dict):
        return None
    model = prov.get(model_id)
    if not isinstance(model, dict):
        return None
    try:
        return ModelPricing(
            input=float(model["input"]),
            output=float(model["output"]),
            cache_read=float(model.get("cache_read") or 0.0),
            cache_write_5m=float(model.get("cache_write_5m") or 0.0),
            cache_write_1h=float(model.get("cache_write_1h") or 0.0),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Invalid opencode pricing entry %s/%s: %s", provider_id, model_id, exc
        )
        return None


def opencode_pricing_version() -> str | None:
    """Version tag of the currently loaded opencode-pricing JSON (for audit)."""
    data = _load_opencode_pricing()
    version = data.get("version")
    return version if isinstance(version, str) else None
