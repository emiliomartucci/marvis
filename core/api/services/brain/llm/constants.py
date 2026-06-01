"""Brain v1.1 polish constants — tier, timeouts, retry budgets, prompts."""

from __future__ import annotations

import re
from typing import Final

# Model + tenant routing.
# tier-write = Gemma 3 12B QAT MLX, the dedicated WRITING tier:
#   - Italian prose production-grade (constraint-following + clean lexicon)
#   - No chain-of-thought, no <|think|> leakage risk in operator-facing output
#   - Warm latency 1.7-4s (handoff 186 benchmark), comparable to tier-fast warm
#   - Quota multiplier 0.5 → marvisx-brain batch 16 × 0.5 = 8 concurrent ample
# Future cherry-picks that classify (B-L4 near-dup, B-L5 owner_hint) route
# instead to tier-fast Gemma 4 E4B with optional <|think|> opt-in.
TIER_DEFAULT: Final[str] = "tier-write"
TENANT: Final[str] = "marvisx-brain"
AGENT_NAME: Final[str] = "brain"
LANE: Final[str] = "batch"

# Output budget per polish purpose (italian word count × ~1.5 with safety
# margin). tier-write has no thinking burn so no 4-8x multiplier needed.
MAX_TOKENS: Final[dict[str, int]] = {
    "journal": 1000,
    "finding_summary": 600,
    "finding_reasoning": 800,
}

# Polish prose needs slight variation, not deterministic extraction.
TEMPERATURE: Final[float] = 0.3

# Network defaults (overridable via settings).
DEFAULT_TIMEOUT_SECONDS: Final[int] = 30
DEFAULT_CACHE_TTL_SECONDS: Final[int] = 3600
DEFAULT_SEMAPHORE_SIZE: Final[int] = 8

# Retry: exponential backoff 1s, 2s, 4s (max attempts inclusive).
RETRY_MAX_ATTEMPTS: Final[int] = 3
RETRY_BACKOFF_BASE_SECONDS: Final[float] = 1.0
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429, 500, 502, 503, 504})

# Gemma 3 12B QAT always wraps JSON output in ```json fences.
# Strip leading ```json\n / ``` and trailing ``` (case-insensitive).
JSON_FENCE_REGEX: Final[re.Pattern[str]] = re.compile(
    r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.DOTALL
)
