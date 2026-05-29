# v1.0.0 - 2026-04-30 - LiteLLM gateway client wrapper for MarvisX
"""Public API for the LiteLLM gateway client.

Importers should never reach into `client.py` directly — use these symbols.
"""

from .client import (
    LLMGatewayClient,
    LLMGatewayUnavailable,
    get_llm_client,
)
from .url_validator import validate_llm_base_url

__all__ = [
    "LLMGatewayClient",
    "LLMGatewayUnavailable",
    "get_llm_client",
    "validate_llm_base_url",
]
