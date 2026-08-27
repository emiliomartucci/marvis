# v1.18.0 - 2026-05-22 - P1.5.E1: remove PaddleOCR local (broken warm-up since 30-apr); OCR live via Mac Gateway tier-ocr
# v1.17.0 - 2026-04-30 - P1.5.E0: add PaddleOCR async warm + /health/ocr backend status
# v1.16.0 - 2026-04-17 - P2: add slowapi rate limiting middleware for /graph endpoints
# v1.15.0 - 2026-03-15 - Add push delivery + embedding client init in lifespan + orphan proxy cleanup
from __future__ import annotations

import asyncio
import logging
import secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import aiosqlite
from fastapi import FastAPI, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from core.api.config import settings
from core.api.db import cleanup_expired_blacklist, cleanup_expired_tickets, run_migrations
from core.api.security import is_local_single_user_mode, is_loopback_request
from core.api.ui_static import apply_ui_response_headers, mount_ui
from core.api.use_cases._errors import ServiceError
from core.api.routers import auth, sessions
from core.api.routers.comments import router as comments_router
from core.api.routers.delegations import router as delegations_router
from core.api.routers.costs import router as costs_router
from core.api.routers.files import router as files_router
from core.api.routers.projects import router as projects_router
from core.api.routers.admin_settings import router as admin_settings_router
from core.api.routers.settings import router as settings_router
from core.api.routers.status_updates import router as status_updates_router
from core.api.routers.tasks import router as tasks_router
from core.api.routers.todos import router as todos_router
from core.api.services import claude_metrics, codex_metrics, opencode_sessions, tmux
from core.api.services.metrics_providers import get_metrics_provider
from core.api.services.providers import get_provider
from core.api.services.session_metrics_service import (
    compute_cost_session_extended,
)
from core.api.routers.agent import router as agent_router
from core.api.routers.monitoring import router as monitoring_router
from core.api.routers.tags import router as tags_router
from core.api.routers.finder import router as finder_router
from core.api.routers.finder import share_router as share_manage_router
from core.api.routers.finder import shared_router as shared_file_router
from core.api.routers.kg import router as kg_router
from core.api.routers.share_repo import router as repo_share_router
from core.api.routers.share_repo import shared_repo_router
from core.api.routers.pull_requests import router as pull_requests_router
from core.api.routers.users import router as users_router
from core.api.routers.raci import router as raci_router
from core.api.routers.audit import router as audit_router
from core.api.routers.webhooks import router as webhooks_router
from core.api.routers.admin_pr_impact import router as admin_pr_impact_router
from core.api.routers.pr_impact import router as pr_impact_router
from core.api.routers.agent_tokens import router as agent_tokens_router
from core.api.routers.handoffs import router as handoffs_router
from core.api.routers.teams import router as teams_router
from core.api.routers.graph import router as graph_router
from core.api.routers.graph_ingest import router as graph_ingest_router
from core.api.routers.learnings import router as learnings_router
from core.api.routers.notifications import router as notifications_router
from core.api.routers.onboarding import router as onboarding_router
from core.api.routers.push import router as push_router
from core.api.routers.ci_checks import router as ci_checks_router
from core.api.routers.search import router as search_router
from core.api.routers.documents import router as documents_router
from core.api.routers.docs_coverage import router as docs_coverage_router
from core.api.routers.docs_governance import router as docs_governance_router
from core.api.routers.judge import router as judge_router
from core.api.routers.inbox import router as inbox_router
from core.api.routers.ingest_triage import router as ingest_triage_router
from core.api.routers.ingest_api_keys import router as ingest_api_keys_router
from core.api.routers.llm_config import router as llm_config_router
from core.api.routers.retired_integrations import router as retired_integrations_router
try:
    from core.api.routers.gui_events import router as gui_events_router
except ImportError:  # Hosted product-events module, absent in public projection
    gui_events_router = None
try:
    from core.api.routers.newsletter import router as newsletter_router
except ImportError:  # SaaS-only module, absent in OSS mirror
    newsletter_router = None
from core.api.routers.bench import router as bench_router
from core.api.routers.app_settings import router as app_settings_router
try:
    from core.api.routers.account import router as hosted_account_router
except ImportError:  # Hosted control-plane account module, absent in public projection
    hosted_account_router = None
from core.api.routers.terminal import router as terminal_metrics_router
from core.api.routers.brain import router as brain_router
from core.api.routers.brain_directions import router as brain_directions_router
from core.api.services.metrics_collector import metrics_collector
from core.api.services.security_collector import security_collector
from core.api.terminal import router as terminal_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

_cleanup_task: asyncio.Task | None = None
_session_maintenance_task: asyncio.Task | None = None
_metrics_task: asyncio.Task | None = None
_security_task: asyncio.Task | None = None
_maintenance_task: asyncio.Task | None = None
_reminder_task: asyncio.Task | None = None
_push_delivery_task: asyncio.Task | None = None
_notifications_sync_task: asyncio.Task | None = None
_inbox_digest_task: asyncio.Task | None = None
_brain_jobs_task: asyncio.Task | None = None
_wal_checkpoint_task: asyncio.Task | None = None
_terminal_metrics_dump_task: asyncio.Task | None = None
_pr_impact_sweep_task: asyncio.Task | None = None
_pr_impact_gc_task: asyncio.Task | None = None


async def _session_has_active_cli_for_provider(
    name: str,
    provider: str | None,
    status: str | None,
    cache: dict[tuple[str, str], bool] | None = None,
) -> bool:
    """Return true when tmux foreground or child process matches the provider CLI."""
    from core.api.services.providers import ALL_KNOWN_PROCESS_NAMES, get_provider

    if status in ALL_KNOWN_PROCESS_NAMES:
        return True

    provider_name = provider or "claude"
    cache_key = (name, provider_name)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    try:
        process_names = get_provider(provider_name).process_names
    except ValueError:
        process_names = ALL_KNOWN_PROCESS_NAMES

    active = await tmux.get_cli_pid(name, process_names) is not None
    if cache is not None:
        cache[cache_key] = active
    return active


async def _periodic_cleanup() -> None:
    """Background task: clean expired tickets and blacklist entries every hour."""
    while True:
        try:
            await asyncio.sleep(3600)
            from core.api.db import write_db

            from core.api.services.ingest.ingress import cleanup_ingest_ephemeral

            async with write_db() as db:
                tickets = await cleanup_expired_tickets(db)
                blacklist = await cleanup_expired_blacklist(db)
                idem, rate, quota = await cleanup_ingest_ephemeral(db)
                if tickets or blacklist or idem or rate or quota:
                    logger.info(
                        "Cleanup: %d tickets, %d blacklist, %d idempotency, "
                        "%d rate, %d quota rows pruned",
                        tickets,
                        blacklist,
                        idem,
                        rate,
                        quota,
                    )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Periodic cleanup error")


