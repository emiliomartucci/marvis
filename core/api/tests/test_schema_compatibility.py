from __future__ import annotations

import sqlite3

import pytest

from core.api.db import assert_schema_compatible


def test_old_binary_denies_forward_schema() -> None:
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE schema_versions "
        "(version INTEGER PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    connection.execute("INSERT INTO schema_versions(version) VALUES (4)")

    with pytest.raises(RuntimeError, match="OLDER image"):
        assert_schema_compatible(connection, code_max=3, known_versions={1, 2, 3})
