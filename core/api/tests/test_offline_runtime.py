from __future__ import annotations

import socket
import sqlite3

import pytest

from core.api.services import embedding_internal, embedding_service
from core.api.services.kg.hybrid_search import hybrid_search
from core.api.tests._db_fixture import apply_migrations


@pytest.mark.asyncio
async def test_empty_model_cache_uses_keyword_search_without_network(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A provisioned OSS runtime stays useful when its model cache is empty."""

    cache_dir = tmp_path / "empty-hf-cache"
    cache_dir.mkdir()
    monkeypatch.setenv("HF_HOME", str(cache_dir))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    monkeypatch.setenv("EMBEDDING_MODE", "granite_local")
    monkeypatch.setenv("MARVIS_TELEMETRY_ENABLED", "false")
    monkeypatch.setattr(embedding_service, "_granite_client", None)
    monkeypatch.setattr(embedding_service, "_granite_run_semaphore", None)

    network_attempts: list[object] = []

    def deny_network(*args: object, **kwargs: object) -> None:
        network_attempts.append((args, kwargs))
        raise AssertionError("offline runtime attempted outbound network access")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)

    imported_modules: list[str] = []
    real_import_module = embedding_internal.importlib.import_module

    def guarded_import(name: str, *args: object, **kwargs: object):
        imported_modules.append(name)
        if name == "huggingface_hub":
            raise AssertionError("offline runtime attempted to import the Hub client")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(embedding_internal.importlib, "import_module", guarded_import)

    db_path = tmp_path / "offline.db"
    apply_migrations(str(db_path))
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO documents (
                file_path, project, workspace_id, doc_type, doc_title, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                str(tmp_path / "offline-proof.md"),
                "marvis",
                "ws_default",
                "file",
                "Offline fallback proof",
                "offline-fixture",
            ),
        )
        doc_id = int(cursor.lastrowid)
        conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
        conn.execute(
            """
            INSERT INTO documents_fts(rowid, doc_id, title, content)
            VALUES (?, ?, ?, ?)
            """,
            (
                doc_id,
                doc_id,
                "Offline fallback proof",
                "cobalt-orchid remains searchable without embeddings",
            ),
        )
        conn.commit()

    assert embedding_service.is_available() is False

    grouped, meta = await hybrid_search(
        "cobalt-orchid",
        "ws_default",
        str(db_path),
        str(tmp_path / "missing-vec0"),
        limit=5,
    )

    assert [hit["path"] for hit in grouped["file"]] == [
        str(tmp_path / "offline-proof.md")
    ]
    assert meta["semantic_available"] is False
    assert meta["semantic_reason"] == "model-not-loadable"
    assert meta["documents_fts_available"] is True
    assert "huggingface_hub" not in imported_modules
    assert network_attempts == []