async def _periodic_session_maintenance() -> None:
    """Background task: auto-hibernate idle sessions + persist costs. Every 2 minutes.

    Structured as gather-then-write: all external I/O (tmux, metrics parsing,
    file checks) happens OUTSIDE the DB write lock. SQL operations are collected
    as (sql, params) tuples and executed in a single write_db() batch at the end.
    Target: write lock held <100ms (was 4-12s before refactor).
    """
    io_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cost-io")
    loop = asyncio.get_running_loop()

    try:
        while True:
            try:
                await asyncio.sleep(120)  # Check every 2 minutes

                from core.api.db import acquire_db, write_db
                import time as _time

                # Pending SQL operations: list of (sql, params) tuples
                pending_ops: list[tuple[str, tuple]] = []
                active_cli_cache: dict[tuple[str, str], bool] = {}
                local_session_compatibility = (
                    settings.deploy_mode == "core"
                    and not settings.multi_tenant_enabled
                )

                if local_session_compatibility:
                    reconciliation_workspaces: list[str | None] = [None]
                else:
                    async with acquire_db() as workspace_db:
                        workspace_cursor = await workspace_db.execute(
                            "SELECT DISTINCT workspace_id FROM sessions_meta "
                            "WHERE workspace_id IS NOT NULL "
                            "AND length(trim(workspace_id)) > 0 "
                            "ORDER BY workspace_id"
                        )
                        reconciliation_workspaces = [
                            str(row["workspace_id"])
                            for row in await workspace_cursor.fetchall()
                        ]

                for workspace_scope in reconciliation_workspaces:
                    try:
                        reconciled = await sessions.reconcile_sessions_metadata(
                            workspace_scope
                        )
                        if reconciled:
                            logger.info(
                                "Session maintenance: reconciled %d metadata rows "
                                "for workspace %s",
                                reconciled,
                                workspace_scope or "local",
                            )
                    except Exception:
                        logger.exception(
                            "Session metadata reconciliation failed for workspace %s",
                            workspace_scope or "local",
                        )

                # ============================================================
                # PHASE 1: READ — fetch all needed data from DB (read pool)
                # ============================================================
                async with acquire_db() as rdb:
                    workspace_filter = (
                        ""
                        if local_session_compatibility
                        else " AND workspace_id IS NOT NULL "
                        "AND length(trim(workspace_id)) > 0"
                    )
                    cursor = await rdb.execute(
                        f"""SELECT name, workspace_id, auto_hibernate_minutes, conversation_id, project_slug, provider
                        FROM sessions_meta
                        WHERE hibernated = 0
                        AND (auto_hibernate_minutes > 0 OR auto_hibernate_minutes IS NULL)
                        {workspace_filter}"""
                    )
                    hibernate_rows = [dict(r) for r in await cursor.fetchall()]
                    if not settings.auto_hibernate_enabled:
                        hibernate_rows = []

                    detect_cursor = await rdb.execute(
                        f"""SELECT name, workspace_id, project_slug, provider FROM sessions_meta
                           WHERE hibernated = 0 AND conversation_id IS NULL
                           {workspace_filter}"""
                    )
                    detect_rows = [dict(r) for r in await detect_cursor.fetchall()]

                    cost_cursor = await rdb.execute(
                        "SELECT name, workspace_id, conversation_id, project_slug, provider "
                        "FROM sessions_meta WHERE conversation_id IS NOT NULL"
                        f"{workspace_filter}"
                    )
                    cost_rows = [dict(r) for r in await cost_cursor.fetchall()]

                    metrics_cursor = await rdb.execute(
                        f"""SELECT name, workspace_id, conversation_id, provider FROM sessions_meta
                           WHERE hibernated = 0 AND conversation_id IS NOT NULL
                           {workspace_filter}"""
                    )
                    metrics_rows = [dict(r) for r in await metrics_cursor.fetchall()]

                    if local_session_compatibility:
                        for row in (
                            hibernate_rows + detect_rows + cost_rows + metrics_rows
                        ):
                            row["workspace_id"] = (
                                str(row.get("workspace_id") or "ws_default").strip()
                                or "ws_default"
                            )

                    # PR3: Step 3 (pane-scraped working_seconds accumulator) removed.
                    # `working_seconds_msg` (migration 087) is the single source of
                    # truth — populated by parse_session via provider-specific
                    # message-gap analysis (claude_metrics, opencode_metrics).

                # ============================================================
                # PHASE 2: GATHER — all external I/O outside any DB context
                # ============================================================

                # Remote mode probes only names already owned by a concrete
                # workspace. Local OSS retains global discovery compatibility.
                registered_names = {
                    row["name"]
                    for row in hibernate_rows + detect_rows + cost_rows + metrics_rows
                }
                if local_session_compatibility:
                    statuses = (
                        await tmux.get_all_session_statuses()
                        if registered_names
                        else {}
                    )
                else:
                    status_values = await asyncio.gather(
                        *(tmux.get_session_status(name) for name in registered_names)
                    )
                    statuses = dict(zip(registered_names, status_values))

                # Cache metrics parsed during hibernate check for reuse in cost upsert
                parsed_metrics: dict[
                    tuple[str, str], claude_metrics.SessionMetrics
                ] = {}

                # --- Step 1: Auto-hibernate gather ---
                for row in hibernate_rows:
                    name = row["name"]
                    workspace_id = row["workspace_id"]
                    max_idle_min = row["auto_hibernate_minutes"] or 240
                    status = statuses.get(name)
                    row_provider = row["provider"] if row["provider"] else "claude"

                    if not await _session_has_active_cli_for_provider(
                        name, row_provider, status, active_cli_cache
                    ):
                        continue
                    mp = get_metrics_provider(row_provider)
                    if mp is None:
                        continue
                    # Auto-hibernate detection currently has Claude-only fallbacks
                    # (pane_start → JSONL timestamp). Non-Claude providers need a
                    # stored conversation_id to be auto-hibernatable. PR2 wires
                    # OpenCode detection through opencode_sessions.
                    conv_id = row["conversation_id"]
                    if not conv_id and row_provider == "claude":
                        pane_start = await tmux.get_pane_start_time(name)
                        if pane_start:
                            conv_id = claude_metrics.detect_conversation_for_session(
                                pane_start
                            )

                    if not conv_id:
                        continue

                    metrics = await loop.run_in_executor(
                        io_pool, mp.parse_session, conv_id, None
                    )
                    if metrics:
                        parsed_metrics[(workspace_id, conv_id)] = metrics

                    if not metrics or not metrics.last_timestamp:
                        continue

                    last_activity = datetime.fromisoformat(
                        metrics.last_timestamp.replace("Z", "+00:00")
                    )
                    idle_minutes = (
                        datetime.now(timezone.utc) - last_activity
                    ).total_seconds() / 60

                    if idle_minutes >= max_idle_min:
                        # Guard: check JSONL mtime
                        _jsonl_stale = True
                        try:
                            for _pdir in claude_metrics.CLAUDE_PROJECTS_DIR.iterdir():
                                _jsonl_path = _pdir / f"{conv_id}.jsonl"
                                if _jsonl_path.exists():
                                    _file_age_min = (
                                        _time.time() - _jsonl_path.stat().st_mtime
                                    ) / 60
                                    _jsonl_stale = _file_age_min >= max_idle_min
                                    break
                        except Exception:
                            pass
                        if not _jsonl_stale:
                            logger.debug(
                                "Session %s: last assistant %.0f min ago but JSONL recently written, skipping hibernate",
                                name,
                                idle_minutes,
                            )
                            continue

                        logger.info(
                            "Auto-hibernating session %s (idle %.0f min, threshold %d min)",
                            name,
                            idle_minutes,
                            max_idle_min,
                        )
                        # Send tmux keys to hibernate (slow I/O, outside DB lock)
                        await tmux.send_keys_raw(name, "C-c")
                        await asyncio.sleep(1.0)
                        await tmux.send_keys_raw(name, "C-u")
                        await asyncio.sleep(0.3)
                        await tmux.send_keys_raw(name, "/exit")
                        await asyncio.sleep(0.5)
                        await tmux.send_keys_raw(name, "Escape")
                        await asyncio.sleep(0.3)
                        await tmux.send_keys_raw(name, "Enter")
                        await asyncio.sleep(2.0)

                        now = datetime.now(timezone.utc).isoformat()
                        # PR3: drop last_context_pct from hibernate write.
                        # Real/scaled columns (migration 087) are populated by
                        # Step 2.5 a few lines below; last_context_pct_legacy
                        # stays for forensic reads only.
                        pending_ops.append(
                            (
                                """UPDATE sessions_meta SET
                                hibernated = 1, hibernated_at = ?,
                                conversation_id = ?, model = ?,
                                last_cost_usd = ?,
                                last_message_count = ?
                            WHERE name = ? AND workspace_id = ?""",
                                (
                                    now,
                                    conv_id,
                                    metrics.model,
                                    metrics.cost_usd,
                                    metrics.message_count,
                                    name,
                                    workspace_id,
                                ),
                            )
                        )

                # --- Step 1.5: Detect conversation_id gather ---
                newly_detected: dict[tuple[str, str], set[str]] = {}
                for row in detect_rows:
                    name = row["name"]
                    workspace_id = row["workspace_id"]
                    status = statuses.get(name)
                    row_provider = row["provider"] if row["provider"] else "claude"
                    if not await _session_has_active_cli_for_provider(
                        name, row_provider, status, active_cli_cache
                    ):
                        continue

                    conv_id: str | None = None

                    if row_provider == "claude":
                        pane_id = await tmux.get_pane_id(name)
                        if pane_id:
                            conv_id = await loop.run_in_executor(
                                io_pool, claude_metrics.read_pane_session_id, pane_id
                            )

                        if not conv_id:
                            claude_pid = await tmux.get_claude_pid(name)
                            if claude_pid:
                                conv_id = claude_metrics.detect_conversation_by_pid(
                                    claude_pid
                                )

                        if not conv_id:
                            pane_start = await tmux.get_pane_start_time(name)
                            if pane_start:
                                conv_id = await loop.run_in_executor(
                                    io_pool,
                                    claude_metrics.detect_conversation_for_session,
                                    pane_start,
                                )
                    elif row_provider == "opencode":
                        # Detect by pane cwd + time_created window. Needed for
                        # sessions that launched OpenCode manually (bypassing
                        # the Console "New Session" modal flow, which wires
                        # conversation_id via wait_for_new_session_id).
                        pane_cwd = await tmux.get_pane_cwd(name)
                        if pane_cwd:
                            pane_start = await tmux.get_pane_start_time(name)
                            pane_start_ms = (
                                int(pane_start * 1000) if pane_start else None
                            )
                            # Exclude ids already bound to OTHER tmux sessions
                            # so we don't steal another pane's conversation
                            # when cwds happen to coincide.
                            already_linked = [
                                r["conversation_id"]
                                for r in cost_rows
                                if r["name"] != name
                                and r["workspace_id"] == workspace_id
                                and r["conversation_id"]
                                and (r["provider"] or "claude") == "opencode"
                            ]
                            conv_id = await loop.run_in_executor(
                                io_pool,
                                opencode_sessions.detect_opencode_for_session,
                                pane_cwd,
                                pane_start_ms,
                                already_linked,
                            )
                    elif row_provider == "codex":
                        already_linked = [
                            r["conversation_id"]
                            for r in cost_rows
                            if r["name"] != name
                            and r["workspace_id"] == workspace_id
                            and r["conversation_id"]
                            and (r["provider"] or "claude") == "codex"
                        ]
                        already_linked.extend(
                            newly_detected.get((workspace_id, "codex"), set())
                        )
                        codex_pid = await tmux.get_cli_pid(
                            name, get_provider("codex").process_names
                        )
                        if codex_pid:
                            conv_id = await loop.run_in_executor(
                                io_pool,
                                codex_metrics.detect_codex_for_process,
                                codex_pid,
                                already_linked,
                            )
                        if not conv_id:
                            pane_start = await tmux.get_pane_start_time(name)
                            pane_cwd = await tmux.get_pane_cwd(name)
                            conv_id = await loop.run_in_executor(
                                io_pool,
                                codex_metrics.detect_codex_for_session,
                                pane_start,
                                pane_cwd,
                                already_linked,
                            )

                    if conv_id:
                        newly_detected.setdefault(
                            (workspace_id, row_provider), set()
                        ).add(conv_id)
                        if row_provider == "codex":
                            # Codex footer fields come from sessions_meta; refresh
                            # the newly linked JSONL in this maintenance cycle
                            # instead of waiting for the next 2-minute pass.
                            linked_row = {
                                "name": name,
                                "workspace_id": workspace_id,
                                "conversation_id": conv_id,
                                "project_slug": row["project_slug"],
                                "provider": row_provider,
                            }
                            cost_rows.append(linked_row)
                            metrics_rows.append(linked_row)
                        pending_ops.append(
                            (
                                "UPDATE sessions_meta SET conversation_id = ? "
                                "WHERE name = ? AND workspace_id = ?",
                                (conv_id, name, workspace_id),
                            )
                        )
                        # PR2: track into resume chain. INSERT OR IGNORE so
                        # repeated detections of the same conv_id are no-ops.
                        pending_ops.append(
                            (
                                "INSERT OR IGNORE INTO session_conversations "
                                "(workspace_id, session_name, conversation_id, ord, created_at) "
                                "VALUES (?, ?, ?, "
                                "COALESCE((SELECT MAX(ord)+1 FROM session_conversations "
                                "WHERE workspace_id=? AND session_name=?), 0), "
                                "?)",
                                (
                                    workspace_id,
                                    name,
                                    conv_id,
                                    workspace_id,
                                    name,
                                    datetime.now(timezone.utc).isoformat(),
                                ),
                            )
                        )
                        logger.info(
                            "Detected conversation_id for %s session %s: %s",
                            row_provider,
                            name,
                            conv_id,
                        )

                # --- Step 2: Cost persistence gather ---
                upserted = 0
                for row in cost_rows:
                    try:
                        conv_id = row["conversation_id"]
                        workspace_id = row["workspace_id"]
                        row_provider = row["provider"] if row["provider"] else "claude"
                        mp = get_metrics_provider(row_provider)
                        if mp is None:
                            continue
                        metrics = parsed_metrics.get((workspace_id, conv_id))
                        if not metrics:
                            metrics = await loop.run_in_executor(
                                io_pool,
                                mp.parse_session,
                                conv_id,
                                None,
                            )
                        if not metrics:
                            continue

                        pending_ops.append(
                            (
                                """
                            INSERT INTO session_costs (workspace_id, conversation_id, session_name, project_slug, model,
                                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
                                cost_usd, message_count, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                            ON CONFLICT(workspace_id, conversation_id) DO UPDATE SET
                                session_name = excluded.session_name,
                                project_slug = excluded.project_slug,
                                model = excluded.model,
                                input_tokens = excluded.input_tokens,
                                output_tokens = excluded.output_tokens,
                                cache_read_tokens = excluded.cache_read_tokens,
                                cache_write_tokens = excluded.cache_write_tokens,
                                cost_usd = excluded.cost_usd,
                                message_count = excluded.message_count,
                                updated_at = excluded.updated_at
                            """,
                                (
                                    workspace_id,
                                    conv_id,
                                    row["name"],
                                    row["project_slug"],
                                    metrics.model,
                                    metrics.input_tokens,
                                    metrics.output_tokens,
                                    metrics.cache_read_tokens,
                                    metrics.cache_write_tokens,
                                    metrics.cost_usd,
                                    metrics.message_count,
                                ),
                            )
                        )
                        # PR2: dual cost (conversation vs session-cumulative).
                        # PR4: plus shadow-cost (equivalent) aggregated across
                        # the same resume chain. Session cost aggregates across
                        # `session_conversations`; equivalent remains None when
                        # no conversation in chain had known pricing (skip).
                        cost_session_total: float = metrics.cost_conversation_usd or metrics.cost_usd or 0.0
                        cost_session_equivalent: float | None = (
                            metrics.cost_session_equivalent_usd
                            if metrics.cost_session_equivalent_usd is not None
                            else metrics.cost_conversation_equivalent_usd
                        )
                        equivalent_version = metrics.cost_equivalent_pricing_version
                        cost_session_incomplete = False
                        try:
                            async with acquire_db() as _rdb:
                                (
                                    cost_session_total,
                                    equiv_total,
                                    equiv_version_db,
                                    cost_session_complete,
                                ) = await compute_cost_session_extended(
                                    _rdb,
                                    row["name"],
                                    row_provider,
                                    workspace_id=workspace_id,
                                )
                                cost_session_incomplete = not cost_session_complete
                                if equiv_total is not None:
                                    cost_session_equivalent = equiv_total
                                if equiv_version_db:
                                    equivalent_version = equiv_version_db
                        except Exception:
                            logger.debug(
                                "cost_session aggregation failed for %s (falling back to conversation-only)",
                                row["name"],
                                exc_info=True,
                            )
                            cost_session_total = metrics.cost_conversation_usd or metrics.cost_usd or 0.0
                            cost_session_incomplete = True

                        pending_ops.append(
                            (
                                """UPDATE sessions_meta SET
                                last_cost_usd = ?,
                                last_cost_conversation_usd = ?,
                                last_cost_session_usd = ?,
                                last_cost_session_incomplete = ?,
                                last_cost_conversation_equivalent_usd = ?,
                                last_cost_session_equivalent_usd = ?,
                                last_cost_equivalent_pricing_version = ?,
                                last_context_pct_real = ?,
                                last_context_pct_scaled = ?,
                                last_input_tokens = ?,
                                last_output_tokens = ?,
                                last_reasoning_tokens = ?,
                                working_seconds_msg = ?,
                                metrics_refreshed_at = datetime('now'),
                                pricing_version = ?,
                                last_message_count = ?,
                                model = COALESCE(?, model)
                            WHERE name = ? AND workspace_id = ?""",
                                (
                                    metrics.cost_usd,  # legacy alias (= conversation)
                                    metrics.cost_conversation_usd or metrics.cost_usd,
                                    cost_session_total,
                                    1 if cost_session_incomplete else 0,
                                    metrics.cost_conversation_equivalent_usd,
                                    cost_session_equivalent,
                                    equivalent_version,
                                    metrics.context_pct_real,
                                    metrics.context_pct_scaled,
                                    metrics.input_tokens,
                                    metrics.output_tokens,
                                    metrics.reasoning_tokens,
                                    metrics.working_seconds_msg,
                                    metrics.pricing_version,
                                    metrics.message_count,
                                    metrics.model,
                                    row["name"],
                                    workspace_id,
                                ),
                            )
                        )
                        upserted += 1
                    except Exception:
                        logger.exception("Cost gather failed for %s", row["name"])

                # --- Step 2.5: Refresh context_pct gather ---
                metrics_updated = 0
                for row in metrics_rows:
                    name = row["name"]
                    workspace_id = row["workspace_id"]
                    conv_id = row["conversation_id"]
                    status = statuses.get(name)
                    row_provider = row["provider"] if row["provider"] else "claude"
                    if not await _session_has_active_cli_for_provider(
                        name, row_provider, status, active_cli_cache
                    ):
                        continue
                    mp = get_metrics_provider(row_provider)
                    if mp is None:
                        continue

                    # Claude-only fast path: statusline.sh writes pane-metrics on
                    # every tick; avoids a JSONL tail read when available.
                    # PR3: write the real ratio, not the /84 auto-compact fudge.
                    # context_pct_scaled is populated by the parser (PR2) —
                    # don't duplicate it here.
                    if row_provider == "claude":
                        pane_id_for_metrics = await tmux.get_pane_id(name)
                        if pane_id_for_metrics:
                            pm = await loop.run_in_executor(
                                io_pool,
                                claude_metrics.read_pane_metrics,
                                pane_id_for_metrics,
                            )
                            if pm and pm.session_id == conv_id:
                                real_pct = min(round(pm.used_pct, 1), 100.0)
                                pending_ops.append(
                                    (
                                        "UPDATE sessions_meta SET last_context_pct_real = ? "
                                        "WHERE name = ? AND workspace_id = ?",
                                        (real_pct, name, workspace_id),
                                    )
                                )
                                metrics_updated += 1
                                continue

                    context_pct = await loop.run_in_executor(
                        io_pool, mp.get_last_context_pct, conv_id, None
                    )
                    if context_pct is not None:
                        pending_ops.append(
                            (
                                "UPDATE sessions_meta SET last_context_pct_real = ? "
                                "WHERE name = ? AND workspace_id = ?",
                                (context_pct, name, workspace_id),
                            )
                        )
                        metrics_updated += 1

                # PR3: Step 3 removed — see note in PHASE 1 query block above.

                # ============================================================
                # PHASE 3: WRITE — single batch, lock held <100ms
                # ============================================================
                if pending_ops:
                    async with write_db() as db:
                        for sql, params in pending_ops:
                            await db.execute(sql, params)
                    # write_db() auto-commits on exit

                if upserted > 0:
                    logger.info(
                        "Session maintenance: upserted %d cost records", upserted
                    )
                if metrics_updated > 0:
                    logger.info(
                        "Session maintenance: refreshed context_pct for %d sessions",
                        metrics_updated,
                    )

            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Session maintenance error")
    except asyncio.CancelledError:
        pass
    finally:
        io_pool.shutdown(wait=False)


