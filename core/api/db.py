# v1.6.0 - 2026-03-15 - Enterprise prerequisites: _column_exists, backup, connection pool + acquire_db() context manager
from __future__ import annotations

import asyncio
import glob
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import time
import uuid as uuid_mod
from collections import Counter, defaultdict, deque
from collections.abc import AsyncGenerator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

import aiosqlite
from starlette.requests import Request

from core.api.config import settings
from core.api.paths import repo_path
from core.platform.locking import exclusive_file_lock

logger = logging.getLogger(__name__)


def _resolve_migrations_dir() -> Path:
    """Locate the ``migrations`` data directory in both layouts.

    In an installed wheel the ``*.sql`` files are shipped as the top-level
    ``migrations`` package-data; ``importlib.resources.files`` resolves them
    correctly regardless of where site-packages lives. ``repo_path`` walking up
    from ``__file__`` only works when the runtime tree mirrors the repo layout
    (learning 9e527cfa: the wheel shipped 0 migrations and the runtime found an
    empty dir without failing). Prefer the resources lookup, fall back to the
    repo layout for an editable/source checkout.
    """
    try:
        import importlib.resources as _res

        candidate = Path(str(_res.files("migrations")))
        if candidate.is_dir():
            return candidate
    except (ModuleNotFoundError, FileNotFoundError, TypeError, AttributeError):
        pass
    return repo_path(__file__, "migrations")


MIGRATIONS_DIR = _resolve_migrations_dir()

# Migration runner hardening (IMPL §C, plan 2026-07-06 dockerization enterprise).
# The runner must survive an UNSUPERVISED boot on a customer box: fail-loud on
# anything it does not recognize instead of guessing an execution order.

# Allowlist for UP migration stems: NNN_name (digits, then a name of word chars
# and dashes). Anything else in the migrations dir is an error, not a skip.
MIGRATION_STEM_RE = re.compile(r"^\d+_[A-Za-z0-9][A-Za-z0-9_-]*$")

# Subdirectory + filename prefix deliberately OUTSIDE every rotation glob
# (runner legacy `<db>.backup-*`, backup-db.sh `<db>.backup-<tag>-<ts>`,
# cleanup-disk.sh non-recursive `console.db.backup-*`): the pre-update backup is
# the ONLY rollback point after a failed upgrade (P0-1) — nothing may evict it.
PRE_UPDATE_BACKUP_SUBDIR = "pre-update"
PRE_UPDATE_BACKUP_KEEP = 2

# These versions install write-rejecting security contracts. Applying them
# while an older process still writes the same SQLite file would break that
# process mid-rollout. Existing databases therefore require an explicit
# operator assertion that all writers are stopped; fresh databases have no
# predecessor process and may bootstrap normally.
QUIESCED_REQUIRED_MIGRATIONS = frozenset(
    {176, 177, 179, 180, 181, 182, 183, 185, 186, 187}
)
QUIESCED_MIGRATION_ENV = "MARVIS_SCHEMA_WRITERS_QUIESCED"


@dataclass(frozen=True)
class MigrationResult:
    """Machine-readable evidence from one migration runner invocation."""

    initial_version: int
    final_version: int
    code_max_version: int
    applied_versions: tuple[int, ...]
    repaired_versions: tuple[int, ...]
    backup_path: str | None


def _require_security_migration_quiescence(
    current_version: int,
    pending_versions: set[int],
    *,
    fresh_database: bool,
    env: dict[str, str] | None = None,
) -> None:
    if (current_version == 0 and fresh_database) or not (
        pending_versions & QUIESCED_REQUIRED_MIGRATIONS
    ):
        return
    source = os.environ if env is None else env
    if source.get(QUIESCED_MIGRATION_ENV, "").strip() == "1":
        return
    versions = ", ".join(
        str(version)
        for version in sorted(pending_versions & QUIESCED_REQUIRED_MIGRATIONS)
    )
    raise RuntimeError(
        "Security migrations require all SQLite writers to be stopped "
        f"(pending: {versions}). Run `marvis schema upgrade` locally or the "
        "managed receipt-backed upgrade command; refusing an online rolling "
        "upgrade."
    )


def _is_rollback_stem(stem: str) -> bool:
    """True for any rollback artifact: `_down`, `_down_v2`, `_rollback`, ... (substring, not suffix)."""
    lowered = stem.lower()
    return "_down" in lowered or "_rollback" in lowered


def _migration_version(path: Path) -> int:
    return int(path.stem.split("_")[0])


def discover_up_migrations(migrations_dir: Path | None = None) -> list[Path]:
    """UP migrations in execution order, allowlisted by stem (F8).

    Rollback stems are skipped; any other ``.sql`` that does not match
    ``NNN_name`` raises (a `002_x_rollback.sql` or stray `notes.sql` would
    otherwise run as an UP on the next boot). An empty dir raises too —
    the wheel once shipped 0 migrations and the runtime booted on an
    unmigrated schema without failing (learning 9e527cfa).
    """
    directory = Path(migrations_dir) if migrations_dir is not None else MIGRATIONS_DIR
    ups: list[Path] = []
    unrecognized: list[str] = []
    for path in sorted(directory.glob("*.sql")):
        stem = path.stem
        if _is_rollback_stem(stem):
            continue
        if MIGRATION_STEM_RE.match(stem):
            ups.append(path)
        else:
            unrecognized.append(path.name)
    if unrecognized:
        raise RuntimeError(
            f"Unrecognized migration file(s) in {directory}: {', '.join(sorted(unrecognized))}. "
            "Expected NNN_name.sql (rollbacks contain '_down'/'_rollback'); refusing to guess "
            "an execution order."
        )
    if not ups:
        raise RuntimeError(
            f"No UP migrations found in {directory} — packaging is broken (learning 9e527cfa); "
            "refusing to boot on an unmigrated schema."
        )
    return ups


def code_max_version(migration_files: list[Path]) -> int:
    return max(_migration_version(f) for f in migration_files)


# Spot checks on the REAL schema (learning 79ce177f): a forward-only MAX() can
# report "current" on a database whose actual DDL silently diverged. Each entry
# is (version that introduced it, table, column); verified when the DB claims to
# be at code max.
SCHEMA_ASSERTIONS: tuple[tuple[int, str, str], ...] = (
    (16, "users", "system_role"),
    (63, "tasks", "completion_mode"),
    (162, "documents", "confidential"),
    (168, "gui_events", "registri_count"),
    (171, "product_event_outbox", "next_attempt_at"),
    (175, "audit_log", "workspace_id"),
    (175, "audit_log", "workspace_sequence"),
    (175, "audit_log", "previous_hash"),
    (175, "audit_log", "entry_hash"),
    (175, "audit_log", "hash_version"),
    (175, "audit_chain_state", "legacy_root_hash"),
    (178, "agent_tokens", "principal_id"),
    (178, "agent_tokens", "expires_at"),
    (178, "agent_tokens", "revoked_at"),
    (178, "agent_tokens", "rotation_family_id"),
    (178, "agent_tokens", "credential_kind"),
    (179, "access_grants", "workspace_id"),
    (179, "file_meta", "workspace_id"),
    (180, "user_provisioning_queue", "workspace_id"),
    (181, "teams", "workspace_id"),
    (182, "todos", "workspace_id"),
    (183, "ingest_pending", "workspace_id"),
    (183, "ingest_skipped", "workspace_id"),
    (183, "ingest_change_history", "workspace_id"),
    (183, "ingest_webhook_nonces", "workspace_id"),
    (185, "session_costs", "workspace_id"),
    (185, "session_conversations", "workspace_id"),
    (185, "task_cost_entries_v185_quarantine", "quarantine_reason"),
    (186, "session_operation_leases", "generation"),
    (187, "project_lifecycle_state", "project_id"),
    (187, "project_lifecycle_state", "writer_watermark"),
    (187, "project_lifecycle_bootstrap", "snapshot_digest"),
    (187, "cloud_f_control", "change_epoch"),
    (187, "cloud_f_active_operations", "lease_generation"),
    (187, "cloud_f_change_operations", "result_epoch"),
    (187, "project_archive_approvals", "expected_project_digest"),
    (187, "project_lifecycle_operations", "request_digest"),
    (187, "project_lifecycle_operations", "actor"),
    (187, "governed_decisions", "body_digest"),
    (187, "governed_decisions", "created_by"),
    (187, "decision_lifecycle_operations", "primary_project_slug"),
    (187, "decision_lifecycle_operations", "cloud_f_epoch"),
    (187, "decision_lifecycle_operations", "request_json"),
    (187, "historical_artifact_pointers", "source_kind"),
)

SCHEMA_EXACT_COLUMN_ASSERTIONS: tuple[
    tuple[int, str, dict[str, tuple[str, bool, str | None, int]]], ...
] = (
    (
        185,
        "task_cost_entries_v185_quarantine",
        {
            "id": ("TEXT", False, None, 1),
            "task_id": ("TEXT", True, None, 0),
            "entry_type": ("TEXT", True, None, 0),
            "source": ("TEXT", True, None, 0),
            "conversation_id": ("TEXT", False, None, 0),
            "pr_id": ("TEXT", False, None, 0),
            "cost_usd_delta": ("REAL", True, None, 0),
            "token_markup_factor": ("REAL", True, None, 0),
            "agent_seconds": ("INTEGER", True, None, 0),
            "agent_cost_rate": ("REAL", True, None, 0),
            "agent_bill_rate": ("REAL", True, None, 0),
            "human_minutes": ("REAL", True, None, 0),
            "human_cost_rate": ("REAL", True, None, 0),
            "human_bill_rate": ("REAL", True, None, 0),
            "total_cost_usd": ("REAL", True, None, 0),
            "total_bill_usd": ("REAL", True, None, 0),
            "is_billable": ("INTEGER", True, None, 0),
            "billable_reason": ("TEXT", False, None, 0),
            "billing_notes": ("TEXT", False, None, 0),
            "idempotency_key": ("TEXT", False, None, 0),
            "description": ("TEXT", False, None, 0),
            "created_by": ("TEXT", True, None, 0),
            "created_at": ("TEXT", True, None, 0),
            "observed_task_workspace_id": ("TEXT", False, None, 0),
            "quarantine_reason": ("TEXT", True, None, 0),
            "quarantined_at": ("TEXT", True, "datetime('now','utc')", 0),
        },
    ),
)

# Exact index contracts for workspace-owned schemas that cannot be represented
# by a column-only spot check. Each entry is:
# (version, table, index, unique, ((column, descending), ...), WHERE predicate).
# The contract is deliberately read-only: a claimed version with malformed DDL
# must stop startup rather than silently rebuilding an index under live writers.
SCHEMA_INDEX_ASSERTIONS: tuple[
    tuple[
        int,
        str,
        str,
        bool,
        tuple[tuple[str, bool], ...],
        str | None,
    ],
    ...,
] = (
    (
        180,
        "user_provisioning_queue",
        "idx_upq_workspace_email_pending",
        True,
        (("workspace_id", False), ("email", False)),
        "status = 'queued' and workspace_id is not null",
    ),
    (
        180,
        "user_provisioning_queue",
        "idx_upq_workspace_status_created",
        False,
        (
            ("workspace_id", False),
            ("status", False),
            ("created_at", False),
        ),
        None,
    ),
    (
        180,
        "user_provisioning_queue",
        "idx_upq_workspace_requester_created",
        False,
        (
            ("workspace_id", False),
            ("requester_id", False),
            ("created_at", True),
        ),
        None,
    ),
    (
        181,
        "teams",
        "idx_teams_workspace_slug",
        True,
        (("workspace_id", False), ("slug", False)),
        None,
    ),
    (
        181,
        "teams",
        "idx_teams_workspace",
        False,
        (("workspace_id", False),),
        None,
    ),
    (
        182,
        "todos",
        "idx_todos_workspace_open",
        False,
        (
            ("workspace_id", False),
            ("status", False),
            ("fu", False),
            ("created_at", True),
        ),
        None,
    ),
    (
        182,
        "todos",
        "idx_todos_workspace_project",
        False,
        (
            ("workspace_id", False),
            ("project", False),
            ("fu", False),
            ("created_at", True),
        ),
        None,
    ),
    (
        182,
        "todos",
        "idx_todos_workspace_source_ref",
        True,
        (
            ("workspace_id", False),
            ("source", False),
            ("source_ref", False),
        ),
        "source_ref is not null",
    ),
    (
        183,
        "ingest_pending",
        "idx_ingest_pending_workspace_sha_project",
        True,
        (
            ("workspace_id", False),
            ("sha256", False),
            ("project_slug", False),
        ),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_pending",
        "idx_ingest_pending_workspace_status",
        False,
        (
            ("workspace_id", False),
            ("status", False),
            ("created_at", True),
        ),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_pending",
        "idx_ingest_pending_workspace_project",
        False,
        (
            ("workspace_id", False),
            ("project_slug", False),
            ("created_at", True),
        ),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_pending",
        "idx_ingest_pending_workspace_created",
        False,
        (("workspace_id", False), ("created_at", True)),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_pending",
        "idx_ingest_pending_workspace_lock",
        False,
        (("workspace_id", False), ("locked_at", False)),
        "workspace_id is not null and locked_at is not null",
    ),
    (
        183,
        "ingest_pending",
        "idx_ingest_pending_workspace_source",
        False,
        (("workspace_id", False), ("source", False)),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_pending",
        "idx_ingest_pending_workspace_api_key",
        False,
        (("workspace_id", False), ("api_key_id", False)),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_skipped",
        "idx_ingest_skipped_workspace_project_created",
        False,
        (
            ("workspace_id", False),
            ("project_slug", False),
            ("created_at", True),
        ),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_skipped",
        "idx_ingest_skipped_workspace_sha256",
        False,
        (
            ("workspace_id", False),
            ("sha256", False),
            ("project_slug", False),
        ),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_change_history",
        "idx_change_hist_workspace_ingest_id",
        False,
        (
            ("workspace_id", False),
            ("ingest_pending_id", False),
            ("changed_at", True),
        ),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_webhook_nonces",
        "idx_ingest_webhook_nonces_workspace_source_nonce",
        True,
        (
            ("workspace_id", False),
            ("source", False),
            ("nonce", False),
        ),
        "workspace_id is not null",
    ),
    (
        183,
        "ingest_webhook_nonces",
        "idx_ingest_webhook_nonces_workspace_received",
        False,
        (("workspace_id", False), ("received_at", True)),
        "workspace_id is not null",
    ),
    (
        185,
        "sessions_meta",
        "idx_sessions_workspace_name_unique",
        True,
        (("workspace_id", False), ("name", False)),
        None,
    ),
    (
        185,
        "session_costs",
        "idx_session_costs_workspace_project_updated",
        False,
        (
            ("workspace_id", False),
            ("project_slug", False),
            ("updated_at", False),
        ),
        "workspace_id is not null",
    ),
    (
        185,
        "session_costs",
        "idx_session_costs_workspace_session",
        False,
        (
            ("workspace_id", False),
            ("session_name", False),
            ("updated_at", False),
        ),
        "workspace_id is not null and session_name is not null",
    ),
    (
        185,
        "session_costs",
        "idx_session_costs_workspace_completed",
        False,
        (("workspace_id", False), ("completed_at", False)),
        "workspace_id is not null and completed_at is not null",
    ),
    (
        185,
        "session_costs",
        "idx_session_costs_conversation_lookup",
        False,
        (("conversation_id", False),),
        None,
    ),
    (
        185,
        "session_conversations",
        "idx_session_conversations_workspace_ord",
        True,
        (
            ("workspace_id", False),
            ("session_name", False),
            ("ord", False),
        ),
        "workspace_id is not null",
    ),
    (
        185,
        "session_conversations",
        "idx_session_conversations_workspace_name",
        False,
        (
            ("workspace_id", False),
            ("session_name", False),
            ("ord", False),
        ),
        "workspace_id is not null",
    ),
    (
        185,
        "session_conversations",
        "idx_session_conversations_workspace_conversation",
        False,
        (("workspace_id", False), ("conversation_id", False)),
        "workspace_id is not null",
    ),
    (
        185,
        "task_cost_entries_v185_quarantine",
        "idx_task_cost_entries_v185_quarantine_task",
        False,
        (("task_id", False),),
        None,
    ),
    (
        186,
        "session_operation_leases",
        "idx_session_operation_leases_active",
        False,
        (("workspace_id", False), ("lease_expires_at", False)),
        "operation is not null",
    ),
)

