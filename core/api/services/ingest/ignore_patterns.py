"""Shared ignore rules for container ingest inputs."""
from __future__ import annotations

from pathlib import PurePosixPath

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024

IGNORED_DIRECTORY_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "tmp",
}

IGNORED_FILE_NAMES = {
    ".DS_Store",
    "Thumbs.db",
    "desktop.ini",
}

IGNORED_SUFFIXES = {
    ".app",
    ".bat",
    ".bin",
    ".cmd",
    ".com",
    ".dll",
    ".dmg",
    ".exe",
    ".iso",
    ".lnk",
    ".msi",
    ".o",
    ".out",
    ".part",
    ".pyc",
    ".so",
    ".swp",
    ".tmp",
}


def normalize_relative_path(path: str | PurePosixPath) -> PurePosixPath:
    normalized = str(path).replace("\\", "/")
    return PurePosixPath(normalized)


def is_ignored_directory_name(name: str) -> bool:
    return name in IGNORED_DIRECTORY_NAMES


def should_ignore(
    relative_path: str | PurePosixPath,
    *,
    file_size_bytes: int | None = None,
) -> str | None:
    path = normalize_relative_path(relative_path)
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts:
        return "empty-path"
    if any(part == ".." for part in parts):
        return "path-traversal"
    if any(is_ignored_directory_name(part) for part in parts[:-1]):
        return "ignored-directory"

    filename = parts[-1]
    if filename in IGNORED_FILE_NAMES or filename.startswith("."):
        return "ignored-file"
    if PurePosixPath(filename).suffix.lower() in IGNORED_SUFFIXES:
        return "ignored-suffix"
    if file_size_bytes is not None and file_size_bytes > MAX_FILE_SIZE_BYTES:
        return "file-too-large"
    return None