async def _periodic_metrics_collection() -> None:
    """Background task: collect server metrics.

    System metrics (CPU/RAM/disk/network) every monitoring_metrics_interval (10s).
    Docker stats every monitoring_docker_interval (60s) — separate to avoid
    blocking the event loop with N JSON parses every 10s.
    """
    docker_counter = 0
    docker_every_n = max(
        1, settings.monitoring_docker_interval // settings.monitoring_metrics_interval
    )
    while True:
        try:
            await asyncio.sleep(settings.monitoring_metrics_interval)
            docker_counter += 1
            include_docker = docker_counter % docker_every_n == 0
            snapshot = await metrics_collector.collect_all(
                include_docker=include_docker
            )
            await metrics_collector.save_to_db(snapshot)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Metrics collection error")


async def _periodic_security_collection() -> None:
    """Background task: collect security events every 60 seconds."""
    while True:
        try:
            await asyncio.sleep(settings.monitoring_security_interval)
            events = await asyncio.to_thread(security_collector.read_new_ssh_events)
            if events:
                await security_collector.save_events_to_db(events)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Security collection error")


async def _periodic_metrics_maintenance() -> None:
    """Background task: aggregate candles and cleanup old data. Runs hourly."""
    while True:
        try:
            await asyncio.sleep(3600)
            # Aggregate BEFORE cleanup to avoid data loss
            candles = await metrics_collector.aggregate_to_candles()
            raw = await metrics_collector.cleanup_old_raw(
                hours=settings.monitoring_retention_raw_hours
            )
            old_candles = await metrics_collector.cleanup_old_candles(
                days=settings.monitoring_retention_candles_days
            )
            old_events = await security_collector.cleanup_old_events(
                days=settings.monitoring_retention_events_days
            )
            if candles or raw or old_candles or old_events:
                logger.info(
                    "Monitoring maintenance: %d candles, cleaned %d raw, %d candles, %d events",
                    candles,
                    raw,
                    old_candles,
                    old_events,
                )

        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Metrics maintenance error")


