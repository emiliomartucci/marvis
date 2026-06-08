from __future__ import annotations

from typing import Any

import aiohttp

from core.api.config import settings

_RESPONSES_URL = "https://api.openai.com/v1/responses"


def extract_output_text(payload: dict[str, Any]) -> str:
    text = str(payload.get("output_text") or "").strip()
    if text:
        return text

    for item in payload.get("output") or []:
        for content in item.get("content") or []:
            text = str(content.get("text") or "").strip()
            if text:
                return text

    return ""


async def create_text_response(
    *, model: str, prompt: str, max_output_tokens: int
) -> str:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    timeout = aiohttp.ClientTimeout(total=60)
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "input": prompt,
        "max_output_tokens": max_output_tokens,
    }

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.post(_RESPONSES_URL, json=payload) as resp:
            if resp.status >= 400:
                detail = (await resp.text()).strip()
                raise RuntimeError(
                    f"OpenAI responses failed HTTP {resp.status}: {detail[:200]}"
                )
            data = await resp.json()

    text = extract_output_text(data)
    if not text:
        raise RuntimeError("OpenAI responses returned empty text output")
    return text
