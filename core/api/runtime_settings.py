# v1.0.0 - 2026-05-28 - S2: shared settings-apply so CLI + MCP server agree on db_path/projects_root
"""Apply the user's ``~/.marvis/settings.yaml`` onto the API ``settings`` singleton.

The OSS runtime has two entrypoints that must reach the SAME SQLite file and the
SAME project directory the user configured with ``marvis init``:

* the thin ``marvis`` CLI (``core.cli._runtime_ctx`` — status/brief/triage/index),
* the stdio MCP server (``core.api.mcp.server`` — search/graph/tasks/...).

Before this helper existed only the CLI mirrored ``settings.yaml`` onto
``core.api.config.settings`` (in ``_runtime_ctx._apply_settings``). The MCP server
ran with the bare defaults (``db_path='console.db'`` relative, default project
dirs), so ``search`` / ``graph_overview`` opened the WRONG (empty) database and a
transmuted project's source path tripped the ``ALLOWED_REPO_PARENTS`` warning.

This module is the single, dependency-light (no ``typer`` / ``fastapi`` / ``rich``)
implementation both entrypoints call. Best-effort: when no settings file exists the
API defaults / ``$PIR_DB_PATH`` env stand, so tests and ad-hoc runs keep working.
"""
from __future__ import annotations

import os
from pathlib import Path

_applied = False


def _settings_yaml_path() -> Path:
    """Resolve the settings.yaml location the same way ``marvis init`` writes it."""
    settings_path = os.environ.get("MARVIS_SETTINGS_PATH")
    if settings_path:
        return Path(settings_path).expanduser()
    vault_dir = os.environ.get("MARVIS_VAULT_DIR")
    base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
    return base / "settings.yaml"


def apply_marvis_settings(*, force: bool = False) -> bool:
    """Point the runtime at the user's configured DB + projects_root (once).

    Mirrors ``storage.db_path`` / ``storage.projects_root`` from
    ``~/.marvis/settings.yaml`` (or ``$MARVIS_SETTINGS_PATH`` / ``$MARVIS_VAULT_DIR``)
    onto ``core.api.config.settings`` and the project-index roots.

    Idempotent: runs at most once per process unless ``force=True``. Returns
    ``True`` when a settings file was found and applied, ``False`` otherwise.
    """
    global _applied
    if _applied and not force:
        return False
    _applied = True

    path = _settings_yaml_path()
    if not path.is_file():
        return False

    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 — malformed settings must not crash the runtime
        return False
    if not isinstance(data, dict):
        return False

    storage = data.get("storage") or {}
    db_path = storage.get("db_path")
    projects_root = storage.get("projects_root")

    applied = False

    if db_path:
        from core.api.config import settings

        settings.db_path = str(Path(db_path).expanduser())
        applied = True

    if projects_root:
        from core.api import config as config_mod
        from core.api.routers.projects import _set_project_dirs

        # Explicit process configuration wins over the persisted settings
        # file.  Normalize the winning value once, then keep every consumer
        # aligned with it.
        effective_projects_root = (
            os.environ.get("MARVIS_PROJECTS_ROOT") or projects_root
        )
        projects_root_path = Path(effective_projects_root).expanduser().resolve()
        os.environ["MARVIS_PROJECTS_ROOT"] = str(projects_root_path)
        _set_project_dirs([projects_root_path])

        # Hosted tenants keep project metadata under ``projects/`` and Git repos
        # under the sibling ``repos/`` directory. ALLOWED_REPO_PARENTS is computed
        # at import time, before settings.yaml is applied, so refresh it in-place
        # to keep modules that imported the list by reference in sync.
        configured_repo_parents = [
            projects_root_path,
            (projects_root_path.parent / "repos").resolve(),
        ]
        existing = list(config_mod.ALLOWED_REPO_PARENTS)
        for parent in configured_repo_parents:
            if parent not in existing:
                existing.append(parent)
        config_mod.ALLOWED_REPO_PARENTS[:] = existing
        applied = True

    _apply_brain_gateway(data)

    return applied


# N3: provider → OpenAI-compatible brain-gateway endpoint + a default model.
# Validated live 2026-06-03 (response_format=json_object works on gpt-4o-mini AND
# an Anthropic-family model through an OpenAI-compat gateway). `bedrock` has no
# simple Bearer+URL shape (SigV4) → no brain gateway, polish stays off.
_BRAIN_GATEWAY_BY_PROVIDER: dict[str, tuple[str | None, str | None]] = {
    "openai": ("https://api.openai.com/v1", "gpt-4o-mini"),
    "anthropic": ("https://api.anthropic.com/v1", "claude-haiku-4-5-20251001"),
    "mac_gateway": (None, "tier-write"),  # base_url comes from the user's vault entry
    "bedrock": (None, None),
}


def _apply_brain_gateway(data: dict) -> None:
    """N3: wire the OSS BYOK key into the brain LLM gateway so a clean install's
    ``full`` reflection polishes without a manual env export.

    ENV ALWAYS WINS: a deployment that configures ``BRAIN_LLM_GATEWAY_BASE_URL``
    via env (managed prod — e.g. the Mac gateway) is left untouched. This helper
    runs ONLY in the OSS CLI / stdio-MCP entrypoints (the FastAPI server never
    calls ``apply_marvis_settings``), so it physically cannot reach a managed
    brain — the env guard is a second line of defence.

    Best-effort + fail-soft: any miss (no provider, unsupported provider, vault
    undecryptable, missing key) DISABLES polish (the deterministic NoOp floor)
    rather than letting the brain factory raise on a missing gateway key.
    """
    if os.environ.get("BRAIN_LLM_GATEWAY_BASE_URL"):
        return  # env-configured (managed prod) → never override

    from core.api.config import settings

    llm = data.get("llm") or {}
    provider = llm.get("provider")
    mapping = _BRAIN_GATEWAY_BY_PROVIDER.get(provider or "")
    if not mapping or mapping[1] is None:
        settings.brain_llm_polish_enabled = False  # skip / bedrock / unknown → NoOp
        return

    base_url, model = mapping
    key = None
    vault_base_url = None
    try:
        from core.wizard.byok_vault import load_vault

        vault_dir = os.environ.get("MARVIS_VAULT_DIR")
        vault = load_vault(Path(vault_dir).expanduser() if vault_dir else None)
        entry = (vault.get("providers") or {}).get(provider) or {}
        key = entry.get("api_key")
        vault_base_url = entry.get("base_url")
    except Exception:  # noqa: BLE001 — undecryptable / missing vault → polish off
        key = None

    if base_url is None:  # mac_gateway → the user-supplied base_url
        base_url = vault_base_url or llm.get("base_url")

    if not key or not base_url:
        settings.brain_llm_polish_enabled = False
        return

    from pydantic import SecretStr

    settings.brain_llm_gateway_base_url = base_url
    settings.brain_llm_gateway_api_key = SecretStr(key)
    settings.brain_llm_model = model
    settings.brain_llm_polish_enabled = True