async def _periodic_reminder_check() -> None:
    """Background task: check and send task due-date reminders every 60 minutes."""
    while True:
        try:
            await asyncio.sleep(3600)
            from core.api.services.reminder_service import check_and_send_reminders

            sent = await check_and_send_reminders()
            if sent:
                logger.info("Reminder check: sent %d reminder(s)", sent)
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Reminder check error")


async def _periodic_push_delivery() -> None:
    """Background task: drain unpushed notifications and send web push every 5s."""
    from core.api.services.push_service import periodic_push_delivery

    while True:
        try:
            await asyncio.sleep(5)
            await periodic_push_delivery()
        except asyncio.CancelledError:
            # Final drain before shutdown
            try:
                await asyncio.wait_for(periodic_push_delivery(), timeout=10.0)
            except (asyncio.TimeoutError, Exception):
                pass
            break
        except Exception:
            logger.exception("Push delivery error")


async def _sync_task_pending_notifications(db, *, now: str) -> int:
    """Mark resolved task notices inside the notice's exact workspace only."""
    cursor = await db.execute(
        # Drive off the small still-pending set + idx_notifications_pending_sync
        # (migration 144). A missing exact-workspace task is treated as resolved,
        # while a foreign pending task can never keep this workspace's notice live.
        """UPDATE notifications
           SET acted_at = ?, read_at = COALESCE(read_at, ?)
           WHERE acted_at IS NULL
             AND type = 'task_pending'
             AND target_type = 'task'
             AND workspace_id IS NOT NULL
             AND length(trim(workspace_id)) > 0
             AND NOT EXISTS (
                 SELECT 1
                   FROM tasks
                  WHERE tasks.id = notifications.target_id
                    AND tasks.workspace_id = notifications.workspace_id
                    AND tasks.status = 'pending'
             )""",
        (now, now),
    )
    return max(int(cursor.rowcount or 0), 0)


