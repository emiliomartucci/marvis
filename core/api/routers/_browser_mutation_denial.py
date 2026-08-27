from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import APIRouter
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import Response


Endpoint = TypeVar("Endpoint", bound=Callable[..., Any])


class BrowserMutationDeniedRoute(APIRoute):
    """Hide legacy project-file mutations from browser sessions before body parsing."""

    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def agent_only(request: Request) -> Response:
            if not request.headers.get("authorization", "").startswith("Bearer "):
                return Response(status_code=404)
            return await route_handler(request)

        return agent_only


def agent_only_route(
    router: APIRouter,
    path: str,
    *,
    methods: list[str],
    **kwargs: Any,
) -> Callable[[Endpoint], Endpoint]:
    """Register a legacy mutation as an internal bearer-only, non-discoverable route."""

    def register(endpoint: Endpoint) -> Endpoint:
        router.add_api_route(
            path,
            endpoint,
            methods=methods,
            include_in_schema=False,
            route_class_override=BrowserMutationDeniedRoute,
            **kwargs,
        )
        return endpoint

    return register
