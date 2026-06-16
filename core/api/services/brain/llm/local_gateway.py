"""Mac Gateway tier-write provider for Brain v1.1 polish layer.

Issues OpenAI-compatible chat completions through the Queue Gateway public
endpoint (configured via `BRAIN_LLM_GATEWAY_BASE_URL`, e.g.
`https://llm.example.com/v1/chat/completions`). Tenant
`marvisx-brain` is the dedicated quota lane — never falls back to the
generic `LLM_GATEWAY_API_KEY` (learning d8bc7da2).

Retry policy is exponential backoff on transient gateway errors only;
parse/grounding failures bubble up as `PolishResult.failed` so the caller
exposes the deterministic baseline immediately.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from core.api.services.brain.llm.base import PolishPurpose, PolishResult
from core.api.services.brain.llm.constants import (
    AGENT_NAME,
    LANE,
    RETRYABLE_STATUS_CODES,
    TEMPERATURE,
)
from core.api.services.brain.llm.direction_alignment import (
    classify_direction_alignment_impl,
)
from core.api.services.brain.llm.parsers import (
    ParseError,
    coerce_cited_refs,
    coerce_text,
    parse_json_or_raise,
)

logger = logging.getLogger(__name__)


def _secret_value(api_key: Any) -> str:
    if hasattr(api_key, "get_secret_value"):
        return str(api_key.get_secret_value())
    return str(api_key)


class LocalGatewayBrainService:
    """Mac Gateway tier-write client dedicated to Brain polish calls."""

    def __init__(
        self,
        *,
        api_key: Any,
        base_url: str,
        tenant: str,
        model: str,
        timeout_seconds: int,
        retry_max_attempts: int,
        retry_backoff_seconds: float,
        semaphore_size: int,
        grounding_strict: bool,
    ) -> None:
        if not api_key:
            raise RuntimeError("Brain LLM gateway api_key is required")
        if not base_url:
            raise RuntimeError("Brain LLM gateway base_url is required")

        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._tenant = tenant
        self._model = model
        self._timeout_seconds = max(5, int(timeout_seconds))
        self._retry_max_attempts = max(1, int(retry_max_attempts))
        self._retry_backoff_seconds = max(0.1, float(retry_backoff_seconds))
        self._semaphore = asyncio.Semaphore(max(1, int(semaphore_size)))
        self._grounding_strict = grounding_strict
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    @property
    def model(self) -> str:
        return self._model

    @property
    def grounding_strict(self) -> bool:
        return self._grounding_strict

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._client is None:
                self._client = httpx.AsyncClient(
                    base_url=self._base_url,
                    timeout=httpx.Timeout(self._timeout_seconds),
                )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def call_polish(
        self,
        *,
        purpose: PolishPurpose,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        idempotency_key: str | None = None,
    ) -> PolishResult:
        """Issue the chat call with retry. Never raises — returns PolishResult.failed."""
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": int(max_tokens),
            "temperature": TEMPERATURE,
            "response_format": {"type": "json_object"},
        }
        headers = self._build_headers(idempotency_key=idempotency_key)

        async with self._semaphore:
            response_data, failure = await self._post_with_retries(
                payload=payload,
                headers=headers,
                purpose=purpose,
            )
        if failure is not None:
            return failure
        return self._parse_response(
            purpose=purpose,
            response_data=response_data,
        )

    async def call_json(
        self,
        *,
        model: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        temperature: float,
        idempotency_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Generic JSON-object chat call with per-call model + temperature.

        Used by Brain v1.2.1 DR8 classifier (tier-fast classify + tier-write
        rewrite). Returns parsed dict on success, None on any failure
        (transport, non-2xx, malformed JSON). Never raises — caller falls
        back to deterministic baseline.
        """
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": int(max_tokens),
            "temperature": float(temperature),
            "response_format": {"type": "json_object"},
        }
        headers = self._build_headers(idempotency_key=idempotency_key)

        async with self._semaphore:
            response_data, failure = await self._post_with_retries(
                payload=payload,
                headers=headers,
                purpose="finding_summary",  # reused log tag; no purpose semantics here
            )
        if failure is not None:
            logger.warning(
                "brain_llm_classify_call_failed model=%s reason=%s",
                model,
                failure.failure_reason,
            )
            return None
        if not response_data:
            return None
        try:
            choices = response_data["choices"]
            message = choices[0]["message"]
            content = message.get("content", "")
        except (KeyError, IndexError, TypeError):
            logger.warning("brain_llm_classify_unexpected_shape model=%s", model)
            return None
        if not content:
            logger.warning("brain_llm_classify_empty_content model=%s", model)
            return None
        try:
            return parse_json_or_raise(content)
        except ParseError as exc:
            logger.warning(
                "brain_llm_classify_parse_failed model=%s reason=%s",
                model,
                str(exc),
            )
            return None

    async def classify_direction_alignment(
        self, payload: dict[str, Any]
    ) -> dict[str, Any] | None:
        """DR8 classifier surface — see direction_alignment.py."""
        return await classify_direction_alignment_impl(
            llm_surface=self, payload=payload
        )

    def _build_headers(self, *, idempotency_key: str | None) -> dict[str, str]:
        # Wave 3.1 silent-skip-v3 fix: Mac Gateway default mode is async — it
        # returns HTTP 202 `queue.accepted` with a `status_url` to poll. The
        # polish path expects a synchronous chat.completion envelope, so we
        # ask the gateway to bypass the queue with `X-Sync: 1`. Verified
        # against the tier-write Gemma 3 12B QAT MLX backend: header alone
        # forces a synchronous 200 with the full chat.completion body
        # (~1s wall vs ~25s for queue-mode round-trip).
        headers = {
            "Authorization": f"Bearer {_secret_value(self._api_key)}",
            "Content-Type": "application/json",
            "X-Tenant": self._tenant,
            "X-Agent-Name": AGENT_NAME,
            "X-Priority": LANE,
            "X-Sync": "1",
        }
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        return headers

    async def _post_with_retries(
        self,
        *,
        payload: dict[str, Any],
        headers: dict[str, str],
        purpose: PolishPurpose,
    ) -> tuple[dict[str, Any] | None, PolishResult | None]:
        last_status: int | None = None
        last_error: str | None = None
        for attempt in range(1, self._retry_max_attempts + 1):
            try:
                client = await self._get_client()
                response = await client.post(
                    "/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                last_error = f"timeout:{exc.__class__.__name__}"
                if attempt >= self._retry_max_attempts:
                    return None, PolishResult.failed(
                        purpose=purpose,
                        reason="gateway_timeout",
                        model=self._model,
                    )
                await asyncio.sleep(self._backoff_delay(attempt))
                continue
            except httpx.ConnectError as exc:
                last_error = f"connect:{exc.__class__.__name__}"
                if attempt >= self._retry_max_attempts:
                    return None, PolishResult.failed(
                        purpose=purpose,
                        reason="gateway_connect_error",
                        model=self._model,
                    )
                await asyncio.sleep(self._backoff_delay(attempt))
                continue
            except httpx.HTTPError as exc:
                last_error = f"http:{exc.__class__.__name__}"
                logger.warning(
                    "brain_llm_gateway_http_error purpose=%s attempt=%d/%d",
                    purpose,
                    attempt,
                    self._retry_max_attempts,
                    exc_info=True,
                )
                return None, PolishResult.failed(
                    purpose=purpose,
                    reason=f"gateway_http_error:{last_error}",
                    model=self._model,
                )

            last_status = response.status_code
            if response.status_code == 200:
                try:
                    return response.json(), None
                except ValueError as exc:
                    logger.warning(
                        "brain_llm_gateway_invalid_json purpose=%s",
                        purpose,
                        exc_info=True,
                    )
                    return None, PolishResult.failed(
                        purpose=purpose,
                        reason=f"gateway_invalid_envelope_json:{exc}",
                        model=self._model,
                    )

            if response.status_code in RETRYABLE_STATUS_CODES:
                if attempt >= self._retry_max_attempts:
                    return None, PolishResult.failed(
                        purpose=purpose,
                        reason=f"gateway_transient_status_{response.status_code}",
                        model=self._model,
                    )
                await asyncio.sleep(self._backoff_delay(attempt))
                continue

            logger.warning(
                "brain_llm_gateway_non_retryable purpose=%s status=%d body=%s",
                purpose,
                response.status_code,
                response.text[:300],
            )
            return None, PolishResult.failed(
                purpose=purpose,
                reason=f"gateway_status_{response.status_code}",
                model=self._model,
            )

        # Should be unreachable — retries either return success or failure above.
        return None, PolishResult.failed(
            purpose=purpose,
            reason=f"gateway_retry_exhausted:{last_status}:{last_error}",
            model=self._model,
        )

    def _backoff_delay(self, attempt: int) -> float:
        return self._retry_backoff_seconds * (2 ** (attempt - 1))

    def _parse_response(
        self,
        *,
        purpose: PolishPurpose,
        response_data: dict[str, Any] | None,
    ) -> PolishResult:
        if not response_data:
            return PolishResult.failed(
                purpose=purpose, reason="empty_response", model=self._model
            )
        try:
            choices = response_data["choices"]
            message = choices[0]["message"]
            content = message.get("content", "")
        except (KeyError, IndexError, TypeError):
            logger.warning("brain_llm_gateway_unexpected_shape purpose=%s", purpose)
            return PolishResult.failed(
                purpose=purpose,
                reason="unexpected_response_shape",
                model=self._model,
            )

        if not content:
            return PolishResult.failed(
                purpose=purpose, reason="empty_content", model=self._model
            )

        # Defensive: tier-write never emits the `<|channel>thought` marker that
        # Gemma 4 E4B uses in thinking mode. If we somehow see it (mis-routed
        # tier, model regression), log it and let the JSON parser fall through —
        # parse_json_or_raise will reject the noisy content cleanly.
        if "<|channel" in content:
            logger.warning(
                "brain_llm_unexpected_channel_marker purpose=%s model=%s",
                purpose,
                self._model,
            )

        try:
            parsed = parse_json_or_raise(content)
        except ParseError as exc:
            logger.warning(
                "brain_llm_gateway_parse_failed purpose=%s reason=%s",
                purpose,
                str(exc),
            )
            return PolishResult.failed(
                purpose=purpose,
                reason=f"json_parse_failed:{exc}",
                model=self._model,
            )

        polished, cited = _project_polished_fields(purpose=purpose, parsed=parsed)
        if not polished:
            return PolishResult.failed(
                purpose=purpose,
                reason="polished_fields_missing",
                model=self._model,
            )

        return PolishResult(
            success=True,
            purpose=purpose,
            polished=polished,
            cited_evidence_refs=cited,
            model=self._model,
        )


def _project_polished_fields(
    *,
    purpose: PolishPurpose,
    parsed: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    """Project the LLM JSON object onto the per-purpose polished schema."""
    cited = coerce_cited_refs(parsed.get("cited_evidence_refs"))
    polished: dict[str, str] = {}

    if purpose == "journal":
        narrative = coerce_text(parsed.get("narrative_polished"))
        if narrative:
            polished["narrative_polished"] = narrative
    elif purpose == "finding_summary":
        summary = coerce_text(parsed.get("summary_polished"))
        why_now = coerce_text(parsed.get("why_now_polished"))
        if summary:
            polished["summary_polished"] = summary
        if why_now:
            polished["why_now_polished"] = why_now
    elif purpose == "finding_reasoning":
        reasoning = coerce_text(parsed.get("reasoning_polished"))
        if reasoning:
            polished["reasoning_polished"] = reasoning

    return polished, cited
