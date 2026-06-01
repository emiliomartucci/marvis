"""Approve-and-insert saga for phase-1 file ingestion."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Literal

from core.api.db import acquire_write_db
from core.api.paths import repo_root
from core.api.services.ingest.embedding_router import embed_and_index
from core.api.services.ingest.events import broadcast_ingest_changed

logger = logging.getLogger(__name__)

# Prod default (Docker): /data/projects exists, so the resolved value stays
# /data/projects there. Kept as a module-level attribute (not inlined) so it
# remains an explicit override point — tests monkeypatch it, and a future
# runtime hook could set it directly. _projects_root() treats any non-default
# value here as authoritative.
PROJECTS_ROOT = Path("/data/projects")
REPO_ROOT = repo_root(__file__)


def _projects_root() -> Path:
    """Resolve the projects root the same way the rest of the runtime does.

    Order (prod-safe by construction):
      1. The module-level ``PROJECTS_ROOT`` when it has been overridden from its
         ``/data/projects`` default (tests monkeypatch it; a runtime hook may
         set it). An explicit override always wins.
      2. ``$MARVIS_PROJECTS_ROOT`` (canonical override, see wizard/defaults.py).
         Prod does NOT set it, so prod is unaffected.
      3. The runtime-applied project dirs (``PROJECT_DIRS[0]``), which
         ``runtime_settings.apply_marvis_settings`` populates from
         ``~/.marvis/settings.yaml`` ``storage.projects_root`` — but ONLY when
         ``/data/projects`` does NOT exist, so prod (where it does) keeps using
         ``/data/projects`` rather than the DB-loaded ``~/workspace/projects``.
      4. ``/data/projects`` default.

    Resolved lazily per-call: the API process applies settings.yaml AFTER this
    module imports, so reading at import time would lock in the bare default.
    """
    default_root = Path("/data/projects")

    # 1. Explicit module-level override (tests / runtime hook).
    if PROJECTS_ROOT != default_root:
        return Path(PROJECTS_ROOT).expanduser().resolve()

    # 2. Canonical env override (OSS non-Docker).
    env_root = os.environ.get("MARVIS_PROJECTS_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    # 3. Runtime settings (settings.yaml → PROJECT_DIRS) on non-Docker installs.
    if not default_root.exists():
        try:
            from core.api.routers.projects import PROJECT_DIRS

            if PROJECT_DIRS:
                return Path(PROJECT_DIRS[0]).expanduser().resolve()
        except Exception:  # noqa: BLE001 — never let resolution crash the saga
            pass

    # 4. Prod Docker default.
    return default_root.resolve()
SUBPROCESS_TIMEOUT_SEC = 60

# Phase 1.5 E6: keep-ref so fire-and-forget background tasks don't get GC'd
# mid-flight (asyncio.create_task warning). Tasks self-discard on completion.
_PENDING_ENRICHMENT_TASKS: set[asyncio.Task[None]] = set()
_NODE_TYPE_ALLOWLIST = {
    "handoff",
    "solution",
    "learning",
    "audit",
    "analysis",
    "research",
    "guide",
    "plan",
    "brainstorm",
    "file",
    # Phase 1.5 E5-fix9: business document taxonomy.
    "policy",
    "contract",
    "transcript",
    "record",
    "report",
}
_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_.:-]+")
_COMMIT_SUBJECT_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_COMMIT_SUBJECT_MAX_FRAGMENT = 120
_TEXT_TARGET_SUFFIXES = {".md", ".txt"}
ProjectType = Literal["work", "code", "system"]


def _project_root(slug: str) -> Path:
    return (_projects_root() / slug).resolve()


def _load_project_entry(slug: str) -> tuple[ProjectType, Path | None]:
    """Resolve project type and repo path from project.yaml via the project index.

    On metadata drift we fail closed as ``work``: no git commit and no external
    embedding unless the separate DB policy says otherwise.
    """
    yaml_path = _project_root(slug) / "project.yaml"
    if yaml_path.exists():
        try:
            import yaml
            from core.api.config import ALLOWED_REPO_PARENTS

            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning("project.yaml is not a mapping for slug=%s", slug)
                return "work", None
            raw_type = data.get("type", "work")
            project_type: ProjectType = (
                raw_type if raw_type in ("work", "code", "system") else "work"
            )
            raw_repo = data.get("repo_path")
            repo_path = Path(raw_repo).resolve() if raw_repo else None
            if repo_path and not any(
                repo_path.is_relative_to(parent) for parent in ALLOWED_REPO_PARENTS
            ):
                logger.warning("repo_path containment violation for slug=%s: %s", slug, repo_path)
                repo_path = None
            return project_type, repo_path
        except Exception:
            logger.warning(
                "project.yaml parse failed for slug=%s; defaulting to work",
                slug,
                exc_info=True,
            )
            return "work", None

    try:
        from core.api.routers.projects import _find_project_entry

        entry = _find_project_entry(slug)
    except Exception:
        logger.warning("project index lookup failed for slug=%s", slug, exc_info=True)
        return "work", None

    if entry is None:
        logger.warning("project index missing for slug=%s, defaulting to work", slug)
        return "work", None
    if entry.project_type not in ("work", "code", "system"):
        logger.warning(
            "project type unknown for slug=%s type=%r, defaulting to work",
            slug,
            entry.project_type,
        )
        return "work", None
    return entry.project_type, entry.repo_path


def _safe_relative_path(value: str) -> Path:
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError(f"Unsafe target path: {value}")
    return rel


def _unique_target_path(project_root: Path, target_folder: str, target_filename: str, sha: str) -> Path:
    folder = _safe_relative_path(target_folder)
    filename = _safe_relative_path(target_filename)
    if len(filename.parts) != 1:
        raise ValueError("target_filename must be a basename")
    target_dir = (project_root / folder).resolve()
    if not target_dir.is_relative_to(project_root):
        raise ValueError("target_folder escapes project root")
    target_dir.mkdir(parents=True, exist_ok=True)

    candidate = (target_dir / filename.name).resolve()
    if not candidate.is_relative_to(project_root):
        raise ValueError("target path escapes project root")
    if not candidate.exists():
        return candidate

    stem = candidate.stem
    suffix = candidate.suffix or ".md"
    return target_dir / f"{stem}-{sha[:8]}{suffix}"


def _sidecar_path(path: Path) -> Path:
    return path.with_suffix(".metadata.json")


def _move_sidecar_if_present(source_path: Path, target_path: Path, sha: str) -> None:
    sidecar = _sidecar_path(source_path)
    if not sidecar.exists():
        return

    target_sidecar = _sidecar_path(target_path)
    if target_sidecar.exists():
        target_sidecar = target_path.with_name(
            f"{target_path.stem}-{sha[:8]}.metadata.json"
        )
    sidecar.replace(target_sidecar)


def _frontmatter_value(value: Any) -> str:
    if isinstance(value, list):
        items = [str(item).strip() for item in value if str(item).strip()]
        return json.dumps(items, ensure_ascii=False)
    return json.dumps(str(value), ensure_ascii=False)


def _classification_frontmatter(classification: dict[str, Any]) -> str:
    lines: list[str] = []
    for key in ("type", "title", "tags"):
        value = classification.get(key)
        if value in (None, "", []):
            continue
        lines.append(f"{key}: {_frontmatter_value(value)}")
    if not lines:
        return ""
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def _looks_like_frontmatter(text: str) -> bool:
    return text.startswith("---\n") or text.startswith("---\r\n")


def _text_artifact_body(extracted_text: str, classification: dict[str, Any]) -> str:
    body = extracted_text.lstrip("\ufeff")
    if _looks_like_frontmatter(body):
        return body
    return f"{_classification_frontmatter(classification)}{body}"


def _should_materialize_extracted_text(
    *,
    source_path: Path,
    target_path: Path,
    extracted_text: str,
) -> bool:
    if not extracted_text.strip():
        return False
    if target_path.suffix.lower() not in _TEXT_TARGET_SUFFIXES:
        return False
    return source_path.suffix.lower() != target_path.suffix.lower()


def _write_text_atomic(target_path: Path, text: str) -> None:
    tmp_path = target_path.with_name(f".{target_path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(target_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _materialize_target_file(
    *,
    source_path: Path,
    target_path: Path,
    row,
    classification: dict[str, Any],
) -> None:
    """Create the target artifact without confusing source bytes and parser output."""
    extracted_text = str(row["extracted_text"] or "")
    if _should_materialize_extracted_text(
        source_path=source_path,
        target_path=target_path,
        extracted_text=extracted_text,
    ):
        _write_text_atomic(
            target_path,
            _text_artifact_body(extracted_text, classification),
        )
        _move_sidecar_if_present(source_path, target_path, row["sha256"])
        source_path.unlink()
        return

    source_path.replace(target_path)
    _move_sidecar_if_present(source_path, target_path, row["sha256"])


async def _run_populator(module: str, target_path: Path) -> tuple[bool, str]:
    cmd = [sys.executable, "-m", module, "--incremental", str(target_path)]
    env = {**os.environ, "KG_HOOK_DISABLED": "1"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(REPO_ROOT),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return False, str(exc)

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=SUBPROCESS_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        return False, f"{module} timed out"

    stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
    stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    if proc.returncode != 0:
        return False, stderr[:500] or stdout[:500] or f"{module} exited {proc.returncode}"
    return True, stdout[:500]


def _commit_subject_fragment(value: str, fallback: str) -> str:
    cleaned = _COMMIT_SUBJECT_CONTROL_RE.sub(" ", str(value))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return fallback
    return cleaned[:_COMMIT_SUBJECT_MAX_FRAGMENT]


def _commit_ingested_file(
    *,
    slug: str,
    repo_path: Path | None,
    target_path: Path,
    target_filename: str,
) -> bool:
    if repo_path is None:
        logger.info("ingest git commit skipped: project=%s has no repo_path", slug)
        return False
    repo = repo_path.resolve()
    if not (repo / ".git").exists():
        logger.info("ingest git commit skipped: repo_path is not a git repo: %s", repo)
        return False
    if not target_path.is_relative_to(repo):
        logger.info("ingest git commit skipped: target outside repo: %s", target_path)
        return False

    rel_target = target_path.relative_to(repo)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "ingest-bot",
        "GIT_AUTHOR_EMAIL": "ingest@marvisx",
        "GIT_COMMITTER_NAME": "ingest-bot",
        "GIT_COMMITTER_EMAIL": "ingest@marvisx",
    }
    subprocess.run(
        ["git", "add", "--", str(rel_target)],
        cwd=str(repo),
        env=env,
        check=True,
        timeout=10,
    )
    result = subprocess.run(
        [
            "git",
            "commit",
            "--only",
            "-m",
            "feat(ingest): "
            f"{_commit_subject_fragment(slug, 'project')} "
            f"{_commit_subject_fragment(target_filename, 'file')}",
            "--",
            str(rel_target),
        ],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip()[:500] or "git commit failed")
    logger.info("ingest git commit created: project=%s target=%s", slug, rel_target)
    return True


async def _set_saga_error(ingest_id: str, project_slug: str, message: str) -> None:
    async with acquire_write_db() as db:
        await db.execute(
            """
            UPDATE ingest_pending
               SET status = 'parse_error',
                   error_message = ?,
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (message[:1000], ingest_id),
        )
        await db.commit()
    await broadcast_ingest_changed(
        "saga_error",
        ingest_id=ingest_id,
        project_slug=project_slug,
        status="parse_error",
    )


