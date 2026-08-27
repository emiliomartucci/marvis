# Project → program resolver. Reads programs.yaml at repo root.
# Plan: sub-01 §9. Single resolver for Digest/Journal/Drift/Memory-Ops/Learn.
from __future__ import annotations

import logging
from threading import Lock
from typing import Any

import yaml

from core.api.paths import repo_path

logger = logging.getLogger(__name__)

_PROGRAMS_YAML_PATH = repo_path(__file__, "programs.yaml")
_cache_lock = Lock()
_cached_map: dict[str, str] | None = None
_cached_mtime: float | None = None


def _load_programs_map() -> dict[str, str]:
    """Return {project_slug: program_key}. Empty dict on missing/invalid yaml.

    Cached by file mtime — re-parses if the file changes on disk.
    """
    global _cached_map, _cached_mtime

    path = _PROGRAMS_YAML_PATH
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        with _cache_lock:
            _cached_map = {}
            _cached_mtime = None
        return {}

    with _cache_lock:
        if _cached_map is not None and _cached_mtime == mtime:
            return _cached_map

    try:
        with path.open("r", encoding="utf-8") as fh:
            doc: dict[str, Any] = yaml.safe_load(fh) or {}
    except (OSError, yaml.YAMLError):
        logger.exception("brain.scope: failed to parse %s", path)
        with _cache_lock:
            _cached_map = {}
            _cached_mtime = mtime
        return {}

    mapping: dict[str, str] = {}
    if isinstance(doc, dict):
        for program_key, payload in doc.items():
            if not isinstance(program_key, str) or not isinstance(payload, dict):
                continue
            for slug in payload.get("projects") or []:
                if isinstance(slug, str):
                    mapping[slug] = program_key

    with _cache_lock:
        _cached_map = mapping
        _cached_mtime = mtime
    return mapping


def resolve_program(project_slug: str | None) -> str | None:
    """Return program_key for the given slug, or None if standalone."""
    if not project_slug:
        return None
    return _load_programs_map().get(project_slug)


def reset_cache() -> None:
    """Reset the cache. Test-only helper."""
    global _cached_map, _cached_mtime
    with _cache_lock:
        _cached_map = None
        _cached_mtime = None
