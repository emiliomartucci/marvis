"""Dispatch uploaded ingest files through the existing ingest queue."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.api.services.ingest.watcher import PROJECTS_ROOT, enqueue_file


@dataclass(frozen=True)
class IngestProvenance:
    """Per-request governance carried into a single ingress item (M1 CAPTURE).

    Threaded through dispatch -> enqueue_file so the ingest_pending row records
    who/what authorized the intake. Owner-surface drops use the default
    (``source_kind='file_drop'``, no key). ``metadata`` is the JSON payload's
    free-form metadata; U3 persists it to ingest_pending.ingress_metadata and
    parse_pending reconciles it into structure_json.
    """

    source_kind: str = "file_drop"
    api_key_id: str | None = None
    source: str | None = None
    ingest_policy: str | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class DispatchDedup:
    """One file silently deduplicated against an existing pending row."""

    file_path: str
    existing_ingest_id: str


@dataclass(frozen=True)
class DispatchResult:
    queued_ids: set[str]
    # UX-6: enqueue saw an existing non-rejected row for the same
    # (sha256, project_slug). enqueue_file already wrote the audit row in
    # ingest_skipped (mig 103); this list only powers the synchronous
    # response payload of the upload-folder router.
    dedup_files: list[DispatchDedup] = field(default_factory=list)

    @property
    def queued_count(self) -> int:
        return len(self.queued_ids)


async def dispatch_files_batched(
    file_paths: list[Path],
    *,
    projects_root: Path = PROJECTS_ROOT,
    source_kind: str = "manual_upload",
    provenance: IngestProvenance | None = None,
) -> DispatchResult:
    """Dispatch saved files into the ingest queue.

    When ``provenance`` is given (API-key ingress), its source_kind and
    governance columns win; otherwise the legacy ``source_kind`` kwarg applies
    and the ingress columns stay NULL (owner-surface drops).
    """
    effective_source_kind = provenance.source_kind if provenance else source_kind
    api_key_id = provenance.api_key_id if provenance else None
    source = provenance.source if provenance else None
    ingest_policy = provenance.ingest_policy if provenance else None
    metadata = provenance.metadata if provenance else None
    queued_ids: set[str] = set()
    dedup_files: list[DispatchDedup] = []
    for path in file_paths:
        ingest_id, outcome = await enqueue_file(
            path,
            projects_root=projects_root,
            source_kind=effective_source_kind,
            api_key_id=api_key_id,
            source=source,
            ingest_policy=ingest_policy,
            metadata=metadata,
        )
        if ingest_id is None:
            continue
        if outcome == "dedup":
            dedup_files.append(
                DispatchDedup(file_path=str(path), existing_ingest_id=ingest_id)
            )
        else:
            queued_ids.add(ingest_id)
    return DispatchResult(queued_ids=queued_ids, dedup_files=dedup_files)