async def _notifications_acted_at_sync_loop() -> None:
    """Background task: auto-mark task_pending notifications as acted/read when
    corresponding task is no longer pending. Runs every 60s to avoid inline
    UPDATE on the GET /notifications endpoint (which caused SQLITE_BUSY).

    Uses write_db() for single-writer pattern (serialized background writes).

    Sleeps FIRST to avoid grabbing _write_lock at startup when the writer is
    still settling after init_pool(). Grabbing the lock in the first tick
    caused a startup deadlock that froze all writes (login hangs, 524 gateway).
    """
    while True:
        try:
            await asyncio.sleep(60)
            from core.api.db import write_db

            async with write_db() as db:
                now = datetime.now(timezone.utc).isoformat()
                await _sync_task_pending_notifications(db, now=now)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("notifications_acted_at_sync_loop error: %s", exc)


async def _periodic_wal_checkpoint() -> None:
    """Background task: PRAGMA wal_checkpoint(TRUNCATE) every 30 minutes.

    Prevents WAL from growing unbounded (incident 2026-04-10: 68MB WAL → 960ms queries).
    Sleep-first pattern per single-writer learning 2026-04-15.
    """
    while True:
        try:
            await asyncio.sleep(1800)
            from core.api.db import wal_checkpoint

            busy, log, checkpointed = await wal_checkpoint()
            if log > 0:
                logger.info(
                    "WAL checkpoint: busy=%d log=%d checkpointed=%d",
                    busy,
                    log,
                    checkpointed,
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("WAL checkpoint error")


async def _periodic_inbox_digest() -> None:
    """Background task: run inbox digest expiry/freeze checks periodically."""
    from core.api.services.inbox_digest_jobs import run_digest_jobs_if_due

    while True:
        try:
            await asyncio.sleep(settings.inbox_digest_scheduler_interval_seconds)
            result = await run_digest_jobs_if_due()
            if result.get("status") == "ok":
                logger.info(
                    "Inbox digest cycle %s: %d visible, %d overflow, %d expired",
                    result.get("cycle_key"),
                    result.get("visible", 0),
                    result.get("overflow", 0),
                    result.get("expired", 0),
                )
            # Invariant guard: every VISIBLE item must be deepened. The 6 UTC
            # precompute is best-effort/one-shot; this sweep catches its failures
            # and items promoted into visible later in the day.
            from core.api.services.inbox_digest_deep_research import (
                sweep_visible_missing_deep_research,
            )

            sweep = await sweep_visible_missing_deep_research()
            if sweep.get("missing"):
                logger.info(
                    "Digest deep-research sweep: deepened %d/%d missing visible items",
                    sweep.get("generated", 0),
                    sweep.get("missing", 0),
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Inbox digest scheduler error")


async def _periodic_brain_jobs() -> None:
    """Background task: run Brain digest/journal cycle once per cycle_key.

    Sleep BEFORE the work (production incident 5c06a53 — sleeping after the
    lock froze login startup). The job itself is idempotent per cycle_key via
    `brain_last_cycle_key` short-circuit + lease.
    """
    from core.api.services.brain import run_brain_jobs_if_due

    while True:
        try:
            await asyncio.sleep(settings.brain_scheduler_interval_seconds)
            result = await run_brain_jobs_if_due()
            status = result.get("status")
            if status in ("ok", "partial"):
                logger.info(
                    "Brain cycle %s %s: %d events, %d journals",
                    result.get("cycle_key"),
                    status,
                    result.get("event_count", 0),
                    result.get("journal_count", 0),
                )
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("Brain scheduler error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events."""
    global \
        _cleanup_task, \
        _session_maintenance_task, \
        _metrics_task, \
        _security_task, \
        _maintenance_task, \
        _reminder_task, \
        _push_delivery_task, \
        _notifications_sync_task, \
        _inbox_digest_task, \
        _brain_jobs_task, \
        _wal_checkpoint_task, \
        _terminal_metrics_dump_task, \
        _pr_impact_sweep_task, \
        _pr_impact_gc_task

    # Startup
    logger.info("Starting Console Marvis API (env=%s)", settings.pir_env)

    # OSS local tier: mirror storage.* (db_path, projects_root) from
    # settings.yaml BEFORE anything touches settings.db_path (migrations,
    # pool init), like every other entry point (CLI, MCP server, doctor).
    # No-op when no settings file exists (the managed deployment).
    try:
        from core.api.runtime_settings import apply_marvis_settings

        apply_marvis_settings()
    except Exception:
        logger.exception("Failed to apply settings.yaml runtime settings")

    # OSS local tier: auto-provision the BYOK encryption secret so the Console
    # "Add provider key" flow works out-of-the-box (gh #36). crypto.py is
    # fail-closed on a missing BYOK_FERNET_SECRET (provider-key save → 503), and
    # `marvis init` never generated one. Gated to local single-user mode; managed
    # deploys set BYOK_FERNET_SECRET via env (a password hash is configured →
    # not local mode) and are never touched. Env always wins regardless.
    try:
        if is_local_single_user_mode() and not settings.byok_fernet_secret:
            from core.wizard.byok_vault import ensure_local_byok_fernet_secret

            settings.byok_fernet_secret = ensure_local_byok_fernet_secret()
            logger.info("Auto-provisioned local BYOK encryption secret (gh #36)")
    except Exception:
        logger.exception("Failed to auto-provision local BYOK secret")

    run_migrations()

    # Phase 1.5 D14: Phoenix client-side OTel SDK (feature flag — default OFF).
    # Set TRACING_ENABLED=true env var to enable end-to-end trace chain through
    # Mac Queue Gateway. Idempotent + safe: missing deps → log warning + skip.
    try:
        from core.api.observability.tracing import init_tracing
        init_tracing(app)
    except Exception:
        logger.exception("init_tracing failed — continuing without Phoenix")

    # Connection pool (bounded, pre-configured PRAGMAs)
    from core.api.db import init_pool

    await init_pool(size=settings.db_pool_size)

    # Kill orphaned tmux-proxy processes from previous API instance
    try:
        from core.api.terminal import kill_orphan_proxies

        await kill_orphan_proxies()
    except Exception:
        logger.warning("Failed to kill orphan tmux-proxy processes", exc_info=True)

    try:
        await tmux.configure_history_limits()
    except Exception:
        logger.warning("Failed to configure tmux history limits", exc_info=True)

    # Register SIGCHLD handler to automatically reap zombie children.
    # os.fork() children (PTY proxies) are not tracked by uvloop, so without
    # this handler SIGCHLD is unhandled and zombie processes accumulate indefinitely.
    import signal as _signal
    from core.api.terminal import reap_zombie_children

    try:
        loop = asyncio.get_event_loop()
        loop.add_signal_handler(_signal.SIGCHLD, reap_zombie_children)
        logger.info("SIGCHLD handler registered (zombie reaping active)")
    except Exception:
        logger.warning("Failed to register SIGCHLD handler", exc_info=True)

    from core.api.services.terminal_metrics import TerminalMetricsCollector
    from core.api.services.terminal_metrics_dump import terminal_metrics_background_loop

    app.state.terminal_metrics = TerminalMetricsCollector()
    app.state.terminal_metrics_by_workspace = {}
    _terminal_metrics_dump_task = asyncio.create_task(
        terminal_metrics_background_loop(
            app.state.terminal_metrics,
            collectors_by_workspace=app.state.terminal_metrics_by_workspace,
        )
    )

    # Load project directories: settings.yaml -> env -> DB row (last wins).
    import json
    import sqlite3
    from pathlib import Path as _Path

    # OSS local tier: the API launched by `marvis console` must scan the
    # configured storage.projects_root (applied from settings.yaml at the top
    # of startup) — without it /api/v1/projects returns [] right after
    # `marvis init` and the first project is invisible in the GUI. The env
    # var covers launches that pass MARVIS_PROJECTS_ROOT without a settings
    # file; the DB 'project_dirs' row below still overrides both.
    try:
        import os as _os

        env_root = _os.environ.get("MARVIS_PROJECTS_ROOT")
        if env_root:
            from core.api.routers.projects import _set_project_dirs

            _set_project_dirs([_Path(env_root).expanduser()])
            logger.info("Project dirs from MARVIS_PROJECTS_ROOT: %s", env_root)
    except Exception:
        logger.exception("Failed to apply MARVIS_PROJECTS_ROOT project root")

    try:
        conn = sqlite3.connect(settings.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'project_dirs'"
        ).fetchone()
        conn.close()
        if row:
            from core.api.routers.projects import _set_project_dirs

            dirs = [_Path(d).expanduser() for d in json.loads(row["value"])]
            _set_project_dirs(dirs)
            logger.info("Project dirs loaded from DB: %s", dirs)
    except Exception:
        logger.exception("Failed to load project dirs from DB, using defaults")

    # OSS local tier: seed a project:artifact:<slug> KG node for every registered
    # project so Universe/Cosmo shows them even before code indexing (gh #35).
    # Registration only wrote project.yaml; the KG node was created solely by
    # indexing, so a fresh install had 0 project nodes → empty graph. Idempotent
    # upsert over the finalized project dirs, best-effort, off the event loop
    # (sqlite is blocking). Gated to local mode: managed deploys run the full
    # populator out-of-band and may have thousands of projects.
    try:
        if is_local_single_user_mode():
            from core.api.routers.projects import PROJECT_DIRS
            from core.scripts.populate_project_nodes import seed_project_nodes_only

            seeded = 0
            for _root in PROJECT_DIRS:
                seeded += await asyncio.to_thread(
                    seed_project_nodes_only, settings.db_path, _root
                )
            if seeded:
                logger.info("Seeded %d project KG node(s) for Cosmo (gh #35)", seeded)
    except Exception:
        logger.exception("Failed to seed project KG nodes")

    canary_mode = settings.pir_instance == "canary"

    _cleanup_task = asyncio.create_task(_periodic_cleanup())
    _session_maintenance_task = asyncio.create_task(_periodic_session_maintenance())
    if canary_mode:
        logger.info(
            "[canary] skipping reminder, notification sync, inbox digest, brain jobs"
        )
    else:
        _reminder_task = asyncio.create_task(_periodic_reminder_check())
        _notifications_sync_task = asyncio.create_task(
            _notifications_acted_at_sync_loop()
        )
        _inbox_digest_task = asyncio.create_task(_periodic_inbox_digest())
        # Brain v1.0.1: register the five canonical source collectors
        # BEFORE spawning _periodic_brain_jobs so the first tick already
        # sees them. Idempotent — safe across lifespan restarts.
        from core.api.services.brain.sources import register_all_collectors

        register_all_collectors()
        _brain_jobs_task = asyncio.create_task(_periodic_brain_jobs())
    _wal_checkpoint_task = asyncio.create_task(_periodic_wal_checkpoint())

    # Web Push delivery (outbox pattern)
    if canary_mode and settings.vapid_private_key:
        logger.info("[canary] skipping web push delivery")
    elif settings.vapid_private_key:
        from core.api.services.push_service import init_push_session

        await init_push_session()
        _push_delivery_task = asyncio.create_task(_periodic_push_delivery())
        logger.info("Push delivery started (interval=5s)")

    # Embedding backend client (remote backend when present + configured,
    # else the in-process local engine). The operator kill-switch lives inside
    # the optional remote backend module, so we ask it rather than config.
    from core.api.services.embedding_service import init_embedding_client
    from core.api.services.embedding_backends import load_remote_backend

    _remote = load_remote_backend()
    if _remote is not None and _remote.is_disabled():
        logger.info("[canary] skipping remote embedding client initialization")
    else:
        init_embedding_client()

    # Monitoring collectors
    await metrics_collector.start()
    if canary_mode:
        logger.info("[canary] skipping security collector")
    else:
        await security_collector.start()
    _metrics_task = asyncio.create_task(_periodic_metrics_collection())
    if not canary_mode:
        _security_task = asyncio.create_task(_periodic_security_collection())
    _maintenance_task = asyncio.create_task(_periodic_metrics_maintenance())
    logger.info("Monitoring collectors started")

    # KG PR-Impact sub-01 D4 + D8: dispatcher sweep + webhook GC.
    # Both honored only when the feature flag is not 'off' so we don't run
    # background work for an inert pipeline.
    pr_impact_enabled = getattr(settings, "pr_impact_enabled", "shadow")
    if pr_impact_enabled != "off" and not canary_mode:
        from core.api.services.pr_impact_pipeline.dispatcher import (
            periodic_pr_impact_sweep,
            restart_replay,
        )
        from core.api.services.pr_impact_pipeline.gc import periodic_webhook_gc

        try:
            await restart_replay(settings.db_path)
        except Exception:
            logger.exception("pr_impact restart_replay failed — continuing")
        _pr_impact_sweep_task = asyncio.create_task(
            periodic_pr_impact_sweep(settings.db_path)
        )
        _pr_impact_gc_task = asyncio.create_task(
            periodic_webhook_gc(settings.db_path)
        )
        logger.info(
            "pr_impact background tasks started (mode=%s)", pr_impact_enabled
        )

    logger.info("API ready")

    yield

    # Shutdown
    logger.info("Shutting down...")
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
    if _session_maintenance_task:
        _session_maintenance_task.cancel()
        try:
            await _session_maintenance_task
        except asyncio.CancelledError:
            pass
    if _reminder_task:
        _reminder_task.cancel()
        try:
            await _reminder_task
        except asyncio.CancelledError:
            pass
    if _notifications_sync_task:
        _notifications_sync_task.cancel()
        try:
            await _notifications_sync_task
        except asyncio.CancelledError:
            pass
    if _inbox_digest_task:
        _inbox_digest_task.cancel()
        try:
            await _inbox_digest_task
        except asyncio.CancelledError:
            pass
    if _brain_jobs_task:
        _brain_jobs_task.cancel()
        try:
            await _brain_jobs_task
        except asyncio.CancelledError:
            pass
    if _wal_checkpoint_task:
        _wal_checkpoint_task.cancel()
        try:
            await _wal_checkpoint_task
        except asyncio.CancelledError:
            pass
    if _terminal_metrics_dump_task:
        _terminal_metrics_dump_task.cancel()
        try:
            await _terminal_metrics_dump_task
        except asyncio.CancelledError:
            pass

    # Stop push delivery
    if _push_delivery_task:
        _push_delivery_task.cancel()
        try:
            await _push_delivery_task
        except asyncio.CancelledError:
            pass
        from core.api.services.push_service import close_push_session

        await close_push_session()

    # Stop monitoring collectors
    for task in (_metrics_task, _security_task, _maintenance_task):
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    await metrics_collector.stop()
    await security_collector.stop()

    # Close connection pool
    from core.api.db import close_pool

    await close_pool()

    # Cleanup terminal sessions if terminal module loaded
    try:
        from core.api.terminal import session_manager

        await session_manager.cleanup_all()
    except ImportError:
        pass

    logger.info("Shutdown complete")


# Shared limiter instance (defined in api.rate_limit to avoid circular imports)
from core.api.rate_limit import limiter  # noqa: E402

app = FastAPI(
    title="Console Marvis API",
    version="1.0.0",
    docs_url="/docs" if settings.expose_openapi else None,
    redoc_url="/redoc" if settings.expose_openapi else None,
    openapi_url="/openapi.json" if settings.expose_openapi else None,
    lifespan=lifespan,
)

# slowapi: attach limiter to app state + register 429 handler
app.state.limiter = limiter
try:
    from core.api.tenant_handoff_runtime import configure_tenant_handoff  # noqa: E402
except ImportError:  # Hosted edge handoff, absent in public projection
    configure_tenant_handoff = None

if configure_tenant_handoff is not None:
    configure_tenant_handoff(app)
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["*"],
)

# Phase 6.5: audit_log row per MCP tool call (allowlist in middleware).
from core.api.middleware.tool_call_audit import MCPToolCallAuditMiddleware  # noqa: E402
app.add_middleware(MCPToolCallAuditMiddleware)


@app.middleware("http")
async def security_headers(request: Request, call_next) -> Response:
    """Add security headers to all responses."""
    if is_local_single_user_mode() and not is_loopback_request(request):
        return JSONResponse(
            status_code=403,
            content={
                "detail": (
                    "Local single-user mode only accepts loopback requests. "
                    "Bind the API to 127.0.0.1 or set MARVIS_ADMIN_PASSWORD_HASH "
                    "before exposing it on a network interface."
                )
            },
        )

    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    if settings.is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    apply_ui_response_headers(request, response)
    return response


# Routers
app.include_router(auth.router, prefix="/api/v1")
app.include_router(auth.handoff_router)
app.include_router(sessions.router, prefix="/api/v1")
app.include_router(tasks_router)
app.include_router(todos_router)
app.include_router(delegations_router)
app.include_router(projects_router)
app.include_router(costs_router)
app.include_router(comments_router)
app.include_router(status_updates_router)
app.include_router(settings_router)
app.include_router(admin_settings_router)
app.include_router(files_router)
app.include_router(terminal_metrics_router)
app.include_router(terminal_router)
app.include_router(monitoring_router)
app.include_router(kg_router)
app.include_router(agent_router)
app.include_router(tags_router)
app.include_router(finder_router)
app.include_router(share_manage_router)
app.include_router(shared_file_router)
app.include_router(repo_share_router)
app.include_router(shared_repo_router)
app.include_router(pull_requests_router)
app.include_router(users_router)
app.include_router(raci_router)
app.include_router(audit_router)
app.include_router(webhooks_router)
app.include_router(admin_pr_impact_router)
app.include_router(pr_impact_router)
app.include_router(agent_tokens_router)
app.include_router(handoffs_router)
app.include_router(teams_router)
app.include_router(learnings_router)
app.include_router(graph_router)
app.include_router(graph_ingest_router)
app.include_router(notifications_router)
app.include_router(onboarding_router)
app.include_router(push_router)
app.include_router(ci_checks_router)
app.include_router(search_router)
app.include_router(documents_router)
app.include_router(docs_coverage_router)
app.include_router(docs_governance_router)
app.include_router(judge_router)
app.include_router(inbox_router)
app.include_router(ingest_triage_router)
app.include_router(ingest_api_keys_router)
app.include_router(llm_config_router)
app.include_router(retired_integrations_router)
if gui_events_router is not None:
    app.include_router(gui_events_router)
if newsletter_router is not None:
    app.include_router(newsletter_router)
else:
    logger.info("newsletter router absent (SaaS-only module) — skipping")
app.include_router(bench_router)
app.include_router(app_settings_router)
if hosted_account_router is not None:
    app.include_router(hosted_account_router)
app.include_router(brain_router)
app.include_router(brain_directions_router)

mount_ui(app)


def _cors_headers_for(request: Request) -> dict[str, str]:
    """Replicate CORSMiddleware logic for error responses.

    Starlette CORSMiddleware does NOT add headers to responses generated
    from unhandled exceptions (encode/starlette#1175). Without these, the
    browser reports a misleading 'CORS error' for every 500/422/404.
    """
    origin = request.headers.get("origin", "")
    allowed = settings.cors_origins or []
    if "*" in allowed or origin in allowed:
        return {
            "Access-Control-Allow-Origin": origin or "*",
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }
    return {}


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler_cors(request: Request, exc: StarletteHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=_cors_headers_for(request),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler_cors(
    request: Request, exc: RequestValidationError
):
    # jsonable_encoder handles non-JSON-serializable items inside exc.errors()
    # (e.g. ValueError/ctx objects from Pydantic custom validators).
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors())},
        headers=_cors_headers_for(request),
    )


