# v1.0.0 - 2026-05-26 - M1 CAPTURE U5 — provider-key + per-function config store
"""Read/write helpers for provider_keys + llm_function_config, and the resolver
that turns a function's config into a usable provider descriptor.

Plaintext keys are encrypted at rest via crypto.encrypt_provider_key and never
logged or returned. The resolver is the single read path U4's classifier factory
consumes for the `classify` function.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import aiosqlite

from core.api.models.llm_config import KEYED_PROVIDERS
from core.api.services.crypto import decrypt_provider_key, encrypt_provider_key

_KEY_PREFIX_CHARS = 6


@dataclass(frozen=True)
class ResolvedProvider:
    """A function's effective provider after config resolution."""

    function_name: str
    provider: str
    model: str | None
    base_url: str | None
    api_key: str | None  # decrypted; None for keyless providers


# --------------------------------------------------------------------------- #
# Provider keys
# --------------------------------------------------------------------------- #


async def create_provider_key(
    db: aiosqlite.Connection,
    *,
    provider: str,
    label: str | None,
    api_key: str | None,
    base_url: str | None,
    workspace_id: str = "ws_default",
) -> str:
    """Insert a provider key (encrypting any plaintext key). Returns its id.

    Caller must commit. Keyed providers without an api_key raise ValueError.
    """
    if provider in KEYED_PROVIDERS and not api_key:
        raise ValueError(f"provider {provider!r} requires an api_key")
    key_id = f"pk_{uuid.uuid4().hex[:16]}"
    ciphertext = encrypt_provider_key(api_key, workspace_id) if api_key else None
    await db.execute(
        """
        INSERT INTO provider_keys (id, provider, label, key_ciphertext, base_url, workspace_id)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (key_id, provider, label, ciphertext, base_url, workspace_id),
    )
    return key_id


def _key_view(ciphertext: str | None, workspace_id: str) -> tuple[bool, str | None, str]:
    """Return (has_key, key_prefix, key_status) without exposing the plaintext."""
    if not ciphertext:
        return False, None, "none"
    plaintext = decrypt_provider_key(ciphertext, workspace_id)
    if plaintext is None:
        return True, None, "unreadable"
    return True, f"{plaintext[:_KEY_PREFIX_CHARS]}…", "set"


async def list_provider_keys(
    db: aiosqlite.Connection, workspace_id: str = "ws_default"
) -> list[dict]:
    async with db.execute(
        "SELECT id, provider, label, key_ciphertext, base_url, created_at, updated_at "
        "FROM provider_keys WHERE workspace_id = ? ORDER BY created_at DESC",
        (workspace_id,),
    ) as cur:
        rows = await cur.fetchall()
    out: list[dict] = []
    for row in rows:
        has_key, key_prefix, key_status = _key_view(row["key_ciphertext"], workspace_id)
        out.append(
            {
                "id": row["id"],
                "provider": row["provider"],
                "label": row["label"],
                "base_url": row["base_url"],
                "has_key": has_key,
                "key_prefix": key_prefix,
                "key_status": key_status,
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
    return out


async def delete_provider_key(
    db: aiosqlite.Connection, key_id: str, workspace_id: str = "ws_default"
) -> bool:
    """Delete a provider key. Referencing function configs get provider_key_id
    NULLed (FK ON DELETE SET NULL) — re-derive their enabled state to 0."""
    cur = await db.execute(
        "DELETE FROM provider_keys WHERE id = ? AND workspace_id = ?",
        (key_id, workspace_id),
    )
    if cur.rowcount:
        await db.execute(
            "UPDATE llm_function_config SET enabled = 0, updated_at = datetime('now') "
            "WHERE provider_key_id IS NULL AND enabled = 1 AND workspace_id = ?",
            (workspace_id,),
        )
    return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Per-function config
# --------------------------------------------------------------------------- #


async def set_function_config(
    db: aiosqlite.Connection,
    *,
    function_name: str,
    provider_key_id: str | None,
    model: str | None,
    enabled: bool,
    workspace_id: str = "ws_default",
) -> None:
    """Upsert a function's config. enabled is forced to 0 when no provider_key_id."""
    effective_enabled = 1 if (enabled and provider_key_id) else 0
    await db.execute(
        """
        INSERT INTO llm_function_config
            (function_name, provider_key_id, model, enabled, workspace_id, updated_at)
        VALUES (?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT(function_name, workspace_id) DO UPDATE SET
            provider_key_id = excluded.provider_key_id,
            model = excluded.model,
            enabled = excluded.enabled,
            updated_at = datetime('now')
        """,
        (function_name, provider_key_id, model, effective_enabled, workspace_id),
    )


async def list_function_configs(
    db: aiosqlite.Connection, workspace_id: str = "ws_default"
) -> list[dict]:
    """Return all three functions' config (defaulting to disabled when unset)."""
    async with db.execute(
        """
        SELECT c.function_name, c.provider_key_id, c.model, c.enabled,
               p.provider AS provider
          FROM llm_function_config c
          LEFT JOIN provider_keys p ON p.id = c.provider_key_id
         WHERE c.workspace_id = ?
        """,
        (workspace_id,),
    ) as cur:
        rows = {r["function_name"]: r for r in await cur.fetchall()}
    out: list[dict] = []
    for fn in ("classify", "embedding", "brain"):
        row = rows.get(fn)
        if row is None:
            out.append(
                {
                    "function_name": fn,
                    "provider_key_id": None,
                    "provider": None,
                    "model": None,
                    "enabled": False,
                    "status": "disabled_no_provider",
                }
            )
            continue
        configured = bool(row["enabled"]) and row["provider_key_id"] is not None
        out.append(
            {
                "function_name": fn,
                "provider_key_id": row["provider_key_id"],
                "provider": row["provider"],
                "model": row["model"],
                "enabled": bool(row["enabled"]),
                "status": "configured" if configured else "disabled_no_provider",
            }
        )
    return out


async def resolve_function_provider(
    db: aiosqlite.Connection,
    function_name: str,
    workspace_id: str = "ws_default",
) -> ResolvedProvider | None:
    """Resolve a function to a usable provider, or None when auto-run is off.

    Returns None when: no config row, not enabled, no linked provider key, a keyed
    provider whose key is missing/unreadable (master key changed). This is the
    single gate: None => the function's auto-run is disabled (no heuristic
    fallback), and callers must surface the disabled state, not guess.
    """
    async with db.execute(
        """
        SELECT c.enabled, c.model, p.provider, p.base_url, p.key_ciphertext
          FROM llm_function_config c
          JOIN provider_keys p ON p.id = c.provider_key_id
         WHERE c.function_name = ? AND c.workspace_id = ?
        """,
        (function_name, workspace_id),
    ) as cur:
        row = await cur.fetchone()
    if row is None or not row["enabled"]:
        return None

    provider = row["provider"]
    api_key: str | None = None
    if row["key_ciphertext"]:
        api_key = decrypt_provider_key(row["key_ciphertext"], workspace_id)
        if api_key is None:
            # Master key changed / corrupt ciphertext -> treat as disabled.
            return None
    if provider in KEYED_PROVIDERS and not api_key:
        return None

    return ResolvedProvider(
        function_name=function_name,
        provider=provider,
        model=row["model"],
        base_url=row["base_url"],
        api_key=api_key,
    )


async def classify_provider_status(
    db: aiosqlite.Connection, workspace_id: str = "ws_default"
) -> str:
    """'configured' when the classify function has a usable provider, else
    'disabled_no_provider' (drives the Console alert + monitoring widget)."""
    resolved = await resolve_function_provider(db, "classify", workspace_id)
    return "configured" if resolved is not None else "disabled_no_provider"
