# v1.0.0 - 2026-07-03 - P2 onboarding agent-native: per-user wizard MCP tools.
"""Onboarding wizard MCP tools (``onboarding_status`` / ``onboarding_answer``).

Backs the transparent, per-user first-run wizard: the MCP proposes steps, the
user's own LLM relays them to the user, the user answers/snoozes/skips, and only
the STATE is recorded (the welcome_profile step persists a profile only WITH
consent). Distinct from :mod:`core.api.mcp.tools.onboarding` (scan_workdir/
seed_demo). Logic in :mod:`core.api.use_cases.onboarding_wizard`.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field

from core.api.mcp._adapter import (
    acquire_db,
    acquire_write_db,
    current_mcp_context,
    dump,
    raise_mcp_error,
)
from core.api.use_cases import onboarding_wizard as onboarding_uc
from core.api.use_cases._errors import ServiceError

Action = Literal["done", "snooze", "skip", "delete_profile"]


def register(mcp) -> None:
    """Register the onboarding wizard tools on the shared FastMCP instance."""

    @mcp.tool()
    async def onboarding_status() -> dict[str, Any]:
        """Guided-setup progress for the calling user (per-person, 6 steps).

        WHEN TO USE: first connection or to resume the tutorial — returns
        {progress, next_step (full proposal payload + position like "1/6"),
        remaining}. Relay next_step.propose to the user; do NOT auto-apply.
        WHEN NOT TO USE: not for project data (session_brief) or tool routing
        (guide). A tenant bearer / non-person caller gets applicable=false (empty).
        NEXT: onboarding_answer(step_key, action)."""
        try:
            ctx = current_mcp_context()
            async with acquire_db() as db:
                result = await onboarding_uc.get_status(
                    ctx, db, workspace_id=ctx.workspace_id, user_id=ctx.user_id
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def onboarding_answer(
        step_key: Annotated[str, Field(max_length=64)],
        action: Action,
        snooze_days: Annotated[int | None, Field(ge=1, le=90)] = None,
        profile: dict[str, Any] | None = None,
        consent: bool = False,
    ) -> dict[str, Any]:
        """Record the user's answer for one onboarding step (or close the wizard).

        action: done | snooze | skip | delete_profile.
        - done: completes the step. For welcome_profile, saves the profile ONLY
          when consent=true AND a profile object {name, role, org_unit,
          response_style} is given; otherwise records the step without saving.
        - snooze: hides the step until snooze_days (default 3) elapse.
        - skip: dismisses the step. Pass step_key='all' to close the whole wizard.
        - delete_profile: erases the saved profile (cancellable anytime).
        Scoped to the calling person; a tenant bearer has no wizard state."""
        try:
            ctx = current_mcp_context()
            async with acquire_write_db(label="mcp.onboarding_answer") as db:
                result = await onboarding_uc.answer(
                    ctx,
                    db,
                    workspace_id=ctx.workspace_id,
                    user_id=ctx.user_id,
                    step_key=step_key,
                    action=action,
                    snooze_days=snooze_days,
                    profile=profile,
                    consent=consent,
                )
                return dump(result)
        except ServiceError as e:
            raise_mcp_error(e)