# Write guards are part of the security shape, not optional migration helpers.
# Each entry is (version, object type, object name, normalized SQL fragments).
SCHEMA_OBJECT_ASSERTIONS: tuple[
    tuple[int, str, str, tuple[str, ...]], ...
] = (
    (
        186,
        "table",
        "session_operation_leases",
        (
            "primary key (workspace_id, session_name)",
            "generation integer not null default 0 check (generation >= 0)",
            "'complete', 'delete', 'hibernate', 'resume', 'restart'",
            "operation is null and lease_expires_at is null",
        ),
    ),
    (
        182,
        "trigger",
        "todos_workspace_required_insert",
        (
            "before insert on todos",
            "new.workspace_id is null",
            "length(trim(new.workspace_id)) = 0",
            "raise(abort, 'todo workspace_id required')",
        ),
    ),
    (
        182,
        "trigger",
        "todos_workspace_required_update",
        (
            "before update on todos",
            "new.workspace_id is null",
            "length(trim(new.workspace_id)) = 0",
            "raise(abort, 'todo workspace_id required')",
        ),
    ),
    (
        182,
        "trigger",
        "todos_workspace_immutable",
        (
            "before update of workspace_id on todos",
            "old.workspace_id is not new.workspace_id",
            "new.workspace_id is not null",
            "length(trim(new.workspace_id)) > 0",
            "raise(abort, 'todo workspace_id immutable')",
        ),
    ),
    (
        182,
        "trigger",
        "todos_historical_attribution_guard",
        (
            "before update of workspace_id on todos",
            "old.workspace_id is null",
            "new.workspace_id is not null",
            "raise(abort, 'todo workspace attribution not proven')",
        ),
    ),
) + tuple(
    (
        183,
        "trigger",
        f"{table}_workspace_required_{operation}",
        (
            f"before {operation} on {table}",
            "new.workspace_id is null",
            "length(trim(new.workspace_id)) = 0",
            f"raise(abort, '{error_prefix} workspace_id required')",
        ),
    )
    for table, error_prefix in (
        ("ingest_pending", "ingest pending"),
        ("ingest_skipped", "ingest skipped"),
        ("ingest_change_history", "ingest change history"),
        ("ingest_webhook_nonces", "ingest webhook nonce"),
    )
    for operation in ("insert", "update")
) + tuple(
    (
        183,
        "trigger",
        f"{table}_workspace_immutable",
        (
            f"before update of workspace_id on {table}",
            "old.workspace_id is not null",
            "old.workspace_id != new.workspace_id",
            f"raise(abort, '{error_prefix} workspace_id immutable')",
        ),
    )
    for table, error_prefix in (
        ("ingest_pending", "ingest pending"),
        ("ingest_skipped", "ingest skipped"),
        ("ingest_change_history", "ingest change history"),
        ("ingest_webhook_nonces", "ingest webhook nonce"),
    )
) + (
    (
        183,
        "trigger",
        "ingest_skipped_parent_workspace_insert",
        (
            "before insert on ingest_skipped",
            "when new.existing_ingest_id is not null and not exists (",
            "p.id = new.existing_ingest_id",
            "p.workspace_id = new.workspace_id",
            "raise(abort, 'ingest skipped parent workspace mismatch')",
        ),
    ),
    (
        183,
        "trigger",
        "ingest_skipped_parent_workspace_update",
        (
            "before update of workspace_id, existing_ingest_id on ingest_skipped",
            "when new.existing_ingest_id is not null and not exists (",
            "p.id = new.existing_ingest_id",
            "p.workspace_id = new.workspace_id",
            "raise(abort, 'ingest skipped parent workspace mismatch')",
        ),
    ),
    (
        183,
        "trigger",
        "ingest_change_history_parent_workspace_insert",
        (
            "before insert on ingest_change_history",
            "when not exists (",
            "p.id = new.ingest_pending_id",
            "p.workspace_id = new.workspace_id",
            "raise(abort, 'ingest change history parent workspace mismatch')",
        ),
    ),
    (
        183,
        "trigger",
        "ingest_change_history_parent_workspace_update",
        (
            "before update of workspace_id, ingest_pending_id on ingest_change_history",
            "when not exists (",
            "p.id = new.ingest_pending_id",
            "p.workspace_id = new.workspace_id",
            "raise(abort, 'ingest change history parent workspace mismatch')",
        ),
    ),
    (
        183,
        "trigger",
        "ingest_pending_api_key_workspace_insert",
        (
            "before insert on ingest_pending",
            "when new.api_key_id is not null and not exists (",
            "k.id = new.api_key_id",
            "k.workspace_id = new.workspace_id",
            "raise(abort, 'ingest pending api key workspace mismatch')",
        ),
    ),
    (
        183,
        "trigger",
        "ingest_pending_api_key_workspace_update",
        (
            "before update of workspace_id, api_key_id on ingest_pending",
            "when new.api_key_id is not null and not exists (",
            "k.id = new.api_key_id",
            "k.workspace_id = new.workspace_id",
            "raise(abort, 'ingest pending api key workspace mismatch')",
        ),
    ),
    (
        183,
        "trigger",
        "ingest_api_keys_pending_workspace_update",
        (
            "before update of workspace_id on ingest_api_keys",
            "when exists (",
            "p.api_key_id = old.id",
            "p.workspace_id != new.workspace_id",
            "raise(abort, 'ingest api key pending workspace mismatch')",
        ),
    ),
) + (
    (
        185,
        "table",
        "task_cost_entries_v185_quarantine",
        (
            "observed_task_workspace_id text",
            "quarantine_reason text not null",
            "quarantined_at text not null default (datetime('now', 'utc'))",
        ),
    ),
    (
        185,
        "trigger",
        "session_costs_workspace_required_insert",
        (
            "before insert on session_costs",
            "new.workspace_id is null",
            "count(distinct sm.workspace_id)",
            "raise(abort, 'session cost workspace_id required')",
        ),
    ),
    (
        185,
        "trigger",
        "session_costs_parent_workspace_insert",
        (
            "before insert on session_costs",
            "sm.name = new.session_name",
            "sm.workspace_id = new.workspace_id",
            "raise(abort, 'session cost parent workspace mismatch')",
        ),
    ),
    (
        185,
        "trigger",
        "session_costs_workspace_immutable",
        (
            "before update of workspace_id on session_costs",
            "old.workspace_id is not new.workspace_id",
            "raise(abort, 'session cost workspace_id immutable')",
        ),
    ),
    (
        185,
        "trigger",
        "session_conversations_workspace_required_insert",
        (
            "before insert on session_conversations",
            "new.workspace_id is null",
            "count(distinct sm.workspace_id)",
            "raise(abort, 'session conversation workspace_id required')",
        ),
    ),
    (
        185,
        "trigger",
        "session_conversations_parent_workspace_insert",
        (
            "before insert on session_conversations",
            "sm.name = new.session_name",
            "sm.workspace_id = new.workspace_id",
            "raise(abort, 'session conversation parent workspace mismatch')",
        ),
    ),
    (
        185,
        "trigger",
        "session_conversations_workspace_immutable",
        (
            "before update of workspace_id on session_conversations",
            "old.workspace_id is not new.workspace_id",
            "raise(abort, 'session conversation workspace_id immutable')",
        ),
    ),
    (
        185,
        "trigger",
        "task_cost_entries_conversation_workspace_insert",
        (
            "before insert on task_cost_entries",
            "sc.workspace_id = t.workspace_id",
            "sc.conversation_id = new.conversation_id",
            "raise(abort, 'task cost conversation workspace mismatch')",
        ),
    ),
    (
        185,
        "trigger",
        "task_cost_entries_v185_quarantine_no_insert",
        (
            "before insert on task_cost_entries_v185_quarantine",
            "raise(abort, 'task cost quarantine is immutable')",
        ),
    ),
    (
        185,
        "trigger",
        "task_cost_entries_v185_quarantine_no_update",
        (
            "before update on task_cost_entries_v185_quarantine",
            "raise(abort, 'task cost quarantine is immutable')",
        ),
    ),
    (
        185,
        "trigger",
        "task_cost_entries_v185_quarantine_no_delete",
        (
            "before delete on task_cost_entries_v185_quarantine",
            "raise(abort, 'task cost quarantine is immutable')",
        ),
    ),
    (
        185,
        "trigger",
        "pull_requests_conversation_workspace_insert",
        (
            "before insert on pull_requests",
            "sc.workspace_id = new.workspace_id",
            "sc.conversation_id = new.conversation_id",
            "raise(abort, 'pull request cost workspace mismatch')",
        ),
    ),
    (
        185,
        "trigger",
        "pull_requests_conversation_workspace_update",
        (
            "before update of workspace_id, conversation_id on pull_requests",
            "sc.workspace_id = new.workspace_id",
            "sc.conversation_id = new.conversation_id",
            "raise(abort, 'pull request cost workspace mismatch')",
        ),
    ),
    (
        187,
        "table",
        "project_lifecycle_bootstrap",
        (
            "state text not null check (state in ('pending','complete'))",
            "state = 'pending' and snapshot_digest is null",
            "state = 'complete' and snapshot_digest is not null",
        ),
    ),
    (
        187,
        "trigger",
        "project_write_events_writability_gate",
        (
            "before insert on project_write_events",
            "state.lifecycle = 'archived'",
            "state.transition_operation_id is not null",
            "raise(abort, 'project_not_writable')",
        ),
    ),
    (
        187,
        "trigger",
        "project_writes_pull_requests_insert",
        (
            "after insert on pull_requests",
            "from tasks where id=new.task_id",
            "new.project,'pull_request',new.id",
        ),
    ),
    (
        187,
        "trigger",
        "project_writes_pull_requests_update",
        (
            "after update on pull_requests",
            "from tasks where id=new.task_id",
            "new.project,'pull_request',new.id",
        ),
    ),
    (
        187,
        "trigger",
        "project_writes_pull_requests_delete",
        (
            "after delete on pull_requests",
            "from tasks where id=old.task_id",
            "old.project,'pull_request',old.id",
        ),
    ),
    (
        187,
        "trigger",
        "project_writes_pull_requests_update_old_scope",
        (
            "before update of project, workspace_id, task_id on pull_requests",
            "from tasks where id=old.task_id",
            "from tasks where id=new.task_id",
            "old.project,'pull_request_move_source',old.id",
        ),
    ),
    (
        187,
        "trigger",
        "project_writes_comment_reactions_immutable",
        (
            "before update on comment_reactions",
            "raise(abort, 'comment_reactions_immutable')",
        ),
    ),
)


def _index_matches_assertion(
    conn: sqlite3.Connection,
    *,
    table: str,
    index: str,
    unique: bool,
    columns: tuple[tuple[str, bool], ...],
    predicate: str | None,
) -> bool:
    """Return whether one SQLite index has the exact declared key contract."""
    quoted_table = '"' + table.replace('"', '""') + '"'
    quoted_index = '"' + index.replace('"', '""') + '"'
    row = next(
        (
            candidate
            for candidate in conn.execute(
                f"PRAGMA index_list({quoted_table})"
            ).fetchall()
            if str(candidate[1]) == index
        ),
        None,
    )
    if row is None:
        return False
    is_partial = predicate is not None
    if bool(row[2]) != unique or bool(row[4]) != is_partial:
        return False

    actual_columns = tuple(
        (str(detail[2]), bool(detail[3]))
        for detail in conn.execute(f"PRAGMA index_xinfo({quoted_index})").fetchall()
        if bool(detail[5])
    )
    if actual_columns != columns:
        return False

    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        (index,),
    ).fetchone()
    sql = str(sql_row[0]) if sql_row and sql_row[0] else ""
    match = re.search(r"\bWHERE\b(?P<predicate>.+?)\s*$", sql, re.IGNORECASE | re.DOTALL)
    actual_predicate = (
        " ".join(match.group("predicate").lower().split()) if match else None
    )
    expected_predicate = (
        " ".join(predicate.lower().split()) if predicate is not None else None
    )
    return actual_predicate == expected_predicate


def _sql_object_matches_assertion(
    conn: sqlite3.Connection,
    *,
    object_type: str,
    name: str,
    required_fragments: tuple[str, ...],
) -> bool:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type=? AND name=?",
        (object_type, name),
    ).fetchone()
    definition = " ".join(str(row[0]).lower().split()) if row and row[0] else ""
    return bool(definition) and all(
        " ".join(fragment.lower().split()) in definition
        for fragment in required_fragments
    )


def _migration_trigger_contract(path: Path) -> dict[str, str]:
    """Return exact normalized CREATE TRIGGER statements from one migration."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return {}
    statements: dict[str, str] = {}
    buffer = ""
    for line in lines:
        buffer += line
        if not sqlite3.complete_statement(buffer):
            continue
        match = re.search(
            r"\bCREATE\s+TRIGGER\s+([A-Za-z_][A-Za-z0-9_]*)\b",
            buffer,
            re.IGNORECASE,
        )
        if match is not None:
            name = match.group(1)
            if name in statements:
                return {}
            statements[name] = " ".join(
                buffer[match.start() :].rstrip().rstrip(";").lower().split()
            )
        buffer = ""
    if buffer.strip():
        return {}
    return statements


def _migration_187_trigger_contract_valid(conn: sqlite3.Connection) -> bool:
    """Prove every lifecycle trigger is present with its canonical SQL body."""
    expected = _migration_trigger_contract(
        MIGRATIONS_DIR / "187_project_lifecycle_control.sql"
    )
    expected = {
        name: definition
        for name, definition in expected.items()
        if name.startswith(("project_write_events_", "project_writes_"))
    }
    if not expected:
        return False
    rows = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
        "AND (name LIKE 'project_write_events_%' OR name LIKE 'project_writes_%')"
    ).fetchall()
    actual = {
        str(row[0]): " ".join(str(row[1] or "").rstrip().rstrip(";").lower().split())
        for row in rows
    }
    return actual == expected


def assert_schema_compatible(
    conn: sqlite3.Connection,
    code_max: int,
    known_versions: set[int] | None = None,
) -> int:
    """F7 guard: refuse to run when the volume is AHEAD of the code.

    Returns the current schema version. The forward-only runner treats
    ``MAX(schema_versions) > code max`` as "nothing pending" and would happily
    serve an unknown schema (silent 500s) — the rollback-of-the-image-only
    scenario. When the DB claims to be exactly at code max, spot-check the real
    schema artifacts so a silently-skipped file does not masquerade as current.
    Spot checks apply only to migrations actually present in the discovered set
    (``known_versions``): tests drive the runner with partial migration dirs.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_versions "
        "(version INTEGER PRIMARY KEY, applied_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    row = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()
    current = row[0] if row and row[0] is not None else 0
    if current > code_max:
        raise RuntimeError(
            f"Database schema is at v{current} but this code only knows migrations up to "
            f"v{code_max}: an OLDER image is running on an already-migrated volume. "
            f"Restore the pre-update backup or run an image >= v{current}; refusing to "
            "boot on an unknown schema."
        )
    if current == code_max:
        for version, table, column in SCHEMA_ASSERTIONS:
            if known_versions is not None and version not in known_versions:
                continue
            if version <= current and not _column_exists(conn, table, column):
                raise RuntimeError(
                    f"schema_versions claims v{current} but {table}.{column} "
                    f"(migration {version:03d}) is missing — the version table and the real "
                    "schema diverged; restore from a known-good backup."
                )
        for version, table, expected in SCHEMA_EXACT_COLUMN_ASSERTIONS:
            if known_versions is not None and version not in known_versions:
                continue
            if version <= current and _schema_column_contract(conn, table) != expected:
                raise RuntimeError(
                    f"schema_versions claims v{current} but table {table} "
                    f"(migration {version:03d}) has a malformed column contract — "
                    "the version table and the real schema diverged; restore from a "
                    "known-good backup."
                )
        for version, table, index, unique, columns, predicate in SCHEMA_INDEX_ASSERTIONS:
            if known_versions is not None and version not in known_versions:
                continue
            if version <= current and not _index_matches_assertion(
                conn,
                table=table,
                index=index,
                unique=unique,
                columns=columns,
                predicate=predicate,
            ):
                raise RuntimeError(
                    f"schema_versions claims v{current} but index {index} "
                    f"(migration {version:03d}) is missing or malformed — the version "
                    "table and the real schema diverged; restore from a known-good backup."
                )
        for version, object_type, name, fragments in SCHEMA_OBJECT_ASSERTIONS:
            if known_versions is not None and version not in known_versions:
                continue
            if version <= current and not _sql_object_matches_assertion(
                conn,
                object_type=object_type,
                name=name,
                required_fragments=fragments,
            ):
                raise RuntimeError(
                    f"schema_versions claims v{current} but {object_type} {name} "
                    f"(migration {version:03d}) is missing or malformed — the version "
                    "table and the real schema diverged; restore from a known-good backup."
                )
        if (
            187 <= current
            and (known_versions is None or 187 in known_versions)
            and not _migration_187_trigger_contract_valid(conn)
        ):
            raise RuntimeError(
                f"schema_versions claims v{current} but the migration 187 project "
                "write-trigger set is missing or malformed — the version table and "
                "the real schema diverged; restore from a known-good backup."
            )
    return current


@contextmanager
def _migration_lock(db_path: str):
    """Cross-platform single-runner guard (F7/6733c88c).

    ``init_pool()`` runs migrations from EVERY entry point (API, MCP, brain,
    CLI, workers) — two processes booting on the same volume must serialize
    here, not interleave executescript batches. Belt-and-braces on top of any
    orchestration-level single runner (e.g. a compose one-shot service).
    """
    if not db_path or db_path == ":memory:" or db_path.startswith("file:"):
        yield
        return
    lock_path = f"{db_path}.migrate.lock"
    with exclusive_file_lock(lock_path, mode=0o644):
        yield


def _db_is_fresh(conn: sqlite3.Connection) -> bool:
    """True when the DB has no user tables yet (nothing to protect with a backup)."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' AND name != 'schema_versions'"
    ).fetchone()
    return (row[0] or 0) == 0


def _database_is_empty_for_security_bootstrap(conn: sqlite3.Connection) -> bool:
    """True only before any application schema object has ever been created.

    An empty ``schema_versions`` table is still evidence of an existing database,
    not permission to run write-rejecting migrations under live writers.
    """
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone()
        is None
    )


def _last_migration_epoch(conn: sqlite3.Connection) -> float | None:
    """Epoch of the newest applied migration (schema_versions.applied_at, UTC)."""
    if not _table_exists(conn, "schema_versions"):
        return None
    row = conn.execute("SELECT MAX(applied_at) FROM schema_versions").fetchone()
    if not row or not row[0]:
        return None
    try:
        return (
            datetime.strptime(str(row[0]), "%Y-%m-%d %H:%M:%S")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except ValueError:
        return None


def _backup_database_before_migration(
    conn: sqlite3.Connection,
    current_version: int,
    *,
    pre_update_backup_key: str | None = None,
) -> str | None:
    """Fail-closed, hot-safe pre-update backup (IMPL §C + P0-1).

    ``shutil.copy2`` on a live WAL database is incoherent and its failure used
    to be tolerated ("continuing") — the runner migrated with no rollback
    point. This uses the sqlite ``.backup()`` API + integrity check and RAISES
    on any failure: the backup IS the rollback strategy (down-migrations are
    forbidden, F11).

    P0-1: the file lives in a dedicated ``pre-update/`` namespace no rotation
    touches, and a pre-update backup newer than the last applied migration is
    REUSED — a failed upgrade retry must never overwrite or rotate away the
    clean pre-run state. Pruning (keep-2) happens only after a successful run.

    Returns the backup path, or None on a fresh DB (nothing to protect;
    rollback = recreate the empty volume).
    """
    if _db_is_fresh(conn):
        logger.info("Pre-migration backup skipped: fresh database")
        return None
    db_path = str(settings.db_path)
    db_name = os.path.basename(db_path)
    base_dir = settings.db_backup_dir or os.path.dirname(db_path) or "."
    backup_dir = os.path.join(base_dir, PRE_UPDATE_BACKUP_SUBDIR)
    if pre_update_backup_key is not None:
        if re.fullmatch(r"[0-9a-f]{64}", pre_update_backup_key) is None:
            raise RuntimeError("Invalid controlled pre-update backup identity")
        backup_dir = os.path.join(
            backup_dir,
            f"attempt-{pre_update_backup_key}",
        )
    try:
        os.makedirs(backup_dir, exist_ok=True)
        directory_metadata = os.lstat(backup_dir)
        if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(
            directory_metadata.st_mode
        ):
            raise OSError("backup path is not a real directory")
        os.chmod(backup_dir, 0o700)
    except OSError as exc:
        raise RuntimeError(
            f"Cannot create pre-update backup dir {backup_dir}: {exc}; refusing to "
            "migrate without a rollback point"
        ) from exc

    backup_path = os.path.join(
        backup_dir, f"{db_name}.pre-update-v{current_version}"
    )
    last_applied = _last_migration_epoch(conn)
    if os.path.isfile(backup_path):
        if last_applied is None or os.path.getmtime(backup_path) >= last_applied:
            metadata = os.lstat(backup_path)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
            ):
                raise RuntimeError(
                    "Existing pre-migration backup is not a private regular file; "
                    "refusing to overwrite the rollback point"
                )
            existing_connection = sqlite3.connect(
                f"file:{backup_path}?mode=ro", uri=True
            )
            try:
                check = existing_connection.execute(
                    "PRAGMA integrity_check"
                ).fetchone()
                existing_version = _claimed_schema_version(existing_connection)
            finally:
                existing_connection.close()
            if (
                not check
                or str(check[0]).lower() != "ok"
                or existing_version != current_version
            ):
                raise RuntimeError(
                    "Existing pre-migration backup failed integrity/version proof; "
                    "refusing to overwrite the rollback point"
                )
            logger.info(
                "Pre-migration backup reused (newer than last applied migration): %s",
                backup_path,
            )
            return backup_path
        metadata = os.lstat(backup_path)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise RuntimeError(
                "Stale pre-migration backup path is unsafe; refusing to migrate"
            )
        os.unlink(backup_path)

    db_size = os.path.getsize(db_path)
    free = shutil.disk_usage(backup_dir).free
    if free < 2 * db_size:
        raise RuntimeError(
            f"Refusing to migrate: free space in {backup_dir} ({free} B) is below 2x "
            f"the database size ({db_size} B) — a backup would fill the volume "
            "(learning b5275327)."
        )

    try:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(backup_path, flags, 0o600)
        os.close(descriptor)
        dest = sqlite3.connect(backup_path)
        try:
            conn.backup(dest)
            check = dest.execute("PRAGMA integrity_check").fetchone()
            if not check or str(check[0]).lower() != "ok":
                raise RuntimeError(f"integrity_check returned {check!r}")
        finally:
            dest.close()
    except Exception as exc:
        try:
            os.remove(backup_path)
        except OSError:
            pass
        raise RuntimeError(
            f"Pre-migration backup to {backup_path} failed — refusing to migrate "
            f"without a rollback point: {exc}"
        ) from exc
    logger.info("Pre-migration backup: %s", backup_path)
    return backup_path


def _prune_pre_update_backups(keep: int = PRE_UPDATE_BACKUP_KEEP) -> None:
    """Keep the newest ``keep`` complete pre-update backup sets.

    SQLite may leave ``-wal`` and ``-shm`` next to a backup after a read-only
    verification.  Those sidecars belong to the main backup; they must never
    consume retention slots and evict the rollback database itself.
    """
    db_name = os.path.basename(str(settings.db_path))
    base_dir = settings.db_backup_dir or os.path.dirname(str(settings.db_path)) or "."
    backup_dir = os.path.join(base_dir, PRE_UPDATE_BACKUP_SUBDIR)
    backup_name = re.compile(
        rf"^{re.escape(db_name)}\.pre-update-v(?P<version>[0-9]+)$"
    )
    backups = sorted(
        (
            path
            for path in glob.glob(
                os.path.join(backup_dir, f"{db_name}.pre-update-v*")
            )
            if backup_name.fullmatch(os.path.basename(path)) is not None
        ),
        key=os.path.getmtime,
    )
    for old_backup in backups[:-keep] if keep > 0 else backups:
        for candidate in (
            f"{old_backup}-journal",
            f"{old_backup}-wal",
            f"{old_backup}-shm",
            old_backup,
        ):
            try:
                metadata = os.lstat(candidate)
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(
                    metadata.st_mode
                ):
                    logger.warning(
                        "Refusing to prune unsafe pre-update backup member: %s",
                        candidate,
                    )
                    continue
                os.remove(candidate)
                logger.info("Pruned old pre-update backup member: %s", candidate)
            except FileNotFoundError:
                continue
            except OSError:
                pass


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check if a column exists in a table. Used for migration idempotency (partial failure recovery)."""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(c[1] == column for c in cols)


