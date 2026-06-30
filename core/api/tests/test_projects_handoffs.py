import os
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


def test_parse_handoffs_prefers_dated_files_over_newer_undated_files(tmp_path: Path) -> None:
    project_dir = tmp_path / "marvisx"
    memory_dir = project_dir / "memory"
    memory_dir.mkdir(parents=True)

    latest = memory_dir / "handoff-2026-06-27-real-work.md"
    latest.write_text(
        """---
date: 2026-06-27
---

## Summary

Real hosted migration handoff.
""",
        encoding="utf-8",
    )
    older = memory_dir / "handoff-2026-06-26-previous-work.md"
    older.write_text(
        """---
date: 2026-06-26
---

## Summary

Previous hosted migration handoff.
""",
        encoding="utf-8",
    )
    undated = memory_dir / "handoff-retest2-kg-edge-20260626T091721Z.md"
    undated.write_text(
        """## Summary

Fixture handoff without a real date.
""",
        encoding="utf-8",
    )
    os.utime(older, (1_000, 1_000))
    os.utime(latest, (2_000, 2_000))
    os.utime(undated, (3_000, 3_000))

    entries = _parse_handoffs(project_dir)

    assert [entry.filename for entry in entries] == [
        "memory/handoff-2026-06-27-real-work.md",
        "memory/handoff-2026-06-26-previous-work.md",
        "memory/handoff-retest2-kg-edge-20260626T091721Z.md",
    ]
    assert entries[0].date == "2026-06-27"
    assert entries[-1].date == ""
