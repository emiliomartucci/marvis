"""Serialize XLSX worksheets to compact Markdown tables."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

MAX_ROWS_PER_SHEET = 50_000


def _cell_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="seconds")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    text = str(value)
    return " ".join(text.replace("|", r"\|").split())


def _trim_trailing_empty(values: tuple[Any, ...]) -> list[str]:
    cells = [_cell_to_text(value) for value in values]
    while cells and cells[-1] == "":
        cells.pop()
    return cells


def _pad(row: list[str], width: int) -> list[str]:
    if len(row) >= width:
        return row[:width]
    return [*row, *([""] * (width - len(row)))]


def _row_to_markdown(row: list[str]) -> str:
    return "| " + " | ".join(row) + " |"


def serialize_sheet_to_markdown(ws: Any) -> str:
    """Return a Markdown table for one worksheet.

    The first non-empty worksheet row becomes the table header. Empty header
    cells are named `Column N`; if the sheet has no values, a compact empty
    marker is returned.
    """
    rows: list[list[str]] = []
    truncated = False

    for index, raw_row in enumerate(ws.iter_rows(values_only=True), start=1):
        if index > MAX_ROWS_PER_SHEET:
            truncated = True
            break
        row = _trim_trailing_empty(tuple(raw_row))
        if not row and not rows:
            continue
        rows.append(row)

    if not rows:
        return "_(empty sheet)_"

    width = max(len(row) for row in rows)
    header = _pad(rows[0], width)
    header = [
        value if value else f"Column {index}"
        for index, value in enumerate(header, start=1)
    ]

    lines = [
        _row_to_markdown(header),
        _row_to_markdown(["---"] * width),
    ]
    lines.extend(_row_to_markdown(_pad(row, width)) for row in rows[1:])
    if truncated:
        lines.append("")
        lines.append(f"_Truncated after {MAX_ROWS_PER_SHEET} rows._")
    return "\n".join(lines)