@app.exception_handler(Exception)
async def global_exception_handler_cors(request: Request, exc: Exception):
    logger.exception(
        "Unhandled exception on %s %s: %s",
        request.method,
        request.url.path,
        exc,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "type": type(exc).__name__},
        headers=_cors_headers_for(request),
    )


@app.exception_handler(ServiceError)
async def service_error_handler(request: Request, exc: ServiceError):
    """Map any domain ServiceError escaping a router to the right HTTP status.

    Makes the incremental use_cases extraction (S1) regression-proof: even a
    not-yet-refactored router that surfaces a ServiceError gets the correct
    status + {code, message} body via this single handler.
    """
    return JSONResponse(
        status_code=exc.http_status,
        content={"code": exc.code, "message": exc.message},
        headers=_cors_headers_for(request),
    )


def _brain_fingerprint() -> str | None:
    """Short stable id of the served DB (sha256 of the resolved db_path).

    Lets `marvis console` detect a surviving API instance that serves a
    DIFFERENT brain than the current settings and restart it instead of
    silently showing stale data (gh issue #16). A hash, not the raw path:
    /health is unauthenticated.
    """
    import hashlib
    from pathlib import Path

    try:
        resolved = str(Path(settings.db_path).expanduser().resolve())
    except OSError:
        return None
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    """Liveness probe — returns service status, version and brain fingerprint."""
    payload = {"status": "ok", "version": app.version}
    fingerprint = _brain_fingerprint()
    if fingerprint:
        payload["brain"] = fingerprint
    return payload


