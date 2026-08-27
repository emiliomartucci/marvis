# v1.0.0 - 2026-07-03 - P2 onboarding agent-native: per-user wizard state + profile.
"""Onboarding wizard use_cases — per-user tutorial state, transport-agnostic.

Distinct from :mod:`core.api.mcp.tools.onboarding` (scan_workdir/seed_demo, the
OSS local first-run helper). This module backs the hosted, agent-native wizard:
the MCP proposes steps, the user's own LLM relays them to the user, the user
answers / snoozes / skips, and only the STATE is recorded here — never the
content of user answers, EXCEPT the welcome_profile step which persists a small
profile WITH explicit consent (deletable). The step registry lives in
:mod:`core.api.mcp.guidance` (``ONBOARDING_STEPS``); this module is pure state
logic over it (table ``user_onboarding`` + ``user_profile``, mig 164).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import aiosqlite

from core.api.mcp.guidance import (
    onboarding_step_keys,
    ordered_onboarding_steps,
)
from core.api.use_cases._errors import ValidationError

if TYPE_CHECKING:  # avoid an import cycle at module load (adapter imports errors)
    from core.api.mcp._adapter import CallerContext


_TERMINAL = ("done", "skipped")  # states that never resurface as actionable
_VALID_RESPONSE_STYLES = ("concise", "detailed")
_VALID_ACTIONS = ("done", "snooze", "skip", "delete_profile")
_DEFAULT_SNOOZE_DAYS = 3
_MAX_SNOOZE_DAYS = 90
_CLOSE_SENTINEL = "all"
_FIELD_MAX = 200


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_in_days(days: int) -> str:
    when = datetime.now(timezone.utc) + timedelta(days=days)
    return when.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _clip(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:_FIELD_MAX] if text else None


def _is_person(ctx: "CallerContext") -> bool:
    """True only for a real OAuth person (not a static tenant bearer / local agent).

    Static bearer -> user_id 'tenant:<id>' (admin break-glass); local/agent default
    -> user_type 'agent'. Onboarding is per-person, so those get an empty wizard.
    """
    uid = getattr(ctx, "user_id", "") or ""
    return (
        getattr(ctx, "user_type", None) == "human"
        and bool(uid)
        and uid != "local"
        and not uid.startswith("tenant:")
    )


def _empty_status() -> dict[str, Any]:
    total = len(onboarding_step_keys())
    return {
        "applicable": False,
        "reason": "onboarding is per-person; this caller is a tenant bearer or local agent",
        "progress": {"done": 0, "total": total, "remaining": 0},
        "next_step": None,
        "remaining": [],
    }


async def _load_state(
    db: aiosqlite.Connection, workspace_id: str, user_id: str
) -> dict[str, tuple[str, str | None]]:
    db.row_factory = aiosqlite.Row
    cur = await db.execute(
        "SELECT step_key, status, snooze_until FROM user_onboarding"
        " WHERE workspace_id = ? AND user_id = ?",
        (workspace_id, user_id),
    )
    rows = await cur.fetchall()
    return {r["step_key"]: (r["status"], r["snooze_until"]) for r in rows}


async def get_status(
    ctx: "CallerContext",
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    user_id: str,
) -> dict[str, Any]:
    """Wizard progress for the calling person. Bearer/local -> empty (applicable=false).

    Pure read (no DB writes, no users-row mint). ``next_step`` is the first step
    that is neither terminal (done/skipped) nor in an active snooze window; it
    carries the FULL step payload plus a ``position`` like "1/6". ``remaining``
    lists the rest compactly (key/title/status).
    """
    if not _is_person(ctx):
        return _empty_status()

    state = await _load_state(db, workspace_id, user_id)
    now = _now_iso()
    steps = ordered_onboarding_steps()
    total = len(steps)
    done = sum(1 for s in steps if state.get(s["key"], (None, None))[0] in _TERMINAL)

    next_step: dict[str, Any] | None = None
    remaining: list[dict[str, Any]] = []
    for idx, s in enumerate(steps, start=1):
        status_val, snooze_until = state.get(s["key"], (None, None))
        if status_val in _TERMINAL:
            continue
        snoozed_active = (
            status_val == "snoozed"
            and snooze_until is not None
            and snooze_until > now
        )
        remaining.append(
            {
                "key": s["key"],
                "title": s["title"],
                "status": status_val or "pending",
                "snooze_until": snooze_until if status_val == "snoozed" else None,
            }
        )
        if next_step is None and not snoozed_active:
            next_step = {**s, "position": f"{idx}/{total}"}

    return {
        "applicable": True,
        "progress": {"done": done, "total": total, "remaining": total - done},
        "next_step": next_step,
        "remaining": remaining,
    }


async def _ensure_person_row(
    db: aiosqlite.Connection, ctx: "CallerContext", user_id: str
) -> None:
    """Ensure the OAuth person's ``users`` row exists so the FK holds.

    An interactive AuthKit person can have NO users row: sync_oauth_user inserts
    only when a mapped role claim is present, and interactive tokens omit it. We
    mint a minimal viewer row (INSERT OR IGNORE) — safe because viewer is the
    default and a later role-bearing token's sync UPDATE lifts it.

    RECONCILE: P1 introduces a canonical ``person_user_id`` helper; replace this
    local mint with it when P1 merges.
    """
    slug = re.sub(r"[^a-z0-9_-]+", "-", str(user_id).lower()).strip("-") or "user"
    display_name = str(getattr(ctx, "username", None) or user_id)
    await db.execute(
        "INSERT OR IGNORE INTO users (id, slug, display_name, type)"
        " VALUES (?, ?, ?, 'human')",
        (user_id, slug, display_name),
    )


async def _save_profile(
    db: aiosqlite.Connection, workspace_id: str, user_id: str, profile: dict[str, Any]
) -> None:
    if not isinstance(profile, dict):
        raise ValidationError(
            code="bad_profile", message="profile must be an object of name/role fields."
        )
    style = profile.get("response_style")
    if style is not None and style not in _VALID_RESPONSE_STYLES:
        raise ValidationError(
            code="bad_response_style",
            message=f"response_style must be one of {list(_VALID_RESPONSE_STYLES)} or omitted.",
        )
    now = _now_iso()
    await db.execute(
        "INSERT INTO user_profile"
        " (workspace_id, user_id, display_name, role_title, org_unit,"
        "  response_style, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(workspace_id, user_id) DO UPDATE SET"
        "  display_name=excluded.display_name,"
        "  role_title=excluded.role_title,"
        "  org_unit=excluded.org_unit,"
        "  response_style=excluded.response_style,"
        "  updated_at=excluded.updated_at",
        (
            workspace_id,
            user_id,
            _clip(profile.get("name") or profile.get("display_name")),
            _clip(profile.get("role") or profile.get("role_title")),
            _clip(profile.get("org_unit")),
            style,
            now,
            now,
        ),
    )


async def _upsert_step(
    db: aiosqlite.Connection,
    workspace_id: str,
    user_id: str,
    step_key: str,
    status_val: str,
    now: str,
    snooze_until: str | None,
    *,
    only_if_not_done: bool = False,
) -> None:
    sql = (
        "INSERT INTO user_onboarding"
        " (workspace_id, user_id, step_key, status, answered_at, snooze_until)"
        " VALUES (?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(workspace_id, user_id, step_key) DO UPDATE SET"
        "  status=excluded.status,"
        "  answered_at=excluded.answered_at,"
        "  snooze_until=excluded.snooze_until"
    )
    if only_if_not_done:
        sql += " WHERE user_onboarding.status != 'done'"
    await db.execute(sql, (workspace_id, user_id, step_key, status_val, now, snooze_until))


async def answer(
    ctx: "CallerContext",
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    user_id: str,
    step_key: str,
    action: str,
    snooze_days: int | None = None,
    profile: dict[str, Any] | None = None,
    consent: bool = False,
) -> dict[str, Any]:
    """Record the caller's answer for one step (or close the wizard).

    action: 'done' | 'snooze' | 'skip' | 'delete_profile'.
      - done: marks the step complete. For welcome_profile, saves the profile
        ONLY when consent=true AND a profile object is given; otherwise the step
        is recorded without saving anything (transparency contract).
      - snooze: hides the step until snooze_until (snooze_days, default 3).
      - skip: permanently dismisses the step. Pass step_key='all' to close the
        whole wizard (skips every not-yet-done step).
      - delete_profile: erases the saved profile row (cancellable at any time).
    Bearer / local caller -> not applicable (raises).
    """
    if not _is_person(ctx):
        raise ValidationError(
            code="onboarding_not_applicable",
            message="Onboarding is per-person; a tenant bearer / local agent has no wizard state.",
        )
    if action not in _VALID_ACTIONS:
        raise ValidationError(
            code="unknown_action",
            message=f"action must be one of {list(_VALID_ACTIONS)}.",
        )
    valid_keys = set(onboarding_step_keys())
    if step_key != _CLOSE_SENTINEL and step_key not in valid_keys:
        raise ValidationError(
            code="unknown_step",
            message=(
                f"Unknown step_key {step_key!r}. Valid: {sorted(valid_keys)} "
                f"or '{_CLOSE_SENTINEL}' to close the wizard."
            ),
        )

    # Every write path needs the person's users row for the FK.
    await _ensure_person_row(db, ctx, user_id)
    now = _now_iso()

    if action == "delete_profile":
        await db.execute(
            "DELETE FROM user_profile WHERE workspace_id = ? AND user_id = ?",
            (workspace_id, user_id),
        )
        await db.commit()
        return {
            "ok": True,
            "step_key": "welcome_profile",
            "action": "delete_profile",
            "profile_deleted": True,
        }

    if step_key == _CLOSE_SENTINEL:
        if action != "skip":
            raise ValidationError(
                code="invalid_close",
                message="step_key='all' only supports action='skip' (close the wizard).",
            )
        for s in ordered_onboarding_steps():
            await _upsert_step(
                db, workspace_id, user_id, s["key"], "skipped", now, None,
                only_if_not_done=True,
            )
        await db.commit()
        status = await get_status(ctx, db, workspace_id=workspace_id, user_id=user_id)
        return {"ok": True, "step_key": "all", "action": "skip", "closed": True, "status": status}

    profile_saved = False
    if action == "done":
        status_val, snooze_until = "done", None
        if step_key == "welcome_profile" and consent and profile:
            await _save_profile(db, workspace_id, user_id, profile)
            profile_saved = True
    elif action == "snooze":
        days = _DEFAULT_SNOOZE_DAYS if snooze_days is None else int(snooze_days)
        if days < 1 or days > _MAX_SNOOZE_DAYS:
            raise ValidationError(
                code="bad_snooze",
                message=f"snooze_days must be an integer in 1..{_MAX_SNOOZE_DAYS}.",
            )
        status_val, snooze_until = "snoozed", _iso_in_days(days)
    else:  # skip
        status_val, snooze_until = "skipped", None

    await _upsert_step(db, workspace_id, user_id, step_key, status_val, now, snooze_until)
    await db.commit()

    status = await get_status(ctx, db, workspace_id=workspace_id, user_id=user_id)
    out: dict[str, Any] = {
        "ok": True,
        "step_key": step_key,
        "action": action,
        "recorded_status": status_val,
        "status": status,
    }
    if step_key == "welcome_profile" and action == "done":
        out["profile_saved"] = profile_saved
        if not profile_saved and (profile or consent):
            out["note"] = "profile NOT saved: requires both a profile object and consent=true"
    return out


async def onboarding_pending(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    user_id: str,
) -> dict[str, int]:
    """Step tallies for the notices producer (F2) — cheap, read-only.

    Returns ``{actionable, remaining, total}``. ``actionable`` = steps that are
    neither terminal (done/skipped) nor in an active snooze window — the ones a
    nudge should surface now. The onboarding notice fires iff ``actionable > 0``,
    so it disappears when the wizard is complete (all terminal) or fully snoozed
    and reappears when a snooze expires. Does NOT mint a users row (pure read).
    """
    state = await _load_state(db, workspace_id, user_id)
    now = _now_iso()
    steps = ordered_onboarding_steps()
    total = len(steps)
    done = 0
    actionable = 0
    for step in steps:
        status_val, snooze_until = state.get(step["key"], (None, None))
        if status_val in _TERMINAL:
            done += 1
            continue
        if status_val == "snoozed" and snooze_until is not None and snooze_until > now:
            continue
        actionable += 1
    return {"actionable": actionable, "remaining": total - done, "total": total}
