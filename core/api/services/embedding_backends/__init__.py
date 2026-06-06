# v1.0.0 - 2026-06-02 - Backend-agnostic remote-embedding discovery seam.
"""Pluggable embedding backends.

The only backend that ships in every build is the in-process local engine
(``embedding_internal.GraniteEmbeddingClient``). An optional *remote* backend may
be dropped in as ``remote_backend.py`` in this package; it is provider-specific
and is NOT shipped in the public OSS mirror. ``embedding_service`` discovers it
through :func:`load_remote_backend` — a try-import that returns the module when
present or ``None`` when absent, exactly the way the API tolerates other
optional, deploy-only modules.

This file is backend-agnostic on purpose: it names no provider and reads no
provider-specific env var, so it is safe to ship publicly.
"""
from __future__ import annotations

from types import ModuleType


def load_remote_backend() -> ModuleType | None:
    """Return the optional remote embedding backend module, or None if absent.

    A clean install ships no remote backend, so the import fails and we return
    None — callers then use the local engine. Deploys that include the remote
    backend module get it back here and route through it when it is configured.
    """
    try:
        from core.api.services.embedding_backends import remote_backend
    except ImportError:
        return None
    return remote_backend
