"""Hosted workspace file tools core.

FastAPI-free service used by MCP now and by the future Console adapter later.
The shell runner is guarded and tenant-workspace scoped, but it is not an OS
filesystem jail: callers still need a trusted tenant session.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from core.api.use_cases._errors import (
    AuthorizationError,
    ConflictError,
    NotFoundError,
    ServiceError,
    ServiceUnavailableError,
    ValidationError,
)


MAX_READ_BYTES = 500_000
MAX_WRITE_BYTES = 500_000
MAX_TREE_ENTRIES = 500
MAX_GREP_RESULTS = 200
GREP_TIMEOUT_MS = 5_000
MAX_BASH_TIMEOUT_MS = 30_000
MAX_BASH_OUTPUT_BYTES = 64_000
MAX_BASH_COMMAND_CHARS = 4_000

SECRET_PATTERNS = (
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "id_rsa",
    "id_ed25519",
    ".ssh",
    ".gnupg",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "node_modules",
)

DEFAULT_STORAGE_WARN_PERCENT = 80
DEFAULT_STORAGE_BACKPRESSURE_PERCENT = 90
DEFAULT_STORAGE_FULL_PERCENT = 95
DEFAULT_STORAGE_SNAPSHOT_MAX_AGE_SECONDS = 24 * 60 * 60
DISABLED_ENV_VALUES = {"0", "false", "no", "off"}

SHELL_METACHARS_DENIED = ("\n", "\r", ";", "&", "|", "<", ">", "`", "$")
DENIED_EXECUTABLES = {
    "bash",
    "sh",
    "zsh",
    "fish",
    "sudo",
    "su",
    "ssh",
    "scp",
    "sftp",
    "rsync",
    "curl",
    "wget",
    "nc",
    "ncat",
    "socat",
    "mount",
    "umount",
    "mkfs",
    "dd",
    "chown",
    "chmod",
}
DENIED_OPTIONS_BY_EXECUTABLE = {
    "python": {"-c"},
    "python3": {"-c"},
    "node": {"-e", "--eval"},
}


@dataclass(frozen=True)
class WorkspaceActor:
    actor_id: str = "mcp"
    tenant_id: str = "tenant"
    auth_mode: str = "static_bearer"
    scopes: tuple[str, ...] = ("read:data", "write:data")


@dataclass(frozen=True)
class WorkspacePolicy:
    name: str = "portal_trust"
    can_read: bool = True
    can_write: bool = True
    can_shell: bool = False
    require_audit_for_writes: bool = False
    denied_patterns: tuple[str, ...] = SECRET_PATTERNS


@dataclass(frozen=True)
class WorkspaceStorageGuard:
    quota_mode: str = "off"
    used_bytes: int | None = None
    quota_bytes: int | None = None
    snapshot_at: str | None = None
    snapshot_age_seconds: int | None = None
    snapshot_stale: bool = False
    warn_percent: int = DEFAULT_STORAGE_WARN_PERCENT
    backpressure_percent: int = DEFAULT_STORAGE_BACKPRESSURE_PERCENT
    full_percent: int = DEFAULT_STORAGE_FULL_PERCENT
    source: str = "registry"


@dataclass
class AuditEvent:
    action: str
    phase: str
    actor_id: str
    tenant_id: str
    path: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    outcome: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


AuditSink = Callable[[AuditEvent], None]
Runner = Callable[..., subprocess.CompletedProcess[str]]


class WorkspaceError(ServiceError):
    """Workspace domain error with optional machine-readable context."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        http_status: int = 400,
        retryable: bool = False,
        suggested_next_tool: str | None = None,
        **extra: Any,
    ) -> None:
        self.http_status = http_status
        self.retryable = retryable
        self.suggested_next_tool = suggested_next_tool
        self.extra = extra
        if suggested_next_tool:
            extra["suggested_next_tool"] = suggested_next_tool
        if retryable:
            extra["retryable"] = True
        if extra:
            details = ", ".join(f"{key}={value}" for key, value in sorted(extra.items()))
            message = f"{message} ({details})"
        super().__init__(code=code, message=message)


