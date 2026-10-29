"""Pure request contract for the retired personal Console entry point."""

from __future__ import annotations

import posixpath
from collections.abc import Mapping
from urllib.parse import quote, quote_from_bytes, unquote_to_bytes


def is_ui_request_path(path: str) -> bool:
    return path == "/ui" or path.startswith("/ui/")


def raw_path_claims_retired_ui(raw_path: bytes) -> bool:
    if raw_path == b"/ui" or raw_path.startswith(b"/ui/"):
        return True
    if not raw_path.startswith(b"/ui%"):
        return False
    if len(raw_path) < 6:
        return True
    try:
        decoded_byte = int(raw_path[4:6], 16)
    except ValueError:
        return True
    return (
        decoded_byte < 0x20
        or decoded_byte == 0x7F
        or decoded_byte in {ord("%"), ord("/"), ord("\\")}
    )


def is_retired_ui_candidate(*, path: str, raw_path: object) -> bool:
    return is_ui_request_path(path) or (
        isinstance(raw_path, (bytes, bytearray))
        and raw_path_claims_retired_ui(bytes(raw_path))
    )


def retired_ui_decision(
    scope: Mapping[str, object],
    *,
    target: str,
) -> tuple[int, str | None] | None:
    """Return the local response contract without auth, wake, or I/O."""
    if scope.get("type") != "http":
        return None
    path = str(scope.get("path", ""))
    raw_path_value = scope.get("raw_path")
    raw_path = (
        bytes(raw_path_value)
        if isinstance(raw_path_value, (bytes, bytearray))
        else quote(path, safe="/!$&'()*+,:;=@-._~").encode("ascii")
    )
    if not is_retired_ui_candidate(path=path, raw_path=raw_path):
        return None
    method = str(scope.get("method", "GET")).upper()
    if method not in {"GET", "HEAD"}:
        return 405, None
    encoded_path = _validated_path(
        raw_path=raw_path,
        decoded_scope_path=path,
    )
    if encoded_path is None:
        return 400, None
    query = scope.get("query_string", b"")
    if not isinstance(query, (bytes, bytearray)):
        return 400, None
    raw_query = bytes(query)
    if not _valid_encoded_component(
        raw_query,
        reject_encoded_percent=False,
        path_component=False,
    ):
        return 400, None
    location = target + encoded_path[len("/ui") :]
    if raw_query:
        location += "?" + quote_from_bytes(
            raw_query,
            safe="!$&'()*+,/:;=?@-._~%",
        )
    return 308, location


def _validated_path(*, raw_path: bytes, decoded_scope_path: str) -> str | None:
    if not (
        raw_path == b"/ui" or raw_path.startswith(b"/ui/")
    ) or not _valid_encoded_component(
        raw_path,
        reject_encoded_percent=True,
        path_component=True,
    ):
        return None
    try:
        encoded_path = raw_path.decode("ascii")
        decoded_path = unquote_to_bytes(raw_path).decode("utf-8")
    except (UnicodeDecodeError, ValueError):
        return None
    if decoded_path != decoded_scope_path or not is_ui_request_path(decoded_path):
        return None
    if "\\" in decoded_path or "//" in decoded_path:
        return None
    if any(segment in {".", ".."} for segment in decoded_path.split("/")):
        return None
    normalized = posixpath.normpath(decoded_path)
    expected = decoded_path.removesuffix("/")
    if normalized != expected:
        return None
    return encoded_path


def _valid_encoded_component(
    value: bytes,
    *,
    reject_encoded_percent: bool,
    path_component: bool,
) -> bool:
    hex_bytes = b"0123456789abcdefABCDEF"
    if any(byte < 0x20 or byte == 0x7F or byte > 0x7F for byte in value):
        return False
    index = 0
    while index < len(value):
        if value[index] != ord("%"):
            index += 1
            continue
        if (
            index + 2 >= len(value)
            or value[index + 1] not in hex_bytes
            or value[index + 2] not in hex_bytes
        ):
            return False
        encoded = value[index + 1 : index + 3].lower()
        decoded_byte = int(encoded, 16)
        if path_component and (decoded_byte < 0x20 or decoded_byte == 0x7F):
            return False
        if (path_component and encoded in {b"2f", b"5c"}) or (
            reject_encoded_percent and encoded == b"25"
        ):
            return False
        index += 3
    forbidden = {ord("\\"), ord("#")}
    if path_component:
        forbidden.add(ord("?"))
    return not any(byte in forbidden for byte in value)
