# v1.0.0 - 2026-04-14 - KG Fase 1c: shared YAML frontmatter parser (DRY helper)
"""Parse YAML frontmatter from markdown files.

Extracted from `scripts/validate-project-structure.py::parse_frontmatter` so
populator scripts (Fase 1c+) and validators share the same parsing rules
without duplicating the four edge cases (no frontmatter, malformed open/close,
non-mapping, YAML errors).

Returns a `(data, body)` tuple:
  - `data` is the parsed YAML mapping (dict) or `None` if the file has no
    valid frontmatter
  - `body` is the markdown content after the closing `---` separator, or
    `None` when no body could be extracted

Calling code is expected to log/skip on `data is None` — see
`scripts/populate_artifacts.py::populate_handoffs` for the negative-control
pattern (skip + structured log when `task_id` missing).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def parse_frontmatter(path: str | Path) -> tuple[dict[str, Any] | None, str | None]:
    """Extract YAML frontmatter from a markdown file.

    Args:
        path: file path (str or Path)

    Returns:
        `(data, body)` tuple. `data` is None when:
          - file cannot be read
          - file does not start with `---`
          - closing `---` is missing
          - YAML cannot be parsed
          - parsed YAML is not a dict (e.g. list or scalar)

        `body` is the raw markdown text after the closing `---`, or None if
        the parser couldn't reach that point.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeDecodeError):
        return None, None

    if not text.startswith("---"):
        return None, text

    parts = text.split("---", 2)
    if len(parts) < 3:
        # Malformed: missing closing ---
        return None, None

    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None, parts[2] if len(parts) > 2 else None

    if not isinstance(data, dict):
        return None, parts[2] if len(parts) > 2 else None

    return data, parts[2]