def projects_root_from_env() -> Path:
    raw = os.environ.get("MARVIS_PROJECTS_ROOT", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    try:
        from core.scripts._projects_root import resolve_projects_root

        return resolve_projects_root()
    except Exception as exc:  # noqa: BLE001 - fail closed for hosted tools
        raise ServiceUnavailableError(
            code="projects_root_unavailable",
            message=f"Cannot resolve MARVIS_PROJECTS_ROOT: {exc}",
        ) from exc


def _root(projects_root: Path | None) -> Path:
    root = (projects_root or projects_root_from_env()).expanduser().resolve()
    if not root.is_dir():
        raise ServiceUnavailableError(
            code="projects_root_unavailable",
            message=f"Projects root is not a directory: {root}",
        )
    return root


def _repos_root(projects_root: Path) -> Path | None:
    raw = os.environ.get("MARVIS_REPOS_ROOT", "").strip()
    candidates = [Path(raw).expanduser()] if raw else []
    candidates.append(projects_root.parent / "repos")
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved.is_dir():
            return resolved
    return None


def _normalize_path(path: str, *, allow_root: bool = False) -> PurePosixPath:
    if "\x00" in path:
        raise ValidationError(code="invalid_path", message="Path contains null byte")
    raw = (path or "").strip()
    if raw in {"", "."}:
        if allow_root:
            return PurePosixPath(".")
        raise ValidationError(code="invalid_path", message="Path is required")
    pure = PurePosixPath(raw)
    if pure.is_absolute():
        raise ValidationError(code="path_outside_workspace", message="Absolute paths are not allowed")
    if any(part in {"..", ""} for part in pure.parts):
        raise ValidationError(code="path_outside_workspace", message="Parent traversal is not allowed")
    return pure


def _select_workspace_root(
    pure: PurePosixPath,
    *,
    projects_root: Path | None,
) -> tuple[Path, PurePosixPath, str | None]:
    projects = _root(projects_root)
    if pure.parts and pure.parts[0] == "projects":
        scoped = PurePosixPath(*pure.parts[1:]) if len(pure.parts) > 1 else PurePosixPath(".")
        return projects, scoped, "projects"
    if pure.parts and pure.parts[0] == "repos":
        repos = _repos_root(projects)
        if repos is None:
            raise ServiceUnavailableError(
                code="repos_root_unavailable",
                message="Repos root is not configured or does not exist",
            )
        scoped = PurePosixPath(*pure.parts[1:]) if len(pure.parts) > 1 else PurePosixPath(".")
        return repos, scoped, "repos"
    return projects, pure, None


def _logical_path(prefix: str | None, pure: PurePosixPath) -> str:
    if str(pure) == ".":
        return prefix or ""
    return f"{prefix}/{pure}" if prefix else str(pure)


def _display_path_for_root(path: str, rel_path: str) -> str:
    if rel_path == "repos" or rel_path.startswith("repos/"):
        return f"repos/{path}"
    if rel_path == "projects" or rel_path.startswith("projects/"):
        return f"projects/{path}"
    return path


def _matches_secret_pattern(part: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(part, pattern) for pattern in patterns)


def _deny_secret_path(pure: PurePosixPath, policy: WorkspacePolicy) -> None:
    for part in pure.parts:
        if part == ".":
            continue
        if _matches_secret_pattern(part, policy.denied_patterns):
            raise AuthorizationError(
                code="workspace_path_denied",
                message=f"Path segment is denied by workspace policy: {part}",
            )


def _resolve_path(
    path: str,
    *,
    projects_root: Path | None = None,
    policy: WorkspacePolicy | None = None,
    allow_root: bool = False,
    ) -> tuple[Path, str, Path]:
    policy = policy or WorkspacePolicy()
    pure_raw = _normalize_path(path, allow_root=allow_root)
    root, pure, prefix = _select_workspace_root(pure_raw, projects_root=projects_root)
    _deny_secret_path(pure, policy)

    target = root if str(pure) == "." else root.joinpath(*pure.parts)
    if target == root:
        resolved_parent = root
    else:
        resolved_parent = target.parent.resolve()
        if not resolved_parent.is_relative_to(root):
            raise AuthorizationError(code="path_outside_workspace", message="Path escapes projects root")

    current = root
    for part in pure.parts:
        if part == ".":
            continue
        candidate = current / part
        try:
            if candidate.exists() and candidate.is_symlink():
                raise AuthorizationError(
                    code="workspace_symlink_denied",
                    message=f"Symlinks are not allowed: {pure}",
                )
        except OSError as exc:
            raise ValidationError(code="path_stat_failed", message=str(exc)) from exc
        current = candidate

    resolved = target.resolve() if target.exists() else target.absolute()
    if not resolved.is_relative_to(root):
        raise AuthorizationError(code="path_outside_workspace", message="Path escapes projects root")
    return target, _logical_path(prefix, pure), root


def _ensure_read(policy: WorkspacePolicy) -> None:
    if not policy.can_read:
        raise AuthorizationError(code="scope_denied", message="read:data scope required")


def _ensure_write(policy: WorkspacePolicy) -> None:
    if not policy.can_write:
        raise AuthorizationError(code="scope_denied", message="write:data scope required")


def _ensure_shell(policy: WorkspacePolicy) -> None:
    if not policy.can_shell:
        raise AuthorizationError(code="workspace_shell_disabled", message="shell:run scope required")


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _quota_bytes_from_storage(storage: dict[str, Any]) -> int | None:
    raw = _int_or_none(storage.get("quota_bytes"))
    if raw is not None:
        return raw
    try:
        quota_gb = storage.get("quota_gb")
        if quota_gb is None:
            return None
        return int(float(quota_gb) * 1024 * 1024 * 1024)
    except (TypeError, ValueError):
        return None


def _snapshot_age_seconds(value: Any) -> int | None:
    if not value:
        return None
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0, int((datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()))
    except (TypeError, ValueError):
        return None


def _storage_snapshot_max_age_seconds() -> int:
    raw = os.environ.get("MARVIS_WORKSPACE_STORAGE_SNAPSHOT_MAX_AGE_SECONDS")
    if raw is None:
        return DEFAULT_STORAGE_SNAPSHOT_MAX_AGE_SECONDS
    parsed = _int_or_none(raw)
    return DEFAULT_STORAGE_SNAPSHOT_MAX_AGE_SECONDS if parsed is None else max(0, parsed)


def _tenant_root_from_projects_root(root: Path) -> Path:
    if root.name in {"projects", "repos"}:
        return root.parent
    return root


def storage_guard_from_env(root: Path) -> WorkspaceStorageGuard | None:
    raw_enabled = os.environ.get("MARVIS_WORKSPACE_STORAGE_GUARD", "1").strip().lower()
    if raw_enabled in DISABLED_ENV_VALUES:
        return None

    tenant_id = (os.environ.get("TENANT_ID") or os.environ.get("MARVIS_TENANT_ID") or "").strip()
    if not tenant_id:
        return None

    registry_path = Path(
        os.environ.get("MARVIS_TENANT_REGISTRY_PATH", "/var/lib/marvis/tenants/registry.json")
    )
    try:
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WorkspaceError(
            code="storage_guard_unavailable",
            message="Workspace storage quota registry is not readable",
            http_status=507,
            retryable=True,
            suggested_next_tool="storage_usage",
            registry_path=str(registry_path),
        ) from exc
    except json.JSONDecodeError as exc:
        raise WorkspaceError(
            code="storage_guard_unavailable",
            message="Workspace storage quota registry is invalid JSON",
            http_status=507,
            retryable=True,
            suggested_next_tool="storage_usage",
            registry_path=str(registry_path),
        ) from exc

    tenants = registry.get("tenants") if isinstance(registry, dict) else None
    tenant_meta = tenants.get(tenant_id) if isinstance(tenants, dict) else None
    storage = tenant_meta.get("storage") if isinstance(tenant_meta, dict) else None
    if not isinstance(storage, dict):
        raise WorkspaceError(
            code="storage_guard_unavailable",
            message="Workspace storage quota metadata is missing",
            http_status=507,
            retryable=True,
            suggested_next_tool="storage_usage",
            registry_path=str(registry_path),
            tenant_id=tenant_id,
        )

    quota_bytes = _quota_bytes_from_storage(storage)
    used_bytes = _int_or_none(storage.get("last_usage_bytes"))
    snapshot_at = storage.get("last_usage_snapshot_at")
    snapshot_age = _snapshot_age_seconds(snapshot_at)
    max_age = _storage_snapshot_max_age_seconds()
    snapshot_stale = bool(
        used_bytes is None
        or snapshot_age is None
        or (max_age > 0 and snapshot_age > max_age)
    )

    return WorkspaceStorageGuard(
        quota_mode=str(storage.get("quota_mode") or "off"),
        used_bytes=used_bytes,
        quota_bytes=quota_bytes,
        snapshot_at=str(snapshot_at) if snapshot_at else None,
        snapshot_age_seconds=snapshot_age,
        snapshot_stale=snapshot_stale,
        warn_percent=_int_or_none(storage.get("warn_percent")) or DEFAULT_STORAGE_WARN_PERCENT,
        backpressure_percent=_int_or_none(storage.get("backpressure_percent"))
        or DEFAULT_STORAGE_BACKPRESSURE_PERCENT,
        full_percent=_int_or_none(storage.get("full_percent")) or DEFAULT_STORAGE_FULL_PERCENT,
        source=f"registry:{registry_path}:{tenant_id}:{_tenant_root_from_projects_root(root)}",
    )


def _used_percent(used_bytes: int, quota_bytes: int) -> float:
    return used_bytes * 100 / quota_bytes


def _ensure_storage_allows_write(
    guard: WorkspaceStorageGuard | None,
    *,
    incoming_bytes: int,
    existing_bytes: int,
) -> None:
    if guard is None or guard.quota_mode == "off":
        return
    if guard.used_bytes is None:
        raise WorkspaceError(
            code="storage_usage_unknown",
            message="Workspace storage quota has no usage snapshot",
            http_status=507,
            retryable=True,
            suggested_next_tool="directory_tree",
            quota_mode=guard.quota_mode,
            quota_bytes=guard.quota_bytes,
        )
    if guard.snapshot_stale:
        raise WorkspaceError(
            code="storage_usage_stale",
            message="Workspace storage quota usage snapshot is stale",
            http_status=507,
            retryable=True,
            suggested_next_tool="directory_tree",
            quota_mode=guard.quota_mode,
            used_bytes=guard.used_bytes,
            quota_bytes=guard.quota_bytes,
            snapshot_at=guard.snapshot_at,
            snapshot_age_seconds=guard.snapshot_age_seconds,
        )
    if not guard.quota_bytes or guard.quota_bytes <= 0:
        raise WorkspaceError(
            code="storage_quota_unknown",
            message="Workspace storage quota bytes are not configured",
            http_status=507,
            retryable=True,
            suggested_next_tool="directory_tree",
            quota_mode=guard.quota_mode,
            used_bytes=guard.used_bytes,
        )

    used_bytes = max(0, guard.used_bytes)
    existing_bytes = max(0, existing_bytes)
    incoming_bytes = max(0, incoming_bytes)
    delta_bytes = max(0, incoming_bytes - existing_bytes)
    projected_bytes = used_bytes + delta_bytes
    current_percent = _used_percent(used_bytes, guard.quota_bytes)
    projected_percent = _used_percent(projected_bytes, guard.quota_bytes)

    common = {
        "quota_mode": guard.quota_mode,
        "used_bytes": used_bytes,
        "quota_bytes": guard.quota_bytes,
        "used_percent": round(current_percent, 2),
        "projected_percent": round(projected_percent, 2),
        "delta_bytes": delta_bytes,
    }
    if current_percent >= guard.full_percent or projected_percent >= guard.full_percent:
        raise WorkspaceError(
            code="storage_full",
            message="Workspace storage quota is full",
            http_status=507,
            retryable=True,
            suggested_next_tool="directory_tree",
            full_percent=guard.full_percent,
            **common,
        )
    if delta_bytes > 0 and (
        current_percent >= guard.backpressure_percent
        or projected_percent >= guard.backpressure_percent
    ):
        raise WorkspaceError(
            code="storage_backpressure",
            message="Workspace storage quota is in backpressure",
            http_status=507,
            retryable=True,
            suggested_next_tool="directory_tree",
            backpressure_percent=guard.backpressure_percent,
            **common,
        )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise NotFoundError(code="file_not_found", message=f"File not found: {path.name}") from exc
    except OSError as exc:
        raise ServiceUnavailableError(code="file_read_failed", message=str(exc)) from exc


def _file_metadata(target: Path, rel_path: str, content_hash: str | None = None) -> dict[str, Any]:
    stat = target.stat()
    if content_hash is None:
        content_hash = _sha256_bytes(target.read_bytes())
    return {
        "path": rel_path,
        "name": target.name,
        "size_bytes": stat.st_size,
        "mtime": stat.st_mtime,
        "sha256": content_hash,
    }


def _freshness(root: Path, rel_path: str, current_sha256: str) -> dict[str, Any]:
    db_path = os.environ.get("MARVIS_DB_PATH") or os.environ.get("PIR_DB_PATH")
    if not db_path or not Path(db_path).is_file():
        return {
            "current_sha256": current_sha256,
            "indexed_sha256": None,
            "freshness_status": "freshness_unavailable",
            "indexed_at": None,
        }
    candidates = [rel_path, f"projects/{rel_path}", str((root / rel_path).resolve())]
    try:
        conn = sqlite3.connect(db_path)
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_state'"
            ).fetchone()
            if not exists:
                raise sqlite3.OperationalError("file_state table missing")
            placeholders = ",".join("?" for _ in candidates)
            row = conn.execute(
                f"SELECT sha256, indexed_at FROM file_state WHERE path IN ({placeholders}) "
                "ORDER BY indexed_at DESC LIMIT 1",
                candidates,
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return {
            "current_sha256": current_sha256,
            "indexed_sha256": None,
            "freshness_status": "freshness_unavailable",
            "indexed_at": None,
        }
    if not row:
        return {
            "current_sha256": current_sha256,
            "indexed_sha256": None,
            "freshness_status": "indexed_missing",
            "indexed_at": None,
        }
    indexed_sha256, indexed_at = row
    return {
        "current_sha256": current_sha256,
        "indexed_sha256": indexed_sha256,
        "freshness_status": "indexed_fresh" if indexed_sha256 == current_sha256 else "indexed_pending",
        "indexed_at": indexed_at,
    }


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:4096]


