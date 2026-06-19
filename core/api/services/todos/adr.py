# v1.0.0 - 2026-06-13 - Decidi gate ADR writer (gh #25)
"""Persist an Architecture Decision Record when a ``decidi`` todo lands.

When the unified queue confirms a ``decidi`` todo (``in_revisione`` →
``deciso``), the legacy code path only flipped the status — no durable
artefact was produced. The README "Funzionalità" §C contract says every
gate must produce an artefact + audit; this module covers the artefact
side. The audit row is written by the caller (``use_cases.todos``).

The writer is intentionally pure: it takes the project slug + payload +
decisore + timestamp, resolves the on-disk root via
``data_project_dir()`` (gh #17), and writes a markdown ADR with YAML
frontmatter under ``<root>/<project_slug>/docs/decisions/``. When the
todo has no project (``project_slug is None``) the function returns
``None`` so the caller can log an audit-only row.

Supersede chain: ADRs in the same project sharing the same ``scope`` are
treated as the previous decision on that scope. The new ADR records
their filenames in ``supersedes``; each previous ADR gets a
``superseded_by`` frontmatter pointer back to the new file. Reading is
frontmatter-only via ``_frontmatter.parse_frontmatter`` to avoid loading
huge bodies.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import yaml

from core.scripts._frontmatter import parse_frontmatter

logger = logging.getLogger(__name__)

_SLUG_MAX = 64
_DEFAULT_SCOPE = "project"


def slugify(text: str | None) -> str:
    """Lossy ASCII kebab-case slug. Empty input collapses to ``decisione``."""
    if not text:
        return "decisione"
    normalised = unicodedata.normalize("NFKD", str(text))
    ascii_only = normalised.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    if not slug:
        return "decisione"
    return slug[:_SLUG_MAX].rstrip("-") or "decisione"


def _today_iso(now: datetime) -> str:
    return now.date().isoformat()


def _unique_path(directory: Path, slug: str, date_str: str) -> Path:
    base = f"{date_str}-{slug}.md"
    candidate = directory / base
    if not candidate.exists():
        return candidate
    n = 2
    while True:
        candidate = directory / f"{date_str}-{slug}-{n}.md"
        if not candidate.exists():
            return candidate
        n += 1


def _dump_frontmatter(data: dict[str, Any]) -> str:
    return yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).rstrip("\n")


def _options_list(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if item is not None and str(item).strip()]
    return []


def _supersede_candidates(
    directory: Path, scope: str, new_filename: str
) -> list[Path]:
    """Return ADRs in ``directory`` with matching ``scope`` and no
    ``superseded_by``. Skips the new file itself."""
    out: list[Path] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("*.md")):
        if path.name == new_filename:
            continue
        data, _body = parse_frontmatter(path)
        if not data:
            continue
        if data.get("superseded_by"):
            continue
        if str(data.get("scope") or _DEFAULT_SCOPE) != scope:
            continue
        out.append(path)
    return out


def _mark_superseded(path: Path, new_filename: str) -> bool:
    """Add ``superseded_by: <new_filename>`` to ``path``'s frontmatter.

    Returns True on a successful rewrite, False if the file has no
    parseable frontmatter (left untouched). I/O errors propagate.
    """
    data, body = parse_frontmatter(path)
    if data is None:
        return False
    data["superseded_by"] = new_filename
    new_text = "---\n" + _dump_frontmatter(data) + "\n---\n" + (body or "")
    path.write_text(new_text, encoding="utf-8")
    return True


def _body(
    *,
    contesto: str,
    opzioni: Iterable[str],
    scelta: str,
    rationale: str,
) -> str:
    lines: list[str] = [f"# {contesto.strip() or 'Decisione'}", ""]
    lines.append("## Opzioni considerate")
    opzioni_list = list(opzioni)
    if opzioni_list:
        lines.extend(f"- {opt}" for opt in opzioni_list)
    else:
        lines.append("- _Nessuna opzione registrata._")
    lines.append("")
    lines.append("## Decisione")
    lines.append(scelta.strip() if scelta.strip() else "_Non specificata._")
    lines.append("")
    lines.append("## Rationale")
    lines.append(rationale.strip() if rationale.strip() else "_Non specificato._")
    lines.append("")
    return "\n".join(lines)


def write_adr(
    *,
    project_slug: str | None,
    payload: dict[str, Any] | None,
    decisore: str,
    now: datetime,
) -> Path | None:
    """Persist a markdown ADR for a confirmed ``decidi`` todo.

    Returns the absolute file path on success, ``None`` when no project
    is associated with the todo (audit-only path) or when the project
    root cannot be resolved on disk. Raises on filesystem errors so the
    caller can audit a structured failure.
    """
    if not project_slug:
        return None

    payload = payload or {}
    contesto = str(payload.get("domanda") or "").strip()
    if not contesto:
        contesto = "Decisione"
    scope = str(payload.get("scope") or _DEFAULT_SCOPE).strip() or _DEFAULT_SCOPE
    scelta = str(payload.get("scelta") or "").strip()
    rationale = str(payload.get("rationale") or "").strip()
    opzioni = _options_list(payload.get("opzioni"))

    from core.api.use_cases.projects import data_project_dir

    project_dir = data_project_dir() / project_slug
    decisions_dir = project_dir / "docs" / "decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)

    slug = slugify(contesto)
    date_str = _today_iso(now)
    target = _unique_path(decisions_dir, slug, date_str)

    predecessors = _supersede_candidates(decisions_dir, scope, target.name)
    supersedes_filenames = [p.name for p in predecessors]

    frontmatter: dict[str, Any] = {
        "date": now.isoformat(),
        "decisore": decisore,
        "contesto": contesto,
        "opzioni": opzioni,
        "scelta": scelta,
        "rationale": rationale,
        "scope": scope,
        "project": project_slug,
        "supersedes": supersedes_filenames,
    }

    body = _body(
        contesto=contesto,
        opzioni=opzioni,
        scelta=scelta,
        rationale=rationale,
    )
    document = "---\n" + _dump_frontmatter(frontmatter) + "\n---\n\n" + body
    target.write_text(document, encoding="utf-8")

    for predecessor in predecessors:
        try:
            _mark_superseded(predecessor, target.name)
        except OSError:
            logger.warning(
                "adr.supersede_failed predecessor=%s new=%s",
                predecessor,
                target.name,
                exc_info=True,
            )

    return target


__all__ = ["write_adr", "slugify"]
