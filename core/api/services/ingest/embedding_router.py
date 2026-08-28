"""Embedding routing for Universal Ingestion E2.1."""

from __future__ import annotations

import asyncio
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import aiosqlite

from core.api.config import settings
from core.api.db import acquire_write_db
from core.api.services import embedding_service
from core.api.services.pii_redactor import redact

logger = logging.getLogger(__name__)

EmbedStatus = Literal["remote", "nomic-local", "skipped"]
ProjectType = Literal["work", "code", "system"]
NOMIC_MODEL_PATH = Path(
    os.environ.get("NOMIC_MODEL_PATH", "/data/pir/models/nomic-embed-text-v1.5")
)
_LOCAL_MODEL_MISSING_LOGGED = False


async def embed_and_index(
    *,
    ingest_id: str,
    workspace_id: str = "ws_default",
    slug: str,
    target_path: Path,
    extracted_text: str | None,
    document_type: str = "file",
    title: str | None = None,
    project_type: ProjectType = "work",
) -> EmbedStatus:
    """Redact, embed, and index one ingested file.

    Work projects skip embeddings unless external embedding is explicitly
    enabled. Code/system projects use local Nomic by default and route to the
    remote backend only through migration 095's project opt-in table.
    """
    if not extracted_text or len(extracted_text.strip()) < 20:
        logger.info(
            "embedding backend: skipped ingest_id=%s reason=text-too-short",
            ingest_id,
        )
        return "skipped"

    allow_external = await _project_allows_external_embedding(slug)
    if project_type == "work" and not allow_external:
        logger.info(
            "embedding backend: skipped ingest_id=%s reason=work-no-opt-in",
            ingest_id,
        )
        return "skipped"

    redacted_text = redact(extracted_text)
    backend: Literal["remote", "nomic-local"]
    try:
        if allow_external:
            vector = await _embed_remote_text(redacted_text)
            backend = "remote"
        else:
            if not _local_embedding_model_available():
                _log_local_embedding_missing_once("local-model-missing")
                logger.info(
                    "embedding backend: skipped ingest_id=%s reason=local-model-missing",
                    ingest_id,
                )
                return "skipped"
            vector = await asyncio.to_thread(_embed_nomic_text_sync, redacted_text)
            backend = "nomic-local"
    except Exception:
        if allow_external:
            logger.warning(
                "Remote embedding failed; falling back to nomic-local", exc_info=True
            )
            if not _local_embedding_model_available():
                _log_local_embedding_missing_once("local-fallback-model-missing")
                logger.info(
                    "embedding backend: skipped ingest_id=%s reason=local-fallback-model-missing",
                    ingest_id,
                )
                return "skipped"
            try:
                vector = await asyncio.to_thread(_embed_nomic_text_sync, redacted_text)
                backend = "nomic-local"
            except Exception:
                logger.warning(
                    "Local embedding fallback failed; skipping index", exc_info=True
                )
                return "skipped"
        else:
            logger.warning("Local embedding unavailable; skipping index", exc_info=True)
            return "skipped"

    await _persist_ingest_embedding(
        workspace_id=workspace_id,
        slug=slug,
        target_path=target_path,
        document_type=document_type,
        title=title or target_path.stem,
        content=extracted_text,
        vector=_coerce_dimensions(vector),
    )
    logger.info(
        "embedding backend: %s ingest_id=%s project=%s", backend, ingest_id, slug
    )
    return backend


async def _project_allows_external_embedding(slug: str) -> bool:
    try:
        async with acquire_write_db() as db:
            async with db.execute(
                """
                SELECT allow_external_embed
                  FROM project_external_embedding_policy
                 WHERE project_slug = ?
                """,
                (slug,),
            ) as cursor:
                row = await cursor.fetchone()
    except Exception:
        logger.warning(
            "external embedding policy unavailable; defaulting to local", exc_info=True
        )
        return False
    return bool(row and row["allow_external_embed"])


