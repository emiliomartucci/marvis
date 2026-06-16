# v1.0.0 - 2026-05-03 - Phoenix client-side OTel SDK setup (Phase 1.5 D14)
"""Phoenix tracing client-side setup for marvisx api.

Net-new in Phase 1.5 — marvisx api had ZERO tracing before. Without this,
trace chain in Phoenix is broken at the consumer→queue boundary (orphan spans
on gateway side, no parent context from marvisx api).

Feature flag `TRACING_ENABLED=false` default for rollback safety. Consumer SDK
(`api/services/local_llm/async_client.py`) uses `httpx.AsyncClient` which
auto-injects W3C `traceparent` once `HTTPXClientInstrumentor()` is active.

Sample rate: `ParentBased(TraceIdRatioBased(rate))` so child sampling decision
inherits from parent — no chain gaps if marvisx api samples 100% but queue
gateway samples 10% (or vice versa).

NO baggage propagator (security M3) — only W3C tracecontext.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

logger = logging.getLogger(__name__)


def init_tracing(app: "FastAPI") -> None:
    """Initialize Phoenix OTel tracing for marvisx api.

    Idempotent — skip if `TRACING_ENABLED` env var is `false` (default for safe
    rollback). To enable, set `TRACING_ENABLED=true` + `PHOENIX_ENDPOINT=...`
    via systemd .env or shell.
    """
    if os.getenv("TRACING_ENABLED", "false").lower() not in ("true", "1", "yes"):
        logger.info("Tracing disabled (TRACING_ENABLED!=true) — skipping Phoenix setup")
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.propagate import set_global_textmap
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
        from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
        from phoenix.otel import register
    except ImportError as e:
        logger.warning(
            "Phoenix/OTel deps not installed (%s) — tracing disabled. "
            "Install via: pip install arize-phoenix-otel "
            "opentelemetry-instrumentation-fastapi opentelemetry-instrumentation-httpx",
            e,
        )
        return

    # NO baggage propagator (security M3) — W3C tracecontext only
    set_global_textmap(TraceContextTextMapPropagator())

    sample_rate_str = os.getenv("TRACING_SAMPLE_RATE", "1.0")
    try:
        sample_rate = float(sample_rate_str)
    except ValueError:
        logger.warning("Invalid TRACING_SAMPLE_RATE=%s, defaulting 1.0", sample_rate_str)
        sample_rate = 1.0

    endpoint = os.getenv("PHOENIX_ENDPOINT", "http://100.103.221.55:6006")
    project = os.getenv("PHOENIX_PROJECT_NAME", "marvisx-api")
    git_sha = os.getenv("GIT_SHA", "dev")

    try:
        register(
            project_name=project,
            endpoint=endpoint,
            auto_instrument=False,  # explicit control
            batch=True,
        )
    except Exception:
        logger.exception("Phoenix register() failed — tracing disabled")
        return

    # Auto-instrument FastAPI inbound + httpx outbound
    # excluded_urls keeps healthcheck/metrics noise out
    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="health,metrics,readiness,livez,api/v1/healthz"
    )
    HTTPXClientInstrumentor().instrument()

    logger.info(
        "Phoenix tracing initialized: project=%s endpoint=%s sample=%.2f sha=%s",
        project, endpoint, sample_rate, git_sha,
    )
