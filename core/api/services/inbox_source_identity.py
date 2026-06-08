from __future__ import annotations

import base64
import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

HTTP_URL_RE = re.compile(r"https?://[^\s<>'\")]+")
GMAIL_HOSTS = {"mail.google.com", "gmail.com"}
LOW_SIGNAL_SOURCE_KEYS = {"substack.com", "mail.google.com", "gmail.com"}


def normalize_domain_key(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.netloc:
            raw = parsed.netloc
    except Exception:  # noqa: BLE001
        pass
    key = raw.strip().lower()
    if key.startswith("www."):
        key = key.removeprefix("www.")
    return key or None


def is_gmail_hosted_url(url: str | None) -> bool:
    if not url:
        return False
    try:
        parsed = urlsplit(url)
    except Exception:  # noqa: BLE001
        return False
    return normalize_domain_key(parsed.netloc) in GMAIL_HOSTS


def is_low_signal_source_key(source_key: str | None) -> bool:
    normalized = normalize_domain_key(source_key)
    return bool(
        normalized
        and (
            normalized in LOW_SIGNAL_SOURCE_KEYS
            or normalized.endswith("@substack.com")
        )
    )


def unwrap_tracking_url(url: str | None) -> str | None:
    if not isinstance(url, str) or not url.strip():
        return None

    raw_url = url.strip()
    try:
        parsed = urlsplit(raw_url)
    except Exception:  # noqa: BLE001
        return raw_url

    if normalize_domain_key(parsed.netloc) != "substack.com":
        return raw_url

    path_parts = [part for part in parsed.path.split("/") if part]
    if len(path_parts) < 3 or path_parts[0] != "redirect":
        return raw_url

    payload = _decode_substack_redirect_payload(path_parts[-1])
    if not isinstance(payload, dict):
        return raw_url

    event_url = payload.get("e")
    if not isinstance(event_url, str):
        return raw_url

    next_url = _extract_query_url(event_url, "next")
    if next_url:
        return next_url
    if _is_http_url(event_url):
        return event_url
    return raw_url


def _decode_substack_redirect_payload(encoded_segment: str) -> dict[str, Any] | None:
    encoded_payload = encoded_segment.split(".", 1)[0]
    if not encoded_payload:
        return None
    try:
        padded_payload = encoded_payload + "=" * (-len(encoded_payload) % 4)
        decoded = base64.urlsafe_b64decode(padded_payload.encode("ascii")).decode(
            "utf-8"
        )
        payload = json.loads(decoded)
    except Exception:  # noqa: BLE001
        return None
    return payload if isinstance(payload, dict) else None


def _extract_query_url(url: str, key: str) -> str | None:
    try:
        values = parse_qs(urlsplit(url).query).get(key, [])
    except Exception:  # noqa: BLE001
        return None
    for value in values:
        if _is_http_url(value):
            return value
    return None


def _is_http_url(url: str | None) -> bool:
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urlsplit(url.strip())
    except Exception:  # noqa: BLE001
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
