"""Regression tests for issue #12 — create_learning must not hard-require the
migration-148 bitemporal columns when the temporal feature is off.

A brain created on <=0.3.7 (schema_version <= 145) that upgrades to 0.3.8b1 has
no ``valid_from`` column until migration 148 is applied. With the temporal flag
off (the default) ``create_learning`` must still write a learning — the flag-off
path consumes none of the bitemporal columns.
"""

from __future__ import annotations

import aiosqlite
import pytest

import core.api.use_cases.learnings as learnings_mod
from core.api.use_cases._context import CallerContext
from core.api.use_cases.learnings import create_learning

# Pre-148 schema: the learnings table exactly as it shipped on <=0.3.7, i.e.
# WITHOUT valid_from / invalid_at / superseded_by / supersede_reason.
_PRE_148_LEARNINGS = """
    CREATE TABLE learnings (
        id TEXT PRIMARY KEY,
        title TEXT,
        category TEXT,
        description TEXT,
        tags TEXT,
        module TEXT,
        severity TEXT,
        frequency INTEGER,
        last_occurrence TEXT,
        prevention TEXT,
        session INTEGER,
        project TEXT,
        created_at TEXT,
        updated_at TEXT,
        workspace_id TEXT
    )
"""

_WITH_148_LEARNINGS = _PRE_148_LEARNINGS.rstrip().rstrip(")") + (
    ",\n        valid_from TIMESTAMP,\n        invalid_at TIMESTAMP\n    )"
)


@pytest.mark.asyncio
async def test_create_learning_on_pre_148_schema_when_flag_off(monkeypatch) -> None:
    monkeypatch.setattr(learnings_mod.settings, "temporal_memory_enabled", False, raising=False)

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(_PRE_148_LEARNINGS)
        await db.commit()

        res = await create_learning(
            CallerContext.local_single_user(),
            db,
            title="Upgrade must not break capture",
            category="architecture",
            description="create_learning works without the mig-148 columns.",
        )

        assert res.title == "Upgrade must not break capture"
        cursor = await db.execute("SELECT COUNT(*) AS n FROM learnings")
        assert (await cursor.fetchone())["n"] == 1


@pytest.mark.asyncio
async def test_create_learning_writes_valid_from_when_flag_on(monkeypatch) -> None:
    monkeypatch.setattr(learnings_mod.settings, "temporal_memory_enabled", True, raising=False)

    # Flag on → the write-time decision runs; isolate the INSERT by stubbing it.
    async def _noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(learnings_mod, "_decide_write_time", _noop, raising=False)

    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(_WITH_148_LEARNINGS)
        await db.commit()

        res = await create_learning(
            CallerContext.local_single_user(),
            db,
            title="Flag on stamps valid_from",
            category="architecture",
            description="valid_from is set to the learn-time when temporal is on.",
        )

        cursor = await db.execute(
            "SELECT valid_from FROM learnings WHERE id = ?", (res.id,)
        )
        row = await cursor.fetchone()
        assert row["valid_from"] is not None
