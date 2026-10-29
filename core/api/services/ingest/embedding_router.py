"""Embedding routing for Universal Ingestion E2.1."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import aiosqlite

from core.api.db import acquire_write_db
from core.api.services import embedding_service
from core.api.services.pii_redactor import redact

EmbedStatus = Literal["remote", "local", "skipped"]
ProjectType = Literal["work", "code", "system"]


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
    """Keep lexical text available; use only the configured semantic model."""
    if not extracted_text or len(extracted_text.strip()) < 20:
        return "skipped"
    fields = dict(
        workspace_id=workspace_id,
        slug=slug,
        target_path=target_path,
        document_type=document_type,
        title=title or target_path.stem,
        content=extracted_text,
    )
    await _persist_ingest_embedding(**fields, vector=None)
    external = embedding_service.uses_external_embedding()
    if external:
        allow_external = await _project_allows_external_embedding(slug)
        if not allow_external:
            if project_type == "work":
                return "skipped"  # intentional privacy policy, not a failed inference
            raise embedding_service.EmbeddingUnavailableError(
                "embedding_model_incompatible"
            )
    try:
        vector = await _embed_remote_text(redact(extracted_text))
    except (
        embedding_service.EmbeddingBackpressureError,
        embedding_service.EmbeddingInputTooLargeError,
    ):
        raise
    except Exception as exc:
        raise embedding_service.EmbeddingUnavailableError(
            "Configured embedding unavailable"
        ) from exc
    await _persist_ingest_embedding(**fields, vector=vector)
    return "remote" if external else "local"


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
    except Exception as exc:
        raise embedding_service.EmbeddingUnavailableError(
            "Embedding policy unavailable"
        ) from exc
    return bool(row and row["allow_external_embed"])


async def _embed_remote_text(text: str) -> list[float]:
    if not await embedding_service.is_available_async(input_type="document"):
        raise embedding_service.EmbeddingUnavailableError(
            "Configured embedding unavailable"
        )
    vectors = await embedding_service.embed_texts([text], input_type="document")
    return vectors[0]


async def _persist_ingest_embedding(
    *,
    workspace_id: str,
    slug: str,
    target_path: Path,
    document_type: str,
    title: str,
    content: str,
    vector: list[float] | None,
) -> None:
    async with acquire_write_db() as db:
        vec_ok = await _ensure_vec_documents(db)
        if vector is None:
            await embedding_service.persist_lexical_file(
                db,
                db,
                workspace_id=workspace_id,
                project=slug,
                file_path=str(target_path),
                title=title,
                doc_type=document_type,
                content=content,
            )
        else:
            if not vec_ok:
                raise embedding_service.EmbeddingUnavailableError(
                    "Vector storage unavailable"
                )
            if len(vector) != embedding_service.DIMENSIONS:
                raise embedding_service.EmbeddingUnavailableError(
                    "Embedding dimensions mismatch"
                )
            row = await (
                await db.execute(
                    "SELECT id FROM documents WHERE workspace_id = ? AND project = ? AND file_path = ?",
                    [workspace_id, slug, str(target_path)],
                )
            ).fetchone()
            if row is None:
                raise embedding_service.EmbeddingUnavailableError(
                    "Document index entry missing"
                )
            await db.execute("DELETE FROM vec_documents WHERE doc_id = ?", [row["id"]])
            await db.execute(
                "INSERT INTO vec_documents(doc_id, embedding) VALUES (?, ?)",
                [row["id"], embedding_service.serialize_f32(vector)],
            )
            await db.execute(
                "UPDATE documents SET content_hash = ? WHERE id = ?",
                [embedding_service.content_hash(content), row["id"]],
            )
            await embedding_service.refresh_documents_fts_row(
                db,
                doc_id=row["id"],
                title=title,
                content=content,
                force=True,
            )
        await db.commit()


async def _ensure_vec_documents(db: aiosqlite.Connection) -> bool:
    from core.api.db import ensure_vec_documents

    return await ensure_vec_documents(db)


async def load_file_embedding_policies(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    slug: str,
    project_root: Path,
) -> dict[Path, bool]:
    """Snapshot ingested destinations and opt-in once per project reindex batch."""
    import json

    try:
        rows = await (
            await db.execute(
                """SELECT file_path, target_folder, target_filename, classification_json
               FROM ingest_pending WHERE workspace_id = ? AND project_slug = ?""",
                [workspace_id, slug],
            )
        ).fetchall()
        if not rows:
            return {}
        if embedding_service.uses_external_embedding():
            policy = await (
                await db.execute(
                    "SELECT allow_external_embed FROM project_external_embedding_policy WHERE project_slug = ?",
                    [slug],
                )
            ).fetchone()
            allowed = bool(policy and policy[0])
        else:
            allowed = True
        root = project_root.resolve()
        destinations = {}
        for row in rows:
            destinations[Path(row["file_path"]).resolve()] = allowed
            classification = json.loads(row["classification_json"] or "{}")
            folder = row["target_folder"] or classification.get("target_folder")
            name = row["target_filename"] or classification.get("target_filename")
            if folder and name:
                destination = (root / folder / name).resolve()
                if destination.is_relative_to(root):
                    destinations[destination] = allowed
        return destinations
    except Exception as exc:
        raise embedding_service.EmbeddingUnavailableError(
            "Embedding policy unavailable"
        ) from exc