# Brain v1 — WebSocket endpoint (sub-05 S5, §4.16).
# Emits `marvisx:brain_cycle_changed` payloads to subscribed clients after
# each cycle phase. Visibility is enforced at emit time, per-subscriber.
async def _resolve_ws_user(ws: WebSocket):
    """Authenticate a /ws/brain client via cookie OR Bearer token."""
    from core.api.db import acquire_db
    from core.api.security import (
        TokenPrincipalInvalid,
        TokenStoreUnavailable,
        _legacy_shared_token_enabled,
        _lookup_agent_token,
        _resolve_agent_userinfo,
    )
    from core.api.security import verify_session_jwt

    # 1. Bearer (agents) — header OR `?token=` query (browsers can't set WS headers)
    auth_header = ws.headers.get("authorization", "")
    bearer_token: str | None = None
    if auth_header.startswith("Bearer "):
        bearer_token = auth_header[7:]
    else:
        token_qp = ws.query_params.get("token")
        if token_qp:
            bearer_token = token_qp

    if bearer_token:
        async with acquire_db() as db:
            db.row_factory = aiosqlite.Row
            try:
                resolved = await _lookup_agent_token(bearer_token, db)
            except TokenStoreUnavailable:
                return None
            if resolved is not None:
                try:
                    return await _resolve_agent_userinfo(
                        resolved.agent_name,
                        db,
                        list(resolved.scopes),
                        resolved.workspace_id,
                        resolved.principal_id,
                    )
                except TokenPrincipalInvalid:
                    return None
            if (
                _legacy_shared_token_enabled()
                and settings.tasks_api_token
                and secrets.compare_digest(bearer_token, settings.tasks_api_token)
            ):
                return await _resolve_agent_userinfo(
                    "agent", db, workspace_id="ws_default"
                )
        return None

    # 2. Cookie (Console Marvis web)
    cookie_token = ws.cookies.get("pir_session")
    if not cookie_token:
        return None
    try:
        payload = verify_session_jwt(cookie_token)
    except Exception:
        return None
    slug = payload.get("sub")
    if not slug:
        return None
    async with acquire_db() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, system_role, display_name, workspace_id FROM users "
            "WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        ) as cur:
            row = await cur.fetchone()
    if not row:
        return None
    from core.api.models import UserInfo

    workspace_id = str(row["workspace_id"] or "").strip()
    claimed_workspace = str(payload.get("workspace_id") or "").strip()
    if not workspace_id or (claimed_workspace and claimed_workspace != workspace_id):
        return None

    return UserInfo(
        username=slug,
        user_id=row["id"],
        system_role=row["system_role"],
        display_name=row["display_name"],
        workspace_id=workspace_id,
    )


