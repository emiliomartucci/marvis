"""Governance configuration loader for MarvisX docs."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from core.api.paths import repo_path

DEFAULT_CONFIG_PATH = repo_path(__file__, "docs", ".governance.yml")

ALLOWED_LAYERS = frozenset(
    {
        "api",
        "mcp",
        "llm-gateway",
        "kg",
        "code-examples",
        "narrative",
        "concept",
    }
)


class GovernanceConfigError(ValueError):
    """Raised when docs governance config is missing or malformed."""


@lru_cache(maxsize=8)
def load_governance_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load the single root governance config and validate its shape."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise GovernanceConfigError(f"governance config not found: {config_path}") from exc
    except yaml.YAMLError as exc:
        raise GovernanceConfigError(f"governance config YAML error: {exc}") from exc

    if not isinstance(raw, dict):
        raise GovernanceConfigError("governance config must be a YAML mapping")

    layers = raw.get("layers")
    if not isinstance(layers, dict) or not layers:
        raise GovernanceConfigError("governance config must define non-empty layers map")
    if "auto_merge_on" in raw:
        raise GovernanceConfigError("auto_merge_on is not supported; use auto_pr_label_on")

    unknown = sorted(set(layers) - ALLOWED_LAYERS)
    if unknown:
        raise GovernanceConfigError(f"unknown governance layer(s): {unknown}")

    for layer, layer_config in layers.items():
        if not isinstance(layer_config, dict):
            raise GovernanceConfigError(f"layer {layer!r} config must be a mapping")
        if "auto_merge_on" in layer_config:
            raise GovernanceConfigError(f"layer {layer!r} uses removed auto_merge_on key")
        threshold = layer_config.get("threshold", raw.get("default_threshold", 1.0))
        try:
            threshold_value = float(threshold)
        except (TypeError, ValueError) as exc:
            raise GovernanceConfigError(f"layer {layer!r} threshold must be numeric") from exc
        if threshold_value < 0.0 or threshold_value > 1.0:
            raise GovernanceConfigError(f"layer {layer!r} threshold must be between 0 and 1")
        if threshold_value != 1.0:
            raise GovernanceConfigError(f"layer {layer!r} threshold must be 1.0")

    return raw


def layer_config(layer: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return merged defaults for one layer."""
    active = config or load_governance_config()
    layers = active.get("layers") or {}
    if layer not in layers:
        raise GovernanceConfigError(f"unknown governance layer: {layer}")
    merged = {
        "threshold": active.get("default_threshold", 1.0),
        "suggest_label_min_score": active.get("suggest_label_min_score", 0.85),
        "hard_gates": [],
        "auto_pr_label_on": active.get("auto_pr_label_on", {}),
        "human_required_on": ["any"],
    }
    merged.update(layers[layer])
    return merged
