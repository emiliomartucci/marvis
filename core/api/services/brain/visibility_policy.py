"""Enterprise Brain list predicates applied before pagination."""
from __future__ import annotations

import os


def enterprise_strict() -> bool:
    return os.environ.get("MARVIS_ENTERPRISE_OIDC_STRICT", "").strip() == "1"


def scope_visible(
    visible: set[str] | None,
    involved_projects: list[str],
    scope_type: str,
    scope_key: str,
) -> bool:
    if visible is None:
        return True
    if not enterprise_strict():
        return not involved_projects or set(involved_projects).issubset(visible)
    if scope_type in ("company", "program"):
        return False
    projects = set(involved_projects)
    if scope_type == "project":
        projects.add(scope_key)
    return bool(projects) and projects.issubset(visible)


def scope_sql(alias: str, visible: set[str] | None) -> tuple[str | None, list[str]]:
    """Filter hidden rows in SQL so a hidden page cannot crowd out visible rows."""
    if not enterprise_strict() or visible is None:
        return None, []
    if not visible:
        return "0", []
    placeholders = ",".join("?" for _ in visible)
    involved = f"{alias}.involved_projects_json"
    safe_involved = f"CASE WHEN json_valid({involved}) THEN {involved} ELSE '[]' END"
    sql = (
        f"(({alias}.scope_type = 'project' AND {alias}.scope_key IN ({placeholders})) "
        f"OR ({alias}.scope_type = 'artifact' AND json_array_length({safe_involved}) > 0)) "
        f"AND NOT EXISTS (SELECT 1 FROM json_each({safe_involved}) involved "
        f"WHERE involved.value NOT IN ({placeholders}))"
    )
    ordered = sorted(visible)
    return sql, ordered + ordered
