"""Transaction-bound, workspace-scoped audit hash chain.

This module is deliberately transport-free: it imports neither FastAPI nor any
router. Domain use cases own the surrounding transaction and may append an audit
entry as one required part of their operation. This helper never starts, commits,
rolls back, or catches that transaction.

Local consistency alone cannot prove that a database writer did not rewrite the
entire chain. :class:`TrustedCheckpoint` is the explicit external trust boundary:
an anchored result proves the prefix through that checkpoint, while any newer tail
is reported as locally consistent but unanchored.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Literal, Mapping, Protocol, runtime_checkable

import aiosqlite


HASH_VERSION = 1
_GENESIS_DOMAIN = b"marvis-audit-genesis-v1\n"
_ENTRY_DOMAIN = b"marvis-audit-entry-v1\n"
_LEGACY_ROOT_DOMAIN = b"marvis-audit-legacy-root-v1\n"
_STORAGE_GUARDS = {
    "audit_log_chain_shape": (
        "before insert on audit_log",
        "new.hash_version",
        "audit_log chain fields are incomplete or invalid",
    ),
    "audit_log_chainless_after_activation": (
        "before insert on audit_log",
        "enforcement_enabled",
        "new.hash_version is null",
        "audit_log chain fields required after activation",
    ),
    "audit_log_no_delete": (
        "before delete on audit_log",
        "raise(abort,'audit_log is append-only')",
    ),
    "audit_log_no_update": (
        "before update on audit_log",
        "raise(abort,'audit_log is append-only')",
    ),
    "idx_audit_log_workspace_sequence": (
        "create unique index",
        "on audit_log(workspace_id,workspace_sequence)",
        "where workspace_id is not null and workspace_sequence is not null",
    ),
}


class AuditTransactionRequired(RuntimeError):
    """Raised when an append has no caller-owned open transaction."""


class AuditChainConflict(RuntimeError):
    """Raised when workspace-head compare-and-set loses its expected head."""


class UnsupportedAuditHashVersion(RuntimeError):
    """Raised when persisted chain state uses an unknown hash version."""


class AuditChainStateInvalid(RuntimeError):
    """Raised when the persisted legacy-root state is absent or malformed."""


@dataclass(frozen=True)
class AuditAppendReceipt:
    entry_id: str
    timestamp: str
    workspace_id: str
    workspace_sequence: int
    previous_hash: str
    entry_hash: str
    hash_version: int = HASH_VERSION


@dataclass(frozen=True)
class TrustedCheckpoint:
    """Authenticated receipt held outside the audited SQLite database.

    The external provider authenticates this DTO and verifies any signature. The
    local verifier deliberately does not trust keys or signatures stored beside the
    audit database.
    """

    workspace_id: str
    workspace_sequence: int
    entry_hash: str
    database_identity: str
    deployment_identity: str
    hash_version: int = HASH_VERSION
    checkpoint_id: str | None = None
    recorded_at: str | None = None


@runtime_checkable
class TrustedCheckpointProvider(Protocol):
    """Authenticated external checkpoint reader.

    Implementations own transport authentication and signature validation before
    returning a DTO. Database-backed self-authentication is outside this protocol.
    """

    async def get_trusted_checkpoint(
        self, workspace_id: str
    ) -> TrustedCheckpoint | None: ...


@dataclass(frozen=True)
class AuditVerificationResult:
    workspace_id: str
    database_identity: str
    deployment_identity: str
    verdict: Literal[
        "locally_consistent_unanchored",
        "anchored",
        "inconsistent",
        "verification_incomplete",
    ]
    verification_complete: bool
    chain_consistent: bool
    storage_guards_present: bool
    missing_storage_guards: tuple[str, ...]
    enforcement_enabled: bool
    head_sequence: int
    anchored_through_sequence: int | None
    uncheckpointed_tail: int | None
    current_completeness_proven: bool
    legacy_row_count: int
    issues: tuple[str, ...]
    limitations: tuple[str, ...]


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON; reject NaN and other non-JSON values."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _normalize_sql(value: object) -> str:
    normalized = " ".join(str(value or "").lower().split())
    normalized = normalized.replace(", ", ",").replace(" ,", ",")
    normalized = normalized.replace(" = ", "=")
    return normalized.rstrip(";")


def legacy_root_hash_v1(rows: Iterable[Mapping[str, Any]]) -> str:
    """Bind every legacy row in deterministic ``timestamp, id`` order."""
    ordered = sorted(
        (dict(row) for row in rows),
        key=lambda row: (str(row["timestamp"]), str(row["id"])),
    )
    digest = hashlib.sha256()
    digest.update(_LEGACY_ROOT_DOMAIN)
    for row in ordered:
        payload = canonical_json(
            {
                "action": row["action"],
                "details_json": row.get("details_json"),
                "id": row["id"],
                "resource_id": row["resource_id"],
                "resource_type": row["resource_type"],
                "timestamp": row["timestamp"],
                "user": row["user"],
            }
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def genesis_hash_v1(workspace_id: str, *, legacy_root_hash: str) -> str:
    """Domain-separated workspace root bound to the immutable legacy prefix."""
    if not workspace_id or not workspace_id.strip():
        raise ValueError("workspace_id must be non-empty")
    if not _is_sha256_hex(legacy_root_hash):
        raise ValueError("legacy_root_hash must be a lowercase SHA-256 hex digest")
    payload = canonical_json(
        {"legacy_root_hash": legacy_root_hash, "workspace_id": workspace_id}
    ).encode("utf-8")
    return hashlib.sha256(_GENESIS_DOMAIN + payload).hexdigest()


def _is_sha256_hex(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def entry_hash_v1(
    *,
    entry_id: str,
    timestamp: str,
    action: str,
    user: str,
    resource_type: str,
    resource_id: str,
    details_json: str | None,
    workspace_id: str,
    workspace_sequence: int,
    previous_hash: str,
) -> str:
    """Hash the exact stored v1 entry envelope in canonical field order."""
    envelope = {
        "action": action,
        "details_json": details_json,
        "entry_id": entry_id,
        "hash_version": HASH_VERSION,
        "previous_hash": previous_hash,
        "resource_id": resource_id,
        "resource_type": resource_type,
        "timestamp": timestamp,
        "user": user,
        "workspace_id": workspace_id,
        "workspace_sequence": workspace_sequence,
    }
    payload = canonical_json(envelope).encode("utf-8")
    return hashlib.sha256(_ENTRY_DOMAIN + payload).hexdigest()


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


async def append_audit_entry(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    action: str,
    user: str,
    resource_type: str,
    resource_id: str,
    details: Mapping[str, Any] | None = None,
    entry_id: str | None = None,
    timestamp: str | None = None,
) -> AuditAppendReceipt:
    """Append under an existing caller-owned transaction.

    Head creation and advancement are workspace-local. The final update is a
    compare-and-set against the head that supplied ``previous_hash``. Every error
    propagates to the transaction owner; callers must roll back the whole domain
    operation rather than commit partial business or audit state.
    """
    if not db.in_transaction:
        raise AuditTransactionRequired(
            "append_audit_entry requires an already-open caller-owned transaction"
        )
    if not workspace_id or not workspace_id.strip():
        raise ValueError("workspace_id must be non-empty")

    resolved_entry_id = entry_id or uuid.uuid4().hex
    resolved_timestamp = timestamp or _utc_now()
    details_json = canonical_json(dict(details)) if details is not None else None
    state = await (
        await db.execute(
            "SELECT enforcement_enabled, legacy_root_hash "
            "FROM audit_chain_state WHERE id=1"
        )
    ).fetchone()
    if state is None or int(state[0]) != 1:
        raise AuditChainStateInvalid("audit chain is not active")
    if not _is_sha256_hex(state[1]):
        raise AuditChainStateInvalid(
            "audit_chain_state.legacy_root_hash is missing or invalid"
        )
    legacy_root_hash = str(state[1])
    genesis = genesis_hash_v1(
        workspace_id, legacy_root_hash=legacy_root_hash
    )

    await db.execute(
        "INSERT INTO audit_chain_heads "
        "(workspace_id, last_sequence, last_entry_hash, hash_version, updated_at) "
        "VALUES (?, 0, ?, ?, ?) "
        "ON CONFLICT(workspace_id) DO NOTHING",
        (workspace_id, genesis, HASH_VERSION, resolved_timestamp),
    )
    head = await (
        await db.execute(
            "SELECT last_sequence, last_entry_hash, hash_version "
            "FROM audit_chain_heads WHERE workspace_id=?",
            (workspace_id,),
        )
    ).fetchone()
    if head is None:
        raise AuditChainConflict(f"audit chain head missing for {workspace_id}")

    previous_sequence = int(head[0])
    previous_hash = str(head[1])
    persisted_version = int(head[2])
    if persisted_version != HASH_VERSION:
        raise UnsupportedAuditHashVersion(
            f"workspace {workspace_id} uses hash version {persisted_version}"
        )
    sequence = previous_sequence + 1
    entry_hash = entry_hash_v1(
        entry_id=resolved_entry_id,
        timestamp=resolved_timestamp,
        action=action,
        user=user,
        resource_type=resource_type,
        resource_id=resource_id,
        details_json=details_json,
        workspace_id=workspace_id,
        workspace_sequence=sequence,
        previous_hash=previous_hash,
    )

    await db.execute(
        "INSERT INTO audit_log "
        "(id, timestamp, action, user, resource_type, resource_id, details_json, "
        "workspace_id, workspace_sequence, previous_hash, entry_hash, hash_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            resolved_entry_id,
            resolved_timestamp,
            action,
            user,
            resource_type,
            resource_id,
            details_json,
            workspace_id,
            sequence,
            previous_hash,
            entry_hash,
            HASH_VERSION,
        ),
    )
    cursor = await db.execute(
        "UPDATE audit_chain_heads "
        "SET last_sequence=?, last_entry_hash=?, hash_version=?, updated_at=? "
        "WHERE workspace_id=? AND last_sequence=? AND last_entry_hash=? "
        "AND hash_version=?",
        (
            sequence,
            entry_hash,
            HASH_VERSION,
            resolved_timestamp,
            workspace_id,
            previous_sequence,
            previous_hash,
            HASH_VERSION,
        ),
    )
    if cursor.rowcount != 1:
        raise AuditChainConflict(
            f"audit chain head changed concurrently for {workspace_id}"
        )

    return AuditAppendReceipt(
        entry_id=resolved_entry_id,
        timestamp=resolved_timestamp,
        workspace_id=workspace_id,
        workspace_sequence=sequence,
        previous_hash=previous_hash,
        entry_hash=entry_hash,
    )


def append_audit_entry_sync(
    db: sqlite3.Connection,
    *,
    workspace_id: str,
    action: str,
    user: str,
    resource_type: str,
    resource_id: str,
    details: Mapping[str, Any] | None = None,
    entry_id: str | None = None,
    timestamp: str | None = None,
) -> AuditAppendReceipt:
    """Synchronous equivalent for audited filesystem tools with sync callbacks."""
    if not db.in_transaction:
        raise AuditTransactionRequired(
            "append_audit_entry_sync requires an already-open caller-owned transaction"
        )
    if not workspace_id or not workspace_id.strip():
        raise ValueError("workspace_id must be non-empty")

    resolved_entry_id = entry_id or uuid.uuid4().hex
    resolved_timestamp = timestamp or _utc_now()
    details_json = canonical_json(dict(details)) if details is not None else None
    state = db.execute(
        "SELECT enforcement_enabled, legacy_root_hash "
        "FROM audit_chain_state WHERE id=1"
    ).fetchone()
    if state is None or int(state[0]) != 1:
        raise AuditChainStateInvalid("audit chain is not active")
    if not _is_sha256_hex(state[1]):
        raise AuditChainStateInvalid(
            "audit_chain_state.legacy_root_hash is missing or invalid"
        )
    previous_root = str(state[1])
    genesis = genesis_hash_v1(workspace_id, legacy_root_hash=previous_root)

    db.execute(
        "INSERT INTO audit_chain_heads "
        "(workspace_id, last_sequence, last_entry_hash, hash_version, updated_at) "
        "VALUES (?, 0, ?, ?, ?) "
        "ON CONFLICT(workspace_id) DO NOTHING",
        (workspace_id, genesis, HASH_VERSION, resolved_timestamp),
    )
    head = db.execute(
        "SELECT last_sequence, last_entry_hash, hash_version "
        "FROM audit_chain_heads WHERE workspace_id=?",
        (workspace_id,),
    ).fetchone()
    if head is None:
        raise AuditChainConflict(f"audit chain head missing for {workspace_id}")

    previous_sequence = int(head[0])
    previous_hash = str(head[1])
    persisted_version = int(head[2])
    if persisted_version != HASH_VERSION:
        raise UnsupportedAuditHashVersion(
            f"workspace {workspace_id} uses hash version {persisted_version}"
        )
    sequence = previous_sequence + 1
    entry_hash = entry_hash_v1(
        entry_id=resolved_entry_id,
        timestamp=resolved_timestamp,
        action=action,
        user=user,
        resource_type=resource_type,
        resource_id=resource_id,
        details_json=details_json,
        workspace_id=workspace_id,
        workspace_sequence=sequence,
        previous_hash=previous_hash,
    )
    db.execute(
        "INSERT INTO audit_log "
        "(id, timestamp, action, user, resource_type, resource_id, details_json, "
        "workspace_id, workspace_sequence, previous_hash, entry_hash, hash_version) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            resolved_entry_id,
            resolved_timestamp,
            action,
            user,
            resource_type,
            resource_id,
            details_json,
            workspace_id,
            sequence,
            previous_hash,
            entry_hash,
            HASH_VERSION,
        ),
    )
    cursor = db.execute(
        "UPDATE audit_chain_heads "
        "SET last_sequence=?, last_entry_hash=?, hash_version=?, updated_at=? "
        "WHERE workspace_id=? AND last_sequence=? AND last_entry_hash=? "
        "AND hash_version=?",
        (
            sequence,
            entry_hash,
            HASH_VERSION,
            resolved_timestamp,
            workspace_id,
            previous_sequence,
            previous_hash,
            HASH_VERSION,
        ),
    )
    if cursor.rowcount != 1:
        raise AuditChainConflict(
            f"audit chain head changed concurrently for {workspace_id}"
        )
    return AuditAppendReceipt(
        entry_id=resolved_entry_id,
        timestamp=resolved_timestamp,
        workspace_id=workspace_id,
        workspace_sequence=sequence,
        previous_hash=previous_hash,
        entry_hash=entry_hash,
    )


async def _verify_audit_chain_snapshot(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    database_identity: str,
    deployment_identity: str,
    checkpoint: TrustedCheckpoint | None = None,
    max_entries: int = 100_000,
) -> AuditVerificationResult:
    """Verify one workspace inside the caller's stable read snapshot."""
    if not workspace_id or not workspace_id.strip():
        raise ValueError("workspace_id must be non-empty")
    if not database_identity or not database_identity.strip():
        raise ValueError("database_identity must be non-empty")
    if not deployment_identity or not deployment_identity.strip():
        raise ValueError("deployment_identity must be non-empty")
    if max_entries < 1:
        raise ValueError("max_entries must be at least 1")

    issues: list[str] = []
    limitations = [
        "A database writer can replace a locally consistent chain unless a trusted "
        "checkpoint is held outside this database."
    ]

    guard_names = tuple(_STORAGE_GUARDS)
    placeholders = ",".join("?" for _name in guard_names)
    guard_rows = await (
        await db.execute(
            "SELECT name, sql FROM sqlite_master "
            f"WHERE type IN ('trigger','index') AND name IN ({placeholders})",
            guard_names,
        )
    ).fetchall()
    definitions = {str(row[0]): _normalize_sql(row[1]) for row in guard_rows}
    present_guards = {
        name
        for name, fragments in _STORAGE_GUARDS.items()
        if name in definitions
        and all(fragment in definitions[name] for fragment in fragments)
    }
    missing_guards = tuple(
        guard for guard in guard_names if guard not in present_guards
    )
    if missing_guards:
        issues.append("storage_guards_missing")
        limitations.append(
            "One or more storage guards are absent, so update, delete, or malformed "
            "insert protection is incomplete."
        )

    table_rows = await (
        await db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('audit_log', 'audit_chain_heads', 'audit_chain_state')"
        )
    ).fetchall()
    tables = {str(row[0]) for row in table_rows}
    missing_tables = {
        "audit_log",
        "audit_chain_heads",
        "audit_chain_state",
    } - tables
    if missing_tables:
        issues.extend(f"storage_table_missing:{name}" for name in sorted(missing_tables))
        return AuditVerificationResult(
            workspace_id=workspace_id,
            database_identity=database_identity,
            deployment_identity=deployment_identity,
            verdict="inconsistent",
            verification_complete=True,
            chain_consistent=False,
            storage_guards_present=not missing_guards,
            missing_storage_guards=missing_guards,
            enforcement_enabled=False,
            head_sequence=0,
            anchored_through_sequence=None,
            uncheckpointed_tail=None,
            current_completeness_proven=False,
            legacy_row_count=0,
            issues=tuple(issues),
            limitations=tuple(limitations),
        )

    state = await (
        await db.execute(
            "SELECT enforcement_enabled, legacy_root_hash "
            "FROM audit_chain_state WHERE id=1"
        )
    ).fetchone()
    enforcement_enabled = bool(state[0]) if state is not None else False
    if state is None:
        issues.append("audit_chain_state_missing")

    legacy_count_row = await (
        await db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE workspace_id IS NULL "
            "AND workspace_sequence IS NULL AND previous_hash IS NULL "
            "AND entry_hash IS NULL AND hash_version IS NULL"
        )
    ).fetchone()
    workspace_count_row = await (
        await db.execute(
            "SELECT COUNT(*) FROM audit_log WHERE workspace_id=?", (workspace_id,)
        )
    ).fetchone()
    legacy_row_count = int(legacy_count_row[0])
    workspace_row_count = int(workspace_count_row[0])
    scan_count = legacy_row_count + workspace_row_count
    if scan_count > max_entries:
        issues.append(
            f"verification_entry_limit_exceeded:{scan_count}:{max_entries}"
        )
        limitations.append(
            f"Verification stopped before hashing {scan_count} rows because the "
            f"declared limit is {max_entries}; no consistency or anchor claim was made."
        )
        return AuditVerificationResult(
            workspace_id=workspace_id,
            database_identity=database_identity,
            deployment_identity=deployment_identity,
            verdict="verification_incomplete",
            verification_complete=False,
            chain_consistent=False,
            storage_guards_present=not missing_guards,
            missing_storage_guards=missing_guards,
            enforcement_enabled=enforcement_enabled,
            head_sequence=0,
            anchored_through_sequence=None,
            uncheckpointed_tail=None,
            current_completeness_proven=False,
            legacy_row_count=legacy_row_count,
            issues=tuple(issues),
            limitations=tuple(limitations),
        )

    legacy_rows = await (
        await db.execute(
            "SELECT id, timestamp, action, user, resource_type, resource_id, "
            "details_json FROM audit_log WHERE workspace_id IS NULL "
            "AND workspace_sequence IS NULL AND previous_hash IS NULL "
            "AND entry_hash IS NULL AND hash_version IS NULL "
            "ORDER BY timestamp, id"
        )
    ).fetchall()
    if legacy_row_count:
        limitations.append(
            f"{legacy_row_count} legacy chainless audit row(s) cannot be assigned to "
            "or verified for this workspace."
        )

    legacy_records = [
        {
            "id": row[0],
            "timestamp": row[1],
            "action": row[2],
            "user": row[3],
            "resource_type": row[4],
            "resource_id": row[5],
            "details_json": row[6],
        }
        for row in legacy_rows
    ]
    computed_legacy_root = legacy_root_hash_v1(legacy_records)
    persisted_legacy_root = str(state[1]) if state is not None else ""
    chain_consistent = state is not None
    if not _is_sha256_hex(persisted_legacy_root):
        issues.append("legacy_root_hash_invalid")
        chain_consistent = False
    elif persisted_legacy_root != computed_legacy_root:
        issues.append("legacy_root_hash_mismatch")
        chain_consistent = False

    rows = await (
        await db.execute(
            "SELECT id, timestamp, action, user, resource_type, resource_id, "
            "details_json, workspace_sequence, previous_hash, entry_hash, hash_version "
            "FROM audit_log WHERE workspace_id=? "
            "ORDER BY workspace_sequence, rowid",
            (workspace_id,),
        )
    ).fetchall()

    root_for_chain = (
        persisted_legacy_root
        if _is_sha256_hex(persisted_legacy_root)
        else computed_legacy_root
    )
    expected_previous = genesis_hash_v1(
        workspace_id, legacy_root_hash=root_for_chain
    )
    expected_sequence = 1
    hashes_by_sequence: dict[int, str] = {}
    for row in rows:
        sequence = int(row[7]) if row[7] is not None else -1
        previous_hash = str(row[8]) if row[8] is not None else ""
        stored_hash = str(row[9]) if row[9] is not None else ""
        version = int(row[10]) if row[10] is not None else -1
        if sequence != expected_sequence:
            issues.append(f"workspace_sequence_gap:{expected_sequence}:{sequence}")
            chain_consistent = False
        if version != HASH_VERSION:
            issues.append(f"unsupported_hash_version:{sequence}:{version}")
            chain_consistent = False
        if previous_hash != expected_previous:
            issues.append(f"previous_hash_mismatch:{sequence}")
            chain_consistent = False

        details_json = row[6]
        if details_json is not None:
            canonical_details: str | None = None
            try:
                canonical_details = canonical_json(json.loads(str(details_json)))
            except (json.JSONDecodeError, TypeError, ValueError):
                issues.append(f"details_json_invalid:{sequence}")
                chain_consistent = False
            if canonical_details is not None and canonical_details != details_json:
                issues.append(f"details_json_noncanonical:{sequence}")
                chain_consistent = False

        recomputed = entry_hash_v1(
            entry_id=str(row[0]),
            timestamp=str(row[1]),
            action=str(row[2]),
            user=str(row[3]),
            resource_type=str(row[4]),
            resource_id=str(row[5]),
            details_json=str(details_json) if details_json is not None else None,
            workspace_id=workspace_id,
            workspace_sequence=sequence,
            previous_hash=previous_hash,
        )
        if stored_hash != recomputed:
            issues.append(f"entry_hash_mismatch:{sequence}")
            chain_consistent = False
        hashes_by_sequence[sequence] = stored_hash
        expected_previous = stored_hash
        expected_sequence = sequence + 1

    head_sequence = max(hashes_by_sequence, default=0)
    head = await (
        await db.execute(
            "SELECT last_sequence, last_entry_hash, hash_version "
            "FROM audit_chain_heads WHERE workspace_id=?",
            (workspace_id,),
        )
    ).fetchone()
    if head is None:
        if rows:
            issues.append("chain_head_missing")
            chain_consistent = False
    else:
        expected_head_hash = (
            hashes_by_sequence.get(head_sequence)
            if head_sequence
            else genesis_hash_v1(
                workspace_id, legacy_root_hash=root_for_chain
            )
        )
        if (
            int(head[0]) != head_sequence
            or str(head[1]) != expected_head_hash
            or int(head[2]) != HASH_VERSION
        ):
            issues.append("chain_head_mismatch")
            chain_consistent = False

    anchored_through: int | None = None
    uncheckpointed_tail: int | None = None
    checkpoint_consistent = True
    if checkpoint is None:
        limitations.append(
            "No trusted external checkpoint was supplied; this result proves only "
            "current local consistency."
        )
    elif checkpoint.workspace_id != workspace_id:
        issues.append("checkpoint_workspace_mismatch")
        checkpoint_consistent = False
    elif checkpoint.database_identity != database_identity:
        issues.append("checkpoint_database_identity_mismatch")
        checkpoint_consistent = False
    elif checkpoint.deployment_identity != deployment_identity:
        issues.append("checkpoint_deployment_identity_mismatch")
        checkpoint_consistent = False
    elif checkpoint.hash_version != HASH_VERSION:
        issues.append("checkpoint_hash_version_unsupported")
        checkpoint_consistent = False
    elif checkpoint.workspace_sequence > head_sequence:
        issues.append("checkpoint_ahead_of_database")
        limitations.append(
            "The checkpoint is ahead of this database; the database may be rolled "
            "back or truncated, or the checkpoint may belong to another snapshot."
        )
        checkpoint_consistent = False
    elif checkpoint.workspace_sequence < 1:
        issues.append("checkpoint_sequence_invalid")
        checkpoint_consistent = False
    elif hashes_by_sequence.get(checkpoint.workspace_sequence) != checkpoint.entry_hash:
        issues.append("checkpoint_hash_mismatch")
        checkpoint_consistent = False
    else:
        anchored_through = checkpoint.workspace_sequence
        uncheckpointed_tail = head_sequence - checkpoint.workspace_sequence
        if uncheckpointed_tail:
            limitations.append(
                f"The {uncheckpointed_tail}-entry tail after the trusted checkpoint "
                "is locally consistent but externally unanchored."
            )

    if checkpoint is not None:
        limitations.append(
            "Deletion or truncation strictly after the anchored sequence is not "
            "detectable from this checkpoint; current completeness is not proven."
        )

    if not chain_consistent or not checkpoint_consistent:
        verdict: Literal[
            "locally_consistent_unanchored",
            "anchored",
            "inconsistent",
            "verification_incomplete",
        ] = "inconsistent"
    elif checkpoint is not None:
        verdict = "anchored"
    else:
        verdict = "locally_consistent_unanchored"

    return AuditVerificationResult(
        workspace_id=workspace_id,
        database_identity=database_identity,
        deployment_identity=deployment_identity,
        verdict=verdict,
        verification_complete=True,
        chain_consistent=chain_consistent,
        storage_guards_present=not missing_guards,
        missing_storage_guards=missing_guards,
        enforcement_enabled=enforcement_enabled,
        head_sequence=head_sequence,
        anchored_through_sequence=anchored_through,
        uncheckpointed_tail=uncheckpointed_tail,
        current_completeness_proven=False,
        legacy_row_count=legacy_row_count,
        issues=tuple(issues),
        limitations=tuple(limitations),
    )


