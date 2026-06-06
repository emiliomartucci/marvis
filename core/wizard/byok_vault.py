"""BYOK vault — Fernet AES-128-CBC + HMAC-SHA256 file store.

OSS single-user. The vault file (`~/.marvis/byok.vault`) is encrypted with a
Fernet "master key". That master key is itself protected at rest:

- **Encrypted (preferred)** — when a passphrase source is available the master
  key is wrapped with a KEK (key-encryption key) derived from the passphrase via
  scrypt (memory-hard KDF) and stored as `master.key.enc` next to a random
  `master.key.salt`. The raw Fernet key never touches disk.
- **Cleartext (legacy / headless fallback)** — older installs (and headless
  servers with no passphrase source) keep a cleartext `master.key`. It still
  works; we just emit a one-time WARNING so the operator can opt into wrapping by
  setting ``MARVIS_MASTER_PASSPHRASE``.

Passphrase source precedence: ``MARVIS_MASTER_PASSPHRASE`` env → OS keyring
(optional, soft dependency) → interactive prompt (only on a TTY). No source +
no TTY => cleartext + warning, never a lockout.
"""

from __future__ import annotations

import base64
import getpass
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from core.platform import secure_path

logger = logging.getLogger(__name__)

DEFAULT_VAULT_DIR = Path.home() / ".marvis"
MASTER_KEY_FILENAME = "master.key"
MASTER_KEY_ENC_FILENAME = "master.key.enc"
MASTER_KEY_SALT_FILENAME = "master.key.salt"
VAULT_FILENAME = "byok.vault"
VAULT_VERSION = 1

# Passphrase source for the KEK that wraps the master key.
PASSPHRASE_ENV = "MARVIS_MASTER_PASSPHRASE"
# OS keyring coordinates (only used when the optional `keyring` backend exists).
_KEYRING_SERVICE = "marvisx-byok"
_KEYRING_USERNAME = "master-passphrase"

# scrypt parameters (memory-hard KDF). n=2**15 ~= 32 MiB working set: strong for a
# local single-user secret, light enough to derive once per process on a small box.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_KEYLEN = 32
_SALT_BYTES = 16

_KNOWN_PROVIDERS = ("anthropic", "openai", "mac_gateway", "bedrock")

# Module-level latch so the "your master.key is unencrypted" notice fires once per
# process instead of on every vault read/write.
_cleartext_warning_emitted = False


class VaultError(Exception):
    pass


class MasterKeyMissing(VaultError):
    pass


class WrongPassphrase(VaultError):
    """Raised when an encrypted master.key exists but the passphrase is wrong."""


def _master_key_path(vault_dir: Path) -> Path:
    return vault_dir / MASTER_KEY_FILENAME


def _master_key_enc_path(vault_dir: Path) -> Path:
    return vault_dir / MASTER_KEY_ENC_FILENAME


def _master_key_salt_path(vault_dir: Path) -> Path:
    return vault_dir / MASTER_KEY_SALT_FILENAME


def _vault_path(vault_dir: Path) -> Path:
    return vault_dir / VAULT_FILENAME


def _resolve_dir(vault_dir: Path | None) -> Path:
    base = vault_dir or DEFAULT_VAULT_DIR
    return Path(base).expanduser()


# --------------------------------------------------------------------------- #
# Passphrase resolution + KEK derivation
# --------------------------------------------------------------------------- #


def _keyring_passphrase() -> str | None:
    """Read the passphrase from the OS keyring.

    `keyring` is an OPTIONAL soft dependency: a headless server has no backend, so
    we import inside the function and swallow any import/backend error. Never a
    hard runtime requirement.
    """
    try:
        import keyring  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — no keyring installed at all
        return None
    try:
        value = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USERNAME)
    except Exception:  # noqa: BLE001 — no usable backend (headless), locked, etc.
        return None
    return value or None


def _is_interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:  # noqa: BLE001 — stdin/stdout replaced in some hosts
        return False


def resolve_passphrase(*, allow_prompt: bool = False) -> str | None:
    """Resolve the master passphrase, or None when no source is available.

    Precedence: ``MARVIS_MASTER_PASSPHRASE`` env → OS keyring (optional) →
    interactive prompt (only when ``allow_prompt`` and a TTY is present).
    """
    env = os.environ.get(PASSPHRASE_ENV)
    if env:
        return env

    from_keyring = _keyring_passphrase()
    if from_keyring:
        return from_keyring

    if allow_prompt and _is_interactive():
        try:
            entered = getpass.getpass(
                "Master passphrase (protects your BYOK API keys): "
            )
        except Exception:  # noqa: BLE001 — non-interactive surprise, EOF, etc.
            return None
        return entered or None

    return None


def _derive_kek(passphrase: str, salt: bytes) -> bytes:
    """Derive a 32-byte Fernet-compatible KEK from the passphrase via scrypt."""
    kdf = Scrypt(
        salt=salt,
        length=_SCRYPT_KEYLEN,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
    )
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def _chmod_600(path: Path) -> None:
    # POSIX chmod 0o600; Windows no-op + honest warning (owner-only needs an ACL).
    secure_path(path, mode=0o600)


# --------------------------------------------------------------------------- #
# Wrap / unwrap the master key
# --------------------------------------------------------------------------- #