def _audit(
    *,
    policy: WorkspacePolicy,
    sink: AuditSink | None,
    event: AuditEvent,
) -> str:
    if sink is None:
        if policy.require_audit_for_writes:
            raise ServiceUnavailableError(
                code="audit_unavailable",
                message="Workspace write audit is required but no audit sink is configured",
            )
        return "skipped"
    try:
        sink(event)
    except Exception as exc:  # noqa: BLE001
        if policy.require_audit_for_writes:
            raise ServiceUnavailableError(
                code="audit_unavailable",
                message=f"Workspace audit sink failed: {exc}",
            ) from exc
        return "failed_open"
    return "written"


def _truncate_text_bytes(text: str | bytes | None, max_bytes: int) -> tuple[str, bool, int]:
    if text is None:
        return "", False, 0
    if isinstance(text, bytes):
        raw = text
    else:
        raw = text.encode("utf-8", errors="replace")
    truncated = len(raw) > max_bytes
    clipped = raw[:max_bytes]
    return clipped.decode("utf-8", errors="replace"), truncated, len(raw)


def _validate_shell_command(command: str) -> list[str]:
    if "\x00" in command:
        raise ValidationError(code="invalid_command", message="Command contains null byte")
    if len(command) > MAX_BASH_COMMAND_CHARS:
        raise ValidationError(
            code="command_too_large",
            message=f"Command exceeds {MAX_BASH_COMMAND_CHARS} characters",
        )
    if any(char in command for char in SHELL_METACHARS_DENIED):
        raise ValidationError(
            code="unsupported_shell_syntax",
            message="Shell metacharacters are not allowed in hosted run_bash",
        )
    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise ValidationError(code="invalid_command", message=str(exc)) from exc
    if not argv:
        raise ValidationError(code="invalid_command", message="Command is required")

    executable = Path(argv[0]).name
    if executable in DENIED_EXECUTABLES:
        raise AuthorizationError(
            code="command_denied",
            message=f"Executable is denied by workspace shell policy: {executable}",
        )
    denied_options = DENIED_OPTIONS_BY_EXECUTABLE.get(executable, set())
    if any(arg in denied_options for arg in argv[1:]):
        raise AuthorizationError(
            code="command_denied",
            message=f"Option is denied by workspace shell policy for {executable}",
        )

    for token in argv:
        if token.startswith("~") or "=" in token:
            raise AuthorizationError(
                code="command_denied",
                message="Home expansion and environment assignment are not allowed",
            )
        pure = PurePosixPath(token)
        if pure.is_absolute():
            raise AuthorizationError(
                code="path_outside_workspace",
                message="Absolute command paths are not allowed",
            )
        if any(part == ".." for part in pure.parts):
            raise AuthorizationError(
                code="path_outside_workspace",
                message="Parent traversal is not allowed in command arguments",
            )
    return argv