def _schema_column_contract(
    conn: sqlite3.Connection,
    table: str,
) -> dict[str, tuple[str, bool, str | None, int]]:
    """Return an exact, normalized SQLite column contract."""
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    contract: dict[str, tuple[str, bool, str | None, int]] = {}
    for row in rows:
        default = row[4]
        normalized_default = None
        if default is not None:
            normalized_default = " ".join(str(default).lower().split())
            normalized_default = normalized_default.replace(", ", ",")
            while (
                normalized_default.startswith("(")
                and normalized_default.endswith(")")
            ):
                normalized_default = normalized_default[1:-1].strip()
        contract[str(row[1])] = (
            str(row[2]).upper(),
            bool(row[3]),
            normalized_default,
            int(row[5]),
        )
    return contract


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return bool(row[0])


def get_sync_connection() -> sqlite3.Connection:
    """Synchronous connection for startup migrations."""
    conn = sqlite3.connect(settings.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


async def _configure_connection(db: aiosqlite.Connection) -> None:
    """Shared PRAGMAs for all async connections."""
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA synchronous=NORMAL")  # 2-3x faster writes vs FULL
    await db.execute(
        "PRAGMA busy_timeout=15000"
    )  # 15s (5s too aggressive, 30s masks issues)
    await db.execute("PRAGMA cache_size=-64000")  # 64MB
    await db.execute("PRAGMA temp_store=MEMORY")
    await db.execute("PRAGMA mmap_size=536870912")  # 512MB (covers 300MB DB)
    await db.execute("PRAGMA wal_autocheckpoint=1000")  # ~4MB WAL trigger


# --- Single-Writer Architecture ---
# SQLite WAL: one writer + many readers. All writes (router + background) go
# through a single dedicated writer connection serialized by asyncio.Lock.
# Pool connections are read-only (PRAGMA query_only=ON) — writes via pool
# raise OperationalError immediately instead of causing lock contention.
#
# Four primitives:
#   get_db()           — read-only pool (FastAPI DI for GET endpoints)
#   get_write_db()     — writer + lock (FastAPI DI for POST/PATCH/DELETE endpoints)
#   write_db()         — writer + lock + auto-commit (background tasks)
#   acquire_write_db() — writer + lock (WebSocket/non-DI writers)
#   acquire_db()       — read-only pool (WebSocket/non-DI readers)
#
# See: "database is locked" incident 2026-04-12, plan 2026-04-13.

_pool: asyncio.Queue[aiosqlite.Connection] | None = None
_pool_size: int = 0
_writer: aiosqlite.Connection | None = None
_write_lock = asyncio.Lock()

WRITER_LOCK_EVENT_LIMIT = 500
WRITER_LOCK_SLOW_WAIT_MS = 50.0
WRITER_LOCK_SLOW_HOLD_MS = 1000.0  # Fase 0: log writer holds >1s to separate offenders from queued victims
_writer_metrics_lock = Lock()
_writer_wait_events: deque[dict[str, Any]] = deque(maxlen=WRITER_LOCK_EVENT_LIMIT)
_writer_hold_events: deque[dict[str, Any]] = deque(maxlen=WRITER_LOCK_EVENT_LIMIT)
_writer_current_holder: dict[str, Any] | None = None
_writer_sequence = 0


async def init_pool(size: int = 2) -> None:
    """Create read-only connection pool + dedicated writer. Call once in lifespan startup.

    Pool connections have PRAGMA query_only=ON — any INSERT/UPDATE/DELETE via
    get_db() or acquire_db() raises OperationalError immediately. All writes
    must go through get_write_db(), write_db(), or acquire_write_db().

    Pool size=8 supports concurrent deep=true requests (4 parallel KG subqueries × 2 concurrent requests).
    """
    # Apply any pending schema migrations before opening the pool, so EVERY
    # entry point (CLI, MCP, brain, API) lands on a current schema — not only
    # `marvis init` and the API lifespan. Idempotent and cheap when already
    # current (the API path migrates explicitly first, so this finds nothing).
    # Fail-loud here is a clearer signal than a later "no such column" on the
    # first write against a half-upgraded DB. (#12)
    run_migrations()
    global _pool, _pool_size, _writer, _write_lock
    # The lock belongs to this pool lifecycle. Reusing a lock after the pool was
    # closed can retain an event-loop binding from an old runtime/test loop and
    # make the next otherwise-valid writer fail with "bound to a different event
    # loop". Production initializes one pool per process; tests and controlled
    # restarts legitimately initialize more than once.
    _write_lock = asyncio.Lock()
    actual_size = 8  # read-only pool: expanded for KG lens 4-subquery parallel pattern (Phase 7.0)
    _pool = asyncio.Queue(maxsize=actual_size)
    _pool_size = actual_size
    for _ in range(actual_size):
        db = await aiosqlite.connect(settings.db_path)
        await _configure_connection(db)
        await db.execute("PRAGMA query_only=ON")
        db.row_factory = aiosqlite.Row
        await _pool.put(db)
    # Dedicated writer for ALL writes (router + background), serialized by _write_lock
    _writer = await aiosqlite.connect(settings.db_path)
    await _configure_connection(_writer)
    _writer.row_factory = aiosqlite.Row
    logger.info(
        "DB initialized: %d read-only pool + 1 dedicated writer (single-writer enforced)",
        actual_size,
    )


async def close_pool() -> None:
    """Close all connections. Call in lifespan shutdown."""
    global _pool, _pool_size, _writer
    if _pool:
        closed = 0
        while not _pool.empty():
            try:
                db = _pool.get_nowait()
                await db.close()
                closed += 1
            except asyncio.QueueEmpty:
                break
        _pool = None
        _pool_size = 0
    if _writer:
        await _writer.close()
        _writer = None
    logger.info("DB connections closed")


async def _reset_pool_connection(db: aiosqlite.Connection) -> None:
    """End any transaction left open on a pooled read connection.

    A read connection handed out (or returned) with an open transaction pins a
    WAL snapshot: every later borrower then reads a frozen, hours-old view, and
    the held snapshot blocks WAL checkpoint so the -wal file grows without bound.
    Known leak path: an explicit BEGIN on a pooled reader (graph_cosmo) whose
    cleanup is skipped when the request task is cancelled (CancelledError is not
    an Exception). Resetting on both borrow and return guarantees each query
    starts from the current snapshot. (learning 4b80fdfb)
    """
    if db.in_transaction:
        await db.rollback()


async def _recycle_pool_connection(bad: aiosqlite.Connection) -> None:
    """Replace a read connection we could not reset with a fresh one.

    Keeps the pool at full size instead of recirculating a connection that is
    still pinned to a stale snapshot.
    """
    try:
        await bad.close()
    except Exception:
        pass
    if _pool is None:
        return
    try:
        fresh = await aiosqlite.connect(settings.db_path)
        await _configure_connection(fresh)
        await fresh.execute("PRAGMA query_only=ON")
        fresh.row_factory = aiosqlite.Row
        _pool.put_nowait(fresh)
    except Exception:
        logger.error(
            "Failed to recycle read pool connection; pool reduced by one",
            exc_info=True,
        )


async def _release_pool_connection(db: aiosqlite.Connection) -> None:
    """Return a read connection to the pool, transaction-reset first.

    If the reset fails the connection is recycled rather than recirculated, so
    the pool never hands out a pinned snapshot.
    """
    if _pool is None:
        await db.close()
        return
    try:
        await _reset_pool_connection(db)
    except Exception:
        logger.warning(
            "Read pool connection reset failed on release; recycling", exc_info=True
        )
        await _recycle_pool_connection(db)
        return
    try:
        _pool.put_nowait(db)
    except asyncio.QueueFull:
        await db.close()


async def get_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Read-only pool connection for request handlers. For writes use get_write_db()."""
    if _pool is not None:
        db = await _pool.get()
        try:
            # Borrow clean: a previously-leaked transaction (e.g. a cancelled
            # request) would otherwise serve this borrower a frozen snapshot.
            await _reset_pool_connection(db)
            yield db
        finally:
            await _release_pool_connection(db)
    else:
        db = await aiosqlite.connect(settings.db_path)
        await _configure_connection(db)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()


@asynccontextmanager
async def acquire_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Read-only pool access outside FastAPI DI (e.g. WebSocket handlers). For writes use acquire_write_db()."""
    if _pool is not None:
        db = await _pool.get()
        try:
            await _reset_pool_connection(db)
            yield db
        finally:
            await _release_pool_connection(db)
    else:
        db = await aiosqlite.connect(settings.db_path)
        await _configure_connection(db)
        db.row_factory = aiosqlite.Row
        try:
            yield db
        finally:
            await db.close()


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def _writer_owner_label(label: str | None = None) -> str:
    if label:
        return label

    repo_root = Path(__file__).parent.parent
    current_file = Path(__file__).resolve()
    frame = inspect.currentframe()
    if frame is not None:
        frame = frame.f_back
    try:
        while frame is not None:
            frame_path = Path(frame.f_code.co_filename).resolve()
            if frame_path != current_file:
                try:
                    rel_path = frame_path.relative_to(repo_root)
                except ValueError:
                    frame = frame.f_back
                    continue
                return f"{rel_path}:{frame.f_code.co_name}:{frame.f_lineno}"
            frame = frame.f_back
    finally:
        del frame
    return "unknown"


def _current_writer_blocker(now_perf: float) -> dict[str, Any] | None:
    with _writer_metrics_lock:
        if not _writer_current_holder:
            return None
        return {
            "label": _writer_current_holder["label"],
            "task_name": _writer_current_holder.get("task_name"),
            "held_ms": max(
                0.0, (now_perf - _writer_current_holder["started_perf"]) * 1000
            ),
        }


def _start_writer_hold(
    *,
    label: str,
    task_name: str | None,
    queued_at: float,
    acquired_at: float,
    wait_ms: float,
    blocked_by: dict[str, Any] | None,
) -> dict[str, Any]:
    global _writer_current_holder, _writer_sequence
    with _writer_metrics_lock:
        _writer_sequence += 1
        holder = {
            "id": _writer_sequence,
            "label": label,
            "task_name": task_name,
            "queued_at": queued_at,
            "acquired_at": acquired_at,
            "started_perf": time.perf_counter(),
            "wait_ms": wait_ms,
            "blocked_by": blocked_by,
        }
        _writer_current_holder = holder
        _writer_wait_events.append(
            {
                "label": label,
                "task_name": task_name,
                "queued_at": queued_at,
                "acquired_at": acquired_at,
                "wait_ms": wait_ms,
                "contended": wait_ms >= 1.0,
                "slow": wait_ms >= WRITER_LOCK_SLOW_WAIT_MS,
                "blocked_by": blocked_by,
            }
        )
    if wait_ms >= WRITER_LOCK_SLOW_WAIT_MS:
        logger.warning(
            "SQLite writer lock WAIT %.0fms for %s (blocked_by=%s)",
            wait_ms,
            label,
            (blocked_by or {}).get("label"),
        )
    return holder


def _finish_writer_hold(holder: dict[str, Any]) -> None:
    global _writer_current_holder
    ended_at = time.time()
    hold_ms = max(0.0, (time.perf_counter() - holder["started_perf"]) * 1000)
    with _writer_metrics_lock:
        if _writer_current_holder and _writer_current_holder.get("id") == holder["id"]:
            _writer_current_holder = None
        _writer_hold_events.append(
            {
                "label": holder["label"],
                "task_name": holder.get("task_name"),
                "acquired_at": holder["acquired_at"],
                "ended_at": ended_at,
                "hold_ms": hold_ms,
                "wait_ms": holder["wait_ms"],
                "blocked_by": holder.get("blocked_by"),
            }
        )
    if hold_ms >= WRITER_LOCK_SLOW_HOLD_MS:
        logger.warning(
            "SQLite writer lock HELD %.0fms by %s (waited %.0fms) — offender, not victim",
            hold_ms,
            holder["label"],
            holder.get("wait_ms", 0.0),
        )


def get_writer_lock_snapshot(window_seconds: float = 60.0) -> dict[str, Any]:
    """Return rolling telemetry for the global SQLite writer lock."""
    now = time.time()
    now_perf = time.perf_counter()
    cutoff = now - window_seconds
    with _writer_metrics_lock:
        waits = [
            event for event in _writer_wait_events if event["queued_at"] >= cutoff
        ]
        holds = [
            event for event in _writer_hold_events if event["ended_at"] >= cutoff
        ]
        current_holder = dict(_writer_current_holder) if _writer_current_holder else None

    wait_values = [float(event["wait_ms"]) for event in waits]
    hold_values = [float(event["hold_ms"]) for event in holds]
    wait_by_label: dict[str, list[float]] = defaultdict(list)
    hold_by_label: dict[str, list[float]] = defaultdict(list)
    blocked_by_labels: Counter[str] = Counter()
    for event in waits:
        wait_by_label[str(event["label"])].append(float(event["wait_ms"]))
        blocked_by = event.get("blocked_by")
        if isinstance(blocked_by, dict) and blocked_by.get("label"):
            blocked_by_labels[str(blocked_by["label"])] += 1
    for event in holds:
        hold_by_label[str(event["label"])].append(float(event["hold_ms"]))

    if current_holder:
        current_holder["held_ms"] = max(
            0.0, (now_perf - current_holder.pop("started_perf")) * 1000
        )

    return {
        "window_seconds": window_seconds,
        "locked": _write_lock.locked(),
        "current_holder": current_holder,
        "wait_ms": _summary(wait_values),
        "hold_ms": _summary(hold_values),
        "contended_wait_count": sum(1 for value in wait_values if value >= 1.0),
        "slow_wait_count": sum(
            1 for value in wait_values if value >= WRITER_LOCK_SLOW_WAIT_MS
        ),
        "wait_by_label": {
            label: _summary(values) for label, values in sorted(wait_by_label.items())
        },
        "hold_by_label": {
            label: _summary(values) for label, values in sorted(hold_by_label.items())
        },
        "blocked_by_label_counts": dict(blocked_by_labels),
        "last_wait_events": waits[-10:],
        "last_hold_events": holds[-10:],
    }


def reset_writer_lock_metrics_for_tests() -> None:
    global _writer_current_holder
    with _writer_metrics_lock:
        _writer_wait_events.clear()
        _writer_hold_events.clear()
        _writer_current_holder = None


def _request_writer_label(request: Request | None) -> str:
    if request is None:
        return "get_write_db"

    scope = request.scope
    route = scope.get("route")
    route_path = getattr(route, "path", None) or scope.get("path") or "unknown"
    method = scope.get("method") or request.method or "REQUEST"
    endpoint = scope.get("endpoint")
    if endpoint is None:
        return f"{method} {route_path}"

    module = getattr(endpoint, "__module__", None)
    qualname = getattr(endpoint, "__qualname__", None) or getattr(
        endpoint, "__name__", None
    )
    endpoint_name = ".".join(part for part in (module, qualname) if part)
    if not endpoint_name:
        return f"{method} {route_path}"
    return f"{method} {route_path} -> {endpoint_name}"


class WriterLockTimeout(TimeoutError):
    """The writer lock could not be acquired within the caller's deadline.

    Raised only by bounded acquisitions (acquire_write_db(timeout=...)).
    Unbounded callers keep the historical wait-forever behavior.
    """


@asynccontextmanager
async def _acquire_writer(
    *, label: str | None = None, timeout: float | None = None
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Internal: acquire write lock, yield writer, rollback on error.

    All public write primitives (write_db, get_write_db, acquire_write_db)
    delegate here for consistent lock + rollback semantics.

    timeout bounds only the lock wait, never the hold: a caller that cannot
    tolerate queuing behind a stuck holder (2026-08-04: a wedged external
    console.db writer kept this lock held and every webhook delivery — then
    the whole tenant write path — queued forever) passes a deadline and gets
    WriterLockTimeout instead of joining the pile-up.
    """
    owner_label = _writer_owner_label(label)
    task = asyncio.current_task()
    task_name = task.get_name() if task else None
    queued_at = time.time()
    queued_perf = time.perf_counter()
    blocked_by = _current_writer_blocker(queued_perf) if _write_lock.locked() else None
    if timeout is None:
        await _write_lock.acquire()
    else:
        try:
            await asyncio.wait_for(_write_lock.acquire(), timeout)
        except TimeoutError:
            holder_label = blocked_by.get("label") if isinstance(blocked_by, dict) else None
            raise WriterLockTimeout(
                f"writer lock not acquired within {timeout}s"
                + (f" (blocked by: {holder_label})" if holder_label else "")
            ) from None
    acquired_at = time.time()
    holder = _start_writer_hold(
        label=owner_label,
        task_name=task_name,
        queued_at=queued_at,
        acquired_at=acquired_at,
        wait_ms=(time.perf_counter() - queued_perf) * 1000,
        blocked_by=blocked_by,
    )
    try:
        if not _writer:
            raise RuntimeError("DB not initialized — call init_pool() first")
        try:
            yield _writer
        except aiosqlite.IntegrityError as exc:
            await _writer.rollback()
            # Migration 187 intentionally aborts the *outer* project mutation
            # from its append-only write journal.  Translate that one stable
            # SQLite reason at the shared writer boundary so HTTP's global
            # ServiceError handler and MCP's surrounding adapter both expose a
            # typed 409/tool error instead of a transport-specific 500.
            if "project_not_writable" in str(exc):
                from core.api.use_cases._errors import ConflictError

                raise ConflictError(
                    code="project_not_writable",
                    message=(
                        "Project is archived or a lifecycle transition is active"
                    ),
                ) from exc
            if "project_workspace_ambiguous" in str(exc):
                from core.api.use_cases._errors import ConflictError

                raise ConflictError(
                    code="project_workspace_ambiguous",
                    message="Project workspace ownership is ambiguous",
                ) from exc
            raise
        except Exception:
            await _writer.rollback()
            raise
    finally:
        _finish_writer_hold(holder)
        _write_lock.release()


@asynccontextmanager
async def write_db(
    label: str | None = None,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Background tasks: auto-commit on success, auto-rollback on error.

    Use for metrics_collector, cost_service, security_collector, or any other
    periodic background write.

    Do NOT do slow work (HTTP calls, computation) inside this context.
    Gather data first, then write in a fast batch.
    """
    async with _acquire_writer(label=label) as w:
        yield w
        await w.commit()


async def get_write_db(
    request: Request,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """Router write endpoints: caller must commit. Auto-rollback on error.

    Use Depends(get_write_db) for any endpoint that does INSERT/UPDATE/DELETE.
    The pool connection (get_db) is read-only — writes will fail with
    OperationalError: attempt to write a readonly database.
    """
    async with _acquire_writer(label=_request_writer_label(request)) as w:
        yield w


@asynccontextmanager
async def acquire_write_db(
    label: str | None = None,
    timeout: float | None = None,
) -> AsyncGenerator[aiosqlite.Connection, None]:
    """WebSocket/non-DI writers: caller must commit. Auto-rollback on error.

    Use this for code that needs to write outside FastAPI dependency injection
    (e.g. WebSocket handlers, terminal upload). With timeout set, raises
    WriterLockTimeout instead of waiting forever behind a stuck holder.
    """
    async with _acquire_writer(label=label, timeout=timeout) as w:
        yield w


async def wal_checkpoint() -> tuple[int, int, int]:
    """Run PRAGMA wal_checkpoint(TRUNCATE) via the writer connection.

    Returns (busy, log, checkpointed) — same as SQLite's checkpoint result row.
    busy>0 means active readers blocked a full truncate (partial checkpoint still ran).
    """
    async with _acquire_writer(label="wal_checkpoint") as writer:
        cursor = await writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        row = await cursor.fetchone()
        return (row[0], row[1], row[2])


# sqlite-vec support. Kept as a compatibility symbol for older tests/importers;
# readiness is connection-scoped below because one process can open many DBs.
_vec_table_ready = False

# Platform-specific loadable-extension suffixes. The vec0 loadable is `.so` on
# Linux (prod), `.dylib` on macOS, `.dll` on Windows.
_VEC0_SUFFIXES = (".so", ".dylib", ".dll")


def resolve_vec0_loadable() -> tuple[str | None, bool]:
    """Resolve the vec0 loadable path, cross-platform.

    Returns ``(load_arg, found)`` where ``load_arg`` is the argument to pass to
    ``SELECT load_extension(?)`` and ``found`` is True when a real loadable file
    exists on disk.

    Resolution (prod-safe by construction):
      1. ``settings.vec0_path`` when it resolves to a real file: if it already
         carries a known suffix use it as-is, otherwise probe ``.so`` /
         ``.dylib`` / ``.dll``. Prod (Linux, ``/data/pir/lib/vec0`` →
         ``vec0.so``, set via ``VEC0_PATH``) resolves here and keeps using the
         exact configured path.
      2. Else the installed ``sqlite_vec`` package's own loadable
         (``sqlite_vec.loadable_path()``) — the OSS clean-install path, where
         ``/data/pir/lib/vec0`` is absent and the bundled loadable is
         platform-correct (.dylib on macOS, no longer rejected by a hardcoded
         ``.so`` check).

    The suffix-less ``load_arg`` lets SQLite append the platform suffix itself.
    """
    vec_path = Path(settings.vec0_path)
    if vec_path.suffix:
        if vec_path.exists():
            return (str(vec_path), True)
    else:
        for suffix in _VEC0_SUFFIXES:
            if vec_path.with_suffix(suffix).exists():
                # Pass the suffix-less path; SQLite appends the platform suffix.
                return (str(vec_path), True)

    try:
        import sqlite_vec  # type: ignore

        pkg_path = Path(str(sqlite_vec.loadable_path()))
        if pkg_path.exists():
            return (str(pkg_path), True)
        # loadable_path() returns a suffix-less base on some builds; probe.
        for suffix in _VEC0_SUFFIXES:
            if pkg_path.with_suffix(suffix).exists():
                return (str(pkg_path), True)
    except Exception:  # noqa: BLE001 — package missing/old → settings path is final
        pass

    # Nothing resolved; return the configured path so callers log a clear miss.
    return (str(vec_path), False)


async def ensure_vec_documents(db: aiosqlite.Connection) -> bool:
    """Load sqlite-vec on an existing connection and ensure vec_documents exists."""
    load_arg, found = resolve_vec0_loadable()
    if not found or load_arg is None:
        return False

    if not getattr(db, "_pir_vec_extension_loaded", False):
        await db._execute(db._conn.enable_load_extension, True)
        await db.execute("SELECT load_extension(?)", [load_arg])
        setattr(db, "_pir_vec_extension_loaded", True)
    if not getattr(db, "_pir_vec_table_ready", False):
        await db.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS vec_documents USING vec0(
                doc_id INTEGER PRIMARY KEY,
                embedding float[512]
            )
        """)
        setattr(db, "_pir_vec_table_ready", True)
    return True


async def get_vec_db() -> AsyncGenerator[aiosqlite.Connection, None]:
    """Dedicated dependency for sqlite-vec endpoints. Mirrors get_db() PRAGMAs + loads vec0."""
    db = await aiosqlite.connect(settings.db_path)
    await _configure_connection(db)
    db.row_factory = aiosqlite.Row
    await ensure_vec_documents(db)
    try:
        yield db
    finally:
        await db.close()


def run_migrations(
    *,
    pre_update_backup_key: str | None = None,
    backup_ready: Callable[[str], None] | None = None,
    _migration_lock_held: bool = False,
) -> MigrationResult:
    """Apply pending SQL migrations in order.

    Hardened for unsupervised boots (enterprise kit, IMPL §C dockerization plan):
    allowlisted discovery (F8), older-image guard (F7), fail-closed hot backup
    pinned outside rotation (P0-1), cross-process lock, per-version post-hooks
    preserved (F-2), and a post-run schema re-check that logs the FINAL version.
    """
    migration_files = discover_up_migrations()
    code_max = code_max_version(migration_files)
    known_versions = {_migration_version(f) for f in migration_files}
    lock_context = (
        nullcontext()
        if _migration_lock_held
        else _migration_lock(str(settings.db_path))
    )
    with lock_context:
        conn = get_sync_connection()
        try:
            # Read the claimed version without creating or repairing anything.
            # Existing databases that still need a security migration must be
            # writer-quiesced before a retryable post-hook can mutate them.
            fresh_database = _database_is_empty_for_security_bootstrap(conn)
            claimed_version = _claimed_schema_version(conn)
            claimed_pending = [
                f for f in migration_files if _migration_version(f) > claimed_version
            ]
            claimed_repairs = _claimed_security_repairs_needed(
                conn, known_versions
            )
            quiescence_versions = {
                _migration_version(path) for path in claimed_pending
            } | claimed_repairs
            # v175 creates the audit-chain shape and activation guard. Once a
            # database claims v176+, repairing any v175 object changes an active
            # write contract and therefore inherits v176's offline requirement.
            if 175 in claimed_repairs and claimed_version >= 176:
                quiescence_versions.add(176)
            _require_security_migration_quiescence(
                claimed_version,
                quiescence_versions,
                fresh_database=fresh_database,
            )
            backup_path: str | None = None
            backup_published = False

            def publish_backup_ready(path: str | None) -> None:
                """Persist the rollback point before the first schema write."""

                nonlocal backup_published
                if path is None or backup_ready is None:
                    return
                if backup_published:
                    raise RuntimeError("Pre-migration backup published twice")
                backup_ready(path)
                backup_published = True

            # Both the compatibility assertion and a repair hook may write:
            # the former bootstraps schema_versions on a legacy, unversioned
            # database, while the latter repairs an already-versioned one.
            # Anchor the rollback point before either path can mutate schema.
            if claimed_repairs or claimed_pending:
                backup_path = _backup_database_before_migration(
                    conn,
                    claimed_version,
                    pre_update_backup_key=pre_update_backup_key,
                )
                publish_backup_ready(backup_path)
            if not claimed_repairs:
                # Preserve the fail-closed version-table drift guard on an
                # otherwise healthy claimed schema. A concrete recoverable
                # security invariant is handled by the controlled repair path
                # below instead, after its writer-quiescence gate.
                assert_schema_compatible(conn, code_max, known_versions)
            # A guarded post-hook can fail after the SQL file has durably marked
            # its version. Repair such retryable, additive invariants before the
            # MAX-version compatibility assertion (v175 is the first one that
            # explicitly guarantees this recovery path).
            _repair_versioned_schema_invariants(conn, known_versions)
            # F7 guard + schema_versions bootstrap. Re-read the version INSIDE
            # the lock: another runner may have migrated while we waited.
            current_version = assert_schema_compatible(conn, code_max, known_versions)

            pending = [
                f for f in migration_files if _migration_version(f) > current_version
            ]

            for migration_file in pending:
                version = _migration_version(migration_file)
                logger.info("Applying migration %s", migration_file.name)
                try:
                    sql = migration_file.read_text()
                    conn.executescript(sql)
                    # executescript() resets PRAGMA foreign_keys; re-enable
                    conn.execute("PRAGMA foreign_keys=ON")
                    conn.execute(
                        "INSERT OR IGNORE INTO schema_versions (version) VALUES (?)",
                        (version,),
                    )
                    conn.commit()
                except Exception as exc:
                    # executescript is NOT atomic (P0-2): a mid-file failure leaves
                    # committed partial DDL with no version row. Any retry or
                    # image re-tag without a restore boots on a half-applied
                    # schema — and if the FIRST new migration failed, the F7
                    # guard cannot catch it (MAX == old code max).
                    hint = (
                        f"restore the pre-update backup ({backup_path})"
                        if backup_path
                        else "recreate the database volume"
                    )
                    raise RuntimeError(
                        f"Migration {migration_file.name} failed: {exc}. The database "
                        "may hold a partially applied schema (executescript is not "
                        f"atomic) — {hint} before retrying or re-tagging the image; "
                        "NEVER apply partial _down migrations (F11)."
                    ) from exc

                # Post-migration hooks (F-2: per-version chain preserved — the
                # v16/v18 seeds are what make a fresh-volume boot usable).
                if version == 8:
                    _backfill_session_uuids(conn)
                if version == 16:
                    _seed_users_and_migrate_owner(conn)
                if version == 18:
                    _seed_agents(conn)
                if version == 45:
                    _add_documents_columns(conn)
                if version == 46:
                    _add_salience_columns(conn)
                if version == 47:
                    _seed_missing_agents(conn)
                if version == 48:
                    _fix_agent_paths_and_roles(conn)
                if version == 49:
                    _migration_049_agent_role_and_learnings(conn)
                if version == 58:
                    _backfill_inbox_status_from_treatment(conn)
                if version == 59:
                    _add_deep_research_column(conn)
                    _cleanup_generic_source_scores(conn)
                if version == 60:
                    _add_sent_in_newsletter_column(conn)
                if version == 61:
                    _migration_061_backfill_sources(conn)
                if version == 62:
                    _migration_062_backfill_from_urls(conn)
                if version == 63:
                    _add_task_completion_mode(conn)
                if version == 70:
                    _migration_070_digest_ranking_inputs_recovery(conn)
                if version == 71:
                    _migration_071_digest_selection_recovery(conn)
                if version == 72:
                    _migration_072_digest_app_settings_recovery(conn)
                if version == 102:
                    _promote_llm_costs_columns(conn)
                if version == 135:
                    _migration_135_graph_edges_provider(conn)
                if version == 136:
                    _backfill_documents_fts(conn)
                if version == 175:
                    _migration_175_audit_chain(conn)
                if version == 176:
                    _migration_176_activate_audit_chain(conn)
                if version == 177:
                    _migration_177_delegation_workspace(conn)
                if version == 178:
                    _migration_178_agent_token_lifecycle(conn)
                if version == 179:
                    _migration_179_workspace_isolation(conn)
                if version == 187:
                    _migration_187_project_lifecycle_bootstrap(conn)

                logger.info("Migration %s applied", migration_file.name)

            if _table_exists(conn, "sessions_meta") and not _column_exists(
                conn, "sessions_meta", "theme_mode"
            ):
                _add_session_theme_mode_column(conn)

            # Re-run recoverable invariants after the pending chain as an
            # idempotency gate. This also proves a successful per-version hook
            # did not depend on a one-shot schema shape.
            _repair_versioned_schema_invariants(conn, known_versions)

            # Post-run: re-check (spot-checks the real schema at code max) and
            # log the FINAL version — the old runner logged the stale pre-run one.
            final_version = assert_schema_compatible(conn, code_max, known_versions)
            if pending and pre_update_backup_key is None:
                _prune_pre_update_backups()
            logger.info(
                "Database at version %d (code max v%d)", final_version, code_max
            )
            return MigrationResult(
                initial_version=claimed_version,
                final_version=final_version,
                code_max_version=code_max,
                applied_versions=tuple(_migration_version(path) for path in pending),
                repaired_versions=tuple(sorted(claimed_repairs)),
                backup_path=backup_path,
            )
        finally:
            conn.close()


def _backfill_session_uuids(conn: sqlite3.Connection) -> None:
    """Backfill session_uuid for existing sessions (migration 008 post-hook)."""
    cursor = conn.execute("SELECT name FROM sessions_meta WHERE session_uuid IS NULL")
    rows = cursor.fetchall()
    if not rows:
        return
    for row in rows:
        name = row["name"] if isinstance(row, sqlite3.Row) else row[0]
        conn.execute(
            "UPDATE sessions_meta SET session_uuid = ? WHERE name = ?",
            (str(uuid_mod.uuid4()), name),
        )
    conn.commit()
    logger.info("Backfilled UUIDs for %d sessions", len(rows))


def _seed_users_and_migrate_owner(conn: sqlite3.Connection) -> None:
    """Migration 016 post-hook: seed the admin user (when configured) + data-migrate
    tasks.owner_id.

    Runs synchronously inside run_migrations() — do NOT use await here.

    Admin seeding is OPTIONAL and config-driven: the admin user is created only when
    a real credential is present (MARVIS_ADMIN_PASSWORD_HASH or MARVIS_PASSWORD; PIR_*
    aliases accepted). With NO credential — the OSS single-user case, where the CLI
    uses the local single-user context and never logs in with a password — the seed
    is SKIPPED (not aborted), so a fresh-DB boot via init_pool (MCP server / brain,
    which now run migrations) does not crash on a clean install. `marvis init` sets
    MARVIS_PASSWORD before migrating, so it still seeds the admin. The owner_id
    slug→id data migration always runs (idempotent), regardless of seeding.

    b3 note: the real cleanup is to decouple admin seeding from run_migrations
    entirely — it belongs in `marvis init` / `marvis account`, not in a schema hook.
    """
    cursor = conn.execute("SELECT COUNT(*) FROM users")
    user_count = cursor.fetchone()[0]
    if user_count == 0:
        # Prefer a pre-hashed password (canonical MARVIS_ADMIN_PASSWORD_HASH;
        # PIR_ADMIN_PASSWORD_HASH stays a deprecated alias).
        hashed = (
            os.environ.get("MARVIS_ADMIN_PASSWORD_HASH")
            or os.environ.get("PIR_ADMIN_PASSWORD_HASH", "")
        ).strip()
        if not hashed:
            seed_password = (
                os.environ.get("MARVIS_PASSWORD")
                or os.environ.get("PIR_PASSWORD", "")
            ).strip()
            if seed_password:
                import bcrypt

                hashed = bcrypt.hashpw(
                    seed_password.encode("utf-8"), bcrypt.gensalt()
                ).decode()
            else:
                logger.warning(
                    "Migration 016: no MARVIS_ADMIN_PASSWORD_HASH / MARVIS_PASSWORD in "
                    "env — admin user NOT seeded (schema applied). Single-user CLI uses "
                    "the local context (no login); create an admin later with `marvis "
                    "init` if you want HTTP login."
                )
        if hashed:  # seed ONLY when we actually have a credential
            admin_id = os.environ.get("MARVIS_ADMIN_USER_ID", "").strip() or "usr_admin"
            admin_slug = os.environ.get("MARVIS_ADMIN_SLUG", "").strip() or "admin"
            admin_name = os.environ.get("MARVIS_ADMIN_DISPLAY_NAME", "").strip() or "Admin"
            conn.execute(
                "INSERT OR IGNORE INTO users "
                "(id, slug, display_name, type, password_hash, system_role) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (admin_id, admin_slug, admin_name, "human", hashed, "super_admin"),
            )
            conn.commit()
            logger.info("Migration 016: seeded admin user '%s' (super_admin)", admin_slug)

    # Data migration: owner_id may contain slug strings (e.g. "emilio") from before
    # the users table existed. Resolve each slug to the corresponding users.id.
    # Values without a matching slug are left unchanged (NULL FK, graceful fallback).
    conn.execute("""
        UPDATE tasks
        SET owner_id = (SELECT id FROM users WHERE slug = tasks.owner_id)
        WHERE owner_id IS NOT NULL
          AND EXISTS (SELECT 1 FROM users WHERE slug = tasks.owner_id)
    """)
    conn.commit()
    logger.info("Migration 016: owner_id slug→id data migration complete")


def _seed_agents(conn: sqlite3.Connection) -> None:
    """Migration 018 post-hook: seed the deploy's configured system agents.

    The agent slugs come from settings.static_agent_identities (deploy .env), so
    OSS core hardcodes no tenant agent names. A fresh OSS install with no config
    seeds nothing here (no internal agents). On prod this migration already ran;
    rows persist independently and re-running is inert (INSERT OR IGNORE).

    IDs follow the convention usr_{slug} / agt-{slug} (same as seed_agent_users.py
    and migration 047). All rows are idempotent.
    """
    for slug in settings.static_agent_identities:
        usr_id = f"usr_{slug}"
        agt_id = f"agt-{slug}"
        conn.execute(
            "INSERT OR IGNORE INTO users (id, slug, display_name, type, avatar_color, system_role, created_at, updated_at) "
            "VALUES (?, ?, ?, 'agent', '#3B82F6', 'operator', datetime('now','utc'), datetime('now','utc'))",
            (usr_id, slug, slug),
        )
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, user_id, scheduler_agent_id, agent_type, model, status, description, created_at, updated_at) "
            "VALUES (?, ?, ?, 'system', 'sonnet', 'active', ?, datetime('now','utc'), datetime('now','utc'))",
            (agt_id, usr_id, slug, f"{slug} agent"),
        )
    conn.commit()
    logger.info(
        "Migration 018: seeded %d configured system agent(s)",
        len(settings.static_agent_identities),
    )


def _add_documents_columns(conn: sqlite3.Connection) -> None:
    """Migration 045 post-hook: add doc_type, doc_title, workspace_id to documents (idempotent).

    This runs AFTER conn.executescript(sql) so we can safely ALTER TABLE and CREATE INDEX.
    The SQL file only does the schema_versions INSERT to avoid index-on-missing-column errors.
    """
    if not _column_exists(conn, "documents", "doc_type"):
        conn.execute(
            "ALTER TABLE documents ADD COLUMN doc_type TEXT NOT NULL DEFAULT 'handoff'"
        )
        logger.info("Migration 045: added documents.doc_type")
    if not _column_exists(conn, "documents", "doc_title"):
        conn.execute("ALTER TABLE documents ADD COLUMN doc_title TEXT")
        logger.info("Migration 045: added documents.doc_title")
    if not _column_exists(conn, "documents", "workspace_id"):
        conn.execute(
            "ALTER TABLE documents ADD COLUMN workspace_id TEXT NOT NULL DEFAULT 'ws_default'"
        )
        logger.info("Migration 045: added documents.workspace_id")
    # Index must be created after the column exists (can't be in SQL file)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_doc_type ON documents(doc_type)"
    )
    conn.commit()


def _add_salience_columns(conn: sqlite3.Connection) -> None:
    """Migration 046 post-hook: add salience, archived, salience_updated_at to documents (idempotent).

    Same pattern as migration 045 — SQL file only does schema_versions INSERT + boost_log table.
    ALTER TABLE + partial indexes run here after columns exist.
    """
    if not _column_exists(conn, "documents", "salience"):
        conn.execute(
            "ALTER TABLE documents ADD COLUMN salience REAL NOT NULL DEFAULT 0.5"
        )
        logger.info("Migration 046: added documents.salience")
    if not _column_exists(conn, "documents", "archived"):
        conn.execute(
            "ALTER TABLE documents ADD COLUMN archived INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("Migration 046: added documents.archived")
    if not _column_exists(conn, "documents", "salience_updated_at"):
        conn.execute("ALTER TABLE documents ADD COLUMN salience_updated_at TEXT")
        logger.info("Migration 046: added documents.salience_updated_at")
    # Partial indexes for active (non-archived) documents
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_active ON documents(doc_type) WHERE archived = 0"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_salience ON documents(salience DESC, doc_type) WHERE archived = 0"
    )
    conn.commit()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _backfill_documents_fts(conn: sqlite3.Connection) -> None:
    """Migration 135 post-hook: backfill full-text bodies for documents_fts.

    SQL migrations cannot read filesystem bodies. The SQL file creates the FTS5
    table, trigger sync, and a file_path-only fallback. This hook replaces that
    fallback with the full body for loadable files and row-backed document
    sources, while staying idempotent through DELETE + INSERT by rowid.
    """
    if not _table_exists(conn, "documents_fts") or not _table_exists(conn, "documents"):
        return

    columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)").fetchall()}
    title_expr = "doc_title" if "doc_title" in columns else "file_path AS doc_title"
    salience_expr = "salience" if "salience" in columns else "0.5 AS salience"
    archived_filter = "WHERE COALESCE(archived, 0) = 0" if "archived" in columns else ""
    rows = conn.execute(
        f"""SELECT id, file_path, project, {title_expr}, {salience_expr}
            FROM documents
            {archived_filter}"""
    ).fetchall()

    for row in rows:
        doc_id = int(row["id"])
        title = row["doc_title"] or row["file_path"] or ""
        content = _documents_fts_content(conn, row)
        conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (doc_id,))
        conn.execute(
            "INSERT INTO documents_fts(rowid, doc_id, title, content) VALUES (?, ?, ?, ?)",
            (doc_id, doc_id, title, content),
        )
    conn.commit()
    logger.info("Migration 135: backfilled documents_fts rows=%d", len(rows))


def _documents_fts_content(conn: sqlite3.Connection, row: sqlite3.Row) -> str:
    file_path = row["file_path"] or ""
    title = row["doc_title"] or file_path
    project = row["project"] or ""

    if file_path.startswith("task:"):
        task_id = file_path.split(":", 1)[1]
        task = _fetch_one_or_none(
            conn,
            "SELECT title, description, status, project, tags FROM tasks WHERE id = ?",
            (task_id,),
        )
        if task is not None:
            return "\n".join(
                str(part)
                for part in (
                    task["title"],
                    task["description"],
                    f"Status: {task['status']}",
                    f"Project: {task['project']}",
                    f"Tags: {task['tags']}",
                )
                if part
            )

    if file_path.startswith("learning:"):
        learning_id = file_path.split(":", 1)[1]
        learning = _fetch_one_or_none(
            conn,
            "SELECT title, description, prevention, category, severity, tags "
            "FROM learnings WHERE id = ?",
            (learning_id,),
        )
        if learning is not None:
            return "\n".join(
                str(part)
                for part in (
                    learning["title"],
                    learning["description"],
                    f"Prevention: {learning['prevention']}",
                    f"Category: {learning['category']}",
                    f"Severity: {learning['severity']}",
                    f"Tags: {learning['tags']}",
                )
                if part
            )

    if file_path.startswith("inbox_item:"):
        inbox_id = file_path.split(":", 1)[1]
        inbox = _fetch_one_or_none(
            conn,
            "SELECT title, content, tldr, source, status FROM inbox_items WHERE id = ?",
            (inbox_id,),
        )
        if inbox is not None:
            return "\n".join(
                str(part)
                for part in (
                    inbox["title"],
                    inbox["content"],
                    f"TLDR: {inbox['tldr']}",
                    f"Source: {inbox['source']}",
                    f"Status: {inbox['status']}",
                )
                if part
            )

    if file_path.startswith("/") and _is_loadable_document_path(file_path):
        try:
            return Path(file_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    return "\n".join(part for part in (str(title), str(project), str(file_path)) if part)


def _fetch_one_or_none(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple[object, ...],
) -> sqlite3.Row | None:
    try:
        return conn.execute(sql, params).fetchone()
    except sqlite3.OperationalError:
        return None


def _is_loadable_document_path(file_path: str) -> bool:
    path = Path(file_path)
    try:
        return path.is_file() and not path.is_symlink() and path.stat().st_size <= 500_000
    except OSError:
        return False


def _add_task_completion_mode(conn: sqlite3.Connection) -> None:
    """Migration 063 post-hook: add tasks.completion_mode (idempotent).

    Values: 'pr' (default, requires merged PR), 'doc' (research/brainstorm/plan),
    'none' (verify/diagnose/free transition).

    Backfill heuristic for existing in_progress tasks: scan title/tags for
    research/brainstorm/plan/verify keywords and set completion_mode='doc'
    so Fix 2 cleanup can close them via normal PATCH. All other existing rows
    stay on default 'pr' (backward compat — code fixes keep the strict guard).
    """
    if not _column_exists(conn, "tasks", "completion_mode"):
        conn.execute(
            "ALTER TABLE tasks ADD COLUMN completion_mode TEXT NOT NULL DEFAULT 'pr'"
        )
        logger.info("Migration 063: added tasks.completion_mode")

        # Backfill existing in_progress tasks that look like research/planning work.
        # This unblocks Fix 2 cleanup of the 32 orphan tasks without manual PATCH.
        # Heuristic: title starts with research/brainstorm/plan/verify/diagnose/analyze/investigate
        # OR any tag in research-y set. Conservative — only in_progress rows.
        research_keywords = (
            "research",
            "brainstorm",
            "plan",
            "verify",
            "verifi",
            "diagnose",
            "diagnost",
            "analyze",
            "analizza",
            "investigate",
            "indaga",
            "indagar",
        )
        research_tags = {
            "research",
            "brainstorm",
            "plan",
            "planning",
            "verification",
            "verify",
            "investigation",
            "diagnostics",
            "analysis",
        }

        cursor = conn.execute(
            "SELECT id, title, tags FROM tasks WHERE status = 'in_progress'"
        )
        rows = cursor.fetchall()
        backfilled = 0
        for row in rows:
            task_id = row["id"] if isinstance(row, sqlite3.Row) else row[0]
            title = (row["title"] if isinstance(row, sqlite3.Row) else row[1]) or ""
            tags_raw = row["tags"] if isinstance(row, sqlite3.Row) else row[2]
            title_lc = title.lower()
            try:
                tags_list = set(json.loads(tags_raw)) if tags_raw else set()
            except (json.JSONDecodeError, TypeError):
                tags_list = set()
            matches_title = any(
                title_lc.startswith(k) or f" {k}" in title_lc for k in research_keywords
            )
            matches_tags = bool(tags_list & research_tags)
            if matches_title or matches_tags:
                conn.execute(
                    "UPDATE tasks SET completion_mode = 'doc' WHERE id = ?",
                    (task_id,),
                )
                backfilled += 1
        conn.commit()
        logger.info(
            "Migration 063: backfilled %d in_progress tasks to completion_mode='doc'",
            backfilled,
        )


def _migration_070_digest_ranking_inputs_recovery(conn: sqlite3.Connection) -> None:
    """Recovery migration for digest ranking inputs on DBs already past version 69."""
    if not _column_exists(conn, "inbox_items", "domain_key"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN domain_key TEXT")
        logger.info("Migration 070: added inbox_items.domain_key")
    if not _column_exists(conn, "inbox_items", "published_at"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN published_at TEXT")
        logger.info("Migration 070: added inbox_items.published_at")
    if not _column_exists(conn, "inbox_items", "freshness_at"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN freshness_at TEXT")
        logger.info("Migration 070: added inbox_items.freshness_at")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inbox_items_workspace_domain_freshness "
        "ON inbox_items(workspace_id, domain_key, freshness_at DESC, created_at DESC)"
    )
    conn.commit()


def _migration_071_digest_selection_recovery(conn: sqlite3.Connection) -> None:
    """Recovery migration for inbox_digest_selections on DBs already past version 69."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS inbox_digest_selections ("
        "id TEXT PRIMARY KEY, "
        "inbox_item_id TEXT NOT NULL, "
        "digest_cycle_key TEXT NOT NULL, "
        "state TEXT NOT NULL CHECK (state IN ('visible', 'overflow', 'expired')), "
        "domain_key TEXT NOT NULL, "
        "score REAL NOT NULL DEFAULT 0, "
        "rank_in_domain INTEGER, "
        "expires_at TEXT, "
        "workspace_id TEXT NOT NULL DEFAULT 'ws_default', "
        "created_at TEXT DEFAULT (datetime('now','utc')), "
        "updated_at TEXT DEFAULT (datetime('now','utc')), "
        "FOREIGN KEY (inbox_item_id) REFERENCES inbox_items(id) ON DELETE CASCADE"
        ")"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_selection_item_cycle "
        "ON inbox_digest_selections(workspace_id, inbox_item_id, digest_cycle_key)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_selection_active_item "
        "ON inbox_digest_selections(workspace_id, inbox_item_id) "
        "WHERE state IN ('visible', 'overflow')"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_digest_selection_cycle_state_domain "
        "ON inbox_digest_selections(workspace_id, digest_cycle_key, state, domain_key, rank_in_domain)"
    )
    conn.commit()


def _migration_072_digest_app_settings_recovery(conn: sqlite3.Connection) -> None:
    """Recovery migration for digest app_settings defaults on DBs already past version 69."""
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_enabled', 'shadow')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_freeze_hour_utc', '6')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_admission_threshold', '1.0')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_overflow_ttl_days', '3')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_last_cycle_key', '')"
    )
    conn.commit()


