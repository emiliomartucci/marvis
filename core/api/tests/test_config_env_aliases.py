"""C2 rebrand (PiR -> Marvis): MARVIS_* env vars are ADDITIVE aliases over PIR_*.

Both names must be accepted by the Settings model; when both are set the
canonical MARVIS_* name wins. PIR_* stays valid so prod (which reads PIR_*)
is unaffected.
"""
from __future__ import annotations

import pytest

from core.api.config import Settings

# (settings_attr, MARVIS_ name, PIR_ name, sample value)
_ALIAS_CASES = [
    ("db_path", "MARVIS_DB_PATH", "PIR_DB_PATH", "/tmp/marvis-alias-test.db"),
    ("pir_env", "MARVIS_ENV", "PIR_ENV", "production"),
    ("pir_instance", "MARVIS_INSTANCE", "PIR_INSTANCE", "alias-test"),
    ("pir_jwt_secret", "MARVIS_JWT_SECRET", "PIR_JWT_SECRET", "secret-xyz"),
    (
        "pir_admin_password_hash",
        "MARVIS_ADMIN_PASSWORD_HASH",
        "PIR_ADMIN_PASSWORD_HASH",
        "$2b$12$abcdefghijklmnopqrstuv",
    ),
]


def _clear(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    # DB_PATH is the third alias for db_path — clear it too so ambient env
    # never shadows the var under test.
    for name in (*names, "DB_PATH"):
        monkeypatch.delenv(name, raising=False)


def _settings_for_alias(attr: str) -> Settings:
    kwargs = {"pir_jwt_secret": "t" * 32} if attr == "pir_env" else {}
    return Settings(_env_file=None, **kwargs)


@pytest.mark.parametrize("attr,marvis,pir,value", _ALIAS_CASES)
def test_pir_alias_still_accepted(monkeypatch, attr, marvis, pir, value):
    _clear(monkeypatch, marvis, pir)
    monkeypatch.setenv(pir, value)
    assert getattr(_settings_for_alias(attr), attr) == value


@pytest.mark.parametrize("attr,marvis,pir,value", _ALIAS_CASES)
def test_marvis_alias_accepted(monkeypatch, attr, marvis, pir, value):
    _clear(monkeypatch, marvis, pir)
    monkeypatch.setenv(marvis, value)
    assert getattr(_settings_for_alias(attr), attr) == value


@pytest.mark.parametrize("attr,marvis,pir,value", _ALIAS_CASES)
def test_marvis_wins_when_both_set(monkeypatch, attr, marvis, pir, value):
    _clear(monkeypatch, marvis, pir)
    monkeypatch.setenv(marvis, value)
    monkeypatch.setenv(pir, f"legacy-{value}")
    assert getattr(_settings_for_alias(attr), attr) == value


def test_canary_banner_both_aliases(monkeypatch):
    _clear(monkeypatch, "MARVIS_CANARY_BANNER", "PIR_CANARY_BANNER")
    monkeypatch.setenv("PIR_CANARY_BANNER", "true")
    assert Settings(_env_file=None).pir_canary_banner is True

    _clear(monkeypatch, "MARVIS_CANARY_BANNER", "PIR_CANARY_BANNER")
    monkeypatch.setenv("MARVIS_CANARY_BANNER", "true")
    assert Settings(_env_file=None).pir_canary_banner is True


def test_remote_embedding_config_not_in_core_settings():
    """The remote embedding backend (and its operator kill-switch + API key) is a
    deploy-only module — none of its config lives on core Settings. Those fields
    are read inside the backend module itself, so they are absent from this build.
    Guarded generically (no provider field name) so the assertion never re-creates
    the very leak the carve-out removed."""
    fields = set(Settings(_env_file=None).model_dump())
    leaked = [f for f in fields if "embed" in f and ("disable" in f or "api_key" in f)]
    assert leaked == [], f"remote-embed config leaked onto core Settings: {leaked}"
