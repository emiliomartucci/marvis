"""Minimal local DOCX parser for Ingestor 2.0."""
from __future__ import annotations

import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from core.api.config import settings
from core.api.services.ingest.parsers.internal_markdown import MarkdownParseResult

DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
WORD_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def parse_docx(path: Path) -> MarkdownParseResult:
    """Extract paragraphs and tables from an Office Open XML document.

    This deliberately avoids a broad conversion dependency. It is enough for
    classification evidence and triage preview, while legacy binary `.doc`
    remains unsupported.
    """
    size = path.stat().st_size
    if size > int(settings.ingest_docx_max_bytes):
        raise ValueError(f"DOCX file too large: {size} bytes")
    if path.suffix.lower() != ".docx":
        raise ValueError(f"Unsupported Word extension: {path.suffix or '<none>'}")

    try:
        with zipfile.ZipFile(path) as archive:
            document_xml = archive.read("word/document.xml")
    except KeyError as exc:
        raise ValueError("Invalid DOCX: word/document.xml missing") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("Invalid DOCX container") from exc

    root = ElementTree.fromstring(document_xml)
    paragraphs: list[str] = []
    tables: list[list[list[str]]] = []

    for child in root.findall(".//w:body/*", WORD_NS):
        tag = _local_name(child.tag)
        if tag == "p":
            text = _paragraph_text(child)
            if text:
                paragraphs.append(text)
        elif tag == "tbl":
            table = _table_rows(child)
            if table:
                tables.append(table)

    markdown = _to_markdown(path, paragraphs, tables)
    return MarkdownParseResult(
        frontmatter=_frontmatter(path, markdown),
        text=markdown,
        structure={
            "kind": "docx",
            "bytes": size,
            "paragraph_count": len(paragraphs),
            "table_count": len(tables),
            "table_cell_count": sum(len(row) for table in tables for row in table),
        },
    )


def _paragraph_text(node: ElementTree.Element) -> str:
    return "".join(
        text_node.text or ""
        for text_node in node.findall(".//w:t", WORD_NS)
        if text_node.text
    ).strip()


def _table_rows(node: ElementTree.Element) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in node.findall(".//w:tr", WORD_NS):
        cells = [_paragraph_text(cell) for cell in row.findall("./w:tc", WORD_NS)]
        if any(cells):
            rows.append(cells)
    return rows


def _to_markdown(path: Path, paragraphs: list[str], tables: list[list[list[str]]]) -> str:
    parts = [f"# {path.stem}"]
    for paragraph in paragraphs:
        parts.extend(["", paragraph])
    for index, table in enumerate(tables, start=1):
        parts.extend(["", f"## Table {index}", "", *_table_markdown(table)])
    return "\n".join(parts).strip() + "\n"


def _table_markdown(rows: list[list[str]]) -> list[str]:
    if not rows:
        return []
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    header = normalized[0]
    body = normalized[1:] or [[""] * width]
    out = [
        "| " + " | ".join(_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
    ]
    for row in body:
        out.append("| " + " | ".join(_cell(cell) for cell in row) + " |")
    return out


def _cell(value: str) -> str:
    return (value or "").replace("|", "\\|").strip()


def _frontmatter(path: Path, markdown: str) -> dict[str, Any]:
    lowered = markdown.lower()
    tags = ["docx"]
    doc_type = "file"
    if any(term in lowered for term in {"contratto", "agreement", "clausola", "firma"}):
        doc_type = "contract"
        tags.append("contract")
    return {
        "type": doc_type,
        "title": path.stem,
        "tags": tags,
    }


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
