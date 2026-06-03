# v1.1.0 - 2026-05-26 - Add submit_and_wait transient-failure retry (litellm_error/upstream_5xx)
"""Async LLM Gateway client — submit + poll pattern.

Wraps the Phase 1.5 Queue Gateway endpoints:
- POST /v1/chat/completions/async → 202 + job_id + status_url + Retry-After
- GET  /v1/jobs/{id} → status (queued|processing|done|failed) + result/error

Use for **long-running** tasks (SEO articles 70-90s, transcript analysis, image
inference) where holding an HTTP connection sync would risk timeout.

For **short** interactive tasks (inbox tldr 2-5s) keep using `LLMGatewayClient`
sync — the queue layer adds ~50ms overhead without observable benefit.

Pattern (matches Stripe Idempotency):
    async with LLMGatewayAsyncClient() as client:
        result = await client.submit_and_wait(
            model="tier-think",
            messages=[{"role": "user", "content": "..."}],
            priority="batch",
            idempotency_key=f"seo:article:{slug}",  # stable id → safe-retry
        )

Errors:
- `LLMGatewayQuotaExceeded` 429 → consumer must retry [400ms, 1500ms, 4000ms]
  exponential backoff (carry-forward learning 16cc554d)
- `LLMGatewayJobFailed` job processed but LiteLLM/LM Studio errored. When the
  error is transient (litellm_error, upstream_5xx, model_not_loaded) the
  failure is often the JIT auto-load reload race documented in
  `docs/runbooks/mac-llm-gateway.md#drift-watchdog-tier-fast`. Pass
  `retry_on_transient_failure=3` to `submit_and_wait` to absorb it with the
  integration-guide 5s/15s/45s backoff.
- `LLMGatewayJobNotFound` 404 — gateway lost the job (Redis crash 1s window)
  → safe to resubmit with same Idempotency-Key
- `asyncio.TimeoutError` poll exceeded `timeout_seconds`
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Iterable, Literal

import httpx

from core.api import config as _config
from core.api.services.local_llm.url_validator import validate_llm_base_url

logger = logging.getLogger(__name__)

Lane = Literal["interactive", "batch", "background"]

# Substrings (lowercase) that identify a transient upstream LLM gateway failure
# worth retrying. Matches the queue gateway error envelope, integration guide
# §"Failure Modes" 500-504, and the drift watchdog race window.
TRANSIENT_FAILURE_MARKERS: tuple[str, ...] = (
    "litellm_error",
    "upstream_5xx",
    "upstream_error",
    "model_not_loaded",
    "model not loaded",
    "worker_timeout",
)

DEFAULT_TRANSIENT_RETRY_BACKOFF_SECONDS: tuple[int, ...] = (5, 15, 45)
TierName = Literal[
    "tier-fast",
    "tier-think",
    "tier-vision",
    "tier-write",
    "tier-ocr",
    "tier-docparse",
    "tier-transcribe",
]


def _settings():
    """Late-bind settings access (matches sync client pattern)."""
    return _config.settings


class LLMGatewayQuotaExceeded(RuntimeError):
    """429 — quota exceeded. retry_after_seconds advises backoff."""

    def __init__(self, message: str, retry_after_seconds: int) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(message)


class LLMGatewayJobFailed(RuntimeError):
    """Job reached state=failed (LiteLLM/LM Studio error or worker timeout)."""

    def __init__(self, error: dict[str, Any] | None) -> None:
        self.error = error or {}
        super().__init__(self.error.get("message", "job failed"))


class LLMGatewayJobNotFound(RuntimeError):
    """404 polling — gateway lost the job (Redis crash window <1s).

    Caller should resubmit with the same Idempotency-Key for safe retry.
    """


def _secret_value(api_key: Any) -> str:
    if hasattr(api_key, "get_secret_value"):
        return api_key.get_secret_value()
    return str(api_key)


class LLMGatewayAsyncClient:
    """Submit + poll wrapper for the Phase 1.5 Queue Gateway."""

    def __init__(
        self,
        *,
        api_key: Any | None = None,
        base_url: str | None = None,
        agent_name: str = "marvisx",
    ) -> None:
        settings = _settings()
        gateway_base_url = base_url or settings.llm_gateway_base_url
        gateway_api_key = api_key or settings.llm_gateway_api_key

        if not gateway_base_url:
            raise RuntimeError("LLM_GATEWAY_BASE_URL is not configured")
        if not gateway_api_key:
            raise RuntimeError("LLM_GATEWAY_API_KEY is not configured")
        if getattr(settings, "llm_gateway_enforce_public_base_url", False):
            validate_llm_base_url(gateway_base_url)

        self._api_key = _secret_value(gateway_api_key)
        self._agent_name = agent_name
        self._base_url = gateway_base_url.rstrip("/")
        if self._base_url.endswith("/v1"):
            self._base_url = self._base_url.removesuffix("/v1")

        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=httpx.Timeout(connect=5.0, read=15.0, write=10.0, pool=5.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        )

    async def __aenter__(self) -> "LLMGatewayAsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(
        self,
        priority: Lane | None,
        idempotency_key: str | None,
        extra: dict[str, str] | None,
    ) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self._api_key}",
            "X-Agent-Name": self._agent_name,
        }
        if priority:
            h["X-Priority"] = priority
        if idempotency_key:
            h["Idempotency-Key"] = idempotency_key
        if extra:
            h.update(extra)
        return h

    async def submit(
        self,
        *,
        model: TierName,
        messages: Iterable[dict[str, Any]],
        priority: Lane = "batch",
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Enqueue job. Returns 202 body `{id, status, status_url, eta_seconds}`.

        On Idempotency-Key replay: returns cached `JobAccepted` + `Idempotent-Replay: true`.
        """
        body: dict[str, Any] = {"model": model, "messages": list(messages)}
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
        if response_format is not None:
            body["response_format"] = response_format
        if extra_body:
            body.update(extra_body)

        # OTel httpx instrumentation auto-injects traceparent in headers.
        resp = await self._http.post(
            "/v1/chat/completions/async",
            json=body,
            headers=self._headers(priority, idempotency_key, extra_headers),
        )

        if resp.status_code == 429:
            err = self._parse_error(resp)
            retry_after = int(resp.headers.get("Retry-After", "30"))
            raise LLMGatewayQuotaExceeded(
                err.get("message", "quota exceeded"), retry_after
            )
        if resp.status_code == 409:
            err = self._parse_error(resp)
            raise LLMGatewayQuotaExceeded(
                err.get("message", "in-flight duplicate"),
                int(resp.headers.get("Retry-After", "5")),
            )
        resp.raise_for_status()
        return resp.json()

    async def poll(
        self,
        job_id: str,
        *,
        timeout_seconds: int = 600,
        initial_delay: int = 5,
    ) -> dict[str, Any]:
        """Poll until done|failed or timeout. Returns the OpenAI ChatCompletion result."""
        delay = initial_delay
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            r = await self._http.get(
                f"/v1/jobs/{job_id}",
                headers=self._headers(priority=None, idempotency_key=None, extra=None),
            )
            if r.status_code == 200:
                data = r.json()
                status = data.get("status")
                if status == "done":
                    return data["result"]
                if status == "failed":
                    raise LLMGatewayJobFailed(data.get("error"))
                # queued or processing — honor server Retry-After
                delay = int(r.headers.get("Retry-After", str(delay)))
            elif r.status_code == 404:
                raise LLMGatewayJobNotFound(job_id)
            elif r.status_code == 429:
                # Polling rate limit — honor Retry-After
                delay = int(r.headers.get("Retry-After", "1"))
            else:
                # 5xx etc — backoff and retry
                logger.warning("poll %s: unexpected %d", job_id, r.status_code)
                delay = max(delay, 5)

            await asyncio.sleep(delay)

        raise asyncio.TimeoutError(
            f"job {job_id} did not complete within {timeout_seconds}s"
        )

    async def submit_and_wait(
        self,
        *,
        model: TierName,
        messages: Iterable[dict[str, Any]],
        priority: Lane = "batch",
        max_tokens: int | None = None,
        temperature: float | None = None,
        response_format: dict[str, Any] | None = None,
        extra_body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        extra_headers: dict[str, str] | None = None,
        timeout_seconds: int = 600,
        initial_poll_delay_seconds: int | None = None,
        retry_on_transient_failure: int = 0,
        retry_backoff_seconds: tuple[int, ...] = DEFAULT_TRANSIENT_RETRY_BACKOFF_SECONDS,
    ) -> dict[str, Any]:
        """Convenience: submit + poll. Returns OpenAI ChatCompletion result.

        Use for sync-style API but with queue benefits (no HTTP timeout, fairness,
        observability). Per long-running tasks that exceed httpx timeouts.
        Pass initial_poll_delay_seconds for tiny jobs where queue ETA is too
        conservative and early completion is common.

        retry_on_transient_failure: max retries when the job ends in `failed` with
            a transient upstream error (litellm_error, upstream_5xx, model_not_loaded).
            Deterministic errors (auth, invalid_payload, context_length_exceeded) are
            never retried. Each retry submits a fresh job (idempotency_key is dropped
            on retry so the gateway does not replay a cached failure).
        retry_backoff_seconds: per-attempt sleep before the next retry. Defaults to
            integration-guide values (5, 15, 45).
        """
        idem = idempotency_key
        for attempt in range(retry_on_transient_failure + 1):
            accepted = await self.submit(
                model=model,
                messages=messages,
                priority=priority,
                max_tokens=max_tokens,
                temperature=temperature,
                response_format=response_format,
                extra_body=extra_body,
                idempotency_key=idem,
                extra_headers=extra_headers,
            )
            try:
                return await self.poll(
                    accepted["id"],
                    timeout_seconds=timeout_seconds,
                    initial_delay=(
                        initial_poll_delay_seconds
                        if initial_poll_delay_seconds is not None
                        else int(accepted.get("eta_seconds", 5)) or 5
                    ),
                )
            except LLMGatewayJobFailed as exc:
                if attempt >= retry_on_transient_failure:
                    raise
                if not self._is_transient_job_failure(exc.error):
                    raise
                wait_s = retry_backoff_seconds[
                    min(attempt, len(retry_backoff_seconds) - 1)
                ]
                logger.warning(
                    "submit_and_wait transient failure attempt=%d/%d sleep=%ds error=%s",
                    attempt + 1,
                    retry_on_transient_failure + 1,
                    wait_s,
                    exc.error,
                )
                await asyncio.sleep(wait_s)
                # Drop the caller's idempotency key on retry. The gateway caches
                # a failed job under that key for 24h, so re-submitting with the
                # same key returns the cached failure instead of running again.
                idem = None
        raise RuntimeError("submit_and_wait retry loop exited without result")

    @staticmethod
    def _is_transient_job_failure(error: dict[str, Any] | None) -> bool:
        """True if the queue gateway error envelope looks transient (worth retrying).

        Matches substrings in TRANSIENT_FAILURE_MARKERS against type/code/message.
        """
        if not error:
            return False
        haystack = " ".join(
            str(error.get(field, "")) for field in ("type", "code", "message")
        ).lower()
        return any(marker in haystack for marker in TRANSIENT_FAILURE_MARKERS)

    @staticmethod
    def _parse_error(resp: httpx.Response) -> dict[str, Any]:
        """OpenAI-compatible error envelope: {"error": {message, type, code}}."""
        try:
            body = resp.json()
        except json.JSONDecodeError:
            return {"message": resp.text or "unknown error"}
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return err
            return body
        return {"message": str(body)}


# ---------------------------------------------------------------------------
# Singleton accessor (matches sync client pattern)
# ---------------------------------------------------------------------------

_async_client_singleton: LLMGatewayAsyncClient | None = None


def get_async_llm_client() -> LLMGatewayAsyncClient:
    """Return the process-wide async LLM gateway client."""
    global _async_client_singleton
    if _async_client_singleton is None:
        _async_client_singleton = LLMGatewayAsyncClient()
    return _async_client_singleton


async def reset_async_llm_client() -> None:
    """Reset singleton (test helper / lifespan teardown)."""
    global _async_client_singleton
    if _async_client_singleton is not None:
        await _async_client_singleton.aclose()
        _async_client_singleton = None
