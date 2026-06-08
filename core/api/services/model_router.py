# model_router.py v1.0.0 — Task-type based model routing for MarvisX
# Routes tasks to optimal LLM based on type, complexity, and cost constraints.
# Used by session_catalog to suggest models at session creation.

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RouteResult:
    model: str
    reason: str
    tier: str  # "premium", "standard", "budget"


# Task types that require deep reasoning → premium model
JUDGMENT_TYPES = frozenset({
    "debug", "security", "architecture", "review",
    "investigation", "audit", "planning",
})

# Task types that are lightweight → budget model
BUDGET_TYPES = frozenset({
    "search", "summary", "docs", "formatting",
    "session-check", "cleanup", "grep",
})

# Tags that signal high complexity regardless of type
HIGH_COMPLEXITY_TAGS = frozenset({
    "multi-file", "refactor", "migration", "cross-layer",
    "security", "architecture", "regression",
})

# Model tier definitions
MODELS = {
    "premium": "anthropic/claude-sonnet-4-6",
    "standard": "openai/gpt-5.4",
    "budget": "openai/gpt-5.4-mini",
}


def route_model(
    task_type: str = "",
    task_tags: Optional[list[str]] = None,
    complexity: int = 5,
    override: Optional[str] = None,
) -> RouteResult:
    """Route a task to the optimal model based on type, tags, and complexity.

    Args:
        task_type: Category of task (debug, code-gen, review, search, etc.)
        task_tags: Marvis task tags for signal extraction
        complexity: 1-10 estimated complexity
        override: Explicit model override (user choice wins)

    Returns:
        RouteResult with model ID, reason, and tier
    """
    if override:
        tier = "premium" if "claude" in override or "opus" in override else "standard"
        return RouteResult(model=override, reason="user override", tier=tier)

    tags = frozenset(t.lower() for t in (task_tags or []))
    task_type_lower = task_type.lower().strip()

    # High complexity tags → premium regardless of type
    if tags & HIGH_COMPLEXITY_TAGS and complexity >= 6:
        return RouteResult(
            model=MODELS["premium"],
            reason=f"high-complexity tags ({', '.join(tags & HIGH_COMPLEXITY_TAGS)})",
            tier="premium",
        )

    # Judgment tasks → premium
    if task_type_lower in JUDGMENT_TYPES:
        return RouteResult(
            model=MODELS["premium"],
            reason=f"judgment task type: {task_type_lower}",
            tier="premium",
        )

    # High complexity regardless of type → premium
    if complexity >= 8:
        return RouteResult(
            model=MODELS["premium"],
            reason=f"complexity {complexity}/10 exceeds premium threshold",
            tier="premium",
        )

    # Budget tasks → cheap model
    if task_type_lower in BUDGET_TYPES:
        return RouteResult(
            model=MODELS["budget"],
            reason=f"budget task type: {task_type_lower}",
            tier="budget",
        )

    # Low complexity → budget
    if complexity <= 3:
        return RouteResult(
            model=MODELS["budget"],
            reason=f"low complexity {complexity}/10",
            tier="budget",
        )

    # Default → standard
    return RouteResult(
        model=MODELS["standard"],
        reason="default standard tier",
        tier="standard",
    )


# Cost tracking per model (USD per 1M tokens, input/output)
MODEL_COSTS = {
    "anthropic/claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
    "anthropic/claude-opus-4-7": {"input": 5.00, "output": 25.00},
    "anthropic/claude-opus-4-6": {"input": 15.00, "output": 75.00},
    "openai/gpt-5.4": {"input": 2.00, "output": 8.00},
    "openai/gpt-5.4-mini": {"input": 0.20, "output": 0.80},
    "groq/qwen3-32b": {"input": 0.00, "output": 0.00},  # free tier
    "scaleway/devstral-2-123b-instruct-2512": {"input": 0.40, "output": 2.00},
    "scaleway/qwen3-coder-30b-a3b-instruct": {"input": 0.20, "output": 0.80},
}


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Estimate cost in USD for a model invocation."""
    costs = MODEL_COSTS.get(model, {"input": 1.00, "output": 5.00})
    return (input_tokens / 1_000_000) * costs["input"] + \
           (output_tokens / 1_000_000) * costs["output"]
