"""Zip container unpacking with traversal and bomb guards."""
from __future__ import annotations

import re
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from core.api.services.ingest.ignore_patterns import MAX_FILE_SIZE_BYTES, should_ignore

MAX_ZIP_FILES = 2_000
MAX_ZIP_RATIO = 100.0
MAX_ZIP_UNCOMPRESSED_BYTES = 500 * 1024 * 1024
_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


class ZipContainerError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ZipExtractedFile:
    path: Path
    relative_path: PurePosixPath
    size_bytes: int


@dataclass(frozen=True)
class ZipSkipped:
    path: str
    reason: str


@dataclass(frozen=True)
class ZipExtractResult:
    files: list[ZipExtractedFile]
    skipped: list[ZipSkipped]


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _safe_relative_name(raw_name: str) -> PurePosixPath:
    normalized = raw_name.replace("\\", "/")
    if normalized.startswith("/") or _WINDOWS_DRIVE_RE.match(normalized):
        raise ZipContainerError("zip-slip")
    rel_path = PurePosixPath(normalized)
    parts = [part for part in rel_path.parts if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise ZipContainerError("zip-slip")
    return PurePosixPath(*parts)


def safe_extract_zip(
    zip_path: Path,
    extract_dir: Path,
    *,
    max_files: int = MAX_ZIP_FILES,
    max_ratio: float = MAX_ZIP_RATIO,
    max_file_size_bytes: int = MAX_FILE_SIZE_BYTES,
    max_uncompressed_bytes: int = MAX_ZIP_UNCOMPRESSED_BYTES,
) -> ZipExtractResult:
    extract_dir = extract_dir.resolve()
    extract_dir.mkdir(parents=True, exist_ok=True)
    skipped: list[ZipSkipped] = []

    try:
        archive_size = zip_path.stat().st_size
        with zipfile.ZipFile(zip_path) as archive:
            infos = archive.infolist()
            file_infos = [info for info in infos if not info.is_dir()]
            if len(file_infos) > max_files:
                raise ZipContainerError("zip-bomb")

            total_uncompressed = sum(info.file_size for info in file_infos)
            total_compressed = sum(max(info.compress_size, 1) for info in file_infos)
            ratio_base = max(archive_size, total_compressed, 1)
            if total_uncompressed > max_uncompressed_bytes:
                raise ZipContainerError("zip-bomb")
            if total_uncompressed / ratio_base > max_ratio:
                raise ZipContainerError("zip-bomb")

            selected: list[tuple[zipfile.ZipInfo, PurePosixPath, Path]] = []
            selected_targets: set[Path] = set()
            for info in file_infos:
                rel_path = _safe_relative_name(info.filename)
                if _is_symlink(info):
                    skipped.append(ZipSkipped(str(rel_path), "symlink"))
                    continue
                ignore_reason = should_ignore(
                    rel_path,
                    file_size_bytes=info.file_size,
                )
                if ignore_reason:
                    skipped.append(ZipSkipped(str(rel_path), ignore_reason))
                    continue
                target = (extract_dir / Path(*rel_path.parts)).resolve()
                if not target.is_relative_to(extract_dir):
                    raise ZipContainerError("zip-slip")
                if target in selected_targets:
                    skipped.append(ZipSkipped(str(rel_path), "duplicate-path"))
                    continue
                selected_targets.add(target)
                selected.append((info, rel_path, target))

            files: list[ZipExtractedFile] = []
            for info, rel_path, target in selected:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst, length=1024 * 1024)
                size_bytes = target.stat().st_size
                if size_bytes > max_file_size_bytes:
                    target.unlink(missing_ok=True)
                    skipped.append(ZipSkipped(str(rel_path), "file-too-large"))
                    continue
                files.append(ZipExtractedFile(target, rel_path, size_bytes))
    except zipfile.BadZipFile as exc:
        raise ZipContainerError("invalid-zip") from exc

    return ZipExtractResult(files=files, skipped=skipped)
