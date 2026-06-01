"""Shared helpers for Mac Gateway AUX endpoints used by ingest parsers."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from core.api import config as _config

logger = logging.getLogger(__name__)

RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
RETRY_AFTER_CAP_SECONDS = 30.0


class MissingGatewayConfig(RuntimeError):
    """Raised when AUX endpoints are not configured in this environment."""


def settings():
    return _config.settings


def secret_value(value: Any) -> str:
    if hasattr(value, "get_secret_value"):
        return str(value.get_secret_value())
    return str(value or "")


def gateway_api_key() -> str:
    cfg = settings()
    if getattr(cfg, "pir_env", "") == "test":
        raise MissingGatewayConfig("AUX gateway is disabled under PIR_ENV=test")
    api_key = secret_value(getattr(cfg, "ingest_llm_gateway_api_key", None))
    if not api_key:
        raise MissingGatewayConfig("INGEST_LLM_GATEWAY_API_KEY is not configured")
    return api_key


def gateway_agent_name() -> str:
    cfg = settings()
    return str(getattr(cfg, "ingest_llm_gateway_agent_name", "") or "marvisx-ingester")


def gateway_priority() -> str:
    cfg = settings()
    priority = str(getattr(cfg, "ingest_llm_gateway_priority", "") or "batch").strip()
    return priority if priority in {"interactive", "batch", "background"} else "batch"


def aux_base_url() -> str:
    cfg = settings()
    if getattr(cfg, "pir_env", "") == "test":
        raise MissingGatewayConfig("AUX gateway is disabled under PIR_ENV=test")
    base_url = cfg.llm_gateway_aux_base_url or cfg.llm_gateway_base_url
    if not base_url:
        raise MissingGatewayConfig("LLM_GATEWAY_BASE_URL is not configured")

    base_url = base_url.rstrip("/")
    if cfg.llm_gateway_aux_base_url:
        return base_url

    # Production chat calls use LiteLLM on :4000. The ASR/OCR AUX proxy lives on
    # :9100 on the same Mac; public CF URLs already route by path and stay as-is.
    if base_url.startswith("http://100.103.221.55:4000"):
        return base_url.replace(":4000", ":9100", 1)
    return base_url


def auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {gateway_api_key()}",
        "X-Agent-Name": gateway_agent_name(),
        "X-Priority": gateway_priority(),
    }


def is_retryable_gateway_response(response: httpx.Response) -> bool:
    return response.status_code in RETRYABLE_STATUS_CODES


def retry_delay_seconds(response: httpx.Response | None, attempt: int) -> float:
    retry_after = response.headers.get("Retry-After") if response is not None else None
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), RETRY_AFTER_CAP_SECONDS)
        except ValueError:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                delta = (parsed - datetime.now(timezone.utc)).total_seconds()
                return min(max(delta, 0.0), RETRY_AFTER_CAP_SECONDS)
            except (TypeError, ValueError):
                pass
    return min(0.5 * attempt, 2.0)


async def request_gateway_with_retries(
    operation: Callable[[], Awaitable[httpx.Response]],
    *,
    service_name: str,
    max_attempts: int = 3,
) -> httpx.Response:
    for attempt in range(1, max_attempts + 1):
        response: httpx.Response | None = None
        try:
            response = await operation()
        except (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.PoolTimeout,
            httpx.ReadTimeout,
            httpx.RemoteProtocolError,
            httpx.WriteError,
            httpx.WriteTimeout,
        ) as exc:
            if attempt == max_attempts:
                raise RuntimeError(
                    f"{service_name} unavailable after {max_attempts} attempts"
                ) from exc
            logger.warning(
                "%s transient request error on attempt %s/%s: %s",
                service_name,
                attempt,
                max_attempts,
                exc.__class__.__name__,
            )
        else:
            if not is_retryable_gateway_response(response) or attempt == max_attempts:
                return response
            logger.warning(
                "%s transient HTTP %s on attempt %s/%s",
                service_name,
                response.status_code,
                attempt,
                max_attempts,
            )

        await asyncio.sleep(retry_delay_seconds(response, attempt))

    raise RuntimeError(f"{service_name} retry loop exited unexpectedly")