def _seed_missing_agents(conn: sqlite3.Connection) -> None:
    """Migration 047 post-hook: seed the retained DevX and System Health agents.

    Must be in Python hook (not SQL) because executescript() + PRAGMA foreign_keys=ON
    causes FK constraint errors when inserting users + agents in the same script.
    """
    agents_base = settings.effective_agents_base
    # (usr_id, slug, display, color, agt_id, agt_type, model, desc, agent_dir)
    agents = [
        (
            "usr_devx",
            "devx",
            "DevX",
            "#EF4444",
            "agt-devx",
            "system",
            "sonnet",
            "DevX Session Monitor",
            "devx",
        ),
        (
            "usr_system_health",
            "system-health",
            "System Health",
            "#10B981",
            "agt-system-health",
            "system",
            "haiku",
            "System Health Check",
            "system-monitor",
        ),
    ]
    for (
        usr_id,
        slug,
        display,
        color,
        agt_id,
        agt_type,
        model,
        desc,
        agent_dir,
    ) in agents:
        soul_path = f"{agents_base}/{agent_dir}/SOUL.md"
        tools_path = f"{agents_base}/{agent_dir}/TOOLS.md"
        identity_path = f"{agents_base}/{agent_dir}/IDENTITY.md"
        conn.execute(
            "INSERT OR IGNORE INTO users (id, slug, display_name, type, avatar_color, system_role, created_at, updated_at) "
            "VALUES (?, ?, ?, 'agent', ?, 'operator', datetime('now','utc'), datetime('now','utc'))",
            [usr_id, slug, display, color],
        )
        conn.execute(
            "INSERT OR IGNORE INTO agents (id, user_id, scheduler_agent_id, agent_type, model, status, description, "
            "soul_path, tools_path, identity_path, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, datetime('now','utc'), datetime('now','utc'))",
            [
                agt_id,
                usr_id,
                slug,
                agt_type,
                model,
                desc,
                soul_path,
                tools_path,
                identity_path,
            ],
        )
    conn.commit()
    logger.info("Migration 047: seeded DevX + System Health agents")


