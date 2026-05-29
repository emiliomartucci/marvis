# v1.0.0 - 2026-05-16 - KG PR-Impact sub-01 D2: tree-sitter language registry
"""Language registry for the PR-impact populator.

Maps file extensions to:
- tree-sitter parser factory
- frozenset of node types that represent a "function-like" definition
- node prefix (`py` vs `ts`) for stable id synthesis
- qualified-name builder (ancestor chain → dotted name)

Adding a new language is a single dict entry — tests `test_pr_impact_languages.py`
exercises the contract.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final, Literal

try:
    import tree_sitter_python as tspython
    import tree_sitter_typescript as tstypescript
    from tree_sitter import Language, Node, Parser
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "PR-impact populator requires tree-sitter. Install: "
        "pip install 'tree-sitter>=0.23.0,<0.26' "
        "'tree-sitter-python>=0.23.0,<0.25' 'tree-sitter-typescript~=0.23.2'"
    ) from exc


PYTHON_FN_TYPES: Final[frozenset[str]] = frozenset(
    {"function_definition", "decorated_definition"}
)
"""async is a child keyword of function_definition, NOT a separate type."""

TS_FN_TYPES: Final[frozenset[str]] = frozenset(
    {
        "function_declaration",
        "arrow_function",
        "method_definition",
        "generator_function_declaration",
        "function_expression",
    }
)

TSX_FN_TYPES: Final[frozenset[str]] = TS_FN_TYPES
"""TSX inherits TS function types; JSX components are arrow_function with
PascalCase name — distinguishing is a v1.1 concern."""


def _python_parser() -> "Parser":
    parser = Parser()
    parser.language = Language(tspython.language())
    return parser


def _typescript_parser() -> "Parser":
    parser = Parser()
    parser.language = Language(tstypescript.language_typescript())
    return parser


def _tsx_parser() -> "Parser":
    parser = Parser()
    parser.language = Language(tstypescript.language_tsx())
    return parser


def _python_qualified_name(node: "Node", ancestors: list["Node"]) -> str:
    """Build `Class.outer_fn.inner_fn` from a function node + its ancestor chain.

    `ancestors` is ordered root-to-target so the outermost class / function
    definitions come first. Both class AND enclosing function names get
    folded into the qualified name so nested closures stay attributable.
    """
    parts: list[str] = []
    for ancestor in ancestors:
        if ancestor.type == "class_definition":
            name = _python_name(ancestor)
            if name:
                parts.append(name)
        elif ancestor.type in ("function_definition", "decorated_definition"):
            name = _python_name(ancestor)
            if name:
                parts.append(name)
    own_name = _python_name(node)
    if own_name:
        parts.append(own_name)
    return ".".join(parts) if parts else "<anonymous>"


def _python_name(node: "Node") -> str | None:
    """Extract the `name` identifier of a Python definition node."""
    if node.type == "decorated_definition":
        for child in node.children:
            if child.type == "function_definition":
                return _python_name(child)
        return None
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node.text.decode("utf-8", errors="replace")
    return None


def _typescript_qualified_name(node: "Node", ancestors: list["Node"]) -> str:
    """Build `Class.method` / `outerFn.inner` chain for TS/TSX.

    Includes both class AND enclosing function names from the ancestor
    chain so nested closures remain attributable.
    """
    parts: list[str] = []
    for ancestor in ancestors:
        if ancestor.type in ("class_declaration", "abstract_class_declaration"):
            name = _ts_name(ancestor)
            if name:
                parts.append(name)
        elif ancestor.type in TS_FN_TYPES:
            name = _ts_name(ancestor)
            if name is None and ancestor.parent is not None:
                if ancestor.parent.type == "variable_declarator":
                    name = _ts_name(ancestor.parent)
            if name:
                parts.append(name)
    own_name = _ts_name(node)
    if own_name is None:
        # Anonymous arrow / function_expression — try the surrounding
        # variable_declarator name so we don't lose attribution entirely.
        parent = node.parent
        if parent is not None and parent.type == "variable_declarator":
            own_name = _ts_name(parent)
    if own_name:
        parts.append(own_name)
    return ".".join(parts) if parts else "<anonymous>"


def _ts_name(node: "Node") -> str | None:
    name_node = node.child_by_field_name("name")
    if name_node is not None:
        return name_node.text.decode("utf-8", errors="replace")
    return None


@dataclass(frozen=True)
class LanguageSpec:
    extension: str
    parser_factory: Callable[[], "Parser"]
    fn_node_types: frozenset[str]
    prefix: Literal["py", "ts"]
    qualified_name_fn: Callable[["Node", list["Node"]], str]


LANGUAGES: Final[dict[str, LanguageSpec]] = {
    ".py": LanguageSpec(
        extension=".py",
        parser_factory=_python_parser,
        fn_node_types=PYTHON_FN_TYPES,
        prefix="py",
        qualified_name_fn=_python_qualified_name,
    ),
    ".ts": LanguageSpec(
        extension=".ts",
        parser_factory=_typescript_parser,
        fn_node_types=TS_FN_TYPES,
        prefix="ts",
        qualified_name_fn=_typescript_qualified_name,
    ),
    ".tsx": LanguageSpec(
        extension=".tsx",
        parser_factory=_tsx_parser,
        fn_node_types=TSX_FN_TYPES,
        prefix="ts",
        qualified_name_fn=_typescript_qualified_name,
    ),
}


def language_for_path(path: str) -> LanguageSpec | None:
    """Return the LanguageSpec matching `path`'s extension, or None if unsupported."""
    for ext, spec in LANGUAGES.items():
        if path.endswith(ext):
            return spec
    return None


__all__ = [
    "LanguageSpec",
    "LANGUAGES",
    "PYTHON_FN_TYPES",
    "TS_FN_TYPES",
    "TSX_FN_TYPES",
    "language_for_path",
]
