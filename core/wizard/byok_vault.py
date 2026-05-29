"""BYOK vault — Fernet AES-128-CBC + HMAC-SHA256 file store.

OSS Fase 1 single-user, master key file `~/.marvis/master.key` chmod 600.
Fase 2 upgrade target: AES-GCM + PBKDF2-derived KEK with user master password.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

DEFAULT_VAULT_DIR = Path.home() / ".marvis"
MASTER_KEY_FILENAME = "master.key"
VAULT_FILENAME = "byok.vault"
VAULT_VERSION = 1

_KNOWN_PROVIDERS = ("anthropic", "openai", "mac_gateway", "bedrock")


class VaultError(Exception):
    pass


class MasterKeyMissing(VaultError):
    pass


def _master_key_path(vault_dir: Path) -> Path:
    return vault_dir / MASTER_KEY_FILENAME


def _vault_path(vault_dir: Path) -> Path:
    return vault_dir / VAULT_FILENAME


def _resolve_dir(vault_dir: Path | None) -> Path:
    base = vault_dir or DEFAULT_VAULT_DIR
    return Path(base).expanduser()


def ensure_master_key(
    vault_dir: Path | None = None, *, create: bool = True
) -> bytes:
    """Read or create the master key file. Returns the raw Fernet key bytes."""
    resolved = _resolve_dir(vault_dir)
    resolved.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(resolved, 0o700)
    except OSError:
        pass

    key_file = _master_key_path(resolved)
    if key_file.exists():
        key = key_file.read_bytes().strip()
        if not key:
            raise VaultError(f"Master key file empty: {key_file}")
        return key

    if not create:
        raise MasterKeyMissing(f"Master key not found at {key_file}")

    key = Fernet.generate_key()
    key_file.write_bytes(key)
    try:
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return key


def _empty_vault() -> dict[str, Any]:
    return {
        "version": VAULT_VERSION,
        "providers": {provider: None for provider in _KNOWN_PROVIDERS},
    }


def load_vault(vault_dir: Path | None = None) -> dict[str, Any]:
    """Decrypt + return the vault. Returns an empty vault if file missing."""
    resolved = _resolve_dir(vault_dir)
    vault_file = _vault_path(resolved)
    if not vault_file.exists():
        return _empty_vault()

    master = ensure_master_key(resolved, create=False)
    fernet = Fernet(master)
    token = vault_file.read_bytes()
    try:
        plaintext = fernet.decrypt(token)
    except InvalidToken as exc:
        raise VaultError("Vault corrupted or master key mismatch") from exc
    return json.loads(plaintext.decode("utf-8"))


def save_vault(
    vault: dict[str, Any], vault_dir: Path | None = None
) -> Path:
    """Encrypt + atomically write the vault. Returns the absolute path."""
    resolved = _resolve_dir(vault_dir)
    master = ensure_master_key(resolved, create=True)
    fernet = Fernet(master)
    payload = json.dumps(vault, sort_keys=True).encode("utf-8")
    token = fernet.encrypt(payload)

    vault_file = _vault_path(resolved)
    tmp_file = vault_file.with_suffix(".tmp")
    tmp_file.write_bytes(token)
    try:
        os.chmod(tmp_file, 0o600)
    except OSError:
        pass
    tmp_file.replace(vault_file)
    return vault_file


def store_provider_key(
    provider: str,
    api_key: str,
    *,
    base_url: str | None = None,
    vault_dir: Path | None = None,
) -> None:
    if provider not in _KNOWN_PROVIDERS:
        raise VaultError(f"Unknown provider: {provider}")
    vault = load_vault(vault_dir)
    entry: dict[str, Any] = {
        "api_key": api_key,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    if base_url is not None:
        entry["base_url"] = base_url
    vault["providers"][provider] = entry
    save_vault(vault, vault_dir)


def mask_api_key(api_key: str | None) -> str:
    """First 6 + last 4. Used for recap display + audit log lines."""
    if not api_key:
        return ""
    if len(api_key) <= 10:
        return "***"
    return f"{api_key[:6]}***{api_key[-4:]}"
