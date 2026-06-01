# v1.2.0 - 2026-03-10 - Add authentication dependency to GET /tags endpoint
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends

from core.api.paths import repo_path
from core.api.security import require_any_auth

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/tags", tags=["tags"])

# tags.yaml è nella workspace root, NON nel repo MarvisX
# In produzione il servizio gira da /data/pir/core/api/ — path relativo non funziona
# Prova percorsi in ordine: assoluto server → relativo repo (dev locale)
_WORKSPACE_ROOT = Path(os.environ.get("MARVIS_WORKSPACE_ROOT", str(Path.home() / "workspace")))
_CANDIDATE_PATHS = [
    _WORKSPACE_ROOT / "tags.yaml",                        # server prod
    repo_path(__file__, "tags.yaml"),                     # runtime repo root
]
_TAGS_YAML = next((p for p in _CANDIDATE_PATHS if p.exists()), None)


def _load_tags() -> list[dict]:
    """Load tags from workspace tags.yaml. Returns empty list on error."""
    if _TAGS_YAML is None:
        logger.warning("tags.yaml not found in any candidate path: %s", _CANDIDATE_PATHS)
        return []
    try:
        data = yaml.safe_load(_TAGS_YAML.read_text())
        return data.get("tags", []) if data else []
    except Exception as exc:
        logger.warning("Could not load tags.yaml from %s: %s", _TAGS_YAML, exc)
        return []


@router.get("")
def list_tags(_=Depends(require_any_auth)) -> list[dict]:
    """Return canonical tag list from tags.yaml."""
    return _load_tags()
