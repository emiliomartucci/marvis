"""Privacy-safe naming and structural classification for XLSX artifacts."""

from __future__ import annotations

import hashlib
import re
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, BinaryIO

from defusedxml import ElementTree as DefusedElementTree

from core.api.services.ingest.ignore_patterns import MAX_FILE_SIZE_BYTES
from core.api.services.ingest.parsers.zip_unpacker import (
    MAX_ZIP_FILES,
    MAX_ZIP_RATIO,
    MAX_ZIP_UNCOMPRESSED_BYTES,
)

NEUTRAL_SHA_CHARS = 12
PROPRIETARY_DETECTOR = "multi_sheet_topology_v1"
MAX_XLSX_FILES = MAX_ZIP_FILES
MAX_XLSX_RATIO = MAX_ZIP_RATIO
MAX_XLSX_MEMBER_BYTES = MAX_FILE_SIZE_BYTES
MAX_XLSX_UNCOMPRESSED_BYTES = MAX_ZIP_UNCOMPRESSED_BYTES
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class XlsxContainerError(ValueError):
    """Bounded error for invalid or unsafe XLSX ZIP containers."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _stream_size(source: BinaryIO) -> int:
    position = source.tell()
    try:
        source.seek(0, 2)
        return int(source.tell())
    finally:
        source.seek(position)


def validate_xlsx_container(
    source: BinaryIO,
    *,
    max_files: int | None = None,
    max_ratio: float | None = None,
    max_member_bytes: int | None = None,
    max_uncompressed_bytes: int | None = None,
) -> None:
    """Reject XLSX containers that exceed the shared ZIP safety bounds."""
    file_limit = MAX_XLSX_FILES if max_files is None else int(max_files)
    ratio_limit = MAX_XLSX_RATIO if max_ratio is None else float(max_ratio)
    member_limit = (
        MAX_XLSX_MEMBER_BYTES
        if max_member_bytes is None
        else int(max_member_bytes)
    )
    total_limit = (
        MAX_XLSX_UNCOMPRESSED_BYTES
        if max_uncompressed_bytes is None
        else int(max_uncompressed_bytes)
    )
    position = source.tell()
    try:
        archive_size = _stream_size(source)
        source.seek(0)
        with zipfile.ZipFile(source) as archive:
            file_infos = [info for info in archive.infolist() if not info.is_dir()]
            if len(file_infos) > file_limit:
                raise XlsxContainerError("xlsx-zip-bomb")
            if any(info.file_size > member_limit for info in file_infos):
                raise XlsxContainerError("xlsx-zip-bomb")
            total_uncompressed = sum(info.file_size for info in file_infos)
            total_compressed = sum(max(info.compress_size, 1) for info in file_infos)
            ratio_base = max(archive_size, total_compressed, 1)
            if total_uncompressed > total_limit:
                raise XlsxContainerError("xlsx-zip-bomb")
            if total_uncompressed / ratio_base > ratio_limit:
                raise XlsxContainerError("xlsx-zip-bomb")
    except zipfile.BadZipFile as exc:
        raise XlsxContainerError("invalid-xlsx") from exc
    finally:
        source.seek(position)


def xlsx_sheet_names(source: BinaryIO) -> tuple[str, ...]:
    """Return sheet labels after validating and bounding the XLSX container."""
    position = source.tell()
    try:
        validate_xlsx_container(source)
        source.seek(0)
        try:
            with zipfile.ZipFile(source) as archive:
                manifest = archive.read("xl/workbook.xml")
        except (KeyError, zipfile.BadZipFile) as exc:
            raise XlsxContainerError("invalid-xlsx") from exc
        try:
            root = DefusedElementTree.fromstring(manifest)
        except DefusedElementTree.ParseError as exc:
            raise XlsxContainerError("invalid-xlsx") from exc
        names = tuple(
            str(element.attrib.get("name") or "")
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "sheet"
        )
        if not names or any(not name for name in names):
            raise XlsxContainerError("invalid-xlsx")
        return names
    finally:
        source.seek(position)


def xlsx_sha256(path: Path) -> str:
    """Return a streaming SHA-256 without retaining workbook bytes in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_sha256(value: str) -> str:
    normalized = str(value).strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return normalized


def neutral_xlsx_filename(sha256: str) -> str:
    """Return the deterministic on-disk name used for ingested workbooks."""
    normalized = _validated_sha256(sha256)
    return f"workbook-{normalized[:NEUTRAL_SHA_CHARS]}.xlsx"


def neutral_xlsx_collision_filename(sha256: str) -> str:
    """Return the collision-safe neutral name using the complete digest."""
    normalized = _validated_sha256(sha256)
    return f"workbook-{normalized}.xlsx"


def neutral_xlsx_filenames(sha256: str) -> tuple[str, str]:
    """Return every valid neutral filename, shortest deterministic name first."""
    return (
        neutral_xlsx_filename(sha256),
        neutral_xlsx_collision_filename(sha256),
    )


def neutral_xlsx_title(sha256: str) -> str:
    normalized = _validated_sha256(sha256)
    return f"Workbook {normalized[:NEUTRAL_SHA_CHARS]}"


def neutral_xlsx_qualified_name(sha256: str) -> str:
    normalized = _validated_sha256(sha256)
    return f"xlsx.workbook.{normalized[:NEUTRAL_SHA_CHARS]}"


def neutral_xlsx_summary(sha256: str, *, sheet_count: int) -> str:
    """Return indexable XLSX text with no worksheet labels or cell content."""
    normalized = _validated_sha256(sha256)
    if int(sheet_count) < 1:
        raise ValueError("sheet_count must be positive")
    return (
        f"# Workbook {normalized[:NEUTRAL_SHA_CHARS]}\n\n"
        "Proprietary workbook content is not indexed.\n"
    )


def is_proprietary_workbook(structure: Mapping[str, Any] | None) -> bool:
    """Conservatively classify workbook topology without inspecting its name.

    A workbook with more than one worksheet is treated as proprietary because
    the sheet topology itself can encode an internal operating model. The rule
    deliberately uses no filename allowlist and no worksheet-name allowlist.
    """
    if not structure:
        return False
    if structure.get("proprietary") is True:
        return True
    raw_count = structure.get("sheet_count")
    try:
        sheet_count = int(raw_count)
    except (TypeError, ValueError):
        sheets = structure.get("sheets")
        sheet_count = len(sheets) if isinstance(sheets, list) else 0
    return sheet_count > 1


__all__ = [
    "NEUTRAL_SHA_CHARS",
    "PROPRIETARY_DETECTOR",
    "XlsxContainerError",
    "is_proprietary_workbook",
    "neutral_xlsx_collision_filename",
    "neutral_xlsx_filename",
    "neutral_xlsx_filenames",
    "neutral_xlsx_qualified_name",
    "neutral_xlsx_summary",
    "neutral_xlsx_title",
    "validate_xlsx_container",
    "xlsx_sheet_names",
    "xlsx_sha256",
]
