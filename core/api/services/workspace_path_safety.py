"""Filesystem path protections shared by workspace readers and indexed search."""
from __future__ import annotations

import fnmatch
from pathlib import Path, PurePosixPath

from core.api.use_cases._errors import AuthorizationError, ValidationError

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


def deny_secret_path(pure: PurePosixPath, patterns: tuple[str, ...]) -> None:
    for part in pure.parts:
        if part != "." and any(fnmatch.fnmatch(part, pattern) for pattern in patterns):
            raise AuthorizationError(
                code="workspace_path_denied",
                message=f"Path segment is denied by workspace policy: {part}",
            )


def resolve_path_in_root(
    root: Path, pure: PurePosixPath, *, denied_patterns: tuple[str, ...] = SECRET_PATTERNS
) -> Path:
    """Validate a relative path under an already resolved workspace root."""
    if "\x00" in str(pure):
        raise ValidationError(code="invalid_path", message="Path contains null byte")
    if pure.is_absolute() or ".." in pure.parts:
        raise ValidationError(code="path_outside_workspace", message="Path must stay inside its root")
    deny_secret_path(pure, denied_patterns)
    target = root if str(pure) == "." else root.joinpath(*pure.parts)
    if target != root and not target.parent.resolve().is_relative_to(root):
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
    return target