def _write_encrypted_master_key(
    vault_dir: Path, fernet_key: bytes, passphrase: str
) -> None:
    """Wrap `fernet_key` with a passphrase-derived KEK and write the .enc + .salt."""
    salt = os.urandom(_SALT_BYTES)
    kek = _derive_kek(passphrase, salt)
    wrapped = Fernet(kek).encrypt(fernet_key)

    salt_file = _master_key_salt_path(vault_dir)
    salt_tmp = salt_file.with_suffix(salt_file.suffix + ".tmp")
    salt_tmp.write_bytes(salt)
    _chmod_600(salt_tmp)
    salt_tmp.replace(salt_file)

    enc_file = _master_key_enc_path(vault_dir)
    enc_tmp = enc_file.with_suffix(enc_file.suffix + ".tmp")
    enc_tmp.write_bytes(wrapped)
    _chmod_600(enc_tmp)
    enc_tmp.replace(enc_file)


def _read_encrypted_master_key(vault_dir: Path, passphrase: str) -> bytes:
    """Unwrap and return the raw Fernet key from the .enc + .salt files."""
    salt = _master_key_salt_path(vault_dir).read_bytes()
    wrapped = _master_key_enc_path(vault_dir).read_bytes().strip()
    if not salt or not wrapped:
        raise VaultError("Encrypted master key files are empty/corrupt")
    kek = _derive_kek(passphrase, salt)
    try:
        return Fernet(kek).decrypt(wrapped)
    except InvalidToken as exc:
        raise WrongPassphrase(
            f"wrong {PASSPHRASE_ENV} (cannot unwrap the master key)"
        ) from exc


def _warn_cleartext_once(key_file: Path) -> None:
    global _cleartext_warning_emitted
    if _cleartext_warning_emitted:
        return
    _cleartext_warning_emitted = True
    logger.warning(
        "%s is unencrypted; set %s to protect your BYOK API keys at rest "
        "(it will be migrated to an encrypted master.key.enc automatically).",
        key_file,
        PASSPHRASE_ENV,
    )


# --------------------------------------------------------------------------- #
# Public master-key entrypoint
# --------------------------------------------------------------------------- #


def ensure_master_key(
    vault_dir: Path | None = None,
    *,
    create: bool = True,
    allow_prompt: bool = False,
) -> bytes:
    """Read or create the master key. Returns the raw Fernet key bytes.

    Resolution order (no lockout, ever):

    1. **Encrypted on disk** (`master.key.enc` + `master.key.salt`) → unwrap with
       the resolved passphrase. Missing passphrase → ``MasterKeyMissing``; wrong
       passphrase → ``WrongPassphrase`` (clean error, no corruption).
    2. **Cleartext on disk** (`master.key`, legacy) → use it. If a passphrase is
       available, transparently MIGRATE it (write .enc/.salt, remove cleartext).
       If not, keep using cleartext + emit a one-time WARNING.
    3. **Nothing on disk** + ``create`` → generate a fresh key. If a passphrase is
       available, write it encrypted from the start (never cleartext). Otherwise
       write cleartext + warning. ``create=False`` → ``MasterKeyMissing``.
    """
    resolved = _resolve_dir(vault_dir)
    resolved.mkdir(parents=True, exist_ok=True)
    secure_path(resolved, mode=0o700)

    enc_file = _master_key_enc_path(resolved)
    salt_file = _master_key_salt_path(resolved)
    key_file = _master_key_path(resolved)

    # 1. Encrypted form present → it is the source of truth.
    if enc_file.exists() and salt_file.exists():
        passphrase = resolve_passphrase(allow_prompt=allow_prompt)
        if not passphrase:
            raise MasterKeyMissing(
                f"Encrypted master key found at {enc_file} but no passphrase "
                f"available; set {PASSPHRASE_ENV} to unlock the BYOK vault."
            )
        return _read_encrypted_master_key(resolved, passphrase)

    # 2. Cleartext form present (legacy / headless) → keep working, migrate if able.
    if key_file.exists():
        key = key_file.read_bytes().strip()
        if not key:
            raise VaultError(f"Master key file empty: {key_file}")
        passphrase = resolve_passphrase(allow_prompt=allow_prompt)
        if passphrase:
            # Transparent migration: wrap, then drop the cleartext copy.
            _write_encrypted_master_key(resolved, key, passphrase)
            try:
                key_file.unlink()
            except OSError:
                logger.warning(
                    "Migrated master key to %s but could not remove the "
                    "cleartext %s; delete it manually.",
                    enc_file,
                    key_file,
                )
            logger.info(
                "Migrated %s to an encrypted master.key.enc (protected by %s).",
                key_file,
                PASSPHRASE_ENV,
            )
        else:
            _warn_cleartext_once(key_file)
        return key

    # 3. Nothing on disk.
    if not create:
        raise MasterKeyMissing(f"Master key not found at {key_file}")

    key = Fernet.generate_key()
    passphrase = resolve_passphrase(allow_prompt=allow_prompt)
    if passphrase:
        _write_encrypted_master_key(resolved, key, passphrase)
    else:
        key_file.write_bytes(key)
        _chmod_600(key_file)
        _warn_cleartext_once(key_file)
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
    _chmod_600(tmp_file)
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
