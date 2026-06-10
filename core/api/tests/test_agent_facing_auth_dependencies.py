from __future__ import annotations

from collections.abc import Iterable

import pytest
from fastapi.routing import APIRoute

from core.api.routers import (
    audit,
    comments,
    costs,
    finder,
    handoffs,
    learnings,
    monitoring,
    projects,
    pull_requests,
    search,
    sessions,
    status_updates,
    tasks,
    teams,
    users,
)


def _route_dependency_names(router, method: str, path: str) -> list[str]:
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        if method not in (route.methods or set()):
            continue
        if route.path != path:
            continue
        return [
            getattr(dep.call, "__name__", str(dep.call))
            for dep in route.dependant.dependencies
        ]
    raise AssertionError(f"Route not found: {method} {path}")


def _agent_compatible_dependency(dependencies: Iterable[str]) -> bool:
    return (
        "get_current_user_or_agent" in dependencies
        or "require_any_auth" in dependencies
    )


@pytest.mark.parametrize(
    ("router", "method", "path"),
    [
        (tasks.router, "GET", "/api/v1/tasks/summary"),
        (tasks.router, "GET", "/api/v1/tasks/projects"),
        (tasks.router, "GET", "/api/v1/tasks"),
        (tasks.router, "GET", "/api/v1/tasks/{task_id}"),
        (tasks.router, "GET", "/api/v1/tasks/{task_id}/cost-entries"),
        (search.router, "GET", "/api/v1/search"),
        (learnings.router, "GET", "/api/v1/learnings/check"),
        (learnings.router, "GET", "/api/v1/learnings"),
        (learnings.router, "GET", "/api/v1/learnings/{learning_id}"),
        (monitoring.router, "GET", "/api/v1/monitoring/current"),
        (monitoring.router, "GET", "/api/v1/monitoring/history"),
        (monitoring.router, "GET", "/api/v1/monitoring/disk-tree"),
        (handoffs.router, "GET", "/api/v1/handoffs/search"),
        (projects.router, "GET", "/api/v1/projects"),
        (projects.router, "GET", "/api/v1/projects/{slug}"),
        (projects.router, "GET", "/api/v1/projects/{slug}/handoffs"),
        (projects.router, "GET", "/api/v1/projects/{slug}/plans"),
        (projects.router, "GET", "/api/v1/projects/{slug}/git/log"),
        (projects.router, "GET", "/api/v1/projects/{slug}/git/diff"),
        (projects.router, "GET", "/api/v1/projects/{slug}/git/branches"),
        (projects.router, "GET", "/api/v1/projects/{slug}/git/graph"),
        (projects.router, "GET", "/api/v1/projects/{slug}/git/commit/{commit_hash}"),
        (sessions.router, "GET", "/sessions"),
        (sessions.router, "GET", "/sessions/catalog"),
        (sessions.router, "GET", "/sessions/{name}/metrics"),
        (sessions.router, "GET", "/sessions/{name}/conversation"),
        (sessions.router, "GET", "/sessions/by-uuid/{session_uuid}"),
        (pull_requests.router, "GET", "/api/v1/pull_requests/merge-conflicts"),
        (pull_requests.router, "GET", "/api/v1/pull_requests/{task_id}"),
        (audit.router, "GET", "/api/v1/audit"),
        (finder.router, "GET", "/api/v1/finder/tree"),
        (finder.router, "GET", "/api/v1/finder/list"),
        (finder.router, "GET", "/api/v1/finder/file"),
        (finder.router, "GET", "/api/v1/finder/download"),
        (finder.router, "POST", "/api/v1/finder/share"),
        (finder.router, "GET", "/api/v1/finder/shares"),
        (finder.router, "DELETE", "/api/v1/finder/share/{token}"),
        (comments.router, "POST", "/api/v1/comments"),
        (comments.router, "GET", "/api/v1/comments"),
        (comments.router, "PATCH", "/api/v1/comments/{comment_id}"),
        (comments.router, "DELETE", "/api/v1/comments/{comment_id}"),
        (comments.router, "POST", "/api/v1/comments/{comment_id}/reactions"),
        (
            comments.router,
            "DELETE",
            "/api/v1/comments/{comment_id}/reactions/{reaction}",
        ),
        (costs.router, "GET", "/api/v1/costs/summary"),
        (costs.router, "GET", "/api/v1/costs/by-project/{slug}"),
        (costs.router, "GET", "/api/v1/costs/billing/{slug}"),
        (status_updates.router, "GET", "/api/v1/status-updates"),
        (status_updates.router, "GET", "/api/v1/status-updates/overdue"),
        (teams.router, "GET", "/api/v1/teams"),
        (teams.router, "GET", "/api/v1/teams/{team_id}/members"),
        (teams.router, "GET", "/api/v1/teams/{team_id}/projects"),
        (users.router, "GET", "/api/v1/users/{user_id}"),
        (users.router, "GET", "/api/v1/users/{user_id}/raci"),
    ],
)
def test_agent_facing_routes_do_not_regress_to_cookie_only_auth(
    router, method: str, path: str
) -> None:
    dependencies = _route_dependency_names(router, method, path)

    assert "get_current_user" not in dependencies, (
        f"{method} {path} regressed to cookie-only auth: {dependencies}"
    )
    assert _agent_compatible_dependency(dependencies), (
        f"{method} {path} is missing agent-compatible auth dependency: {dependencies}"
    )


def test_search_reindex_route_does_not_use_readonly_pool_dependency() -> None:
    dependencies = _route_dependency_names(
        search.router, "POST", "/api/v1/search/reindex"
    )

    assert "get_db" not in dependencies, (
        "POST /api/v1/search/reindex must not depend on the read-only pool; "
        f"found dependencies: {dependencies}"
    )
