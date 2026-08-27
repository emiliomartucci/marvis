# bench.py v1.0.0 — Model benchmark endpoint for MarvisX
# Runs the same prompt on N models in parallel, collects latency/cost/output.
# POST /api/v1/bench/run

from __future__ import annotations

import asyncio
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from core.api.security import (
    get_current_user,
    is_local_single_user_mode,
    is_loopback_request,
)
from core.api.services.model_router import estimate_cost

router = APIRouter(prefix="/bench", tags=["bench"])

_LOCAL_HOST_DETAIL = (
    "This host-global benchmark operation is available only to the trusted "
    "local OSS loopback runtime."
)


def _require_local_host_request(request: Request) -> None:
    if is_local_single_user_mode() and is_loopback_request(request):
        return
    raise HTTPException(status_code=403, detail=_LOCAL_HOST_DETAIL)


_DEFAULT_CWD = os.environ.get("MARVIS_WORKSPACE_ROOT", str(Path.home() / "workspace"))


class BenchRequest(BaseModel):
    prompt: str = Field(..., max_length=4000, description="Prompt to run on each model")
    models: list[str] = Field(
        default=["openai/gpt-5.4", "anthropic/claude-sonnet-4-6"],
        max_length=5,
        description="Model IDs to compare (max 5)",
    )
    task_type: str = Field(default="code-gen", description="Task type for routing context")
    timeout_seconds: int = Field(default=120, ge=30, le=300, description="Timeout per model")
    cwd: str = Field(default=_DEFAULT_CWD, description="Working directory")


class BenchModelResult(BaseModel):
    model: str
    status: str  # "ok" | "error" | "timeout"
    latency_ms: int = 0
    output_preview: str = ""
    error: Optional[str] = None
    cost_estimate_usd: float = 0.0


class BenchResponse(BaseModel):
    bench_id: str
    prompt_preview: str
    task_type: str
    results: list[BenchModelResult]
    total_duration_ms: int


async def _run_single_model(
    model: str,
    prompt: str,
    timeout: int,
    cwd: str,
) -> BenchModelResult:
    """Run opencode on a single model and capture output."""
    start = time.monotonic()

    # Parse provider/model for opencode CLI
    # opencode uses --provider and --model separately
    parts = model.split("/", 1)
    provider = parts[0] if len(parts) > 1 else "openai"
    model_id = parts[1] if len(parts) > 1 else model

    cmd = [
        "opencode", "run",
        "--provider", provider,
        "--model", model_id,
        "-p", prompt,
        "--max-turns", "5",
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout,
        )
        elapsed_ms = int((time.monotonic() - start) * 1000)

        output = stdout.decode("utf-8", errors="replace")
        preview = output[:500] if output else "(empty)"

        # Rough token estimate: 1 token ≈ 4 chars
        est_input_tokens = len(prompt) // 4
        est_output_tokens = len(output) // 4
        cost = estimate_cost(model, est_input_tokens, est_output_tokens)

        return BenchModelResult(
            model=model,
            status="ok" if proc.returncode == 0 else "error",
            latency_ms=elapsed_ms,
            output_preview=preview,
            error=stderr.decode("utf-8", errors="replace")[:300] if proc.returncode != 0 else None,
            cost_estimate_usd=round(cost, 4),
        )

    except asyncio.TimeoutError:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return BenchModelResult(
            model=model,
            status="timeout",
            latency_ms=elapsed_ms,
            error=f"Timed out after {timeout}s",
        )
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        return BenchModelResult(
            model=model,
            status="error",
            latency_ms=elapsed_ms,
            error=str(e)[:300],
        )


@router.post(
    "/run",
    response_model=BenchResponse,
    dependencies=[Depends(_require_local_host_request)],
)
async def run_bench(
    req: BenchRequest,
    user=Depends(get_current_user),
):
    """Run the same prompt on N models in parallel and compare results."""
    bench_id = str(uuid.uuid4())[:8]
    start = time.monotonic()

    # Run all models in parallel
    tasks = [
        _run_single_model(model, req.prompt, req.timeout_seconds, req.cwd)
        for model in req.models
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Convert exceptions to error results
    final_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            final_results.append(BenchModelResult(
                model=req.models[i],
                status="error",
                error=str(result)[:300],
            ))
        else:
            final_results.append(result)

    total_ms = int((time.monotonic() - start) * 1000)

    return BenchResponse(
        bench_id=bench_id,
        prompt_preview=req.prompt[:200],
        task_type=req.task_type,
        results=final_results,
        total_duration_ms=total_ms,
    )