def _fix_agent_paths_and_roles(conn: sqlite3.Connection) -> None:
    """Migration 048 post-hook: fix retained agent paths and roles."""
    agents_base = settings.effective_agents_base
    # Fix paths for the retained system agents (Bug 1: were NULL).
    path_fixes = [
        ("agt-devx", "devx"),
        ("agt-system-health", "system-monitor"),
    ]
    for agt_id, agent_dir in path_fixes:
        conn.execute(
            "UPDATE agents SET soul_path = ?, tools_path = ?, identity_path = ?, updated_at = datetime('now','utc') WHERE id = ?",
            [
                f"{agents_base}/{agent_dir}/SOUL.md",
                f"{agents_base}/{agent_dir}/TOOLS.md",
                f"{agents_base}/{agent_dir}/IDENTITY.md",
                agt_id,
            ],
        )
    # Fix analyst paths (Bug 2: pointed to .openclaw which is root-only)
    conn.execute(
        "UPDATE agents SET soul_path = ?, tools_path = ?, identity_path = ?, updated_at = datetime('now','utc') WHERE id = 'agt-analyst'",
        [
            f"{agents_base}/analyst/SOUL.md",
            f"{agents_base}/analyst/TOOLS.md",
            f"{agents_base}/analyst/IDENTITY.md",
        ],
    )
    # Fix system_role from 'agent' to 'operator' for the retained agent users (Bug 4).
    conn.execute(
        "UPDATE users SET system_role = 'operator', updated_at = datetime('now','utc') "
        "WHERE id IN ('usr_devx', 'usr_system_health') AND system_role = 'agent'"
    )
    conn.commit()
    logger.info("Migration 048: fixed agent paths + system_role")


def _migration_049_agent_role_and_learnings(conn: sqlite3.Connection) -> None:
    """Migration 049: normalize agent roles + add learnings schema for REM consolidation."""
    # 1. Normalize agent roles to 'operator' (compatible with existing CHECK constraint).
    # Some agents may be 'admin' (from a prior hotfix) or 'agent' (from migration 018).
    # DB-driven (every type='agent' user) so no agent slugs are hardcoded in core.
    conn.execute(
        "UPDATE users SET system_role = 'operator', updated_at = datetime('now','utc') "
        "WHERE type = 'agent' AND system_role != 'operator'"
    )

    # 2. Seed the deploy's configured self-improvement / consolidation agents.
    # Slugs come from settings.self_improvement_agents (deploy .env); OSS core
    # hardcodes no internal agent names. Idempotent; inert on prod (already ran).
    for slug in settings.self_improvement_agents:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, slug, display_name, type, system_role, "
            "avatar_color, created_at, updated_at) "
            "VALUES (?, ?, ?, 'agent', 'operator', '#8B5CF6', datetime('now','utc'), datetime('now','utc'))",
            (f"usr_{slug}", slug, slug),
        )

    # 3. Add last_accessed_at to documents (spaced repetition tracking)
    if not _column_exists(conn, "documents", "last_accessed_at"):
        conn.execute("ALTER TABLE documents ADD COLUMN last_accessed_at TEXT")
        logger.info("Migration 049: added documents.last_accessed_at")

    # 4. Add status + consolidated_from to learnings (draft lifecycle + anti-cycle)
    if not _column_exists(conn, "learnings", "status"):
        conn.execute(
            "ALTER TABLE learnings ADD COLUMN status TEXT NOT NULL DEFAULT 'active'"
        )
        logger.info("Migration 049: added learnings.status")
    if not _column_exists(conn, "learnings", "consolidated_from"):
        conn.execute("ALTER TABLE learnings ADD COLUMN consolidated_from TEXT")
        logger.info("Migration 049: added learnings.consolidated_from")

    conn.commit()
    logger.info(
        "Migration 049: agent roles normalized + learnings schema + access tracking"
    )


def _backfill_inbox_status_from_treatment(conn: sqlite3.Connection) -> None:
    """Migration 058 post-hook: backfill inbox_items.status from treatment.

    Idempotent: only updates rows still at 'received' status.
    """
    cursor = conn.execute(
        """
        UPDATE inbox_items SET status = CASE treatment
            WHEN 'read'      THEN 'unread'
            WHEN 'save'      THEN 'saved'
            WHEN 'read_save' THEN 'unread'
            WHEN 'ignore'    THEN 'auto_ignored'
            ELSE 'unread'
        END
        WHERE status = 'received'
        """
    )
    conn.commit()
    logger.info(
        "Migration 058: backfilled %d inbox_items status from treatment",
        cursor.rowcount,
    )