def _shell_env(root: Path, cwd: Path) -> dict[str, str]:
    default_path = "/usr/local/bin:/usr/bin:/bin"
    path = os.environ.get("PATH", default_path)
    venv = os.environ.get("MARVIS_VENV", "").strip()
    if venv:
        path = f"{venv}/bin:{path}"
    return {
        "HOME": str(root),
        "PATH": path,
        "PWD": str(cwd),
        "MARVIS_PROJECTS_ROOT": str(root),
        "PYTHONUNBUFFERED": "1",
    }


def read_file(
    path: str,
    *,
    projects_root: Path | None = None,
    actor: WorkspaceActor | None = None,
    policy: WorkspacePolicy | None = None,
    max_bytes: int = MAX_READ_BYTES,
) -> dict[str, Any]:
    del actor
    policy = policy or WorkspacePolicy()
    _ensure_read(policy)
    target, rel_path, root = _resolve_path(path, projects_root=projects_root, policy=policy)
    if not target.is_file():
        raise NotFoundError(code="file_not_found", message=f"File not found: {rel_path}")
    data = _read_bytes(target)
    if _is_binary(data):
        raise ValidationError(code="binary_file_unsupported", message=f"Binary files are not supported: {rel_path}")
    truncated = len(data) > max_bytes
    returned = data[:max_bytes]
    content = returned.decode("utf-8", errors="replace")
    digest = _sha256_bytes(data)
    meta = _file_metadata(target, rel_path, digest)
    return {
        "ok": True,
        **meta,
        "content": content,
        "truncated": truncated,
        "bytes_returned": len(returned),
        "bytes_total": len(data),
        "freshness": _freshness(root, rel_path, digest),
    }