@app.websocket("/ws/brain")
async def brain_websocket(ws: WebSocket) -> None:
    """Brain v1 — real-time cycle phase events.

    Auth via cookie OR Bearer (header / `?token=`). Visibility is applied at
    emit time per-subscriber — admins/super_admins bypass. Heartbeat ping
    every 30s; reconnect is client-side (no server replay buffer in v1).
    """
    from core.api.services.brain.ws_emitter import Subscriber, _serialize, get_hub

    await ws.accept()
    user = await _resolve_ws_user(ws)
    if user is None:
        await ws.send_text(
            _serialize({"type": "error", "code": "unauthorized"})
        )
        await ws.close(code=4401)
        return

    is_unrestricted = user.system_role in ("admin", "super_admin")
    visible_projects: set[str] = set()
    if not is_unrestricted:
        try:
            from core.api.db import acquire_db
            from core.api.visibility import get_visible_projects

            async with acquire_db() as db:
                db.row_factory = aiosqlite.Row
                visible_projects = set(
                    await get_visible_projects(
                        db,
                        user,
                        user.workspace_id,
                    )
                )
        except Exception:
            logger.exception("brain ws: visible_projects resolution failed")

    subscriber = Subscriber(
        ws=ws,
        user_id=user.user_id or user.username or "",
        system_role=user.system_role,
        workspace_id=user.workspace_id,
        visible_projects=visible_projects,
        is_unrestricted=is_unrestricted,
    )
    hub = get_hub()
    await hub.register(subscriber)

    try:
        await ws.send_text(
            _serialize({
                "type": "marvisx:brain_subscribed",
                "user_id": subscriber.user_id,
                "role": subscriber.system_role,
            })
        )

        async def _send_loop() -> None:
            while True:
                payload = await subscriber.queue.get()
                await ws.send_text(_serialize(payload))

        async def _heartbeat_loop() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    await ws.send_text(_serialize({"type": "ping"}))
                except Exception:
                    return

        async def _recv_loop() -> None:
            while True:
                # The client may send pongs / explicit close frames. Discard
                # text frames; on disconnect WebSocketDisconnect bubbles up.
                await ws.receive_text()

        send_task = asyncio.create_task(_send_loop())
        heartbeat_task = asyncio.create_task(_heartbeat_loop())
        recv_task = asyncio.create_task(_recv_loop())

        done, pending = await asyncio.wait(
            {send_task, heartbeat_task, recv_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        for task in done:
            exc = task.exception()
            if exc and not isinstance(exc, (WebSocketDisconnect, asyncio.CancelledError)):
                logger.exception(
                    "brain ws: task ended with error", exc_info=exc
                )
    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("brain ws: unhandled error")
    finally:
        await hub.unregister(subscriber)
        try:
            await ws.close()
        except Exception:
            pass


@app.get("/health/ocr", tags=["health"])
async def health_ocr() -> dict:
    """OCR backend health (P1.5.E1).

    Reports whether the tesseract system binary is callable with the Italian
    language pack installed. PaddleOCR-VL lives upstream on the Mac Gateway
    (tier-ocr/tier-docparse) and is monitored separately. Used as post-deploy
    gate to verify the local OCR pipeline never silently regresses to
    ``parser_used=null``.
    """
    import subprocess

    result = {"tesseract": False, "italian_lang": False}
    try:
        completed = await asyncio.to_thread(
            subprocess.run,
            ["tesseract", "--list-langs"],
            capture_output=True,
            timeout=5,
        )
        result["tesseract"] = completed.returncode == 0
        result["italian_lang"] = b"ita" in (completed.stdout or b"")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    except Exception:
        logger.exception("Unexpected error probing tesseract")
    return result