def _add_deep_research_column(conn: sqlite3.Connection) -> None:
    """Migration 059 post-hook: add deep_research column to inbox_items if missing."""
    if not _column_exists(conn, "inbox_items", "deep_research"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN deep_research TEXT")
        conn.commit()
        logger.info("Migration 059: added inbox_items.deep_research column")


def _cleanup_generic_source_scores(conn: sqlite3.Connection) -> None:
    """Remove generic source score entries (rss-marvisx, gmail-marvisx) that are no longer useful."""
    cursor = conn.execute(
        "DELETE FROM source_scores WHERE source_key IN ('rss-marvisx', 'gmail-marvisx')"
    )
    conn.commit()
    if cursor.rowcount > 0:
        logger.info("Cleaned up %d generic source_scores entries", cursor.rowcount)


def _add_sent_in_newsletter_column(conn: sqlite3.Connection) -> None:
    """Migration 060 post-hook: add sent_in_newsletter column to inbox_items if missing."""
    if not _column_exists(conn, "inbox_items", "sent_in_newsletter"):
        conn.execute("ALTER TABLE inbox_items ADD COLUMN sent_in_newsletter TEXT")
        conn.commit()
        logger.info("Migration 060: added inbox_items.sent_in_newsletter column")


def _migration_061_backfill_sources(conn: sqlite3.Connection) -> None:
    """Migration 061 post-hook: backfill inbox_sources from distinct inbox_items.source.

    Normalizes source_key the SAME way as _update_source_score in inbox_triage:
    - URLs -> parsed netloc with optional www. prefix removed
    - non-URLs -> lowercase trimmed raw string

    Idempotent: INSERT OR IGNORE on the unique (workspace_id, source_key) index.
    Collisions (two raw sources that normalize to the same key) are logged but
    do not fail the migration.
    """
    from urllib.parse import urlparse

    if not _column_exists(conn, "inbox_items", "source"):
        logger.info("Migration 061: inbox_items.source missing, skipping backfill")
        return

    cursor = conn.execute(
        "SELECT DISTINCT source, COALESCE(workspace_id, 'ws_default') AS ws "
        "FROM inbox_items WHERE source IS NOT NULL AND source != ''"
    )
    rows = cursor.fetchall()

    seen_keys: set[tuple[str, str]] = set()
    collisions = 0
    inserted = 0

    for row in rows:
        raw_source = row["source"] if isinstance(row, sqlite3.Row) else row[0]
        ws = row["ws"] if isinstance(row, sqlite3.Row) else row[1]
        if not raw_source:
            continue

        source_key = raw_source
        try:
            parsed = urlparse(raw_source)
            if parsed.netloc:
                source_key = parsed.netloc.removeprefix("www.").lower()
            else:
                source_key = raw_source.strip().lower()
        except Exception:  # noqa: BLE001 - defensive, never fail migration
            source_key = raw_source.strip().lower()

        if not source_key:
            continue

        key_tuple = (ws, source_key)
        if key_tuple in seen_keys:
            collisions += 1
            continue
        seen_keys.add(key_tuple)

        result = conn.execute(
            "INSERT OR IGNORE INTO inbox_sources "
            "(id, name, source_key, source_type, active, workspace_id) "
            "VALUES (?, ?, ?, 'legacy', 1, ?)",
            (str(uuid_mod.uuid4()), raw_source[:200], source_key, ws),
        )
        if result.rowcount > 0:
            inserted += 1

    conn.commit()
    logger.info(
        "Migration 061: backfilled inbox_sources (inserted=%d, collisions=%d, total_distinct=%d)",
        inserted,
        collisions,
        len(rows),
    )


def _migration_062_backfill_from_urls(conn: sqlite3.Connection) -> None:
    """Migration 062 post-hook: re-backfill inbox_sources from URL domains.

    Migration 061 populated inbox_sources from inbox_items.source, but in
    production that column holds generic strings ("rss-marvisx", "gmail", ...)
    while the real article domain lives in inbox_items.url. The Sources
    Dashboard joins inbox_sources against source_scores, and source_scores
    is keyed by URL domain (see _update_source_score in inbox_triage), so the
    061 entries never matched any score row and all metrics rendered as zero.

    This hook extracts the real domain from DISTINCT inbox_items.url rows
    using the same urlparse + removeprefix("www.") + lowercase normalization
    that _update_source_score uses (modulo the explicit lowercase, which this
    hook applies defensively so case differences never break the JOIN).
    The legacy 061 rows are soft-deleted in the SQL portion of this migration
    (source_type='legacy', active=0) and left in place for audit history.

    Idempotent via the UNIQUE (workspace_id, source_key) index on
    inbox_sources; re-runs are safe and only log zero insertions.
    """
    from urllib.parse import urlparse

    if not _column_exists(conn, "inbox_items", "url"):
        logger.info("Migration 062: inbox_items.url missing, skipping backfill")
        return

    cursor = conn.execute(
        "SELECT DISTINCT url, COALESCE(workspace_id, 'ws_default') AS ws "
        "FROM inbox_items "
        "WHERE url IS NOT NULL AND url != ''"
    )
    rows = cursor.fetchall()

    seen: set[tuple[str, str]] = set()
    inserted = 0
    skipped = 0

    for row in rows:
        url = row["url"] if isinstance(row, sqlite3.Row) else row[0]
        ws = row["ws"] if isinstance(row, sqlite3.Row) else row[1]
        if not url:
            continue

        try:
            parsed = urlparse(url)
            netloc = (parsed.netloc or "").removeprefix("www.").lower()
        except Exception:  # noqa: BLE001 - defensive, never fail migration
            netloc = ""

        if not netloc:
            skipped += 1
            continue

        key = (ws, netloc)
        if key in seen:
            continue
        seen.add(key)

        result = conn.execute(
            "INSERT OR IGNORE INTO inbox_sources "
            "(id, name, source_key, source_type, active, workspace_id) "
            "VALUES (?, ?, ?, 'rss', 1, ?)",
            (str(uuid_mod.uuid4()), netloc, netloc, ws),
        )
        if result.rowcount > 0:
            inserted += 1

    conn.commit()
    logger.info(
        "Migration 062: backfilled inbox_sources from URL domains "
        "(inserted=%d, skipped_no_netloc=%d, distinct_urls=%d)",
        inserted,
        skipped,
        len(rows),
    )


def _promote_llm_costs_columns(conn: sqlite3.Connection) -> None:
    """Migration 102 post-hook: ALTER llm_costs to add tier_logical / fallback_used / litellm_request_id.

    The SQL migration 102 only does CREATE TABLE IF NOT EXISTS (idempotent,
    fresh DBs get the full new schema directly). Production DBs already had
    the table lazy-created by inbox_llm_classifier with the old 8-column
    schema; this hook adds the 3 new columns guarded by _column_exists().

    Why a hook instead of pure SQL: SQLite has no `ALTER TABLE ... ADD COLUMN
    IF NOT EXISTS`, and bare ALTER inside an executescript() raises
    "duplicate column name" on fresh DBs (where CREATE already provisioned
    them) which would abort the whole script and leave the migration in an
    inconsistent state. The hook runs Python-side after CREATE so we can
    branch safely.
    """
    if not _column_exists(conn, "llm_costs", "tier_logical"):
        conn.execute("ALTER TABLE llm_costs ADD COLUMN tier_logical TEXT")
        logger.info("Migration 102: added llm_costs.tier_logical")
    if not _column_exists(conn, "llm_costs", "fallback_used"):
        conn.execute(
            "ALTER TABLE llm_costs ADD COLUMN fallback_used INTEGER NOT NULL DEFAULT 0"
        )
        logger.info("Migration 102: added llm_costs.fallback_used")
    if not _column_exists(conn, "llm_costs", "litellm_request_id"):
        conn.execute("ALTER TABLE llm_costs ADD COLUMN litellm_request_id TEXT")
        logger.info("Migration 102: added llm_costs.litellm_request_id")
    conn.commit()


def _repair_versioned_schema_invariants(
    conn: sqlite3.Connection, known_versions: set[int]
) -> None:
    """Repair idempotent post-hook state already claimed by schema_versions."""
    needed = _claimed_security_repairs_needed(conn, known_versions)
    if 175 in needed and _table_exists(conn, "audit_log"):
        _migration_175_audit_chain(conn)
    if 176 in needed and _table_exists(conn, "audit_log"):
        _migration_176_activate_audit_chain(conn)
    if 177 in needed and _table_exists(conn, "delegations"):
        _migration_177_delegation_workspace(conn)
    if 178 in needed and _table_exists(conn, "agent_tokens"):
        _migration_178_agent_token_lifecycle(conn)
    if 179 in needed and _table_exists(conn, "access_grants"):
        _migration_179_workspace_isolation(conn)
    if 187 in needed and _table_exists(conn, "project_lifecycle_bootstrap"):
        _migration_187_project_lifecycle_bootstrap(conn)


def _claimed_schema_version(conn: sqlite3.Connection) -> int:
    """Read the version marker without creating schema_versions or repairing state."""
    if not _table_exists(conn, "schema_versions"):
        return 0
    row = conn.execute("SELECT MAX(version) FROM schema_versions").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def _sqlite_object_exists(
    conn: sqlite3.Connection, object_type: str, name: str
) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?",
            (object_type, name),
        ).fetchone()
    )


def _claimed_security_repairs_needed(
    conn: sqlite3.Connection, known_versions: set[int]
) -> set[int]:
    """Return claimed security hooks that need a write to restore invariants.

    This preflight is intentionally read-only: a normal startup with all
    concrete artifacts present does not acquire the controlled-migration
    quiescence requirement or execute a no-op repair hook.
    """
    if not known_versions or not _table_exists(conn, "schema_versions"):
        return set()
    claimed = _claimed_schema_version(conn)
    if claimed > max(known_versions):
        # The older-image guard owns unknown schemas; never infer repairs.
        return set()

    needed: set[int] = set()
    if 175 in known_versions and claimed >= 175 and _table_exists(conn, "audit_log"):
        required_columns = (
            "workspace_id",
            "workspace_sequence",
            "previous_hash",
            "entry_hash",
            "hash_version",
        )
        state_ready = (
            _table_exists(conn, "audit_chain_state")
            and _column_exists(conn, "audit_chain_state", "legacy_root_hash")
            and conn.execute(
                "SELECT 1 FROM audit_chain_state "
                "WHERE id = 1 AND legacy_root_hash IS NOT NULL"
            ).fetchone()
            is not None
        )
        if (
            not state_ready
            or not _table_exists(conn, "audit_chain_heads")
            or any(not _column_exists(conn, "audit_log", column) for column in required_columns)
            or not _sqlite_object_exists(
                conn, "index", "idx_audit_log_workspace_sequence"
            )
            or not _sqlite_object_exists(conn, "trigger", "audit_log_chain_shape")
            or not _sqlite_object_exists(
                conn, "trigger", "audit_log_chainless_after_activation"
            )
        ):
            needed.add(175)

    if (
        176 in known_versions
        and claimed >= 176
        and (
            _table_exists(conn, "audit_chain_state")
            or _table_exists(conn, "audit_log")
        )
    ):
        active = (
            conn.execute(
                "SELECT enforcement_enabled, activated_at, legacy_root_hash "
                "FROM audit_chain_state WHERE id = 1"
            ).fetchone()
            if _table_exists(conn, "audit_chain_state")
            else None
        )
        if active is None or int(active[0]) != 1 or not active[1] or not active[2]:
            needed.add(176)

    if 177 in known_versions and claimed >= 177 and _table_exists(conn, "delegations"):
        if (
            not _column_exists(conn, "delegations", "workspace_id")
            or _sqlite_object_exists(conn, "index", "idx_delegations_agent_active")
            or not _sqlite_object_exists(
                conn, "index", "idx_delegations_workspace_agent_active"
            )
            or not _sqlite_object_exists(
                conn, "trigger", "delegations_workspace_required"
            )
        ):
            needed.add(177)

    if 178 in known_versions and claimed >= 178 and _table_exists(conn, "agent_tokens"):
        required_columns = (
            "principal_id",
            "principal_type",
            "label",
            "issued_at",
            "expires_at",
            "revoked_at",
            "revoked_by",
            "rotation_family_id",
            "supersedes_id",
            "overlap_until",
            "acknowledged_at",
            "acknowledgement_actor",
            "credential_kind",
        )
        backfill_needed = False
        if all(_column_exists(conn, "agent_tokens", column) for column in required_columns):
            backfill_needed = bool(
                conn.execute(
                    "SELECT 1 FROM agent_tokens WHERE "
                    "(issued_at IS NULL AND created_at IS NOT NULL) "
                    "OR rotation_family_id IS NULL "
                    "OR credential_kind IS NULL "
                    "OR (principal_type IS NULL AND principal_id IS NOT NULL) LIMIT 1"
                ).fetchone()
            )
        if (
            any(not _column_exists(conn, "agent_tokens", column) for column in required_columns)
            or backfill_needed
            or _sqlite_object_exists(conn, "index", "idx_agent_tokens_active")
            or not _sqlite_object_exists(
                conn, "index", "idx_agent_tokens_workspace_principal_active"
            )
            or not _sqlite_object_exists(
                conn, "index", "idx_agent_tokens_rotation_family"
            )
            or not _sqlite_object_exists(
                conn, "index", "idx_agent_tokens_live_successor"
            )
            or not _sqlite_object_exists(
                conn, "trigger", "agent_tokens_individual_shape_insert"
            )
            or not _sqlite_object_exists(
                conn, "trigger", "agent_tokens_individual_shape_update"
            )
        ):
            needed.add(178)

    if 179 in known_versions and claimed >= 179 and _table_exists(conn, "access_grants"):
        file_meta_present = _table_exists(conn, "file_meta")
        required_triggers = (
            "access_grants_workspace_required_insert",
            "access_grants_workspace_required_update",
            "access_grants_workspace_immutable",
        ) + (
            (
                "file_meta_workspace_required_insert",
                "file_meta_workspace_required_update",
                "file_meta_workspace_immutable",
            )
            if file_meta_present
            else ()
        )
        required_indexes = (
            "idx_tasks_workspace_id",
            "idx_prs_workspace_task_status_created",
            "idx_prs_workspace_project_status_created",
            "idx_learnings_workspace_id",
        )
        if (
            not _column_exists(conn, "access_grants", "workspace_id")
            or (file_meta_present and not _column_exists(conn, "file_meta", "workspace_id"))
            or not _table_exists(conn, "workspace_projects")
            or not _unique_index_has_columns(
                conn, "access_grants", ("workspace_id", "identity", "project_slug")
            )
            or (
                file_meta_present
                and not _unique_index_has_columns(
                    conn, "file_meta", ("workspace_id", "project_slug", "rel_path")
                )
            )
            or any(
                not _sqlite_object_exists(conn, "trigger", trigger)
                for trigger in required_triggers
            )
            or any(
                not _sqlite_object_exists(conn, "index", index)
                for index in required_indexes
            )
        ):
            needed.add(179)

    if 187 in known_versions and claimed >= 187:
        bootstrap_schema_present = all(
            _table_exists(conn, table)
            for table in (
                "project_lifecycle_bootstrap",
                "project_lifecycle_state",
            )
        )
        if bootstrap_schema_present and not _migration_187_bootstrap_evidence_valid(
            conn
        ):
            needed.add(187)

    return needed


def _migration_187_bootstrap_evidence_valid(conn: sqlite3.Connection) -> bool:
    """Validate the immutable v187 bootstrap marker and DB-owned coordinates."""
    if not _migration_187_trigger_contract_valid(conn):
        return False
    if not all(
        _table_exists(conn, table)
        for table in (
            "project_lifecycle_bootstrap",
            "project_lifecycle_state",
        )
    ):
        return False
    required_marker_columns = (
        "state",
        "project_count",
        "archived_count",
        "snapshot_digest",
        "completed_at",
    )
    if not all(
        _column_exists(conn, "project_lifecycle_bootstrap", column)
        for column in required_marker_columns
    ):
        return False
    marker_rows = conn.execute(
        "SELECT state,project_count,archived_count,snapshot_digest,completed_at "
        "FROM project_lifecycle_bootstrap WHERE id=1"
    ).fetchall()
    if len(marker_rows) != 1:
        return False
    state, project_count, archived_count, snapshot_digest, completed_at = marker_rows[0]
    digest = str(snapshot_digest or "")
    if (
        state != "complete"
        or not isinstance(project_count, int)
        or not isinstance(archived_count, int)
        or project_count < archived_count
        or archived_count < 0
        or len(digest) != 64
        or digest != digest.lower()
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or not str(completed_at or "").strip()
    ):
        return False
    counts = conn.execute(
        "SELECT COUNT(DISTINCT project_slug),"
        "COUNT(DISTINCT CASE WHEN lifecycle='archived' THEN project_slug END) "
        "FROM project_lifecycle_state"
    ).fetchone()
    if (
        counts is None
        or project_count > int(counts[0] or 0)
        or archived_count > int(counts[1] or 0)
    ):
        return False
    if _table_exists(conn, "workspace_projects"):
        missing_owner = conn.execute(
            "SELECT 1 FROM workspace_projects owner WHERE NOT EXISTS ("
            "SELECT 1 FROM project_lifecycle_state state "
            "WHERE state.workspace_id=owner.workspace_id "
            "AND state.project_slug=owner.project_slug) LIMIT 1"
        ).fetchone()
        if missing_owner is not None:
            return False
    if _table_exists(conn, "tasks") and all(
        _column_exists(conn, "tasks", column)
        for column in ("project", "workspace_id")
    ):
        missing_task = conn.execute(
            "SELECT 1 FROM tasks task WHERE task.project IS NOT NULL "
            "AND length(trim(task.project))>0 AND NOT EXISTS (SELECT 1 "
            "FROM project_lifecycle_state state WHERE state.workspace_id="
            "COALESCE(NULLIF(trim(task.workspace_id),''),'ws_default') "
            "AND state.project_slug=task.project) LIMIT 1"
        ).fetchone()
        if missing_task is not None:
            return False
    if (
        _table_exists(conn, "pull_requests")
        and _table_exists(conn, "tasks")
        and _table_exists(conn, "workspace_projects")
        and all(
            _column_exists(conn, "pull_requests", column)
            for column in ("project", "task_id", "workspace_id")
        )
    ):
        missing_pull_request = conn.execute(
            "SELECT 1 FROM pull_requests pr WHERE NOT EXISTS (SELECT 1 "
            "FROM project_lifecycle_state state WHERE state.workspace_id=COALESCE("
            "(SELECT NULLIF(trim(task.workspace_id),'') FROM tasks task "
            "WHERE task.id=pr.task_id),NULLIF(trim(pr.workspace_id),''),"
            "(SELECT MIN(owner.workspace_id) FROM workspace_projects owner "
            "WHERE owner.project_slug=pr.project HAVING "
            "COUNT(DISTINCT owner.workspace_id)=1),'ws_default') "
            "AND state.project_slug=pr.project) LIMIT 1"
        ).fetchone()
        if missing_pull_request is not None:
            return False
    return True


