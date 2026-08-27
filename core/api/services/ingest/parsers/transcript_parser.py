"""Audio/video transcript parser backed by the Mac Gateway ASR endpoint."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from core.api.services.ingest.parsers.gateway_aux import (
    auth_headers,
    aux_base_url,
    request_gateway_with_retries,
    settings,
)

logger = logging.getLogger(__name__)

SUPPORTED_AUDIO_SUFFIXES = frozenset(
    {".aac", ".flac", ".m4a", ".mp3", ".oga", ".ogg", ".opus", ".wav", ".webm"}
)
SUPPORTED_VIDEO_SUFFIXES = frozenset(
    {".m4v", ".mkv", ".mov", ".mp4", ".mpeg", ".mpg", ".webm"}
)
AUDIO_MIME_BY_SUFFIX = {
    ".aac": "audio/aac",
    ".flac": "audio/flac",
    ".m4a": "audio/x-m4a",
    ".mp3": "audio/mpeg",
    ".oga": "audio/ogg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}
VIDEO_MIME_BY_SUFFIX = {
    ".m4v": "video/x-m4v",
    ".mkv": "video/x-matroska",
    ".mov": "video/quicktime",
    ".mp4": "video/mp4",
    ".mpeg": "video/mpeg",
    ".mpg": "video/mpeg",
    ".webm": "video/webm",
}


@dataclass(frozen=True)
class TranscriptParseResult:
    frontmatter: dict[str, Any]
    text: str
    structure: dict[str, Any]


def _media_kind(path: Path, mime_type: str) -> str:
    suffix = path.suffix.lower()
    if suffix in SUPPORTED_VIDEO_SUFFIXES or mime_type.startswith("video/"):
        return "video"
    return "audio"


def _format_ts(value: float | int | None) -> str:
    seconds = max(0, int(float(value or 0)))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _segments_markdown(segments: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for segment in segments:
        text = str(segment.get("text") or "").strip()
        if not text:
            continue
        start = _format_ts(segment.get("start"))
        end = _format_ts(segment.get("end"))
        lines.append(f"[{start}-{end}] {text}")
    return "\n\n".join(lines)


def _normalize_segments(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    segments: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        segments.append(
            {
                "start": item.get("start"),
                "end": item.get("end"),
                "text": str(item.get("text") or "").strip(),
            }
        )
    return segments


async def _extract_audio_from_video(video_path: Path) -> Path:
    cfg = settings()
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_path = Path(tmp.name)
    tmp.close()

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=float(cfg.ingest_transcribe_ffmpeg_timeout_seconds),
        )
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError("video audio extraction timed out")

    if proc.returncode != 0:
        tmp_path.unlink(missing_ok=True)
        detail = stderr.decode("utf-8", "replace").strip()[:500]
        raise RuntimeError(f"video audio extraction failed: {detail}")
    return tmp_path


async def _call_transcribe_endpoint(media_path: Path, mime_type: str) -> dict[str, Any]:
    cfg = settings()
    data = {"model": "whisper-large-v3"}
    language = cfg.ingest_transcribe_language.strip()
    if language:
        data["language"] = language

    timeout = httpx.Timeout(
        connect=5.0,
        read=float(cfg.ingest_transcribe_timeout_seconds),
        write=60.0,
        pool=5.0,
    )
    async with httpx.AsyncClient(
        base_url=f"{aux_base_url()}/",
        timeout=timeout,
        limits=httpx.Limits(max_connections=2, max_keepalive_connections=1),
    ) as client:
        async def post_transcription() -> httpx.Response:
            with media_path.open("rb") as handle:
                return await client.post(
                    "audio/transcriptions",
                    headers=auth_headers(),
                    data=data,
                    files={"file": (media_path.name, handle, mime_type)},
                )

        response = await request_gateway_with_retries(
            post_transcription,
            service_name="tier-transcribe",
        )
        return await _resolve_transcribe_response(
            client,
            response,
            timeout_seconds=float(cfg.ingest_transcribe_timeout_seconds),
        )


async def _resolve_transcribe_response(
    client: httpx.AsyncClient,
    response: httpx.Response,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    if response.status_code in {401, 403}:
        raise RuntimeError("tier-transcribe authorization failed")
    if response.status_code == 429:
        raise RuntimeError("tier-transcribe rate limited")
    if response.status_code >= 500:
        raise RuntimeError(f"tier-transcribe unavailable: HTTP {response.status_code}")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("tier-transcribe returned invalid payload")
    if response.status_code != 202:
        return payload

    status_url = str(payload.get("status_url") or "").strip()
    if not status_url:
        raise RuntimeError("tier-transcribe async response missing status_url")
    return await _poll_transcribe_job(client, status_url, timeout_seconds=timeout_seconds)


async def _poll_transcribe_job(
    client: httpx.AsyncClient,
    status_url: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    request_url = _status_request_url(client, status_url)
    while time.monotonic() < deadline:
        async def get_status() -> httpx.Response:
            return await client.get(request_url, headers=auth_headers())

        response = await request_gateway_with_retries(
            get_status,
            service_name="tier-transcribe-poll",
        )
        if response.status_code in {401, 403}:
            raise RuntimeError("tier-transcribe poll authorization failed")
        if response.status_code == 404:
            raise RuntimeError("tier-transcribe job not found")
        if response.status_code >= 500:
            raise RuntimeError(
                f"tier-transcribe poll unavailable: HTTP {response.status_code}"
            )
        response.raise_for_status()
        status_payload = response.json()
        if not isinstance(status_payload, dict):
            raise RuntimeError("tier-transcribe poll returned invalid payload")

        status = status_payload.get("status")
        if status == "done":
            result = status_payload.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("tier-transcribe completed without result")
            return result
        if status == "failed":
            error = status_payload.get("error") or {}
            message = error.get("message") if isinstance(error, dict) else None
            raise RuntimeError(
                f"tier-transcribe job failed: {message or 'unknown error'}"
            )

        retry_after = response.headers.get("Retry-After")
        try:
            sleep_for = float(retry_after) if retry_after else 2.0
        except ValueError:
            sleep_for = 2.0
        await asyncio.sleep(max(0.2, min(sleep_for, 5.0)))

    raise TimeoutError(f"tier-transcribe job timed out after {timeout_seconds:g}s")


def _status_request_url(client: httpx.AsyncClient, status_url: str) -> str:
    if status_url.startswith(("http://", "https://")):
        return status_url
    if status_url.startswith("/"):
        return str(client.base_url.copy_with(path=status_url, query=None, fragment=None))
    return status_url


async def parse_media_transcript(path: Path, mime_type: str) -> TranscriptParseResult:
    cfg = settings()
    size = path.stat().st_size
    if size > int(cfg.ingest_transcribe_max_bytes):
        raise ValueError(f"Media file too large for transcription: {size} bytes")

    kind = _media_kind(path, mime_type)
    submitted_path = path
    submitted_mime = mime_type
    cleanup_path: Path | None = None
    if kind == "video":
        submitted_path = await _extract_audio_from_video(path)
        submitted_mime = "audio/wav"
        cleanup_path = submitted_path

    try:
        payload = await _call_transcribe_endpoint(submitted_path, submitted_mime)
    finally:
        if cleanup_path is not None:
            cleanup_path.unlink(missing_ok=True)

    text = str(payload.get("text") or "").strip()
    segments = _normalize_segments(payload.get("segments"))
    body = _segments_markdown(segments) or text
    if not body:
        raise RuntimeError("tier-transcribe returned empty transcript")

    title = path.stem.strip() or path.name
    language = payload.get("language")
    duration = payload.get("duration")
    tags = ["transcript", kind]
    frontmatter: dict[str, Any] = {
        "type": "transcript",
        "title": f"Transcript: {title}",
        "tags": tags,
        "source_file": path.name,
        "language": language,
    }
    transcript = (
        f"# Transcript: {title}\n\n"
        f"- Source file: `{path.name}`\n"
        f"- Source kind: `{kind}`\n"
        f"- Language: `{language or 'unknown'}`\n"
        f"- Duration: `{duration if duration is not None else 'unknown'}`\n\n"
        "## Transcript\n\n"
        f"{body}\n"
    )
    structure = {
        "source_kind": kind,
        "source_mime_type": mime_type,
        "language": language,
        "duration": duration,
        "chunked": payload.get("chunked"),
        "chunk_count": payload.get("chunk_count"),
        "segments": segments,
    }
    return TranscriptParseResult(
        frontmatter=frontmatter,
        text=transcript,
        structure=structure,
    )
