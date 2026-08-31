"""Controlled, receipt-backed SQLite schema upgrades and restores.

This module is the common transaction used by OSS, Enterprise and Hosted.
Callers must first stop and prove their own writer fleet; only this module may
translate that orchestration proof into the private migration-runner flag.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
from typing import Any

from core.api import db as db_mod
from core.api.config import settings


RECEIPT_VERSION = 1
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+/-]{0,255}$", re.ASCII)
_PROOF_KIND_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$", re.ASCII)
_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "rolled_back"})


class SchemaUpgradeError(RuntimeError):
    """The controlled upgrade or restore could not be proved safe."""


@dataclass(frozen=True)
class SchemaUpgradeReceipt:
    receipt_version: int
    release_id: str
    database_path: str
    status: str
    proof_kind: str
    attempt: int
    started_at: str
    completed_at: str | None
    initial_version: int
    final_version: int | None
    code_max_version: int
    applied_versions: tuple[int, ...]
    repaired_versions: tuple[int, ...]
    backup_path: str | None
    fresh_database: bool
    backup_identity: str | None = None
    backup_sha256: str | None = None


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _release_id(value: str) -> str:
    normalized = value.strip()
    if _RELEASE_ID_RE.fullmatch(normalized) is None or ".." in normalized:
        raise SchemaUpgradeError("release_id_invalid")
    return normalized


def _proof_kind(value: str) -> str:
    normalized = value.strip()
    if _PROOF_KIND_RE.fullmatch(normalized) is None:
        raise SchemaUpgradeError("quiescence_proof_invalid")
    return normalized


def _database_path() -> Path:
    raw = str(settings.db_path)
    path = Path(raw).expanduser()
    if not path.is_absolute() or raw != raw.strip():
        raise SchemaUpgradeError("database_path_not_absolute")
    path = Path(os.path.abspath(path))
    parent = path.parent
    try:
        metadata = parent.lstat()
    except OSError as exc:
        raise SchemaUpgradeError("database_parent_unavailable") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SchemaUpgradeError("database_parent_invalid")
    if os.path.lexists(path):
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise SchemaUpgradeError("database_file_invalid")
    return path


def _schema_state(path: Path) -> tuple[int, bool]:
    if not path.exists():
        return 0, True
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            integrity = connection.execute("PRAGMA quick_check").fetchone()
            if not integrity or str(integrity[0]).lower() != "ok":
                raise SchemaUpgradeError("database_integrity_red")
            objects = connection.execute(
                "SELECT COUNT(*) FROM sqlite_master "
                "WHERE name NOT LIKE 'sqlite_%'"
            ).fetchone()
            table = connection.execute(
                "SELECT 1 FROM sqlite_master "
                "WHERE type='table' AND name='schema_versions'"
            ).fetchone()
            version = 0
            if table is not None:
                row = connection.execute(
                    "SELECT MAX(version) FROM schema_versions"
                ).fetchone()
                version = int(row[0]) if row and row[0] is not None else 0
            return version, not bool(objects and objects[0])
        finally:
            connection.close()
    except SchemaUpgradeError:
        raise
    except sqlite3.Error as exc:
        raise SchemaUpgradeError("database_state_unavailable") from exc


def _planned_mutation(path: Path) -> tuple[int, int, bool, bool]:
    migration_files = db_mod.discover_up_migrations()
    code_max = db_mod.code_max_version(migration_files)
    known_versions = {db_mod._migration_version(item) for item in migration_files}
    initial_version, fresh = _schema_state(path)
    if not path.exists():
        return initial_version, code_max, fresh, bool(migration_files)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    try:
        repairs = db_mod._claimed_security_repairs_needed(
            connection, known_versions
        )
    finally:
        connection.close()
    pending = any(
        db_mod._migration_version(item) > initial_version
        for item in migration_files
    )
    return initial_version, code_max, fresh, bool(repairs or pending)


def _backup_root(path: Path) -> Path:
    base = (
        Path(settings.db_backup_dir).expanduser()
        if settings.db_backup_dir
        else path.parent
    )
    if not base.is_absolute():
        raise SchemaUpgradeError("database_backup_path_not_absolute")
    return Path(os.path.abspath(base))


def _backup_identity(release_id: str, attempt: int) -> str:
    return hashlib.sha256(
        f"{_release_id(release_id)}\0{attempt}".encode("utf-8")
    ).hexdigest()


def _expected_backup_path(
    path: Path,
    initial_version: int,
    backup_identity: str,
) -> Path:
    if re.fullmatch(r"[0-9a-f]{64}", backup_identity) is None:
        raise SchemaUpgradeError("backup_identity_invalid")
    base = _backup_root(path)
    return (
        base
        / db_mod.PRE_UPDATE_BACKUP_SUBDIR
        / f"attempt-{backup_identity}"
        / f"{path.name}.pre-update-v{initial_version}"
    )


def _file_sha256(path: Path) -> str:
    flags = os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise SchemaUpgradeError("backup_unavailable") from exc
    digest = hashlib.sha256()
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise SchemaUpgradeError("backup_invalid")
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
    except OSError as exc:
        raise SchemaUpgradeError("backup_unavailable") from exc
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def default_receipt_path(release_id: str) -> Path:
    release_id = _release_id(release_id)
    path = _database_path()
    digest = hashlib.sha256(release_id.encode("utf-8")).hexdigest()[:24]
    base = _backup_root(path)
    return base / "schema-upgrade-receipts" / f"upgrade-{digest}.json"


def _validate_receipt_path(path: Path) -> Path:
    path = Path(path).expanduser()
    if not path.is_absolute():
        raise SchemaUpgradeError("receipt_path_not_absolute")
    path = Path(os.path.abspath(path))
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        parent = path.parent.lstat()
    except OSError as exc:
        raise SchemaUpgradeError("receipt_directory_unavailable") from exc
    if stat.S_ISLNK(parent.st_mode) or not stat.S_ISDIR(parent.st_mode):
        raise SchemaUpgradeError("receipt_directory_invalid")
    os.chmod(path.parent, 0o700)
    if os.path.lexists(path):
        metadata = path.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
        ):
            raise SchemaUpgradeError("receipt_file_invalid")
    return path


def _write_receipt(path: Path, receipt: SchemaUpgradeReceipt) -> None:
    payload = asdict(receipt)
    payload["applied_versions"] = list(receipt.applied_versions)
    payload["repaired_versions"] = list(receipt.repaired_versions)
    raw = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    temp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(8)}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_CLOEXEC
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temp, flags, 0o600)
        try:
            written = 0
            while written < len(raw):
                count = os.write(descriptor, raw[written:])
                if count <= 0:
                    raise SchemaUpgradeError("receipt_write_failed")
                written += count
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temp, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def _read_receipt(path: Path) -> SchemaUpgradeReceipt:
    path = _validate_receipt_path(path)
    if not path.exists():
        raise SchemaUpgradeError("receipt_missing")
    try:
        payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        receipt = SchemaUpgradeReceipt(
            receipt_version=int(payload["receipt_version"]),
            release_id=_release_id(str(payload["release_id"])),
            database_path=str(payload["database_path"]),
            status=str(payload["status"]),
            proof_kind=_proof_kind(str(payload["proof_kind"])),
            attempt=int(payload["attempt"]),
            started_at=str(payload["started_at"]),
            completed_at=(
                str(payload["completed_at"])
                if payload.get("completed_at") is not None
                else None
            ),
            initial_version=int(payload["initial_version"]),
            final_version=(
                int(payload["final_version"])
                if payload.get("final_version") is not None
                else None
            ),
            code_max_version=int(payload["code_max_version"]),
            applied_versions=tuple(int(v) for v in payload["applied_versions"]),
            repaired_versions=tuple(int(v) for v in payload["repaired_versions"]),
            backup_path=(
                str(payload["backup_path"])
                if payload.get("backup_path") is not None
                else None
            ),
            fresh_database=bool(payload["fresh_database"]),
            backup_identity=(
                str(payload["backup_identity"])
                if payload.get("backup_identity") is not None
                else None
            ),
            backup_sha256=(
                str(payload["backup_sha256"])
                if payload.get("backup_sha256") is not None
                else None
            ),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SchemaUpgradeError("receipt_invalid") from exc
    if (
        receipt.receipt_version != RECEIPT_VERSION
        or receipt.status not in {*_TERMINAL_STATUSES, "running"}
        or receipt.attempt < 1
        or receipt.initial_version < 0
        or receipt.code_max_version < 1
        or receipt.database_path != str(_database_path())
        or (
            receipt.backup_identity is not None
            and re.fullmatch(r"[0-9a-f]{64}", receipt.backup_identity) is None
        )
        or (
            receipt.backup_sha256 is not None
            and re.fullmatch(r"[0-9a-f]{64}", receipt.backup_sha256) is None
        )
        or (receipt.backup_path is None) != (receipt.backup_identity is None)
    ):
        raise SchemaUpgradeError("receipt_invalid")
    return receipt


def run_controlled_upgrade(
    release_id: str,
    *,
    proof_kind: str,
    receipt_path: Path | None = None,
) -> SchemaUpgradeReceipt:
    """Run one offline upgrade after a caller has proved all writers stopped."""

    release_id = _release_id(release_id)
    proof_kind = _proof_kind(proof_kind)
    database_path = _database_path()
    receipt_path = _validate_receipt_path(
        receipt_path or default_receipt_path(release_id)
    )
    with db_mod._migration_lock(str(database_path)):
        return _run_controlled_upgrade_locked(
            release_id,
            proof_kind=proof_kind,
            database_path=database_path,
            receipt_path=receipt_path,
        )


def _run_controlled_upgrade_locked(
    release_id: str,
    *,
    proof_kind: str,
    database_path: Path,
    receipt_path: Path,
) -> SchemaUpgradeReceipt:
    """Run the complete receipt transition while its database lock is held."""

    previous: SchemaUpgradeReceipt | None = None
    if receipt_path.exists():
        previous = _read_receipt(receipt_path)
        if previous.release_id != release_id:
            raise SchemaUpgradeError("receipt_release_mismatch")
        if previous.status in {"running", "failed"}:
            raise SchemaUpgradeError("prior_attempt_requires_restore")
        if previous.status == "succeeded":
            observed, _fresh = _schema_state(database_path)
            if observed != previous.final_version:
                raise SchemaUpgradeError("successful_receipt_database_mismatch")
            return previous

    initial_version, code_max, fresh, mutates = _planned_mutation(database_path)
    attempt = previous.attempt + 1 if previous is not None else 1
    backup_identity = (
        _backup_identity(release_id, attempt)
        if mutates and not fresh
        else None
    )
    backup_path = (
        str(
            _expected_backup_path(
                database_path,
                initial_version,
                backup_identity,
            )
        )
        if backup_identity is not None
        else None
    )
    started = SchemaUpgradeReceipt(
        receipt_version=RECEIPT_VERSION,
        release_id=release_id,
        database_path=str(database_path),
        status="running",
        proof_kind=proof_kind,
        attempt=attempt,
        started_at=_now(),
        completed_at=None,
        initial_version=initial_version,
        final_version=None,
        code_max_version=code_max,
        applied_versions=(),
        repaired_versions=(),
        backup_path=backup_path,
        fresh_database=fresh,
        backup_identity=backup_identity,
        backup_sha256=None,
    )
    _write_receipt(receipt_path, started)
    running = started

    def persist_backup_ready(observed_path: str) -> None:
        """Anchor the complete backup before the migration runner may write."""

        nonlocal running
        if backup_path is None or observed_path != backup_path:
            raise SchemaUpgradeError("migration_backup_mismatch")
        running = replace(
            running,
            backup_sha256=_file_sha256(Path(observed_path)),
        )
        _write_receipt(receipt_path, running)

    prior_flag = os.environ.get(db_mod.QUIESCED_MIGRATION_ENV)
    os.environ[db_mod.QUIESCED_MIGRATION_ENV] = "1"
    try:
        result = db_mod.run_migrations(
            pre_update_backup_key=backup_identity,
            backup_ready=persist_backup_ready,
            _migration_lock_held=True,
        )
        if result.initial_version != initial_version or result.code_max_version != code_max:
            raise SchemaUpgradeError("migration_result_drift")
        if result.backup_path != backup_path:
            raise SchemaUpgradeError("migration_backup_mismatch")
        if (result.backup_path is None) != (running.backup_sha256 is None):
            raise SchemaUpgradeError("migration_backup_not_anchored")
    except Exception:
        try:
            observed, _fresh = _schema_state(database_path)
        except SchemaUpgradeError:
            observed = None
        backup_sha256 = running.backup_sha256
        if (
            backup_sha256 is None
            and backup_path is not None
            and os.path.lexists(backup_path)
        ):
            try:
                backup_sha256 = _file_sha256(Path(backup_path))
            except SchemaUpgradeError:
                backup_sha256 = None
        failed = SchemaUpgradeReceipt(
            **{
                **asdict(running),
                "status": "failed",
                "completed_at": _now(),
                "final_version": observed,
                "backup_sha256": backup_sha256,
            }
        )
        _write_receipt(receipt_path, failed)
        raise
    finally:
        if prior_flag is None:
            os.environ.pop(db_mod.QUIESCED_MIGRATION_ENV, None)
        else:
            os.environ[db_mod.QUIESCED_MIGRATION_ENV] = prior_flag

    completed = SchemaUpgradeReceipt(
        receipt_version=RECEIPT_VERSION,
        release_id=release_id,
        database_path=str(database_path),
        status="succeeded",
        proof_kind=proof_kind,
        attempt=started.attempt,
        started_at=started.started_at,
        completed_at=_now(),
        initial_version=result.initial_version,
        final_version=result.final_version,
        code_max_version=result.code_max_version,
        applied_versions=result.applied_versions,
        repaired_versions=result.repaired_versions,
        backup_path=result.backup_path,
        fresh_database=fresh,
        backup_identity=backup_identity,
        backup_sha256=running.backup_sha256,
    )
    _write_receipt(receipt_path, completed)
    return completed


def _unlink_sidecars(path: Path) -> None:
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if not os.path.lexists(sidecar):
            continue
        metadata = sidecar.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SchemaUpgradeError("database_sidecar_invalid")
        sidecar.unlink()


def _restore_backup(database_path: Path, backup_path: Path) -> None:
    if not backup_path.is_absolute() or not backup_path.exists():
        raise SchemaUpgradeError("backup_missing")
    metadata = backup_path.lstat()
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise SchemaUpgradeError("backup_invalid")
    source = sqlite3.connect(f"file:{backup_path}?mode=ro", uri=True)
    temp = database_path.parent / (
        f".{database_path.name}.restore-{os.getpid()}-{secrets.token_hex(8)}"
    )
    try:
        check = source.execute("PRAGMA integrity_check").fetchone()
        if not check or str(check[0]).lower() != "ok":
            raise SchemaUpgradeError("backup_integrity_red")
        destination = sqlite3.connect(temp)
        try:
            source.backup(destination)
            restored = destination.execute("PRAGMA integrity_check").fetchone()
            if not restored or str(restored[0]).lower() != "ok":
                raise SchemaUpgradeError("restored_integrity_red")
        finally:
            destination.close()
    finally:
        source.close()
    try:
        os.chmod(temp, 0o600)
        _unlink_sidecars(database_path)
        os.replace(temp, database_path)
        descriptor = os.open(database_path, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(
            database_path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.lexists(temp):
            os.unlink(temp)


def restore_controlled_upgrade(
    release_id: str,
    *,
    proof_kind: str,
    receipt_path: Path | None = None,
) -> SchemaUpgradeReceipt:
    """Restore the exact pre-upgrade database named by a trusted receipt."""

    release_id = _release_id(release_id)
    _proof_kind(proof_kind)
    database_path = _database_path()
    receipt_path = _validate_receipt_path(
        receipt_path or default_receipt_path(release_id)
    )
    with db_mod._migration_lock(str(database_path)):
        # The receipt, its backup digest, and the database form one state
        # transition. Read and validate all three under the same lock so a
        # concurrent migration runner cannot change the source after proof.
        receipt = _read_receipt(receipt_path)
        if receipt.release_id != release_id:
            raise SchemaUpgradeError("receipt_release_mismatch")
        if receipt.status == "rolled_back":
            observed, fresh = _schema_state(database_path)
            if observed != receipt.initial_version or fresh != receipt.fresh_database:
                raise SchemaUpgradeError("rollback_receipt_database_mismatch")
            return receipt
        if (
            receipt.status == "succeeded"
            and not receipt.fresh_database
            and receipt.backup_path is None
            and receipt.final_version == receipt.initial_version
            and not receipt.applied_versions
            and not receipt.repaired_versions
        ):
            observed, fresh = _schema_state(database_path)
            if observed != receipt.initial_version or fresh != receipt.fresh_database:
                raise SchemaUpgradeError("no_op_receipt_database_mismatch")
            rolled_back = SchemaUpgradeReceipt(
                **{
                    **asdict(receipt),
                    "status": "rolled_back",
                    "completed_at": _now(),
                    "final_version": observed,
                }
            )
            _write_receipt(receipt_path, rolled_back)
            return rolled_back
        if receipt.status not in {"running", "failed", "succeeded"}:
            raise SchemaUpgradeError("receipt_not_restorable")

        trusted_backup: Path | None = None
        if not receipt.fresh_database:
            if receipt.backup_path is None:
                raise SchemaUpgradeError("receipt_backup_missing")
            expected_identity = _backup_identity(
                release_id,
                receipt.attempt,
            )
            if receipt.backup_identity != expected_identity:
                raise SchemaUpgradeError("receipt_backup_identity_mismatch")
            trusted_backup = Path(receipt.backup_path)
            expected = _expected_backup_path(
                database_path,
                receipt.initial_version,
                expected_identity,
            )
            if trusted_backup != expected:
                raise SchemaUpgradeError("receipt_backup_mismatch")
            observed_digest = _file_sha256(trusted_backup)
            if receipt.backup_sha256 is None:
                if receipt.status != "running":
                    raise SchemaUpgradeError("receipt_backup_digest_missing")
                # A hard kill can land after SQLite completed and fsynced the
                # rollback point but before the callback persisted its digest.
                # The receipt fixes the exact path and identity; the backup reader
                # additionally requires a private, regular, single-link file.
                receipt = replace(
                    receipt,
                    backup_sha256=observed_digest,
                )
                _write_receipt(receipt_path, receipt)
            if observed_digest != receipt.backup_sha256:
                raise SchemaUpgradeError("receipt_backup_digest_mismatch")

        if receipt.fresh_database:
            _unlink_sidecars(database_path)
            if database_path.exists():
                database_path.unlink()
        else:
            if trusted_backup is None:
                raise SchemaUpgradeError("receipt_backup_missing")
            _restore_backup(database_path, trusted_backup)

        observed, fresh = _schema_state(database_path)
        if observed != receipt.initial_version or fresh != receipt.fresh_database:
            raise SchemaUpgradeError("rollback_database_mismatch")
        rolled_back = SchemaUpgradeReceipt(
            **{
                **asdict(receipt),
                "status": "rolled_back",
                "completed_at": _now(),
                "final_version": observed,
            }
        )
        _write_receipt(receipt_path, rolled_back)
        return rolled_back


def receipt_as_json(receipt: SchemaUpgradeReceipt) -> str:
    payload = asdict(receipt)
    payload["applied_versions"] = list(receipt.applied_versions)
    payload["repaired_versions"] = list(receipt.repaired_versions)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def prove_local_writers_stopped() -> str:
    """Prove no other process owned by this user has the SQLite files open.

    This is the OSS single-user boundary. Managed deployments use their own
    service/container inventory proof and call ``run_controlled_upgrade`` only
    after stopping that inventory.
    """

    try:
        import psutil
    except ImportError as exc:  # pragma: no cover - required OSS dependency
        raise SchemaUpgradeError("local_process_probe_unavailable") from exc

    database = _database_path()
    normalize = lambda value: os.path.normcase(os.path.abspath(value))
    protected = {
        normalize(str(database)),
        *(normalize(f"{database}{suffix}") for suffix in ("-journal", "-wal", "-shm")),
    }
    current_pid = os.getpid()
    current_uid = os.getuid() if hasattr(os, "getuid") else None
    blockers: list[int] = []
    for process in psutil.process_iter(["pid"]):
        if process.pid == current_pid:
            continue
        try:
            if current_uid is not None:
                uids = process.uids()
                if uids.real != current_uid:
                    continue
            open_files = process.open_files()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            continue
        except psutil.AccessDenied as exc:
            raise SchemaUpgradeError("local_process_probe_incomplete") from exc
        if any(normalize(item.path) in protected for item in open_files):
            blockers.append(process.pid)
    if blockers:
        joined = ",".join(str(pid) for pid in sorted(blockers))
        raise SchemaUpgradeError(f"sqlite_writers_still_running:{joined}")
    return "local_process_scan"