def _migration_187_project_lifecycle_bootstrap(
    conn: sqlite3.Connection,
    *,
    projects_root: Path | None = None,
) -> None:
    """Seed exact filesystem lifecycle before v187 writers can serve traffic.

    SQL cannot read ``project.yaml``.  This quiesced, idempotent post-hook pins
    every existing project to its current lifecycle and content digest so a
    legacy archived project is never auto-created as writable by the first DB
    writer.  A durable completion marker makes a failed hook repairable without
    silently re-reading changed files on every startup.
    """
    if not _table_exists(conn, "project_lifecycle_bootstrap") or not _table_exists(
        conn, "project_lifecycle_state"
    ):
        raise RuntimeError("Migration 187 lifecycle bootstrap schema is incomplete")

    marker = conn.execute(
        "SELECT state FROM project_lifecycle_bootstrap WHERE id=1"
    ).fetchone()
    if marker is None:
        raise RuntimeError("Migration 187 lifecycle bootstrap marker is missing")
    if str(marker[0]) == "complete":
        if not _migration_187_bootstrap_evidence_valid(conn):
            raise RuntimeError(
                "Migration 187 completed lifecycle bootstrap evidence is invalid"
            )
        return

    if projects_root is None:
        configured = os.environ.get("MARVIS_PROJECTS_ROOT", "").strip()
        if configured:
            root = Path(configured).expanduser()
        else:
            from core.platform import projects_root_default

            root = projects_root_default()
    else:
        root = Path(projects_root).expanduser()

    if root.is_symlink():
        raise RuntimeError("Migration 187 projects root cannot be a symlink")
    root = root.resolve()

    bindings: dict[str, set[str]] = {}
    binding_sources = (
        ("workspace_projects", "project_slug", "workspace_id"),
        ("access_grants", "project_slug", "workspace_id"),
        ("tasks", "project", "workspace_id"),
        ("learnings", "project", "workspace_id"),
        ("pull_requests", "project", "workspace_id"),
        ("todos", "project", "workspace_id"),
        ("file_meta", "project_slug", "workspace_id"),
        ("documents", "project", "workspace_id"),
        ("ingest_pending", "project_slug", "workspace_id"),
        ("project_gui_metadata", "project_slug", "workspace_id"),
        ("project_status_updates", "project", None),
    )
    for table, slug_column, workspace_column in binding_sources:
        if not _table_exists(conn, table) or not _column_exists(
            conn, table, slug_column
        ):
            continue
        if workspace_column is not None and not _column_exists(
            conn, table, workspace_column
        ):
            workspace_column = None
        workspace_expression = (
            f'"{workspace_column}"' if workspace_column is not None else "NULL"
        )
        rows = conn.execute(
            f'SELECT DISTINCT "{slug_column}",{workspace_expression} '
            f'FROM "{table}" WHERE "{slug_column}" IS NOT NULL '
            f'AND length(trim("{slug_column}")) > 0'
        ).fetchall()
        for raw_slug, raw_workspace in rows:
            slug = str(raw_slug).strip()
            workspace_id = str(raw_workspace or "").strip() or "ws_default"
            bindings.setdefault(slug, set()).add(workspace_id)

    if (
        _table_exists(conn, "pull_requests")
        and _table_exists(conn, "tasks")
        and _table_exists(conn, "workspace_projects")
        and all(
            _column_exists(conn, "pull_requests", column)
            for column in ("project", "task_id", "workspace_id")
        )
    ):
        for raw_slug, raw_workspace in conn.execute(
            "SELECT pr.project,COALESCE("
            "(SELECT NULLIF(trim(task.workspace_id),'') FROM tasks task "
            "WHERE task.id=pr.task_id),NULLIF(trim(pr.workspace_id),''),"
            "(SELECT MIN(owner.workspace_id) FROM workspace_projects owner "
            "WHERE owner.project_slug=pr.project HAVING "
            "COUNT(DISTINCT owner.workspace_id)=1),'ws_default') "
            "FROM pull_requests pr WHERE pr.project IS NOT NULL "
            "AND length(trim(pr.project))>0"
        ).fetchall():
            slug = str(raw_slug).strip()
            workspace_id = str(raw_workspace or "").strip() or "ws_default"
            bindings.setdefault(slug, set()).add(workspace_id)

    if _table_exists(conn, "comments") and all(
        _column_exists(conn, "comments", column)
        for column in ("target_type", "target_id")
    ):
        for row in conn.execute(
            "SELECT DISTINCT target_id FROM comments "
            "WHERE target_type='project' AND target_id IS NOT NULL "
            "AND length(trim(target_id)) > 0"
        ).fetchall():
            bindings.setdefault(str(row[0]).strip(), set()).add("ws_default")

    known_slugs = set(bindings)

    if not root.exists():
        if known_slugs:
            raise RuntimeError(
                "Migration 187 projects root is missing for existing project state"
            )
        project_files: list[Path] = []
    else:
        if not root.is_dir():
            raise RuntimeError("Migration 187 projects root is not a directory")
        project_files = []
        for child in sorted(root.iterdir(), key=lambda item: item.name):
            if child.is_symlink():
                raise RuntimeError(
                    f"Migration 187 project directory is a symlink: {child.name}"
                )
            metadata_path = child / "project.yaml"
            if child.is_dir() and metadata_path.exists():
                if metadata_path.is_symlink() or not metadata_path.is_file():
                    raise RuntimeError(
                        f"Migration 187 project metadata is not a regular file: {child.name}"
                    )
                project_files.append(metadata_path)

    filesystem_slugs = {path.parent.name for path in project_files}
    missing_slugs = sorted(known_slugs - filesystem_slugs)

    import yaml

    allowed_lifecycles = {"idea", "planning", "active", "maintenance", "archived"}
    snapshot: list[dict[str, str]] = []
    archived_slugs: set[str] = set()
    unsupported_lifecycle_slugs: set[str] = set()
    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for metadata_path in project_files:
        slug = metadata_path.parent.name
        if not re.fullmatch(r"[a-z0-9][a-z0-9&+_.\-]{0,62}", slug):
            raise RuntimeError(f"Migration 187 invalid project slug: {slug}")
        raw = metadata_path.read_bytes()
        try:
            metadata = yaml.safe_load(raw.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise RuntimeError(
                f"Migration 187 invalid project metadata: {slug}"
            ) from exc
        if not isinstance(metadata, dict):
            raise RuntimeError(f"Migration 187 invalid project metadata: {slug}")
        declared_lifecycle = (
            metadata["lifecycle"] if "lifecycle" in metadata else "active"
        )
        unsupported_lifecycle = (
            not isinstance(declared_lifecycle, str)
            or declared_lifecycle not in allowed_lifecycles
        )
        lifecycle = "archived" if unsupported_lifecycle else declared_lifecycle
        archived_by = None
        if unsupported_lifecycle:
            unsupported_lifecycle_slugs.add(slug)
            archived_by = "migration:187:unsupported_lifecycle_quarantine"
        elif lifecycle == "archived":
            archived_by = "migration:187"
        if lifecycle == "archived":
            archived_slugs.add(slug)
        project_digest = hashlib.sha256(raw).hexdigest()

        workspace_ids = sorted(bindings.get(slug) or {"ws_default"})

        for workspace_id in workspace_ids:
            existing = conn.execute(
                "SELECT lifecycle,project_digest FROM project_lifecycle_state "
                "WHERE workspace_id=? AND project_slug=?",
                (workspace_id, slug),
            ).fetchone()
            if existing is not None and (
                str(existing[0]) != lifecycle or str(existing[1] or "") != project_digest
            ):
                raise RuntimeError(
                    "Migration 187 lifecycle bootstrap conflicts with persisted state "
                    f"for {workspace_id}:{slug}"
                )
            archived_at = (
                completed_at
                if lifecycle == "archived"
                else None
            )
            conn.execute(
                "INSERT OR IGNORE INTO project_lifecycle_state "
                "(workspace_id,project_slug,project_id,lifecycle,project_digest,"
                "archived_at,archived_by) VALUES (?,?,?,?,?,?,?)",
                (
                    workspace_id,
                    slug,
                    "prj_" + uuid_mod.uuid4().hex,
                    lifecycle,
                    project_digest,
                    archived_at,
                    archived_by,
                ),
            )
            snapshot.append(
                {
                    "workspace_id": workspace_id,
                    "project_slug": slug,
                    "lifecycle": lifecycle,
                    "project_digest": project_digest,
                }
            )

    # A database-only slug has no canonical lifecycle bytes to trust.  Keep all
    # historical rows, but seed an archived state with a deterministic sentinel
    # digest so lazy writer triggers cannot silently recreate it as active.  The
    # distinct archived_by marker makes this quarantine reversible by a later
    # governed reconciliation once real metadata exists.
    for slug in missing_slugs:
        project_digest = hashlib.sha256(
            f"migration:187:missing_metadata:{slug}".encode("utf-8")
        ).hexdigest()
        archived_slugs.add(slug)
        for workspace_id in sorted(bindings[slug]):
            existing = conn.execute(
                "SELECT lifecycle,project_digest,archived_by "
                "FROM project_lifecycle_state "
                "WHERE workspace_id=? AND project_slug=?",
                (workspace_id, slug),
            ).fetchone()
            if existing is not None and (
                str(existing[0]) != "archived"
                or str(existing[1] or "") != project_digest
                or str(existing[2] or "")
                != "migration:187:missing_metadata_quarantine"
            ):
                raise RuntimeError(
                    "Migration 187 missing-metadata quarantine conflicts with "
                    f"persisted state for {workspace_id}:{slug}"
                )
            conn.execute(
                "INSERT OR IGNORE INTO project_lifecycle_state "
                "(workspace_id,project_slug,project_id,lifecycle,project_digest,"
                "archived_at,archived_by) VALUES (?,?,?,?,?,?,?)",
                (
                    workspace_id,
                    slug,
                    "prj_" + uuid_mod.uuid4().hex,
                    "archived",
                    project_digest,
                    completed_at,
                    "migration:187:missing_metadata_quarantine",
                ),
            )
            snapshot.append(
                {
                    "workspace_id": workspace_id,
                    "project_slug": slug,
                    "lifecycle": "archived",
                    "project_digest": project_digest,
                }
            )

    snapshot_digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    conn.execute(
        "UPDATE project_lifecycle_bootstrap SET state='complete',project_count=?,"
        "archived_count=?,snapshot_digest=?,completed_at=? WHERE id=1 AND state='pending'",
        (
            len(filesystem_slugs | set(missing_slugs)),
            len(archived_slugs),
            snapshot_digest,
            completed_at,
        ),
    )
    conn.commit()
    logger.info(
        "Migration 187: pinned %d project lifecycle records "
        "(%d archived, %d missing-metadata quarantined, "
        "%d unsupported-lifecycle quarantined)",
        len(snapshot),
        len(archived_slugs),
        len(missing_slugs),
        len(unsupported_lifecycle_slugs),
    )


def _iter_legacy_audit_rows_for_v1_hash(
    conn: sqlite3.Connection,
) -> Iterator[dict[str, Any]]:
    """Stream legacy audit rows in the stable order required by v1 hashing."""
    return (
        {
            "id": row[0],
            "timestamp": row[1],
            "action": row[2],
            "user": row[3],
            "resource_type": row[4],
            "resource_id": row[5],
            "details_json": row[6],
        }
        for row in conn.execute(
            "SELECT id, timestamp, action, user, resource_type, resource_id, "
            "details_json FROM audit_log "
            "WHERE workspace_id IS NULL AND workspace_sequence IS NULL "
            "AND previous_hash IS NULL AND entry_hash IS NULL "
            "AND hash_version IS NULL "
            "ORDER BY timestamp COLLATE BINARY, "
            "CASE WHEN id IS NULL THEN 'None' ELSE CAST(id AS TEXT) END "
            "COLLATE BINARY, id, rowid"
        )
    )


def _migration_175_audit_chain(conn: sqlite3.Connection) -> None:
    """Install/repair the additive v175 audit-chain schema.

    SQLite has no portable ``ALTER TABLE ADD COLUMN IF NOT EXISTS``. Every
    operation here is guarded or ``IF NOT EXISTS`` so a retry can finish after
    either a partially executed hook or a version row committed before a hook
    failure. Existing rows and the migration-145 immutability triggers are never
    removed or rewritten. Activation is inserted only when state is absent and
    therefore cannot be reset by a retry.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_chain_heads ("
        "workspace_id TEXT PRIMARY KEY CHECK (length(trim(workspace_id)) > 0), "
        "last_sequence INTEGER NOT NULL CHECK (last_sequence >= 0), "
        "last_entry_hash TEXT NOT NULL CHECK ("
        "length(last_entry_hash) = 64 "
        "AND last_entry_hash = lower(last_entry_hash) "
        "AND last_entry_hash NOT GLOB '*[^0-9a-f]*'), "
        "hash_version INTEGER NOT NULL DEFAULT 1 CHECK (hash_version = 1), "
        "updated_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS audit_chain_state ("
        "id INTEGER PRIMARY KEY CHECK (id = 1), "
        "enforcement_enabled INTEGER NOT NULL DEFAULT 0 "
        "CHECK (enforcement_enabled IN (0, 1)), "
        "activated_at TEXT, "
        "legacy_root_hash TEXT CHECK (legacy_root_hash IS NULL OR ("
        "length(legacy_root_hash) = 64 "
        "AND legacy_root_hash = lower(legacy_root_hash) "
        "AND legacy_root_hash NOT GLOB '*[^0-9a-f]*')), "
        "CHECK ((enforcement_enabled = 0 AND activated_at IS NULL) "
        "OR (enforcement_enabled = 1 AND activated_at IS NOT NULL)))"
    )
    if not _column_exists(conn, "audit_chain_state", "legacy_root_hash"):
        conn.execute("ALTER TABLE audit_chain_state ADD COLUMN legacy_root_hash TEXT")
        logger.info("Migration 175: added audit_chain_state.legacy_root_hash")
    conn.execute(
        "INSERT OR IGNORE INTO audit_chain_state "
        "(id, enforcement_enabled, activated_at, legacy_root_hash) "
        "VALUES (1, 0, NULL, NULL)"
    )

    columns = (
        ("workspace_id", "TEXT"),
        ("workspace_sequence", "INTEGER"),
        ("previous_hash", "TEXT"),
        ("entry_hash", "TEXT"),
        ("hash_version", "INTEGER"),
    )
    for column, declaration in columns:
        if not _column_exists(conn, "audit_log", column):
            conn.execute(f"ALTER TABLE audit_log ADD COLUMN {column} {declaration}")
            logger.info("Migration 175: added audit_log.%s", column)

    state = conn.execute(
        "SELECT legacy_root_hash FROM audit_chain_state WHERE id=1"
    ).fetchone()
    if state is None:
        raise RuntimeError("migration 175 audit_chain_state singleton is missing")
    if state[0] is None:
        from core.api.services.audit_chain import legacy_root_hash_v1

        conn.execute(
            "UPDATE audit_chain_state SET legacy_root_hash=? "
            "WHERE id=1 AND legacy_root_hash IS NULL",
            (
                legacy_root_hash_v1(
                    _iter_legacy_audit_rows_for_v1_hash(conn),
                    rows_already_ordered=True,
                ),
            ),
        )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_audit_log_workspace_sequence "
        "ON audit_log(workspace_id, workspace_sequence) "
        "WHERE workspace_id IS NOT NULL AND workspace_sequence IS NOT NULL"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS audit_log_chain_shape
        BEFORE INSERT ON audit_log
        WHEN NOT (
            (
                NEW.workspace_id IS NULL
                AND NEW.workspace_sequence IS NULL
                AND NEW.previous_hash IS NULL
                AND NEW.entry_hash IS NULL
                AND NEW.hash_version IS NULL
            )
            OR
            (
                typeof(NEW.workspace_id) = 'text'
                AND length(trim(NEW.workspace_id)) > 0
                AND typeof(NEW.workspace_sequence) = 'integer'
                AND NEW.workspace_sequence >= 1
                AND typeof(NEW.previous_hash) = 'text'
                AND length(NEW.previous_hash) = 64
                AND NEW.previous_hash = lower(NEW.previous_hash)
                AND NEW.previous_hash NOT GLOB '*[^0-9a-f]*'
                AND typeof(NEW.entry_hash) = 'text'
                AND length(NEW.entry_hash) = 64
                AND NEW.entry_hash = lower(NEW.entry_hash)
                AND NEW.entry_hash NOT GLOB '*[^0-9a-f]*'
                AND NEW.hash_version = 1
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'audit_log chain fields are incomplete or invalid');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS audit_log_chainless_after_activation
        BEFORE INSERT ON audit_log
        WHEN (
            SELECT enforcement_enabled FROM audit_chain_state WHERE id = 1
        ) = 1 AND NEW.hash_version IS NULL
        BEGIN
            SELECT RAISE(ABORT, 'audit_log chain fields required after activation');
        END
        """
    )
    conn.commit()


def _migration_176_activate_audit_chain(conn: sqlite3.Connection) -> None:
    """Freeze the complete legacy prefix and enable chain enforcement atomically.

    This security floor is intentionally forward-only. The hook is recovery-safe
    when the SQL migration has already recorded v176: an active chain is left
    byte-identical, while an inactive state is activated only when no chained
    rows or heads exist yet.
    """
    if conn.in_transaction:
        raise RuntimeError("audit chain activation requires transaction ownership")

    from core.api.services.audit_chain import legacy_root_hash_v1

    conn.execute("BEGIN IMMEDIATE")
    try:
        state = conn.execute(
            "SELECT enforcement_enabled, activated_at, legacy_root_hash "
            "FROM audit_chain_state WHERE id=1"
        ).fetchone()
        if state is None:
            raise RuntimeError("audit_chain_state singleton is missing")
        if int(state[0]) == 1:
            if not state[1] or not state[2]:
                raise RuntimeError("active audit chain state is incomplete")
            conn.commit()
            return

        head_count = int(
            conn.execute("SELECT COUNT(*) FROM audit_chain_heads").fetchone()[0]
        )
        chained_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE hash_version IS NOT NULL"
            ).fetchone()[0]
        )
        if head_count or chained_count:
            raise RuntimeError(
                "inactive audit chain has persisted heads or chained rows"
            )

        legacy_root = legacy_root_hash_v1(
            _iter_legacy_audit_rows_for_v1_hash(conn),
            rows_already_ordered=True,
        )
        activated_at = datetime.now(timezone.utc).isoformat()
        cursor = conn.execute(
            "UPDATE audit_chain_state SET legacy_root_hash=?, "
            "enforcement_enabled=1, activated_at=? "
            "WHERE id=1 AND enforcement_enabled=0",
            (legacy_root, activated_at),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("audit chain activation compare-and-set failed")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migration_177_delegation_workspace(conn: sqlite3.Connection) -> None:
    """Install/repair fail-closed workspace scoping for delegations.

    Existing grants remain NULL intentionally. The runtime requires an exact
    workspace match, so an older unscoped credential can never become live by
    inference during upgrade or hook retry.
    """
    if not _column_exists(conn, "delegations", "workspace_id"):
        conn.execute("ALTER TABLE delegations ADD COLUMN workspace_id TEXT")
        logger.info("Migration 177: added delegations.workspace_id")
    conn.execute("DROP INDEX IF EXISTS idx_delegations_agent_active")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_delegations_workspace_agent_active "
        "ON delegations(workspace_id, agent_username, expires_at)"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS delegations_workspace_required "
        "BEFORE INSERT ON delegations "
        "WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0 "
        "BEGIN SELECT RAISE(ABORT, 'delegation workspace_id required'); END"
    )
    conn.commit()


