"""XLSX parser for universal ingestion."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from core.api.services.ingest.serializers.xlsx_to_markdown import (
    MAX_ROWS_PER_SHEET,
    serialize_sheet_to_markdown,
)

STREAMING_THRESHOLD_BYTES = 10 * 1024 * 1024
PARSER_USED = "openpyxl"


def _workbook_markdown(path: Path, sheets: list[dict[str, Any]]) -> str:
    parts = [f"# {path.name}"]
    for sheet in sheets:
        parts.extend([
            "",
            f"## Sheet: {sheet['name']}",
            "",
            sheet["markdown"],
        ])
    return "\n".join(parts).strip() + "\n"


def parse_xlsx(path: Path) -> dict[str, Any]:
    """Parse an `.xlsx` workbook into markdown text and sheet metadata.

    Security contract:
    - `.xlsm` is rejected before openpyxl touches the file.
    - Formulae are never evaluated (`data_only=True` reads cached values).
    - External workbook links are disabled (`keep_links=False`).
    - Files over 10MB use openpyxl's read-only streaming mode.
    """
    suffix = path.suffix.lower()
    if suffix == ".xlsm":
        raise ValueError("XLSM macro-enabled workbooks are not supported")
    if suffix != ".xlsx":
        raise ValueError(f"Unsupported spreadsheet extension: {suffix or '<none>'}")

    size = path.stat().st_size
    read_only = size > STREAMING_THRESHOLD_BYTES
    workbook = load_workbook(
        filename=path,
        read_only=read_only,
        data_only=True,
        keep_links=False,
    )
    try:
        sheets: list[dict[str, Any]] = []
        for index, worksheet in enumerate(workbook.worksheets, start=1):
            markdown = serialize_sheet_to_markdown(worksheet)
            row_count = worksheet.max_row or 0
            sheets.append({
                "name": worksheet.title,
                "index": index,
                "row_count": row_count,
                "column_count": worksheet.max_column,
                "markdown": markdown,
                "truncated": row_count > MAX_ROWS_PER_SHEET,
            })

        structure_sheets = [
            {key: value for key, value in sheet.items() if key != "markdown"}
            for sheet in sheets
        ]
        return {
            "frontmatter": {
                "type": "file",
                "title": path.stem,
                "tags": ["xlsx", "spreadsheet"],
            },
            "text": _workbook_markdown(path, sheets),
            "structure": {
                "kind": "xlsx",
                "bytes": size,
                "sheet_count": len(sheets),
                "sheets": structure_sheets,
                "streaming": read_only,
                "data_only": True,
                "keep_links": False,
                "max_rows_per_sheet": MAX_ROWS_PER_SHEET,
            },
            "parser_used": PARSER_USED,
        }
    finally:
        workbook.close()