async def verify_audit_chain(
    db: aiosqlite.Connection,
    *,
    workspace_id: str,
    database_identity: str,
    deployment_identity: str,
    checkpoint_provider: TrustedCheckpointProvider | None = None,
    max_entries: int = 100_000,
) -> AuditVerificationResult:
    """Read one stable snapshot and restore the caller's transaction state.

    A caller-owned transaction is reused. Otherwise the verifier opens a read-only
    snapshot and rolls it back after the SELECTs; it never writes application data,
    changes pragmas, commits, or leaves a transaction open.
    """
    checkpoint = (
        await checkpoint_provider.get_trusted_checkpoint(workspace_id)
        if checkpoint_provider is not None
        else None
    )
    owns_snapshot = not db.in_transaction
    if owns_snapshot:
        await db.execute("BEGIN")
    try:
        return await _verify_audit_chain_snapshot(
            db,
            workspace_id=workspace_id,
            database_identity=database_identity,
            deployment_identity=deployment_identity,
            checkpoint=checkpoint,
            max_entries=max_entries,
        )
    finally:
        if owns_snapshot:
            await db.rollback()


__all__ = [
    "AuditAppendReceipt",
    "AuditChainStateInvalid",
    "AuditChainConflict",
    "AuditTransactionRequired",
    "AuditVerificationResult",
    "HASH_VERSION",
    "TrustedCheckpoint",
    "TrustedCheckpointProvider",
    "UnsupportedAuditHashVersion",
    "append_audit_entry",
    "canonical_json",
    "entry_hash_v1",
    "genesis_hash_v1",
    "legacy_root_hash_v1",
    "verify_audit_chain",
]
