"""Folder traversal helpers for Universal Ingestion containers."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from core.api.services.ingest.ignore_patterns import (
    is_ignored_directory_name,
    should_ignore,
)


@dataclass(frozen=True)
class FolderFile:
    path: Path
    relative_path: PurePosixPath
    size_bytes: int


@dataclass(frozen=True)
class FolderSkipped:
    path: str
    reason: str


@dataclass(frozen=True)
class FolderScanResult:
    files: list[FolderFile]
    skipped: list[FolderSkipped]


def scan_folder_files(root: Path, *, max_depth: int = 5) -> FolderScanResult:
    root = root.resolve()
    files: list[FolderFile] = []
    skipped: list[FolderSkipped] = []
    visited_dirs: set[Path] = set()

    def walk(directory: Path, rel_dir: PurePosixPath, depth: int) -> None:
        if depth > max_depth:
            skipped.append(FolderSkipped(str(rel_dir), "max-depth"))
            return
        try:
            resolved_dir = directory.resolve()
        except OSError:
            skipped.append(FolderSkipped(str(rel_dir), "unreadable"))
            return
        if resolved_dir in visited_dirs:
            skipped.append(FolderSkipped(str(rel_dir), "symlink-loop"))
            return
        visited_dirs.add(resolved_dir)

        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
        except OSError:
            skipped.append(FolderSkipped(str(rel_dir), "unreadable"))
            return

        for child in children:
            rel_path = rel_dir / child.name
            if child.is_symlink():
                skipped.append(FolderSkipped(str(rel_path), "symlink"))
                continue
            if child.is_dir():
                if is_ignored_directory_name(child.name):
                    skipped.append(FolderSkipped(str(rel_path), "ignored-directory"))
                    continue
                walk(child, rel_path, depth + 1)
                continue
            if not child.is_file():
                skipped.append(FolderSkipped(str(rel_path), "not-file"))
                continue

            try:
                size_bytes = child.stat().st_size
            except OSError:
                skipped.append(FolderSkipped(str(rel_path), "unreadable"))
                continue
            ignore_reason = should_ignore(rel_path, file_size_bytes=size_bytes)
            if ignore_reason:
                skipped.append(FolderSkipped(str(rel_path), ignore_reason))
                continue
            files.append(FolderFile(child, rel_path, size_bytes))

    walk(root, PurePosixPath("."), 0)
    return FolderScanResult(files=files, skipped=skipped)
