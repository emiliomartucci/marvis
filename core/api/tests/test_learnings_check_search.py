from __future__ import annotations

import aiosqlite
import pytest

from core.api.routers.learnings import _extract_check_terms, _search_learning_rows


@pytest.mark.asyncio
async def test_extract_check_terms_drops_noise_and_keeps_signal() -> None:
    terms = _extract_check_terms(
        "get_current_user_or_agent agent-facing endpoint bearer auth regression prevention"
    )

    assert "auth" in terms
    assert "bearer" in terms
    assert "agent-facing" in terms
    assert "and" not in terms


@pytest.mark.asyncio
async def test_search_learning_rows_falls_back_to_keywords_for_long_queries() -> None:
    async with aiosqlite.connect(":memory:") as db:
        db.row_factory = aiosqlite.Row
        await db.execute(
            """
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
        )
        await db.execute(
            """
            INSERT INTO learnings (
                id, title, category, description, tags, module, severity, frequency,
                last_occurrence, prevention, session, project, created_at, updated_at, workspace_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "e74a0ea1-75e2-433b-8b20-02522b4330f2",
                "Nuovi endpoint API: usare get_current_user_or_agent non get_current_user",
                "auth",
                "Gli endpoint agent-facing devono supportare Bearer auth e X-Agent-Name.",
                '["auth", "bearer", "api"]',
                "api/routers/search.py",
                "high",
                2,
                "2026-03-29T00:00:00Z",
                "Usare get_current_user_or_agent sugli endpoint agent-facing.",
                None,
                "marvisx",
                "2026-03-29T00:00:00Z",
                None,
                "ws_default",
            ),
        )
        await db.commit()

        rows = await _search_learning_rows(
            db,
            "ws_default",
            "get_current_user_or_agent agent-facing endpoint bearer auth regression prevention",
            None,
        )

        assert len(rows) == 1
        assert rows[0]["id"] == "e74a0ea1-75e2-433b-8b20-02522b4330f2"
