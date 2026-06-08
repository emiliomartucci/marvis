#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "${1:-}" != "--inside-container" ]]; then
  cd "$TEMPLATE_DIR"
  docker compose run --rm api /app/deploy/_template/scripts/init.sh --inside-container
  exit 0
fi

export PIR_DB_PATH="${PIR_DB_PATH:-/data/pir/console.db}"
mkdir -p "$(dirname "$PIR_DB_PATH")" "${WORKSPACE_ROOT:-/data/workspace}" "${RUNTIME_HOME:-/data/runtime}"

python - <<'PY'
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import bcrypt

from core.api.db import run_migrations


def env(name: str, default: str) -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def password_hash() -> str:
    explicit_hash = os.environ.get("PIR_ADMIN_PASSWORD_HASH", "").strip()
    if explicit_hash:
        return explicit_hash
    password = env("PIR_PASSWORD", "local-admin-password")
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        [table],
    ).fetchone()
    return row is not None


def column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    return any(row[1] == column for row in conn.execute(f"PRAGMA table_info({table})"))


def seed_minimal() -> None:
    db_path = Path(env("PIR_DB_PATH", "/data/pir/console.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    run_migrations()

    admin_id = env("MARVIS_ADMIN_USER_ID", "usr_admin")
    admin_slug = env("MARVIS_ADMIN_SLUG", "admin")
    admin_name = env("MARVIS_ADMIN_DISPLAY_NAME", "Admin")
    admin_email = env("MARVIS_ADMIN_EMAIL", "admin@example.local")
    team_id = env("MARVIS_TEAM_ID", "team_local")
    team_slug = env("MARVIS_TEAM_SLUG", "local")
    team_name = env("MARVIS_TEAM_DISPLAY_NAME", "Local Team")
    hashed = password_hash()

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")

        if table_exists(conn, "users"):
            existing_admin = conn.execute(
                "SELECT id FROM users WHERE slug = ? AND deleted_at IS NULL",
                [admin_slug],
            ).fetchone()
            if existing_admin is None:
                super_admins = conn.execute(
                    "SELECT id FROM users WHERE type = 'human' AND system_role = 'super_admin' AND deleted_at IS NULL"
                ).fetchall()
                if len(super_admins) == 1:
                    conn.execute(
                        """UPDATE users
                           SET id = ?, slug = ?, display_name = ?, email = ?,
                               password_hash = COALESCE(NULLIF(password_hash, ''), ?),
                               system_role = 'super_admin', updated_at = datetime('now')
                           WHERE id = ?""",
                        [admin_id, admin_slug, admin_name, admin_email, hashed, super_admins[0]["id"]],
                    )
                else:
                    conn.execute(
                        """INSERT OR IGNORE INTO users (
                            id, slug, display_name, type, email, password_hash,
                            system_role, created_at, updated_at
                        ) VALUES (?, ?, ?, 'human', ?, ?, 'super_admin', datetime('now'), datetime('now'))""",
                        [admin_id, admin_slug, admin_name, admin_email, hashed],
                    )
            else:
                conn.execute(
                    """UPDATE users
                       SET display_name = ?, email = ?, system_role = 'super_admin',
                           updated_at = datetime('now')
                       WHERE id = ?""",
                    [admin_name, admin_email, existing_admin["id"]],
                )

        if table_exists(conn, "teams"):
            team_columns = {row[1] for row in conn.execute("PRAGMA table_info(teams)")}
            if "avatar_color" in team_columns:
                conn.execute(
                    """INSERT OR IGNORE INTO teams (
                        id, slug, display_name, description, avatar_color, created_by
                    ) VALUES (?, ?, ?, 'Local bootstrap team', '#2563eb', ?)""",
                    [team_id, team_slug, team_name, admin_id],
                )
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO teams (
                        id, slug, display_name, description, created_by
                    ) VALUES (?, ?, ?, 'Local bootstrap team', ?)""",
                    [team_id, team_slug, team_name, admin_id],
                )

        if table_exists(conn, "team_members"):
            if column_exists(conn, "team_members", "role"):
                conn.execute(
                    """INSERT OR IGNORE INTO team_members (
                        team_id, user_id, is_admin, role, joined_at
                    ) VALUES (?, ?, 1, 'admin', datetime('now'))""",
                    [team_id, admin_id],
                )
            else:
                conn.execute(
                    """INSERT OR IGNORE INTO team_members (
                        team_id, user_id, is_admin, joined_at
                    ) VALUES (?, ?, 1, datetime('now'))""",
                    [team_id, admin_id],
                )

        if table_exists(conn, "project_teams"):
            conn.execute(
                """INSERT OR IGNORE INTO project_teams (
                    project, team_id, is_public, assigned_at
                ) VALUES ('local-workspace', ?, 1, datetime('now'))""",
                [team_id],
            )

        conn.commit()

    print(f"schema ready: {db_path}")
    print(f"admin ready: {admin_slug}")


if __name__ == "__main__":
    seed_minimal()
PY