async def compensate_step(ingest_id: str, step_failed: str) -> None:
    """Best-effort compensation for partially written ingest side effects."""
    pattern = f'%"ingest_id":"{ingest_id}"%'
    spaced_pattern = f'%"ingest_id": "{ingest_id}"%'
    async with acquire_write_db() as db:
        if step_failed in {"populate", "kg_edges", "ensure_kg_edge"}:
            await db.execute(
                "DELETE FROM kg_edge_activity WHERE metadata_json LIKE ? OR metadata_json LIKE ?",
                (pattern, spaced_pattern),
            )
            await db.execute(
                "DELETE FROM graph_edges WHERE metadata LIKE ? OR metadata LIKE ?",
                (pattern, spaced_pattern),
            )
            await db.execute(
                "DELETE FROM graph_nodes WHERE metadata LIKE ? OR metadata LIKE ?",
                (pattern, spaced_pattern),
            )
        elif step_failed == "voyage":
            from core.api.services.ingest.retry_voyage import enqueue_voyage_removal

            await enqueue_voyage_removal(ingest_id)
        await db.execute(
            """
            UPDATE ingest_pending
               SET status = 'parse_error',
                   error_message = ?,
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (f"compensated failed ingest step: {step_failed}", ingest_id),
        )
        await db.commit()


def _fallback_node_id(document_type: str, slug: str, target_path: Path) -> str:
    digest = hashlib.sha1(str(target_path).encode("utf-8")).hexdigest()[:12]
    safe_slug = _SAFE_ID_RE.sub("-", slug)
    return f"{document_type}:artifact:{safe_slug}-{digest}"


async def _ensure_kg_edge(
    *,
    ingest_id: str,
    project_slug: str,
    target_path: Path,
    document_type: str,
    classification: dict[str, Any],
    populator_warnings: list[str],
) -> str | None:
    """Returns the artifact node id (for downstream Phase 1.5 E6 KG enrichment trigger)."""
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    project_node_id = f"project:artifact:{project_slug}"
    metadata_json = json.dumps(
        {
            "ingest_id": ingest_id,
            "target_path": str(target_path),
            "classification": classification,
            "populator_warnings": populator_warnings,
        },
        ensure_ascii=False,
    )
    projects_root = _projects_root()
    candidate_paths = [
        str(target_path),
        str(target_path.relative_to(projects_root)),
    ]
    if project_slug == "marvisx":
        candidate_paths.append(f"projects/{target_path.relative_to(projects_root)}")

    async with acquire_write_db() as db:
        await db.execute(
            """
            INSERT OR IGNORE INTO graph_nodes
                (id, type, name, qualified_name, file_path, metadata, project_id,
                 created_at, updated_at, last_seen_at)
            VALUES (?, 'project', ?, ?, NULL, '{}', ?, datetime('now'), datetime('now'), ?)
            """,
            (project_node_id, project_slug, f"project.{project_slug}", project_slug, now_iso),
        )

        placeholders = ",".join("?" for _ in candidate_paths)
        async with db.execute(
            f"""
            SELECT id, type
              FROM graph_nodes
             WHERE project_id = ?
               AND file_path IN ({placeholders})
               AND deprecated_at IS NULL
             ORDER BY
               CASE
                 WHEN id LIKE 'xlsx:artifact:%' THEN 0
                 WHEN type != 'file' THEN 1
                 ELSE 2
               END
             LIMIT 1
            """,
            (project_slug, *candidate_paths),
        ) as cursor:
            node = await cursor.fetchone()

        if node is None:
            node_type = document_type if document_type in _NODE_TYPE_ALLOWLIST else "file"
            artifact_node_id = _fallback_node_id(node_type, project_slug, target_path)
            await db.execute(
                """
                INSERT OR IGNORE INTO graph_nodes
                    (id, type, name, qualified_name, file_path, metadata, project_id,
                     created_at, updated_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?)
                """,
                (
                    artifact_node_id,
                    node_type,
                    target_path.name,
                    f"{node_type}.{target_path.stem}",
                    str(target_path),
                    metadata_json,
                    project_slug,
                    now_iso,
                ),
            )
        else:
            artifact_node_id = node["id"]

        await db.execute(
            """
            INSERT INTO graph_edges
                (source_id, target_id, relation, confidence, source, metadata,
                 source_file, created_at, first_seen_at, last_seen_at, project_id,
                 weight, last_touched_at)
            VALUES (?, ?, 'contains', 1.0, 'db', ?, ?, datetime('now'), ?, ?, ?, 1.0, ?)
            ON CONFLICT(source_id, target_id, relation) DO UPDATE SET
                metadata = excluded.metadata,
                last_seen_at = excluded.last_seen_at,
                weight = 1.0,
                last_touched_at = excluded.last_touched_at
            """,
            (
                project_node_id,
                artifact_node_id,
                metadata_json,
                str(target_path),
                now_iso,
                now_iso,
                project_slug,
                now_iso,
            ),
        )
        await db.execute(
            """
            INSERT INTO kg_edge_activity
                (edge_source_id, edge_target_id, edge_relation, event_type,
                 source_agent, metadata_json)
            VALUES (?, ?, 'contains', 'ingest_insert', 'pir-ingest', ?)
            """,
            (project_node_id, artifact_node_id, metadata_json),
        )
        await db.commit()
    return artifact_node_id


async def _load_ingest_row(ingest_id: str):
    async with acquire_write_db() as db:
        async with db.execute(
            "SELECT * FROM ingest_pending WHERE id = ?", (ingest_id,)
        ) as cursor:
            return await cursor.fetchone()


def _target_path_from_inserted_row(row) -> Path:
    project_root = _project_root(row["project_slug"])
    classification = json.loads(row["classification_json"] or "{}")
    target_folder = row["target_folder"] or classification.get("target_folder")
    target_filename = row["target_filename"] or classification.get("target_filename")
    if not target_folder or not target_filename:
        raise ValueError("missing target_folder/target_filename")
    folder = _safe_relative_path(str(target_folder))
    filename = _safe_relative_path(str(target_filename))
    if len(filename.parts) != 1:
        raise ValueError("target_filename must be a basename")
    target_path = (project_root / folder / filename.name).resolve()
    if not target_path.is_relative_to(project_root):
        raise ValueError("target path escapes project root")
    return target_path


async def _finish_inserted_row(row) -> None:
    project_slug = row["project_slug"]
    target_path = _target_path_from_inserted_row(row)
    if not target_path.exists():
        raise FileNotFoundError(str(target_path))

    project_type, _repo_path = _load_project_entry(project_slug)
    classification = json.loads(row["classification_json"] or "{}")
    populator_warnings: list[str] = []
    for module in ("core.scripts.populate_artifacts", "core.scripts.populate_cross_project"):
        ok, message = await _run_populator(module, target_path)
        if not ok:
            populator_warnings.append(f"{module}: {message}")

    await _ensure_kg_edge(
        ingest_id=row["id"],
        project_slug=project_slug,
        target_path=target_path,
        document_type=str(classification.get("type") or "file"),
        classification=classification,
        populator_warnings=populator_warnings,
    )
    embed_status = await embed_and_index(
        ingest_id=row["id"],
        slug=project_slug,
        target_path=target_path,
        extracted_text=row["extracted_text"],
        document_type=str(classification.get("type") or "file"),
        title=str(classification.get("title") or target_path.stem),
        project_type=project_type,
    )
    logger.info("saga recovery embed: ingest_id=%s status=%s", row["id"], embed_status)

    error_message = "; ".join(populator_warnings)[:1000] if populator_warnings else None
    async with acquire_write_db() as db:
        await db.execute(
            """
            UPDATE ingest_pending
               SET status = 'done',
                   error_message = ?,
                   updated_at = datetime('now')
             WHERE id = ?
               AND status = 'inserted'
            """,
            (error_message, row["id"]),
        )
        await db.commit()
    await broadcast_ingest_changed(
        "done",
        ingest_id=row["id"],
        project_slug=project_slug,
        status="done",
    )


async def resume_saga_from_status(
    ingest_id: str,
    prev_status: str,
    timeout: int | None = None,
) -> None:
    """Idempotently resume a stale ingest saga row from the live schema states."""
    step_timeout = timeout or int(os.environ.get("INGEST_STEP_TIMEOUT_SECONDS", "60"))
    if prev_status == "approved":
        await asyncio.wait_for(execute_saga(ingest_id), timeout=step_timeout)
        return
    if prev_status == "inserted":
        row = await _load_ingest_row(ingest_id)
        if row is None or row["status"] != "inserted":
            return
        await asyncio.wait_for(_finish_inserted_row(row), timeout=step_timeout)
        return
    raise ValueError(f"unsupported ingest recovery status: {prev_status}")


async def execute_saga(ingest_id: str) -> None:
    row = None
    async with acquire_write_db() as db:
        async with db.execute(
            "SELECT * FROM ingest_pending WHERE id = ?", (ingest_id,)
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row["status"] != "approved":
            return
        # U3 defense-in-depth: an api_ingress row must never AUTO-insert (no human
        # in the loop) unless its key policy is 'trusted'. This backstop guards
        # AUTO-insert only — explicit human triage approval (basis "approve:<user>")
        # is an insertion authorization and is always honored, so the primary
        # 'open' flow (triage -> human approve -> inserted) works. The parse-time
        # policy gate already enforces this for the auto path; default-deny here.
        basis = row["triage_decision_id"] or ""
        if (
            row["source_kind"] == "api_ingress"
            and row["ingest_policy"] != "trusted"
            and not basis.startswith("approve:")
        ):
            logger.error(
                "saga refused api_ingress auto-insert for row %s: policy=%r is not "
                "'trusted' (basis=%r); open/unknown keys auto-insert only via human triage",
                ingest_id,
                row["ingest_policy"],
                basis,
            )
            return

    project_slug = row["project_slug"]
    try:
        project_root = _project_root(project_slug)
        project_type, repo_path = _load_project_entry(project_slug)
        logger.info(
            "saga branching: ingest_id=%s project=%s project_type=%s",
            ingest_id,
            project_slug,
            project_type,
        )
        source_path = Path(row["file_path"]).resolve()
        if not source_path.exists():
            raise FileNotFoundError(str(source_path))
        if not source_path.is_relative_to(project_root):
            raise ValueError("source path escapes project root")

        classification = json.loads(row["classification_json"] or "{}")
        target_folder = row["target_folder"] or classification.get("target_folder")
        target_filename = row["target_filename"] or classification.get("target_filename")
        if not target_folder or not target_filename:
            raise ValueError("missing target_folder/target_filename")

        target_path = _unique_target_path(
            project_root,
            str(target_folder),
            str(target_filename),
            row["sha256"],
        )
        _materialize_target_file(
            source_path=source_path,
            target_path=target_path,
            row=row,
            classification=classification,
        )

        commit_created = False
        if project_type in ("code", "system"):
            try:
                commit_created = _commit_ingested_file(
                    slug=project_slug,
                    repo_path=repo_path,
                    target_path=target_path,
                    target_filename=target_path.name,
                )
            except Exception as exc:
                logger.warning("ingest git commit blocked: %s", exc)
                await _set_saga_error(ingest_id, project_slug, f"workspace guard: {exc}")
                return

        async with acquire_write_db() as db:
            await db.execute(
                """
                UPDATE ingest_pending
                   SET status = 'inserted',
                       target_folder = ?,
                       target_filename = ?,
                       updated_at = datetime('now')
                 WHERE id = ?
                """,
                (
                    str(target_path.parent.relative_to(project_root)),
                    target_path.name,
                    ingest_id,
                ),
            )
            await db.commit()

        populator_warnings: list[str] = []
        if commit_created:
            await asyncio.sleep(1.5)
        else:
            for module in ("core.scripts.populate_artifacts", "core.scripts.populate_cross_project"):
                ok, message = await _run_populator(module, target_path)
                if not ok:
                    populator_warnings.append(f"{module}: {message}")

        artifact_node_id = await _ensure_kg_edge(
            ingest_id=ingest_id,
            project_slug=project_slug,
            target_path=target_path,
            document_type=str(classification.get("type") or "file"),
            classification=classification,
            populator_warnings=populator_warnings,
        )
        embed_status = await embed_and_index(
            ingest_id=ingest_id,
            slug=project_slug,
            target_path=target_path,
            extracted_text=row["extracted_text"],
            document_type=str(classification.get("type") or "file"),
            title=str(classification.get("title") or target_path.stem),
            project_type=project_type,
        )
        logger.info("saga embed: ingest_id=%s status=%s", ingest_id, embed_status)

        error_message = "; ".join(populator_warnings)[:1000] if populator_warnings else None
        async with acquire_write_db() as db:
            await db.execute(
                """
                UPDATE ingest_pending
                   SET status = 'done',
                       error_message = ?,
                       updated_at = datetime('now')
                 WHERE id = ?
                """,
                (error_message, ingest_id),
            )
            await db.commit()
        await broadcast_ingest_changed(
            "done",
            ingest_id=ingest_id,
            project_slug=project_slug,
            status="done",
        )
        # Phase 1.5 E6: KG edge enrichment background (LLM #2). Fire-and-forget,
        # never raises. Guarded by LLM_KG_ENRICHER_ENABLED env. Cron Phase 2 future
        # re-triggers nodes with kg_enriched_at IS NULL.
        if artifact_node_id and (
            (os.environ.get("LLM_KG_ENRICHER_ENABLED", "false") or "").strip().lower() == "true"
        ):
            from core.api.services.ingest.llm.kg_enricher import enrich_kg_for_node

            task = asyncio.create_task(enrich_kg_for_node(artifact_node_id))
            _PENDING_ENRICHMENT_TASKS.add(task)
            task.add_done_callback(_PENDING_ENRICHMENT_TASKS.discard)
    except Exception as exc:
        logger.exception("ingest saga failed: id=%s", ingest_id)
        async with acquire_write_db() as db:
            await db.execute(
                """
                UPDATE ingest_pending
                   SET status = 'parse_error',
                       error_message = ?,
                       updated_at = datetime('now')
                 WHERE id = ?
                """,
                (str(exc)[:1000], ingest_id),
            )
            await db.commit()
        await broadcast_ingest_changed(
            "saga_error",
            ingest_id=ingest_id,
            project_slug=project_slug,
            status="parse_error",
        )