def _migration_178_agent_token_lifecycle(conn: sqlite3.Connection) -> None:
    """Install/repair the additive per-principal token lifecycle schema.

    Existing rows become ``legacy_individual`` credentials.  They retain their
    compatibility-mode behavior only when their principal can be resolved
    unambiguously at authentication time; the migration never invents an
    expiry or silently upgrades them into the strict credential class.
    """
    columns = (
        ("principal_id", "TEXT"),
        ("principal_type", "TEXT"),
        ("label", "TEXT"),
        ("issued_at", "TEXT"),
        ("expires_at", "TEXT"),
        ("revoked_at", "TEXT"),
        ("revoked_by", "TEXT"),
        ("rotation_family_id", "TEXT"),
        ("supersedes_id", "TEXT"),
        ("overlap_until", "TEXT"),
        ("acknowledged_at", "TEXT"),
        ("acknowledgement_actor", "TEXT"),
        ("credential_kind", "TEXT"),
    )
    for column, declaration in columns:
        if not _column_exists(conn, "agent_tokens", column):
            conn.execute(
                f"ALTER TABLE agent_tokens ADD COLUMN {column} {declaration}"
            )
            logger.info("Migration 178: added agent_tokens.%s", column)

    # Backfill only facts that already exist.  Ambiguous/missing principals stay
    # NULL and therefore fail closed in both compatibility and strict modes.
    conn.execute(
        "UPDATE agent_tokens SET issued_at = created_at "
        "WHERE issued_at IS NULL AND created_at IS NOT NULL"
    )
    conn.execute(
        "UPDATE agent_tokens SET rotation_family_id = id "
        "WHERE rotation_family_id IS NULL"
    )
    conn.execute(
        "UPDATE agent_tokens SET credential_kind = 'legacy_individual' "
        "WHERE credential_kind IS NULL"
    )
    conn.execute(
        "UPDATE agent_tokens SET principal_id = ("
        " SELECT MIN(u.id) FROM users u"
        " WHERE u.slug = agent_tokens.agent_name"
        " AND u.deleted_at IS NULL"
        " AND COALESCE(u.workspace_id, 'ws_default') = "
        "     COALESCE(agent_tokens.workspace_id, 'ws_default')"
        ") WHERE principal_id IS NULL AND ("
        " SELECT COUNT(*) FROM users u"
        " WHERE u.slug = agent_tokens.agent_name"
        " AND u.deleted_at IS NULL"
        " AND COALESCE(u.workspace_id, 'ws_default') = "
        "     COALESCE(agent_tokens.workspace_id, 'ws_default')"
        ") = 1"
    )
    conn.execute(
        "UPDATE agent_tokens SET principal_type = ("
        " SELECT u.type FROM users u WHERE u.id = agent_tokens.principal_id"
        ") WHERE principal_type IS NULL AND principal_id IS NOT NULL"
    )

    conn.execute("DROP INDEX IF EXISTS idx_agent_tokens_active")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tokens_workspace_principal_active "
        "ON agent_tokens(workspace_id, principal_id, is_active, expires_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_tokens_rotation_family "
        "ON agent_tokens(rotation_family_id, issued_at)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_tokens_live_successor "
        "ON agent_tokens(supersedes_id) WHERE supersedes_id IS NOT NULL "
        "AND revoked_at IS NULL AND is_active = 1"
    )

    strict_shape = """
        NEW.credential_kind = 'individual' AND (
            NEW.principal_id IS NULL OR length(trim(NEW.principal_id)) = 0 OR
            NEW.principal_type IS NULL OR
            NEW.principal_type NOT IN ('human', 'agent') OR
            NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0 OR
            NEW.issued_at IS NULL OR length(trim(NEW.issued_at)) = 0 OR
            NEW.expires_at IS NULL OR length(trim(NEW.expires_at)) = 0 OR
            NEW.rotation_family_id IS NULL OR
            length(trim(NEW.rotation_family_id)) = 0
        )
    """
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS agent_tokens_individual_shape_insert "
        f"BEFORE INSERT ON agent_tokens WHEN {strict_shape} "
        "BEGIN SELECT RAISE(ABORT, "
        "'individual token lifecycle fields required'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS agent_tokens_individual_shape_update "
        f"BEFORE UPDATE ON agent_tokens WHEN {strict_shape} "
        "BEGIN SELECT RAISE(ABORT, "
        "'individual token lifecycle fields required'); END"
    )
    conn.commit()


def _unique_index_has_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[str, ...]
) -> bool:
    for row in conn.execute(f'PRAGMA index_list("{table}")').fetchall():
        if not bool(row[2]) or bool(row[4]):
            continue
        actual = tuple(
            str(detail[2])
            for detail in conn.execute(
                f'PRAGMA index_info("{str(row[1])}")'
            ).fetchall()
        )
        if actual == columns:
            return True
    return False


def _foreign_key_violation_counts(
    conn: sqlite3.Connection,
    *,
    child_tables: frozenset[str] | None = None,
) -> Counter[tuple[object, ...]]:
    """Return the exact FK-violation multiset without exposing row contents."""
    if child_tables is None:
        return Counter(tuple(row) for row in conn.execute("PRAGMA foreign_key_check"))

    violations: Counter[tuple[object, ...]] = Counter()
    for table in sorted(child_tables):
        quoted = table.replace('"', '""')
        violations.update(
            tuple(row)
            for row in conn.execute(f'PRAGMA foreign_key_check("{quoted}")')
        )
    return violations


def _foreign_key_children(
    conn: sqlite3.Connection,
    parent_tables: frozenset[str],
) -> frozenset[str]:
    """Return tables whose FK integrity can change when parents are rebuilt."""
    affected = set(parent_tables)
    for (table_name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ):
        table = str(table_name)
        quoted = table.replace('"', '""')
        if any(
            str(row[2]) in parent_tables
            for row in conn.execute(f'PRAGMA foreign_key_list("{quoted}")')
        ):
            affected.add(table)
    return frozenset(affected)


def _rebuild_workspace_keyed_tables(conn: sqlite3.Connection) -> None:
    """Replace legacy global uniqueness with workspace-scoped uniqueness."""
    access_ready = _unique_index_has_columns(
        conn, "access_grants", ("workspace_id", "identity", "project_slug")
    )
    file_ready = not _table_exists(conn, "file_meta") or _unique_index_has_columns(
        conn, "file_meta", ("workspace_id", "project_slug", "rel_path")
    )
    if access_ready and file_ready:
        return

    if conn.in_transaction:
        conn.commit()
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("BEGIN IMMEDIATE")
    try:
        rebuilt_parents = frozenset(
            table
            for table, ready in (
                ("access_grants", access_ready),
                ("file_meta", file_ready),
            )
            if not ready
        )
        affected_fk_children = _foreign_key_children(conn, rebuilt_parents)
        baseline_violations = _foreign_key_violation_counts(
            conn,
            child_tables=affected_fk_children,
        )
        if not access_ready:
            access_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(access_grants)")
            }
            created = (
                "created_at"
                if "created_at" in access_columns
                else "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            )
            updated = (
                "updated_at"
                if "updated_at" in access_columns
                else "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            )
            confidential = (
                "COALESCE(confidential_clearance, 0)"
                if "confidential_clearance" in access_columns
                else "0"
            )
            clearance = (
                "COALESCE(clearance, CASE WHEN confidential_clearance=1 "
                "THEN 'confidential' ELSE 'internal' END)"
                if "clearance" in access_columns
                else "CASE WHEN " + confidential + "=1 THEN 'confidential' ELSE 'internal' END"
            )
            scope = (
                "COALESCE(scope, 'project:' || project_slug)"
                if "scope" in access_columns
                else "'project:' || project_slug"
            )
            conn.execute(
                "CREATE TABLE access_grants_workspace_new ("
                "identity TEXT NOT NULL, project_slug TEXT NOT NULL, "
                "role TEXT NOT NULL CHECK(role IN ('admin','member','viewer','membro')), "
                "confidential_clearance INTEGER NOT NULL DEFAULT 0 "
                "CHECK(confidential_clearance IN (0,1)), "
                "clearance TEXT NOT NULL DEFAULT 'internal' "
                "CHECK(clearance IN ('public','internal','confidential')), "
                "scope TEXT NOT NULL DEFAULT 'all', created_at TEXT NOT NULL "
                "DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
                "updated_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')), workspace_id TEXT)"
            )
            conn.execute(
                "INSERT INTO access_grants_workspace_new "
                "(identity,project_slug,role,confidential_clearance,clearance,"
                "scope,created_at,updated_at,workspace_id) "
                "SELECT identity,project_slug,CASE role WHEN 'membro' THEN 'member' "
                "ELSE COALESCE(role,'viewer') END," + confidential + "," +
                clearance + "," + scope + "," + created + "," + updated +
                ",workspace_id FROM access_grants"
            )
            conn.execute("DROP TABLE access_grants")
            conn.execute(
                "ALTER TABLE access_grants_workspace_new RENAME TO access_grants"
            )

        if not file_ready:
            file_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(file_meta)")
            }
            created = (
                "created_at"
                if "created_at" in file_columns
                else "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            )
            updated = (
                "updated_at"
                if "updated_at" in file_columns
                else "strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            )
            conn.execute(
                "CREATE TABLE file_meta_workspace_new ("
                "id TEXT PRIMARY KEY, project_slug TEXT NOT NULL, "
                "rel_path TEXT NOT NULL, owner_user_id TEXT, "
                "confidential INTEGER NOT NULL DEFAULT 0 "
                "CHECK(confidential IN (0,1)), created_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
                "updated_at TEXT NOT NULL DEFAULT "
                "(strftime('%Y-%m-%dT%H:%M:%fZ','now')), workspace_id TEXT)"
            )
            conn.execute(
                "INSERT INTO file_meta_workspace_new "
                "(id,project_slug,rel_path,owner_user_id,confidential,created_at,"
                "updated_at,workspace_id) SELECT id,project_slug,rel_path,"
                "owner_user_id,COALESCE(confidential,0)," + created + "," + updated +
                ",workspace_id FROM file_meta"
            )
            conn.execute("DROP TABLE file_meta")
            conn.execute(
                "ALTER TABLE file_meta_workspace_new RENAME TO file_meta"
            )

        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_access_grants_workspace_identity_project "
            "ON access_grants(workspace_id, identity, project_slug)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_access_grants_identity "
            "ON access_grants(identity)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_access_grants_project "
            "ON access_grants(project_slug)"
        )
        if _table_exists(conn, "file_meta"):
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "idx_file_meta_workspace_project_path "
                "ON file_meta(workspace_id, project_slug, rel_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_file_meta_confidential "
                "ON file_meta(project_slug) WHERE confidential = 1"
            )
        affected_violations = _foreign_key_violation_counts(
            conn,
            child_tables=affected_fk_children,
        )
        introduced_violations = affected_violations - baseline_violations
        if introduced_violations:
            raise RuntimeError(
                "migration 179 introduced "
                f"{sum(introduced_violations.values())} foreign-key violation(s)"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(
            "PRAGMA foreign_keys=" + ("ON" if foreign_keys_enabled else "OFF")
        )


def _migration_179_workspace_isolation(conn: sqlite3.Connection) -> None:
    """Install workspace-scoped ownership, uniqueness, and lookup contracts."""
    if not _column_exists(conn, "access_grants", "workspace_id"):
        conn.execute("ALTER TABLE access_grants ADD COLUMN workspace_id TEXT")
        logger.info("Migration 179: added access_grants.workspace_id")

    if _table_exists(conn, "users") and _column_exists(conn, "users", "workspace_id"):
        conn.execute(
            "UPDATE access_grants SET workspace_id = ("
            " SELECT MIN(COALESCE(u.workspace_id, 'ws_default')) FROM users u"
            " WHERE u.deleted_at IS NULL AND ("
            " u.id = access_grants.identity OR u.slug = access_grants.identity"
            " OR 'agent:' || u.slug = access_grants.identity))"
            " WHERE workspace_id IS NULL AND ("
            " SELECT COUNT(DISTINCT COALESCE(u.workspace_id, 'ws_default'))"
            " FROM users u WHERE u.deleted_at IS NULL AND ("
            " u.id = access_grants.identity OR u.slug = access_grants.identity"
            " OR 'agent:' || u.slug = access_grants.identity)) = 1"
        )

    if _table_exists(conn, "file_meta"):
        if not _column_exists(conn, "file_meta", "workspace_id"):
            conn.execute("ALTER TABLE file_meta ADD COLUMN workspace_id TEXT")
            logger.info("Migration 179: added file_meta.workspace_id")
        if _table_exists(conn, "users") and _column_exists(conn, "users", "workspace_id"):
            conn.execute(
                "UPDATE file_meta SET workspace_id = ("
                " SELECT MIN(COALESCE(u.workspace_id, 'ws_default')) FROM users u"
                " WHERE u.deleted_at IS NULL AND u.id = file_meta.owner_user_id)"
                " WHERE workspace_id IS NULL AND owner_user_id IS NOT NULL AND ("
                " SELECT COUNT(DISTINCT COALESCE(u.workspace_id, 'ws_default'))"
                " FROM users u WHERE u.deleted_at IS NULL"
                " AND u.id = file_meta.owner_user_id) = 1"
            )
        if (
            _table_exists(conn, "documents")
            and _column_exists(conn, "documents", "workspace_id")
            and _column_exists(conn, "documents", "project")
        ):
            conn.execute(
                "UPDATE file_meta SET workspace_id = ("
                " SELECT MIN(COALESCE(d.workspace_id, 'ws_default'))"
                " FROM documents d WHERE d.project = file_meta.project_slug)"
                " WHERE workspace_id IS NULL AND ("
                " SELECT COUNT(DISTINCT COALESCE(d.workspace_id, 'ws_default'))"
                " FROM documents d WHERE d.project = file_meta.project_slug) = 1"
            )
    conn.commit()
    _rebuild_workspace_keyed_tables(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS workspace_projects ("
            "workspace_id TEXT NOT NULL, project_slug TEXT NOT NULL, "
            "source TEXT NOT NULL CHECK(source IN ('project_create','grant','team','migration')), "
            "created_by TEXT, created_at TEXT NOT NULL DEFAULT "
            "(strftime('%Y-%m-%dT%H:%M:%fZ','now')), "
            "PRIMARY KEY(workspace_id, project_slug))"
        )
        conn.execute(
            "INSERT OR IGNORE INTO workspace_projects "
            "(workspace_id,project_slug,source,created_by) "
            "SELECT workspace_id,project_slug,'grant',identity FROM access_grants "
            "WHERE workspace_id IS NOT NULL AND length(trim(workspace_id)) > 0"
        )
        if _table_exists(conn, "teams") and _table_exists(conn, "project_teams"):
            if _column_exists(conn, "teams", "workspace_id"):
                conn.execute(
                    "INSERT OR IGNORE INTO workspace_projects "
                    "(workspace_id,project_slug,source,created_by) "
                    "SELECT DISTINCT t.workspace_id,pt.project,'team',t.id "
                    "FROM project_teams pt JOIN teams t ON t.id=pt.team_id "
                    "WHERE t.workspace_id IS NOT NULL "
                    "AND length(trim(t.workspace_id)) > 0"
                )
        trigger_statements = (
            "CREATE TRIGGER IF NOT EXISTS access_grants_workspace_required_insert "
            "BEFORE INSERT ON access_grants WHEN NEW.workspace_id IS NULL OR "
            "length(trim(NEW.workspace_id))=0 BEGIN SELECT RAISE(ABORT, "
            "'access grant workspace_id required'); END",
            "CREATE TRIGGER IF NOT EXISTS access_grants_workspace_required_update "
            "BEFORE UPDATE ON access_grants WHEN NEW.workspace_id IS NULL OR "
            "length(trim(NEW.workspace_id))=0 BEGIN SELECT RAISE(ABORT, "
            "'access grant workspace_id required'); END",
            "CREATE TRIGGER IF NOT EXISTS access_grants_workspace_immutable "
            "BEFORE UPDATE OF workspace_id ON access_grants WHEN "
            "OLD.workspace_id IS NOT NULL AND OLD.workspace_id != NEW.workspace_id "
            "BEGIN SELECT RAISE(ABORT, 'access grant workspace_id immutable'); END",
        )
        for statement in trigger_statements:
            conn.execute(statement)
        if _table_exists(conn, "file_meta"):
            for statement in (
                "CREATE TRIGGER IF NOT EXISTS file_meta_workspace_required_insert "
                "BEFORE INSERT ON file_meta WHEN NEW.workspace_id IS NULL OR "
                "length(trim(NEW.workspace_id))=0 BEGIN SELECT RAISE(ABORT, "
                "'file metadata workspace_id required'); END",
                "CREATE TRIGGER IF NOT EXISTS file_meta_workspace_required_update "
                "BEFORE UPDATE ON file_meta WHEN NEW.workspace_id IS NULL OR "
                "length(trim(NEW.workspace_id))=0 BEGIN SELECT RAISE(ABORT, "
                "'file metadata workspace_id required'); END",
                "CREATE TRIGGER IF NOT EXISTS file_meta_workspace_immutable "
                "BEFORE UPDATE OF workspace_id ON file_meta WHEN "
                "OLD.workspace_id IS NOT NULL AND OLD.workspace_id != NEW.workspace_id "
                "BEGIN SELECT RAISE(ABORT, 'file metadata workspace_id immutable'); END",
            ):
                conn.execute(statement)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tasks_workspace_id "
            "ON tasks(workspace_id, id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prs_workspace_task_status_created "
            "ON pull_requests(workspace_id, task_id, status, created_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_prs_workspace_project_status_created "
            "ON pull_requests(workspace_id, project, status, created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_learnings_workspace_id "
            "ON learnings(workspace_id, id)"
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _migration_135_graph_edges_provider(conn: sqlite3.Connection) -> None:
    """Add provider column/index for KG edges without retroactive backfill."""
    if not _column_exists(conn, "graph_edges", "provider"):
        conn.execute("ALTER TABLE graph_edges ADD COLUMN provider TEXT")
        logger.info("Migration 135: added graph_edges.provider")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_graph_edges_provider "
        "ON graph_edges(provider, relation)"
    )
    conn.commit()


def _add_session_theme_mode_column(conn: sqlite3.Connection) -> None:
    """Ensure sessions_meta.theme_mode exists for existing databases."""
    if not _column_exists(conn, "sessions_meta", "theme_mode"):
        conn.execute(
            "ALTER TABLE sessions_meta ADD COLUMN theme_mode TEXT DEFAULT NULL"
        )
        logger.info("Added sessions_meta.theme_mode")
    conn.commit()


async def cleanup_expired_tickets(db: aiosqlite.Connection) -> int:
    """Remove expired WS tickets. Returns count deleted."""
    cursor = await db.execute(
        "DELETE FROM ws_tickets WHERE expires_at < datetime('now')"
    )
    await db.commit()
    return cursor.rowcount


async def cleanup_expired_blacklist(db: aiosqlite.Connection) -> int:
    """Remove expired blacklist entries. Returns count deleted."""
    cursor = await db.execute(
        "DELETE FROM token_blacklist WHERE expires_at < datetime('now')"
    )
    await db.commit()
    return cursor.rowcount
