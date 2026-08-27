# v1.0.0 - 2026-07-03 - P1 F3: brain cycle notify producer (findings + drift -> per-user notifications)
"""Brain cycle notify producer (P1 F3).

A deterministic (no-LLM) post-hook of the cycle: once findings + drift for the run
are persisted, deliver ONE per-user notification per actionable output to the users
who can act on it, via the single-writer ``notify()``:

* **findings** (``approval_state in (open, pending_bootstrap)``) and **drift**
  (``state=open, severity>=medium``) with ``scope_type=project`` -> the persons with
  a grant on ``scope_key`` (or any project in ``involved_projects_json``).
* company/program scope -> workspace admins only (a project-less brain notice the
  F1 read-time filter already hides from non-admins).

Idempotent: a user who already has an UNREAD notification for the target is skipped
(no duplicate, no rollup bump on cycle recompute). Bounded: at most ``cap`` brain
notifications per user per cycle — the rest stay discoverable via list_notifications.
Never raises into the cycle: the caller isolates it, and this module logs on error.
"""
from __future__ import annotations

import json
import logging

import aiosqlite

logger = logging.getLogger(__name__)

BRAIN_FINDING_TYPE = "brain_finding"
BRAIN_DRIFT_TYPE = "brain_drift"
_ACTIONABLE_FINDING_STATES = ("open", "pending_bootstrap")
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
DEFAULT_CAP_PER_USER = 10