def directory_tree(
    path: str = "",
    *,
    projects_root: Path | None = None,
    actor: WorkspaceActor | None = None,
    policy: WorkspacePolicy | None = None,
    max_depth: int = 3,
    max_entries: int = MAX_TREE_ENTRIES,
) -> dict[str, Any]:
    del actor
    policy = policy or WorkspacePolicy()
    _ensure_read(policy)
    target, rel_path, root = _resolve_path(path, projects_root=projects_root, policy=policy, allow_root=True)
    if not target.is_dir():
        raise ValidationError(code="not_a_directory", message=f"Not a directory: {rel_path}")
    entries: list[dict[str, Any]] = []
    truncated = False

    def _walk(current: Path, depth: int) -> None:
        nonlocal truncated
        if truncated or depth > max_depth:
            return
        try:
            children = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for child in children:
            if len(entries) >= max_entries:
                truncated = True
                return
            if child.is_symlink():
                continue
            try:
                child_rel_base = str(child.resolve().relative_to(root))
                child_rel = _display_path_for_root(child_rel_base, rel_path)
            except (OSError, ValueError):
                continue
            try:
                _resolve_path(child_rel, projects_root=projects_root, policy=policy)
            except ServiceError:
                continue
            kind = "directory" if child.is_dir() else "file"
            stat = child.stat()
            entries.append(
                {
                    "path": child_rel,
                    "name": child.name,
                    "type": kind,
                    "size_bytes": stat.st_size if kind == "file" else None,
                    "mtime": stat.st_mtime,
                }
            )
            if kind == "directory":
                _walk(child, depth + 1)

    _walk(target, 1)
    return {
        "ok": True,
        "path": rel_path,
        "entries": entries,
        "entry_count": len(entries),
        "truncated": truncated,
        "max_depth": max_depth,
        "max_entries": max_entries,
    }


