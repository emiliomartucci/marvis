# v1.0.0 - 2026-05-16 - KG PR-Impact sub-01 D2: tree-sitter function extraction
"""Tree-sitter helpers for the PR-impact populator.

Two responsibilities:

1. Parse a source file (Python / TS / TSX) and produce a list of
   `FunctionSpan` records — one per function-like definition. Used to
   pre-compute the function map for a file once, then look up by line.

2. Locate the enclosing function for a byte range using
   `descendant_for_byte_range` + ancestor walk. Used by the differ when
   mapping diff hunks → functions.

We deliberately keep this thin: no DB writes, no git, no LLM. Pure
parsing + range math, so the unit tests can run in milliseconds against
in-memory source strings.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.api.services.pr_impact_pipeline.languages import LanguageSpec


@dataclass(frozen=True)
class FunctionSpan:
    """One function-like definition in a parsed source file.

    Line numbers are 1-based, inclusive (matching git diff convention).
    Byte offsets are 0-based, half-open `[start_byte, end_byte)`.
    """

    qualified_name: str
    line_start: int
    line_end: int
    byte_start: int
    byte_end: int
    node_type: str
    is_async: bool = False
    has_decorator: bool = False


def parse_tree(spec: LanguageSpec, source: bytes):
    """Build a tree-sitter Tree from raw source bytes.

    Returns the Tree directly so callers can inspect `root_node.has_error`.
    """
    parser = spec.parser_factory()
    return parser.parse(source)


def extract_functions(spec: LanguageSpec, source: bytes) -> list[FunctionSpan]:
    """Walk the tree and produce a list of FunctionSpan for every fn-like node."""
    tree = parse_tree(spec, source)
    spans: list[FunctionSpan] = []
    _walk(tree.root_node, spec, [], spans)
    return spans


def _walk(
    node,
    spec: LanguageSpec,
    ancestors: list,
    out: list[FunctionSpan],
) -> None:
    if node.type in spec.fn_node_types:
        qname = spec.qualified_name_fn(node, ancestors)
        out.append(
            FunctionSpan(
                qualified_name=qname,
                line_start=node.start_point[0] + 1,
                line_end=node.end_point[0] + 1,
                byte_start=node.start_byte,
                byte_end=node.end_byte,
                node_type=node.type,
                is_async=_is_async(node),
                has_decorator=_has_decorator(node),
            )
        )
        # Continue walking the body so nested functions are captured too.
    new_ancestors = ancestors + [node]
    for child in node.children:
        _walk(child, spec, new_ancestors, out)


def _is_async(node) -> bool:
    """Best-effort async detection for both Python and TS."""
    for child in node.children:
        if child.type == "async":
            return True
    return False


def _has_decorator(node) -> bool:
    if node.type == "decorated_definition":
        return True
    parent = node.parent
    return bool(parent and parent.type == "decorated_definition")


def find_enclosing_function(
    spec: LanguageSpec,
    tree,
    byte_start: int,
    byte_end: int,
) -> FunctionSpan | None:
    """Locate the smallest function-like ancestor enclosing the byte range.

    Returns None when the range falls outside any function (top-level code,
    imports, module-level constants).
    """
    if byte_end <= byte_start:
        byte_end = byte_start + 1
    node = tree.root_node.descendant_for_byte_range(byte_start, byte_end)
    if node is None:
        return None

    # Walk up to the innermost enclosing function definition.
    fn_node = None
    cursor = node
    while cursor is not None:
        if cursor.type in spec.fn_node_types:
            fn_node = cursor
            break
        cursor = cursor.parent
    if fn_node is None:
        return None

    # Collect class/function ancestors ABOVE fn_node so qualified_name_fn
    # can fold the full dotted chain (Outer.method.nested).
    ancestor_chain: list = []
    cursor = fn_node.parent
    while cursor is not None:
        if cursor.type in spec.fn_node_types or cursor.type in (
            "class_definition",
            "class_declaration",
            "abstract_class_declaration",
        ):
            ancestor_chain.insert(0, cursor)
        cursor = cursor.parent

    return FunctionSpan(
        qualified_name=spec.qualified_name_fn(fn_node, ancestor_chain),
        line_start=fn_node.start_point[0] + 1,
        line_end=fn_node.end_point[0] + 1,
        byte_start=fn_node.start_byte,
        byte_end=fn_node.end_byte,
        node_type=fn_node.type,
        is_async=_is_async(fn_node),
        has_decorator=_has_decorator(fn_node),
    )


def line_to_byte_offset(source: bytes, line_number: int) -> int:
    """Convert a 1-based line number into a 0-based byte offset.

    Returns the offset of the first byte of `line_number` (or len(source) if
    the line is past EOF). Handles `\\n`-terminated input (Unix LF).
    """
    if line_number <= 1:
        return 0
    needed = line_number - 1
    seen = 0
    for i, byte in enumerate(source):
        if byte == 0x0A:  # \n
            seen += 1
            if seen == needed:
                return i + 1
    return len(source)


__all__ = [
    "FunctionSpan",
    "parse_tree",
    "extract_functions",
    "find_enclosing_function",
    "line_to_byte_offset",
]
