from pathlib import Path

from core.api.routers.projects import _parse_handoffs


def test_parse_handoffs_accepts_alphanumeric_session(tmp_path: Path) -> None:
    project_dir = tmp_path / "marvisx"
    memory_dir = project_dir / "memory"
    memory_dir.mkdir(parents=True)

    handoff = memory_dir / "handoff-2026-04-06-test.md"
    handoff.write_text(
        """---
session: "116c"
date: 2026-04-06
branch: null
tags: [console, projects]
---

## Summary

Investigated project detail crash.
""",
        encoding="utf-8",
    )

    entries = _parse_handoffs(project_dir)

    assert len(entries) == 1
    assert entries[0].session == "116c"
    assert entries[0].date == "2026-04-06"
