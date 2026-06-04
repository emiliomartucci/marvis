"""Tenant-scoped Fernet helpers for per-user API keys."""
from __future__ import annotations

import base64
import logging

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core.api.config import settings

logger = logging.getLogger(__name__)


def _derive_fernet_key(
    master_secret: str,
    user_id: str,
    tenant_slug: str,
    salt_version: str = "v1",
) -> bytes:
    """Derive one Fernet key bound to tenant and user context."""
    if not master_secret:
        raise ValueError("master_secret is required")
    if not user_id:
        raise ValueError("user_id is required")
    if not tenant_slug:
        raise ValueError("tenant_slug is required")

    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=f"marvisx-tenant-{tenant_slug}-fernet-{salt_version}".encode("utf-8"),
        info=f"api-key-encryption:user:{user_id}".encode("utf-8"),
    )
    raw = hkdf.derive(master_secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def get_user_cipher(user_id: str, tenant_slug: str | None = None) -> MultiFernet:
    """Return a MultiFernet cipher for one user when the feature is enabled."""
    if not settings.per_user_api_key_enabled:
        raise RuntimeError("per-user API key encryption is disabled")
    if not settings.byok_fernet_secret:
        raise RuntimeError("BYOK_FERNET_SECRET is not configured")

    tenant = tenant_slug or settings.deploy_mode
    key = _derive_fernet_key(
        settings.byok_fernet_secret,
        user_id,
        tenant,
        settings.fernet_salt_version,
    )
    return MultiFernet([Fernet(key)])


def encrypt_user_api_key(
    plaintext: str,
    user_id: str,
    tenant_slug: str | None = None,
) -> str:
    """Encrypt a per-user API key with a tenant-scoped key."""
    return get_user_cipher(user_id, tenant_slug).encrypt(
        plaintext.encode("utf-8")
    ).decode("utf-8")


def decrypt_user_api_key(
    ciphertext: str,
    user_id: str,
    tenant_slug: str | None = None,
) -> str | None:
    """Decrypt a per-user API key, returning None on invalid/rotated tokens."""
    try:
        return get_user_cipher(user_id, tenant_slug).decrypt(
            ciphertext.encode("utf-8")
        ).decode("utf-8")
    except (InvalidToken, RuntimeError, ValueError):
        logger.warning("Failed to decrypt user API key", exc_info=True)
        return None


# --------------------------------------------------------------------------- #
# Org-scoped provider keys (M1 CAPTURE U5 — BYOK LLM provider keys)
#
# Single-org by design: keyed by workspace_id ('ws_default'), independent of the
# per-user feature flag. Same HKDF + MultiFernet primitive as the per-user path
# (one encryption scheme in the codebase, rotation comes for free). Ciphertext is
# versioned with a 'v1:' scheme prefix so a future master-key rotation is a
# decrypt-old / encrypt-new loop (M7.6).
# --------------------------------------------------------------------------- #

_PROVIDER_KEY_SCHEME = "v1"


def _derive_org_fernet_key(
    master_secret: str,
    workspace_id: str,
    salt_version: str = "v1",
) -> bytes:
    """Derive one Fernet key bound to an organization (workspace) context."""
    if not master_secret:
        raise ValueError("master_secret is required")
    if not workspace_id:
        raise ValueError("workspace_id is required")
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=f"marvisx-org-{workspace_id}-provider-key-{salt_version}".encode("utf-8"),
        info=f"provider-key-encryption:ws:{workspace_id}".encode("utf-8"),
    )
    raw = hkdf.derive(master_secret.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def get_org_cipher(workspace_id: str = "ws_default") -> MultiFernet:
    """Return a MultiFernet cipher for an organization's provider keys.

    Fail-closed: requires BYOK_FERNET_SECRET. No per-user feature flag — these
    are org-level config secrets.
    """
    if not settings.byok_fernet_secret:
        raise RuntimeError("BYOK_FERNET_SECRET is not configured")
    key = _derive_org_fernet_key(
        settings.byok_fernet_secret, workspace_id, settings.fernet_salt_version
    )
    return MultiFernet([Fernet(key)])


def encrypt_provider_key(plaintext: str, workspace_id: str = "ws_default") -> str:
    """Encrypt a provider API key at rest. Returns a versioned ciphertext."""
    token = get_org_cipher(workspace_id).encrypt(plaintext.encode("utf-8")).decode("utf-8")
    return f"{_PROVIDER_KEY_SCHEME}:{token}"


def decrypt_provider_key(
    ciphertext: str, workspace_id: str = "ws_default"
) -> str | None:
    """Decrypt a versioned provider-key ciphertext; None on invalid/rotated/changed key."""
    try:
        scheme, _, token = (ciphertext or "").partition(":")
        if scheme != _PROVIDER_KEY_SCHEME or not token:
            return None
        return get_org_cipher(workspace_id).decrypt(token.encode("utf-8")).decode("utf-8")
    except (InvalidToken, RuntimeError, ValueError):
        logger.warning("Failed to decrypt provider key", exc_info=True)
        return None
