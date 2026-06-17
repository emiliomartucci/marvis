# v1.0.0 - 2026-06-09 - P1: claude -p (Claude Code headless) provider for the Brain.
"""Brain LLM backend that runs `claude -p` (Claude Code headless) as a subprocess.

The brain runs on the user's Claude Code **subscription** — zero API key. Selected
with `BRAIN_LLM_PROVIDER=claude_cli`; the default `gateway` path is unchanged.

Same `BrainLLMService` contract as `LocalGatewayBrainService` (call_polish / call_json
/ aclose + model / grounding_strict). Only the transport differs: instead of an HTTP
chat.completion, we exec `claude -p --output-format json`, write the user prompt to
**stdin**, and read the single-result envelope. Verified flags/schema against the
installed binary (2026-06-09): envelope `{result: str, is_error: bool, …}`.

CRITICAL: `claude -p` is an AGENT by default (loads the Claude Code system prompt,
may use tools). For pure generation we pass `--allowedTools ""` (no tools),
`--system-prompt <brain>` (replace, not append) + `--exclude-dynamic-system-prompt-sections`
(strip the agentic preamble). Everything fails SOFT: any error/timeout/non-zero
exit/malformed JSON → PolishResult.failed (or None for call_json) → the brain
degrades to its deterministic baseline, never a crash.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from contextlib import suppress
from typing import Any

from core.api.services.brain.llm.base import PolishPurpose, PolishResult
from core.api.services.brain.llm.direction_alignment import (
    classify_direction_alignment_impl,
)
from core.api.services.brain.llm.local_gateway import _project_polished_fields
from core.api.services.brain.llm.parsers import ParseError, parse_json_or_raise

logger = logging.getLogger(__name__)


class ClaudeCliBrainService:
    """Brain polish via `claude -p` headless on the local Claude Code subscription."""

    def __init__(
        self,
        *,
        model: str,
        binary: str = "claude",
        timeout_seconds: int = 60,
        semaphore_size: int = 1,
        grounding_strict: bool = False,
    ) -> None:
        self._model = model or ""
        self._binary = binary or "claude"
        self._timeout_seconds = max(5, int(timeout_seconds))
        # Subprocess is heavy (CLI boot per call); keep concurrency low.
        self._semaphore = asyncio.Semaphore(max(1, int(semaphore_size)))
        self._grounding_strict = grounding_strict

    @property
    def model(self) -> str:
        return self._model

    @property
    def grounding_strict(self) -> bool:
        return self._grounding_strict

    async def aclose(self) -> None:
        # No persistent client/process to release.
        return None

    # ----------------------------------------------------------------- #
    # subprocess transport                                              #
    # ----------------------------------------------------------------- #

    async def _run(self, *, system_prompt: str, user_prompt: str) -> tuple[str | None, str]:
        """Run `claude -p` once. Returns (content, "") on success, (None, reason) on
        any failure. `content` is the assistant text (the envelope's `result`)."""
        if not os.path.isabs(self._binary) and shutil.which(self._binary) is None:
            return (None, "claude_not_on_path")

        argv = [
            self._binary,
            "-p",
            "--output-format", "json",
            "--allowedTools", "",  # no tools → pure generation, no agent loop
            "--exclude-dynamic-system-prompt-sections",  # drop the Claude Code agentic preamble
            "--system-prompt", system_prompt,
        ]
        if self._model:
            argv += ["--model", self._model]

        async with self._semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:  # binary missing / exec error
                return (None, f"spawn_failed:{exc}")
            try:
                out, err = await asyncio.wait_for(
                    proc.communicate(user_prompt.encode("utf-8")),
                    timeout=self._timeout_seconds,
                )
            except asyncio.TimeoutError:
                proc.kill()
                with suppress(Exception):
                    await proc.wait()
                return (None, "timeout")
            except Exception as exc:
                return (None, f"communicate_failed:{exc}")

        if proc.returncode != 0:
            tail = err.decode("utf-8", "replace")[:200] if err else ""
            return (None, f"exit_{proc.returncode}:{tail}")

        try:
            env = json.loads(out.decode("utf-8"))
        except Exception:
            return (None, "envelope_not_json")
        if not isinstance(env, dict):
            return (None, "envelope_not_object")
        if env.get("is_error"):
            return (None, f"claude_error:{str(env.get('result'))[:200]}")

        content = env.get("result")
        if not isinstance(content, str) or not content.strip():
            # Tolerant of schema drift: fall back to the first non-empty string field.
            content = next(
                (v for v in env.values() if isinstance(v, str) and v.strip()), ""
            )
        if not content:
            return (None, "empty_result")
        return (content, "")

    # ----------------------------------------------------------------- #
    # BrainLLMService contract                                          #
    # ----------------------------------------------------------------- #

    async def call_polish(
        self,
        *,
        purpose: PolishPurpose,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        idempotency_key: str | None = None,
    ) -> PolishResult:
        """Issue the polish call. Never raises — returns PolishResult.failed on errors.
        Shares the content→PolishResult projection with the gateway provider."""
        content, reason = await self._run(system_prompt=system_prompt, user_prompt=user_prompt)
        if content is None:
            logger.warning("brain_claude_cli_polish_failed purpose=%s reason=%s", purpose, reason)
            return PolishResult.failed(purpose=purpose, reason=reason, model=self._model)
        try:
            parsed = parse_json_or_raise(content)
        except ParseError as exc:
            return PolishResult.failed(
                purpose=purpose, reason=f"json_parse_failed:{exc}", model=self._model
            )
        polished, cited = _project_polished_fields(purpose=purpose, parsed=parsed)
        if not polished:
            return PolishResult.failed(
                purpose=purpose, reason="polished_fields_missing", model=self._model
            )
        return PolishResult(
            success=True,
            purpose=purpose,
            polished=polished,
            cited_evidence_refs=cited,
            model=self._model,
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
        """Generic JSON-object call (DR8 classifier). The per-call `model` is a
        gateway model id with no Claude equivalent, so the configured Claude model
        (`self._model`) is used. None on any failure — caller falls back."""
        content, reason = await self._run(system_prompt=system_prompt, user_prompt=user_prompt)
        if content is None:
            logger.warning("brain_claude_cli_json_failed reason=%s", reason)
            return None
        try:
            return parse_json_or_raise(content)
        except ParseError as exc:
            logger.warning("brain_claude_cli_json_parse_failed reason=%s", str(exc))
            return None

    async def classify_direction_alignment(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        """DR8 classifier surface — same impl the gateway provider uses."""
        return await classify_direction_alignment_impl(llm_surface=self, payload=payload)