def _parse_projects(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        val = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if isinstance(val, list):
        return [str(p) for p in val if p]
    return []


def _sev_rank(sev: str | None) -> int:
    return _SEVERITY_RANK.get((sev or "").strip().lower(), 0)


async def _project_recipients(
    db: aiosqlite.Connection, project: str, workspace_id: str
) -> set[str]:
    """Person users.id who can see ``project`` via a direct grant or a team grant."""
    uids: set[str] = set()
    rows = await (
        await db.execute(
            "SELECT DISTINCT identity FROM access_grants "
            "WHERE project_slug = ? AND workspace_id = ?",
            (project, workspace_id),
        )
    ).fetchall()
    for (ident,) in rows:
        urows = await (
            await db.execute(
                "SELECT id FROM users WHERE type = 'human' AND deleted_at IS NULL "
                "AND workspace_id = ? AND (id = ? OR slug = ?)",
                (workspace_id, ident, ident),
            )
        ).fetchall()
        uids.update(u[0] for u in urows)
    try:
        trows = await (
            await db.execute(
                "SELECT DISTINCT tm.user_id FROM team_members tm "
                "JOIN project_teams pt ON pt.team_id = tm.team_id "
                "JOIN teams t ON t.id = tm.team_id AND t.workspace_id = ? "
                "JOIN users u ON u.id = tm.user_id AND u.type = 'human' "
                "AND u.deleted_at IS NULL AND u.workspace_id = ? "
                "WHERE pt.project = ?",
                (workspace_id, workspace_id, project),
            )
        ).fetchall()
        uids.update(t[0] for t in trows)
    except Exception:  # noqa: BLE001 — team tables optional; direct grants still count
        logger.debug(
            "notify_producer: team-grant lookup skipped for %s",
            project,
            exc_info=True,
        )
    return uids


async def _admin_recipients(
    db: aiosqlite.Connection, workspace_id: str
) -> set[str]:
    rows = await (
        await db.execute(
            "SELECT id FROM users WHERE type = 'human' "
            "AND system_role IN ('admin', 'super_admin') "
            "AND workspace_id = ? AND deleted_at IS NULL",
            (workspace_id,),
        )
    ).fetchall()
    return {r[0] for r in rows}


async def _recipients_for(
    db: aiosqlite.Connection,
    scope_type: str,
    scope_key: str | None,
    involved_raw: str | None,
    workspace_id: str,
) -> tuple[set[str], str | None]:
    """(recipient user_ids, project_field). project scope -> grantees + project set."""
    if scope_type == "project" and scope_key:
        projects = {scope_key, *(_parse_projects(involved_raw))}
        recipients: set[str] = set()
        for p in projects:
            recipients |= await _project_recipients(db, p, workspace_id)
        return recipients, scope_key
    # company / program scope: admins only, project-less (F1 filter keeps it admin-only)
    return await _admin_recipients(db, workspace_id), None


async def _emit(
    db: aiosqlite.Connection,
    per_user: dict[str, int],
    cap: int,
    *,
    ntype: str,
    target_type: str,
    target_id: str,
    scope_type: str,
    scope_key: str | None,
    involved_raw: str | None,
    workspace_id: str,
    title: str,
    body: str | None,
) -> int:
    from core.api.services.notification_service import notify

    recipients, project_field = await _recipients_for(
        db, scope_type, scope_key, involved_raw, workspace_id
    )
    emitted = 0
    for uid in recipients:
        if per_user.get(uid, 0) >= cap:
            continue
        # Idempotent: skip if this user already has an UNREAD notice for this target
        # (no duplicate / no rollup bump on cycle recompute).
        existing = await (
            await db.execute(
                "SELECT 1 FROM notifications WHERE user_id = ? AND type = ? "
                "AND target_id = ? AND workspace_id = ? "
                "AND read_at IS NULL LIMIT 1",
                (uid, ntype, target_id, workspace_id),
            )
        ).fetchone()
        if existing:
            continue
        n = await notify(
            db,
            user_ids=[uid],
            type=ntype,
            title=title,
            body=body,
            target_type=target_type,
            target_id=target_id,
            project=project_field,
            workspace_id=workspace_id,
        )
        if n:
            per_user[uid] = per_user.get(uid, 0) + 1
            emitted += 1
    return emitted


async def run_notify_phase(
    db: aiosqlite.Connection,
    *,
    run_id: str,
    cycle_key: str,
    workspace_id: str,
    now,
    cap: int = DEFAULT_CAP_PER_USER,
) -> dict:
    """Emit per-user notifications for this run's actionable findings + drift. Commits."""
    per_user: dict[str, int] = {}
    emitted = 0

    placeholders = ",".join("?" for _ in _ACTIONABLE_FINDING_STATES)
    frows = await (
        await db.execute(
            "SELECT f.finding_id, f.scope_type, f.scope_key, "
            "f.involved_projects_json, f.title "
            "FROM brain_findings f JOIN brain_runs br ON br.run_id = f.run_id "
            f"WHERE f.run_id = ? AND br.workspace_id = ? "
            f"AND f.approval_state IN ({placeholders}) "
            "AND f.superseded_by_finding_id IS NULL",
            (run_id, workspace_id, *_ACTIONABLE_FINDING_STATES),
        )
    ).fetchall()
    for r in frows:
        emitted += await _emit(
            db,
            per_user,
            cap,
            ntype=BRAIN_FINDING_TYPE,
            target_type="finding",
            target_id=r[0],
            scope_type=r[1],
            scope_key=r[2],
            involved_raw=r[3],
            workspace_id=workspace_id,
            title="Nuovo finding da rivedere",
            body=(str(r[4])[:500] if r[4] else None),
        )

    drows = await (
        await db.execute(
            "SELECT d.signal_id, d.scope_type, d.scope_key, "
            "d.involved_projects_json, d.severity "
            "FROM brain_drift_signals d "
            "JOIN brain_runs br ON br.run_id = d.run_id "
            "WHERE d.run_id = ? AND br.workspace_id = ? "
            "AND d.state = 'open' AND d.scope_type = 'project'",
            (run_id, workspace_id),
        )
    ).fetchall()
    for r in drows:
        if _sev_rank(r[4]) < _SEVERITY_RANK["medium"]:
            continue
        emitted += await _emit(
            db,
            per_user,
            cap,
            ntype=BRAIN_DRIFT_TYPE,
            target_type="drift",
            target_id=r[0],
            scope_type=r[1],
            scope_key=r[2],
            involved_raw=r[3],
            workspace_id=workspace_id,
            title="Drift rilevato da verificare",
            body=None,
        )

    await db.commit()
    logger.info(
        "brain.notify_producer: run=%s emitted=%d users=%d", run_id, emitted, len(per_user)
    )
    return {"emitted": emitted, "users": len(per_user)}
