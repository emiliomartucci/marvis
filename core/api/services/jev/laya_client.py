"""Local-only Laya implementation of the typed ScoringProvider port.

This module has no dependency on the hosted Jev client, which is deliberately
absent from the Enterprise projection. Scoring remains off until the tenant
flag is enabled; model quality must be calibrated on owner labels first.
"""
from __future__ import annotations

import asyncio
import math
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx

from core.api.services.jev import ledger
from core.api.services.jev.flags import FlagResolver, get_flag_resolver
from core.api.services.jev.outbound import sanitize_payload
from core.api.services.jev.types import ScoringAnswer, ScoringBatchResult, ScoringQuestion


def _unit_probability(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and 0.0 <= value <= 1.0
    )


class LayaScoringProvider:
    """One event-loop-local client with fail-closed limits and usage accounting."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str = "multilingual",
        api_key: str | None = None,
        timeout_seconds: float = 15.0,
        daily_call_ceiling: int = 1000,
        max_concurrent: int = 2,
        breaker_failures: int = 3,
        breaker_cooldown_seconds: float = 60.0,
        flag_resolver: FlagResolver | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        destination = base_url or os.environ.get("MARVIS_LAYA_URL", "http://marvis-laya:8000")
        parsed = urlsplit(destination)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"marvis-laya", "localhost", "127.0.0.1"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Laya must use the internal service or loopback HTTP endpoint")
        # "marvis-v1" is the fleet-tuned checkpoint served by the local
        # laya-serve under LAYA_MODELS; the three upstream names remain valid.
        if model not in {"english", "multilingual", "typed-decisions", "marvis-v1"}:
            raise ValueError("Unsupported pinned Laya checkpoint")
        if not 0.01 <= timeout_seconds <= 120.0 or not 1 <= max_concurrent <= 32:
            raise ValueError("Invalid Laya timeout or concurrency limit")
        self._base_url = destination.rstrip("/")
        self._model = model
        self._api_key = api_key or os.environ.get("MARVIS_LAYA_API_KEY") or ""
        self._timeout = timeout_seconds
        self._ceiling = max(1, daily_call_ceiling)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._breaker_failures = max(1, breaker_failures)
        self._breaker_cooldown = max(1.0, breaker_cooldown_seconds)
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._resolver = flag_resolver or get_flag_resolver()
        self._http = http
        self._owns_http = http is None
        self._health: dict[str, Any] = {
            "scope": "process_event_loop", "attempts": 0, "successes": 0,
            "failures": 0, "last_outcome": None,
        }

    def check_configuration(self, tenant: str) -> None:
        """The local service needs no vendor credential; flags govern activation."""
        self._resolver.decide(tenant)

    def health_snapshot(self) -> dict[str, Any]:
        return dict(self._health)

    async def aclose(self) -> None:
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url, follow_redirects=False,
                timeout=httpx.Timeout(self._timeout, connect=min(self._timeout, 2.0)),
                trust_env=False,
            )
        return self._http

    def _reserve(self, tenant: str) -> bool:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return ledger.reserve_daily_attempt(tenant, day, self._ceiling)

    def _record(self, tenant: str, surface: str, outcome: str, started: float,
                *, attempted: bool, result: ScoringBatchResult | None = None,
                state: Any = None,
                questions: list[ScoringQuestion] | None = None) -> None:
        if attempted:
            self._health["attempts"] += 1
        if outcome == "ok":
            self._health["successes"] += 1
        elif attempted:
            self._health["failures"] += 1
        self._health["last_outcome"] = outcome
        latency_ms = int((time.monotonic() - started) * 1000)
        ledger.record_call(
            tenant_id=tenant, surface=surface, outcome=outcome,
            model=f"laya/{self._model}", attempted=attempted,
            latency_ms=latency_ms,
            input_tokens=result.input_tokens if result else 0,
            output_tokens=result.output_tokens if result else 0,
        )
        # Task 7af2b38c (Laya round-2 U1): opt-in sanitized payload capture for
        # REAL decisions — the training set round-2 needs. Only attempts that
        # actually reached the provider are captured (disabled_tenant /
        # breaker_open / ceiling_hit carry no decision worth training on).
        if attempted and questions is not None:
            from dataclasses import asdict

            ledger.record_capture(
                tenant_id=tenant,
                surface=surface,
                state=state,
                questions={q.id: q.to_payload() for q in questions},
                answers=(
                    {qid: asdict(ans) for qid, ans in result.answers.items()}
                    if result is not None
                    else None
                ),
                outcome=outcome,
                model=f"laya/{self._model}",
                latency_ms=latency_ms,
            )

    async def score(self, state: Any, questions: list[ScoringQuestion], *,
                    tenant_id: str | None = None, surface: str = "unknown") -> ScoringBatchResult | None:
        result, _ = await self.score_with_outcome(
            state, questions, tenant_id=tenant_id, surface=surface,
        )
        return result

    async def score_with_outcome(self, state: Any, questions: list[ScoringQuestion], *,
                                 tenant_id: str | None = None,
                                 surface: str = "unknown") -> tuple[ScoringBatchResult | None, str]:
        tenant = (tenant_id or "tenant").strip() or "tenant"
        if os.environ.get("MARVIS_LAYA_CALIBRATION_APPROVED") != "1":
            return None, "disabled_tenant"
        if not self._resolver.decide(tenant).enabled:
            return None, "disabled_tenant"
        if self._opened_at.get(tenant, 0.0) + self._breaker_cooldown > time.monotonic():
            self._record(tenant, surface, "breaker_open", time.monotonic(), attempted=False)
            return None, "breaker_open"
        if self._semaphore.locked():
            return None, "error"
        if not questions or len({q.id for q in questions}) != len(questions):
            return None, "error"
        try:
            for q in questions:
                if not q.id or not isinstance(q.instructions, str) or not q.instructions:
                    return None, "error"
                if q.type == "choice" and (not isinstance(q.criteria, dict) or not q.criteria or len(q.criteria) > 20):
                    return None, "error"
                if q.type == "score" and (not isinstance(q.criteria, list) or not q.criteria or any(v is None for v in q.criteria)):
                    return None, "error"
                if q.type not in {"choice", "score", "noul"}:
                    return None, "error"
            payload = sanitize_payload({
                "model": self._model, "state": state,
                "questions": {q.id: q.to_payload() for q in questions},
            })
        except (TypeError, ValueError, AttributeError, OverflowError, RecursionError):
            return None, "error"
        started = time.monotonic()
        async with self._semaphore:
            # Reservation can wait on SQLite and reconcile an append-only
            # ledger. Keep that synchronous work off the MCP request loop.
            if not await asyncio.to_thread(self._reserve, tenant):
                self._record(tenant, surface, "ceiling_hit", started, attempted=False)
                return None, "ceiling_hit"
            headers = {"Content-Type": "application/json"}
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"
            try:
                response = await asyncio.wait_for(
                    self._client().post("/v1/systemone", json=payload, headers=headers),
                    timeout=self._timeout,
                )
            except asyncio.CancelledError:
                self._record(tenant, surface, "error", started, attempted=True,
                             state=state, questions=questions)
                raise
            except (asyncio.TimeoutError, httpx.TimeoutException, httpx.TransportError):
                self._failed(tenant)
                self._record(tenant, surface, "timeout_fallback", started, attempted=True,
                             state=state, questions=questions)
                return None, "timeout_fallback"
            except Exception:
                self._failed(tenant)
                self._record(tenant, surface, "error", started, attempted=True,
                             state=state, questions=questions)
                return None, "error"
        if response.status_code != 200:
            self._failed(tenant)
            self._record(tenant, surface, "provider_error", started, attempted=True,
                         state=state, questions=questions)
            return None, "provider_error"
        try:
            result = self._validate(response.json(), questions)
        except (TypeError, ValueError, AttributeError, OverflowError, RecursionError):
            result = None
        if result is None:
            self._failed(tenant)
            self._record(tenant, surface, "provider_error", started, attempted=True,
                         state=state, questions=questions)
            return None, "provider_error"
        self._failures[tenant] = 0
        self._opened_at.pop(tenant, None)
        self._record(tenant, surface, "ok", started, attempted=True, result=result,
                     state=state, questions=questions)
        return ScoringBatchResult(
            model=result.model, answers=result.answers,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            latency_ms=int((time.monotonic() - started) * 1000),
        ), "ok"

    def _failed(self, tenant: str) -> None:
        self._failures[tenant] = self._failures.get(tenant, 0) + 1
        if self._failures[tenant] >= self._breaker_failures:
            self._opened_at[tenant] = time.monotonic()

    def _validate(self, body: Any, questions: list[ScoringQuestion]) -> ScoringBatchResult | None:
        if not isinstance(body, dict) or body.get("model") != "laya-rl-agent":
            return None
        routing = body.get("routing")
        if not isinstance(routing, dict) or routing.get("model") != self._model:
            return None
        answers = body.get("answers")
        if not isinstance(answers, dict) or set(answers) != {q.id for q in questions}:
            return None
        parsed: dict[str, ScoringAnswer] = {}
        for q in questions:
            raw = answers[q.id]
            if not isinstance(raw, dict) or raw.get("type") != q.type:
                return None
            probability = raw.get("answer_confidence")
            if q.type in {"choice", "score"} and not _unit_probability(probability):
                return None
            answer = ScoringAnswer(
                type=q.type,
                confidence=float(probability) if q.type in {"choice", "score"} else None,
            )
            probabilities = raw.get("probabilities")
            if probabilities is not None:
                if not isinstance(probabilities, dict) or not all(
                    isinstance(key, str) and _unit_probability(value)
                    for key, value in probabilities.items()
                ):
                    return None
                answer.probabilities = {key: float(value) for key, value in probabilities.items()}
            if q.type == "noul":
                if not _unit_probability(raw.get("noul")):
                    return None
                answer.noul = float(raw["noul"])
            elif q.type == "choice":
                if not isinstance(q.criteria, dict) or raw.get("choice") not in q.criteria:
                    return None
                answer.choice = raw["choice"]
            elif q.type == "score":
                value = raw.get("score")
                if not _unit_probability(value) and not (
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    and math.isfinite(value) and 0 <= value <= len(q.criteria) - 1
                ):
                    return None
                answer.score = float(value)
            parsed[q.id] = answer
        usage = body.get("usage", {})
        if not isinstance(usage, dict):
            return None
        counts = (usage.get("input_tokens", 0), usage.get("output_tokens", 0))
        if any(type(value) is not int or not 0 <= value <= 1_000_000_000 for value in counts):
            return None
        return ScoringBatchResult(
            model=f"laya/{self._model}", answers=parsed,
            input_tokens=counts[0], output_tokens=counts[1],
        )