async def _embed_remote_text(text: str) -> list[float]:
    if not embedding_service.is_available():
        embedding_service.init_embedding_client()
    if not embedding_service.is_available():
        raise RuntimeError("Remote embedding client not initialized")

    last_error: Exception | None = None
    for attempt, delay in enumerate((0.0, 0.4, 1.5, 4.0), start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            vectors = await embedding_service.embed_texts([text], input_type="document")
            return vectors[0]
        except Exception as exc:
            last_error = exc
            message = str(exc)
            if any(code in message for code in ("400", "401", "403", "422")):
                raise
            logger.warning("Remote embed attempt %d failed: %s", attempt, exc)
    assert last_error is not None
    raise last_error


def _embed_nomic_text_sync(text: str) -> list[float]:
    model = _nomic_model()
    try:
        vector = model.encode(
            text,
            normalize_embeddings=True,
            truncate_dim=embedding_service.DIMENSIONS,
        )
    except TypeError:
        vector = model.encode(text, normalize_embeddings=True)
    return vector.tolist()


def _local_embedding_model_available() -> bool:
    return NOMIC_MODEL_PATH.exists()


def _log_local_embedding_missing_once(reason: str) -> None:
    global _LOCAL_MODEL_MISSING_LOGGED
    if _LOCAL_MODEL_MISSING_LOGGED:
        return
    _LOCAL_MODEL_MISSING_LOGGED = True
    logger.warning(
        "embedding backend: local embedding disabled reason=%s path=%s",
        reason,
        NOMIC_MODEL_PATH,
    )


@lru_cache(maxsize=1)
def _nomic_model():
    from sentence_transformers import SentenceTransformer

    return SentenceTransformer(str(NOMIC_MODEL_PATH), trust_remote_code=True)


def _coerce_dimensions(vector: list[float]) -> list[float]:
    if len(vector) == embedding_service.DIMENSIONS:
        return vector
    if len(vector) > embedding_service.DIMENSIONS:
        return vector[: embedding_service.DIMENSIONS]
    padding = [0.0] * (embedding_service.DIMENSIONS - len(vector))
    return [*vector, *padding]


async def _persist_ingest_embedding(
    *,
    workspace_id: str,
    slug: str,
    target_path: Path,
    document_type: str,
    title: str,
    content: str,
    vector: list[float],
) -> None:
    file_path = str(target_path)
    async with acquire_write_db() as db:
        await db.execute(
            """INSERT INTO documents (file_path, project, workspace_id, doc_type, doc_title, content_hash)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(file_path) DO UPDATE SET
                 content_hash = excluded.content_hash,
                 project = excluded.project,
                 workspace_id = excluded.workspace_id,
                 doc_type = excluded.doc_type,
                 doc_title = excluded.doc_title
               WHERE documents.workspace_id = excluded.workspace_id""",
            [
                file_path,
                slug,
                workspace_id,
                document_type,
                title,
                embedding_service.content_hash(content),
            ],
        )
        async with db.execute(
            "SELECT id FROM documents WHERE workspace_id = ? AND file_path = ?",
            [workspace_id, file_path],
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            raise RuntimeError(f"document upsert failed for {file_path}")

        if await _ensure_vec_documents(db):
            await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [row["id"]])
            await db.execute(
                "INSERT INTO vec_documents (doc_id, embedding) VALUES (?, ?)",
                [row["id"], embedding_service.serialize_f32(vector)],
            )
        # B-fix (MARVIS_FTS_BODIES): repair the trigger-degraded FTS row.
        await embedding_service.refresh_documents_fts_row(
            db, doc_id=row["id"], title=title, content=content
        )
        await db.commit()


async def _ensure_vec_documents(db: aiosqlite.Connection) -> bool:
    vec_path = Path(settings.vec0_path)
    vec_so = vec_path.with_suffix(".so") if not vec_path.suffix else vec_path
    if not vec_so.exists():
        logger.warning(
            "sqlite-vec extension missing at %s; document metadata stored without vector",
            vec_so,
        )
        return False
    try:
        await db._execute(db._conn.enable_load_extension, True)
        await db.execute("SELECT load_extension(?)", [str(vec_path)])
        await db.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_documents USING vec0(
                doc_id INTEGER PRIMARY KEY,
                embedding float[512]
            )
            """
        )
        return True
    except Exception:
        logger.warning(
            "sqlite-vec setup failed; document metadata stored without vector",
            exc_info=True,
        )
        return False
