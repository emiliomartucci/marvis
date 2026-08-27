# v1.0.0 - 2026-05-26 - M1 CAPTURE U2 — ingress content negotiation + idempotency + rate/quota
"""Side logic for the unified POST /api/v1/ingest endpoint.

Deliberately imports nothing from ingest_triage (the endpoint handler lives
there and owns the path guards) to avoid an import cycle. This module owns:
- JSON content decode (text / base64; url deferred to v1.1)
- request fingerprinting for idempotency
- the claim-first idempotency protocol
- atomic per-key rate + daily-quota counters
- the ephemeral-table TTL sweep (called from the hourly _periodic_cleanup)

All writes go through the single SQLite writer (write_db / acquire_write_db),
learning 6130bc49. Counters are atomic check-and-increment so two concurrent
requests cannot both pass the gate.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Mapping

import aiosqlite

from core.api.db import write_db
from core.api.models.ingest_keys import IngestJsonContent, IngestJsonPayload
from core.api.services.ingest.ignore_patterns import MAX_FILE_SIZE_BYTES
from core.api.use_cases._errors import ServiceError, ValidationError

# A JSON request body larger than this is rejected before parsing (raw-body cap).
MAX_JSON_BODY_BYTES = 32 * 1024 * 1024
MAX_INGRESS_FILE_COUNT = 50
MAX_IDEMPOTENCY_KEY_LEN = 255
IDEMPOTENCY_TTL_HOURS = 24
WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS = 300
MAX_WEBHOOK_NONCE_LEN = 255
MAX_WEBHOOK_SOURCE_LEN = 128



# --------------------------------------------------------------------------- #
# Signed webhook auth (U8)
# --------------------------------------------------------------------------- #


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return []
        if isinstance(parsed, list):
            return [str(item).strip() for item in parsed if str(item).strip()]
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def webhook_secrets() -> list[str]:
    secrets = _env_list("MARVIS_INGEST_WEBHOOK_SECRETS")
    single = os.environ.get("MARVIS_INGEST_WEBHOOK_SECRET", "").strip()
    if single:
        secrets.append(single)
    seen: list[str] = []
    for secret in secrets:
        if secret and secret not in seen:
            seen.append(secret)
    return seen


def webhook_project_scope() -> set[str]:
    return set(_env_list("MARVIS_INGEST_WEBHOOK_PROJECT_SCOPE"))


def webhook_workspace_id(env: Mapping[str, str] | None = None) -> str:
    """Return the exact workspace configured for the signed webhook surface."""
    source = os.environ if env is None else env
    workspace_id = (
        source.get("MARVIS_INGEST_WEBHOOK_WORKSPACE_ID", "").strip()
        or source.get("MARVIS_MCP_WORKSPACE_ID", "").strip()
    )
    if not workspace_id:
        err = ServiceError(
            code="webhook_workspace_not_configured",
            message="Webhook workspace is not configured.",
        )
        err.http_status = 401
        raise err
    return workspace_id


def parse_webhook_headers(headers: Mapping[str, str]) -> tuple[str, str, str, str]:
    source = (headers.get("x-marvis-webhook-source") or "").strip()
    timestamp = (headers.get("x-marvis-webhook-timestamp") or "").strip()
    nonce = (headers.get("x-marvis-webhook-nonce") or "").strip()
    signature = (headers.get("x-marvis-webhook-signature") or "").strip()
    if not source or not timestamp or not nonce or not signature:
        raise ValueError("missing webhook headers")
    if len(source) > MAX_WEBHOOK_SOURCE_LEN or len(nonce) > MAX_WEBHOOK_NONCE_LEN:
        raise ValueError("webhook header too long")
    if not re.fullmatch(r"[A-Za-z0-9_.:&-]+", source):
        raise ValueError("invalid webhook source")
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", nonce):
        raise ValueError("invalid webhook nonce")
    if not timestamp.isdigit():
        raise ValueError("invalid webhook timestamp")
    if not re.fullmatch(r"sha256=[0-9a-fA-F]{64}", signature):
        raise ValueError("invalid webhook signature format")
    return source, timestamp, nonce, signature


def verify_webhook_signature(raw_body: bytes, *, timestamp: str, signature: str, now: int | None = None) -> str:
    secrets = webhook_secrets()
    if not secrets:
        err = ServiceError(code="webhook_not_configured", message="Webhook signing secret is not configured.")
        err.http_status = 401
        raise err
    current = int(time.time()) if now is None else now
    ts = int(timestamp)
    if abs(current - ts) > WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS:
        err = ServiceError(code="webhook_timestamp_stale", message="Webhook timestamp is outside the allowed window.")
        err.http_status = 401
        raise err
    expected_prefix = "sha256="
    received = signature[len(expected_prefix):]
    signed_payload = timestamp.encode("utf-8") + b"." + raw_body
    for secret in secrets:
        digest = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
        if hmac.compare_digest(received, digest):
            return hashlib.sha256(raw_body).hexdigest()
    err = ServiceError(code="webhook_signature_invalid", message="Webhook signature is invalid.")
    err.http_status = 401
    raise err


async def claim_webhook_nonce(
    workspace_id: str,
    source: str,
    nonce: str,
    request_sha256: str,
) -> bool:
    if not workspace_id.strip():
        raise ValueError("workspace_id is required for webhook nonce claims")
    async with write_db(label="ingest.webhook.nonce_claim") as db:
        cur = await db.execute(
            """
            INSERT INTO ingest_webhook_nonces(
                workspace_id, source, nonce, request_sha256, received_at
            )
            VALUES (?, ?, ?, ?, datetime('now'))
            ON CONFLICT(workspace_id, source, nonce) WHERE workspace_id IS NOT NULL
            DO NOTHING
            """,
            (workspace_id, source, nonce, request_sha256),
        )
        return cur.rowcount == 1


# --------------------------------------------------------------------------- #
# Content decode
# --------------------------------------------------------------------------- #


def decode_json_content(content: IngestJsonContent) -> tuple[bytes, str]:
    """Decode JSON content to (bytes, filename). Raises 4xx on bad input.

    `url` is rejected (422) — server-side fetch is deferred to v1.1 (SSRF pivot).
    Exactly one of text / base64 must be present.
    """
    if content.url is not None:
        raise ValidationError(
            code="content_url_unsupported",
            message=(
                "content.url (server-side fetch) is not supported in M1 — it is an "
                "SSRF pivot and unnecessary when the external client has the bytes. "
                "Fix: send the bytes inline as content.text or content.base64."
            ),
        )
    if content.text is not None and content.base64 is not None:
        raise ValidationError(
            code="content_text_base64_both",
            message="Provide exactly one of content.text or content.base64, not both.",
        )

    if content.text is not None:
        data = content.text.encode("utf-8")
        default_ext = ".txt"
    elif content.base64 is not None:
        try:
            # binascii.Error is a ValueError subclass, so this also covers bad padding.
            data = base64.b64decode(content.base64, validate=True)
        except (ValueError, TypeError):
            raise ValidationError(
                code="content_base64_invalid",
                message="content.base64 is not valid base64.",
            )
        default_ext = ".bin"
    else:
        raise ValidationError(
            code="content_missing",
            message="content must provide text or base64.",
        )

    if not data:
        raise ValidationError(code="content_empty", message="content is empty.")
    # Cap the DECODED size (base64 inflates ~33%); the buffer is already in memory.
    if len(data) > MAX_FILE_SIZE_BYTES:
        err = ServiceError(
            code="content_too_large",
            message=f"content exceeds the maximum size of {MAX_FILE_SIZE_BYTES} bytes.",
        )
        err.http_status = 413  # preserve the original 413 status (was a 413 raise)
        raise err

    filename = content.filename or f"ingest-{uuid.uuid4().hex[:8]}{default_ext}"
    return data, filename


# --------------------------------------------------------------------------- #
# Request fingerprint (idempotency binding)
# --------------------------------------------------------------------------- #


def json_request_fingerprint(payload: IngestJsonPayload) -> str:
    canonical = json.dumps(
        payload.model_dump(), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def multipart_request_fingerprint(project: str, files: list[tuple[str, int]]) -> str:
    parts = [project, *sorted(f"{name}:{size}" for name, size in files)]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Idempotency (claim-first protocol)
# --------------------------------------------------------------------------- #


async def claim_idempotency(
    key_id: str, idem_key: str, request_sha256: str
) -> tuple[str, str | None]:
    """Atomically claim an Idempotency-Key. Returns (state, response_json).

    state ∈ {
      "claimed"  -> winner, proceed and finalize when done,
      "replay"   -> finished earlier, response_json is the stored snapshot,
      "pending"  -> a concurrent request holds the claim (caller should 409),
      "mismatch" -> same key, different payload (caller should 422),
    }
    The INSERT is the first write of the whole request, so a duplicate cannot
    create a second ingest_pending row (reads use the read-only pool and would
    both miss).
    """
    async with write_db(label="ingest.ingress.idem_claim") as db:
        cur = await db.execute(
            """
            INSERT INTO ingest_idempotency
                (api_key_id, idem_key, request_sha256, status, created_at)
            VALUES (?, ?, ?, 'pending', datetime('now'))
            ON CONFLICT(api_key_id, idem_key) DO NOTHING
            """,
            (key_id, idem_key, request_sha256),
        )
        if cur.rowcount == 1:
            return "claimed", None

        async with db.execute(
            "SELECT request_sha256, status, response_json FROM ingest_idempotency "
            "WHERE api_key_id = ? AND idem_key = ?",
            (key_id, idem_key),
        ) as c:
            row = await c.fetchone()

    if row is None:
        # Lost the row to a concurrent TTL sweep between INSERT and SELECT — treat
        # as a fresh claim (the duplicate-protection window already elapsed).
        return "claimed", None
    if row["request_sha256"] != request_sha256:
        return "mismatch", None
    if row["status"] == "done":
        return "replay", row["response_json"]
    return "pending", None


async def finalize_idempotency(key_id: str, idem_key: str, response_json: str) -> None:
    async with write_db(label="ingest.ingress.idem_finalize") as db:
        await db.execute(
            "UPDATE ingest_idempotency SET status = 'done', response_json = ? "
            "WHERE api_key_id = ? AND idem_key = ?",
            (response_json, key_id, idem_key),
        )


async def release_idempotency(key_id: str, idem_key: str) -> None:
    """Drop a still-pending claim so a failed request does not block retries."""
    async with write_db(label="ingest.ingress.idem_release") as db:
        await db.execute(
            "DELETE FROM ingest_idempotency "
            "WHERE api_key_id = ? AND idem_key = ? AND status = 'pending'",
            (key_id, idem_key),
        )


# --------------------------------------------------------------------------- #
# Rate + quota (atomic check-and-increment)
# --------------------------------------------------------------------------- #


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def check_and_increment_rate(key_id: str, limit_per_min: int) -> bool:
    """Per-minute rate gate. Returns True if allowed (and consumed), else False."""
    bucket = _utc_now().strftime("%Y-%m-%dT%H:%M")
    async with write_db(label="ingest.ingress.rate") as db:
        await db.execute(
            "INSERT INTO ingest_rate_usage (api_key_id, minute_bucket, count) "
            "VALUES (?, ?, 0) ON CONFLICT(api_key_id, minute_bucket) DO NOTHING",
            (key_id, bucket),
        )
        cur = await db.execute(
            "UPDATE ingest_rate_usage SET count = count + 1 "
            "WHERE api_key_id = ? AND minute_bucket = ? AND count < ?",
            (key_id, bucket, limit_per_min),
        )
        return cur.rowcount == 1


async def check_and_increment_quota(key_id: str, daily_quota: int) -> bool:
    """Daily quota gate. Returns True if allowed (and consumed), else False."""
    today = _utc_now().strftime("%Y-%m-%d")
    async with write_db(label="ingest.ingress.quota") as db:
        await db.execute(
            "INSERT INTO ingest_quota_usage (api_key_id, usage_date, count) "
            "VALUES (?, ?, 0) ON CONFLICT(api_key_id, usage_date) DO NOTHING",
            (key_id, today),
        )
        cur = await db.execute(
            "UPDATE ingest_quota_usage SET count = count + 1 "
            "WHERE api_key_id = ? AND usage_date = ? AND count < ?",
            (key_id, today, daily_quota),
        )
        return cur.rowcount == 1


def seconds_to_next_minute() -> int:
    return max(1, 60 - _utc_now().second)


def seconds_to_midnight_utc() -> int:
    now = _utc_now()
    secs = (23 - now.hour) * 3600 + (59 - now.minute) * 60 + (60 - now.second)
    return max(1, secs)


# --------------------------------------------------------------------------- #
# TTL sweep (called from main._periodic_cleanup — sleep-before-lock, 4d4278e4)
# --------------------------------------------------------------------------- #


async def cleanup_ingest_ephemeral(db: aiosqlite.Connection) -> tuple[int, int, int]:
    """Prune stale idempotency / rate / quota rows. Caller holds the writer.

    Returns (idempotency_deleted, rate_deleted, quota_deleted).
    """
    idem = await db.execute(
        "DELETE FROM ingest_idempotency "
        "WHERE created_at < datetime('now', ?)",
        (f"-{IDEMPOTENCY_TTL_HOURS} hours",),
    )
    # Rate buckets only matter for the current minute; anything older is dead.
    rate = await db.execute(
        "DELETE FROM ingest_rate_usage "
        "WHERE minute_bucket < strftime('%Y-%m-%dT%H:%M', datetime('now', '-2 minutes'))"
    )
    # Keep a week of daily quota rows for audit, then drop.
    quota = await db.execute(
        "DELETE FROM ingest_quota_usage "
        "WHERE usage_date < strftime('%Y-%m-%d', datetime('now', '-7 days'))"
    )
    return idem.rowcount, rate.rowcount, quota.rowcount