def grep(
    pattern: str,
    *,
    path: str = "",
    projects_root: Path | None = None,
    actor: WorkspaceActor | None = None,
    policy: WorkspacePolicy | None = None,
    max_results: int = MAX_GREP_RESULTS,
    timeout_ms: int = GREP_TIMEOUT_MS,
    rg_path: str | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    del actor
    if not pattern:
        raise ValidationError(code="invalid_pattern", message="Pattern is required")
    policy = policy or WorkspacePolicy()
    _ensure_read(policy)
    target, rel_path, root = _resolve_path(path, projects_root=projects_root, policy=policy, allow_root=True)
    binary = shutil.which("rg") if rg_path is None else rg_path
    if not binary:
        raise ServiceUnavailableError(
            code="grep_backend_unavailable",
            message="ripgrep (rg) is required for workspace grep but is not installed",
        )
    globs: list[str] = []
    for denied in policy.denied_patterns:
        globs.extend(["--glob", f"!{denied}", "--glob", f"!**/{denied}/**"])
    cmd = [
        binary,
        "--line-number",
        "--with-filename",
        "--color",
        "never",
        "--hidden",
        "--no-messages",
        "--max-columns",
        "500",
        *globs,
        "--",
        pattern,
        str(target),
    ]
    try:
        result = runner(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
        )
    except subprocess.TimeoutExpired as exc:
        raise ServiceUnavailableError(
            code="grep_timeout",
            message=f"grep timed out after {timeout_ms}ms",
        ) from exc
    if result.returncode not in (0, 1):
        raise ServiceUnavailableError(
            code="grep_failed",
            message=(result.stderr or result.stdout or "ripgrep failed").strip(),
        )

    matches: list[dict[str, Any]] = []
    truncated = False
    for line in result.stdout.splitlines():
        if len(matches) >= max_results:
            truncated = True
            break
        file_part, sep, rest = line.partition(":")
        if not sep:
            continue
        line_no, sep, text = rest.partition(":")
        if not sep:
            continue
        try:
            found_path = Path(file_part).resolve()
            found_rel_base = str(found_path.relative_to(root))
            found_rel = _display_path_for_root(found_rel_base, rel_path)
            _resolve_path(found_rel, projects_root=projects_root, policy=policy)
        except (OSError, ValueError, ServiceError):
            continue
        try:
            parsed_line = int(line_no)
        except ValueError:
            continue
        matches.append({"path": found_rel, "line": parsed_line, "text": text})
    return {
        "ok": True,
        "path": rel_path,
        "pattern": pattern,
        "matched": bool(matches),
        "matches": matches,
        "match_count": len(matches),
        "truncated": truncated,
        "timeout_ms": timeout_ms,
    }


def write_file(
    path: str,
    content: str,
    *,
    projects_root: Path | None = None,
    actor: WorkspaceActor | None = None,
    policy: WorkspacePolicy | None = None,
    audit_sink: AuditSink | None = None,
    if_match_sha256: str | None = None,
    overwrite: bool = False,
    create_parent: bool = False,
    max_bytes: int = MAX_WRITE_BYTES,
    storage_guard: WorkspaceStorageGuard | None = None,
    audit_action: str = "write_file",
) -> dict[str, Any]:
    actor = actor or WorkspaceActor()
    policy = policy or WorkspacePolicy()
    _ensure_write(policy)
    target, rel_path, root = _resolve_path(path, projects_root=projects_root, policy=policy)
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise ValidationError(code="content_too_large", message=f"Content exceeds {max_bytes} bytes")
    existed = target.exists()
    before_sha = None
    existing_size = 0
    if existed:
        if not target.is_file():
            raise ValidationError(code="not_a_file", message=f"Not a file: {rel_path}")
        before_data = _read_bytes(target)
        existing_size = len(before_data)
        before_sha = _sha256_bytes(before_data)
        if if_match_sha256 and if_match_sha256 != before_sha:
            raise WorkspaceError(
                code="conflict",
                message="if_match_sha256 does not match current file",
                http_status=409,
                current_sha256=before_sha,
                suggested_next_tool="read_file",
            )
        if not if_match_sha256 and not overwrite:
            raise WorkspaceError(
                code="conflict",
                message="Existing file requires if_match_sha256 or overwrite=true",
                http_status=409,
                current_sha256=before_sha,
                suggested_next_tool="read_file",
            )
    elif not target.parent.exists():
        if not create_parent:
            raise NotFoundError(
                code="parent_not_found",
                message=f"Parent directory does not exist: {str(PurePosixPath(rel_path).parent)}",
            )

    _ensure_storage_allows_write(
        storage_guard if storage_guard is not None else storage_guard_from_env(root),
        incoming_bytes=len(encoded),
        existing_bytes=existing_size,
    )

    if not existed and not target.parent.exists():
        target.parent.mkdir(parents=True, exist_ok=True)

    audit_status = _audit(
        policy=policy,
        sink=audit_sink,
        event=AuditEvent(
            action=audit_action,
            phase="intent",
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            path=rel_path,
            before_sha256=before_sha,
        ),
    )
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".marvis-write-", suffix=".tmp")
        try:
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp_name, target)
        try:
            dir_fd = os.open(str(target.parent), os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except OSError as exc:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
        raise ServiceUnavailableError(code="file_write_failed", message=str(exc)) from exc

    after_sha = _sha256_bytes(encoded)
    completion_audit = _audit(
        policy=policy,
        sink=audit_sink,
        event=AuditEvent(
            action=audit_action,
            phase="completion",
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            path=rel_path,
            before_sha256=before_sha,
            after_sha256=after_sha,
            outcome="ok",
        ),
    )
    meta = _file_metadata(target, rel_path, after_sha)
    return {
        "ok": True,
        **meta,
        "created": not existed,
        "previous_sha256": before_sha,
        "audit_status": completion_audit if completion_audit != "skipped" else audit_status,
        "freshness": _freshness(root, rel_path, after_sha),
    }


def edit(
    path: str,
    old_text: str,
    new_text: str,
    *,
    projects_root: Path | None = None,
    actor: WorkspaceActor | None = None,
    policy: WorkspacePolicy | None = None,
    audit_sink: AuditSink | None = None,
    if_match_sha256: str | None = None,
    replace_all: bool = False,
    expected_replacements: int | None = None,
    storage_guard: WorkspaceStorageGuard | None = None,
) -> dict[str, Any]:
    if old_text == "":
        raise ValidationError(code="invalid_edit", message="old_text cannot be empty")
    current = read_file(path, projects_root=projects_root, actor=actor, policy=policy, max_bytes=MAX_WRITE_BYTES)
    content = current["content"]
    current_sha = current["sha256"]
    if if_match_sha256 and if_match_sha256 != current_sha:
        raise WorkspaceError(
            code="conflict",
            message="if_match_sha256 does not match current file",
            http_status=409,
            current_sha256=current_sha,
            suggested_next_tool="read_file",
        )
    count = content.count(old_text)
    if count == 0:
        raise ValidationError(code="edit_no_match", message="old_text was not found")
    if expected_replacements is not None and count != expected_replacements:
        raise WorkspaceError(
            code="unexpected_replacement_count",
            message="old_text match count differs from expected_replacements",
            matches_found=count,
            suggested_next_tool="read_file",
        )
    if count > 1 and not replace_all:
        raise WorkspaceError(
            code="ambiguous_edit",
            message="old_text matched multiple times and replace_all=false",
            matches_found=count,
            suggested_next_tool="read_file",
        )
    replaced = content.replace(old_text, new_text, -1 if replace_all else 1)
    result = write_file(
        path,
        replaced,
        projects_root=projects_root,
        actor=actor,
        policy=policy,
        audit_sink=audit_sink,
        if_match_sha256=current_sha,
        overwrite=False,
        create_parent=False,
        storage_guard=storage_guard,
        audit_action="edit",
    )
    result["replacements"] = count if replace_all else 1
    return result


def run_bash(
    command: str,
    *,
    cwd: str = "",
    projects_root: Path | None = None,
    actor: WorkspaceActor | None = None,
    policy: WorkspacePolicy | None = None,
    audit_sink: AuditSink | None = None,
    timeout_ms: int = MAX_BASH_TIMEOUT_MS,
    max_output_bytes: int = MAX_BASH_OUTPUT_BYTES,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    actor = actor or WorkspaceActor()
    policy = policy or WorkspacePolicy()
    _ensure_shell(policy)
    argv = _validate_shell_command(command)
    target, rel_path, root = _resolve_path(cwd, projects_root=projects_root, policy=policy, allow_root=True)
    if not target.is_dir():
        raise ValidationError(code="not_a_directory", message=f"Not a directory: {rel_path}")
    timeout_ms = max(100, min(timeout_ms, MAX_BASH_TIMEOUT_MS))
    max_output_bytes = max(1, min(max_output_bytes, MAX_BASH_OUTPUT_BYTES))

    audit_status = _audit(
        policy=policy,
        sink=audit_sink,
        event=AuditEvent(
            action="run_bash",
            phase="intent",
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            path=rel_path,
            metadata={"command": command, "timeout_ms": timeout_ms},
        ),
    )

    started = time.monotonic()
    timed_out = False
    returncode: int | None
    stdout_raw: str | bytes | None
    stderr_raw: str | bytes | None
    try:
        result = runner(
            ["/bin/bash", "-lc", command],
            cwd=str(target),
            env=_shell_env(root, target),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
        )
        returncode = result.returncode
        stdout_raw = result.stdout
        stderr_raw = result.stderr
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        stdout_raw = exc.stdout
        stderr_raw = exc.stderr or f"Command timed out after {timeout_ms}ms"
    except OSError as exc:
        raise ServiceUnavailableError(code="run_bash_failed", message=str(exc)) from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    stdout, stdout_truncated, stdout_total = _truncate_text_bytes(stdout_raw, max_output_bytes)
    stderr, stderr_truncated, stderr_total = _truncate_text_bytes(stderr_raw, max_output_bytes)
    ok = bool(not timed_out and returncode == 0)

    completion_audit = _audit(
        policy=policy,
        sink=audit_sink,
        event=AuditEvent(
            action="run_bash",
            phase="completion",
            actor_id=actor.actor_id,
            tenant_id=actor.tenant_id,
            path=rel_path,
            outcome="ok" if ok else "failed",
            metadata={
                "command": command,
                "argv0": argv[0],
                "returncode": returncode,
                "timed_out": timed_out,
                "duration_ms": duration_ms,
            },
        ),
    )

    return {
        "ok": ok,
        "command": command,
        "cwd": rel_path,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_ms": duration_ms,
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
        "stdout_bytes_total": stdout_total,
        "stderr_bytes_total": stderr_total,
        "audit_status": completion_audit if completion_audit != "skipped" else audit_status,
        "safety": {
            "mode": "guarded_command_runner",
            "filesystem_jail": False,
            "denies_absolute_paths": True,
            "denies_parent_traversal": True,
            "denies_shell_metacharacters": True,
        },
    }
