"""Validate docs MDX frontmatter used by KG coverage tracking."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.scripts._frontmatter import parse_frontmatter

AUDIENCES = frozenset({"integrator", "agent", "operator", "internal"})
VISIBILITIES = frozenset({"external", "internal", "draft"})
LAYERS = frozenset(
    {"api", "mcp", "llm-gateway", "kg", "narrative", "concept", "code-examples"}
)
NODE_LAYERS_REQUIRING_DOCUMENTS = frozenset({"api", "mcp", "kg"})


@dataclass(frozen=True)
class FrontmatterValidation:
    valid: bool
    errors: list[str] = field(default_factory=list)
    frontmatter: dict[str, Any] = field(default_factory=dict)


def validate_docs_frontmatter(path: str | Path) -> FrontmatterValidation:
    """Validate one MDX/Markdown docs page.

    `documents_nodes` is deliberately opt-in for narrative/concept layers, but
    required for API/MCP/KG pages where coverage can be machine-derived.
    """
    data, _body = parse_frontmatter(path)
    errors: list[str] = []
    if data is None:
        return FrontmatterValidation(False, ["missing_or_invalid_frontmatter"], {})

    for required in ("title", "audience", "visibility", "layer"):
        if required not in data:
            errors.append(f"missing_{required}")

    layer = str(data.get("layer") or "")
    if layer and layer not in LAYERS:
        errors.append(f"invalid_layer:{layer}")

    visibility = str(data.get("visibility") or "")
    if visibility and visibility not in VISIBILITIES:
        errors.append(f"invalid_visibility:{visibility}")

    audience = data.get("audience")
    if not isinstance(audience, list) or not audience:
        errors.append("audience_must_be_non_empty_list")
    else:
        invalid_audience = sorted(str(item) for item in audience if str(item) not in AUDIENCES)
        if invalid_audience:
            errors.append(f"invalid_audience:{','.join(invalid_audience)}")

    documents_nodes = data.get("documents_nodes")
    if documents_nodes is None:
        if layer in NODE_LAYERS_REQUIRING_DOCUMENTS:
            errors.append("missing_documents_nodes")
    elif not isinstance(documents_nodes, list):
        errors.append("documents_nodes_must_be_list")
    else:
        for node_id in documents_nodes:
            if not isinstance(node_id, str) or node_id.count(":") < 2:
                errors.append(f"invalid_documents_node:{node_id!r}")

    return FrontmatterValidation(not errors, errors, data)


def validate_docs_tree(root: str | Path) -> list[dict[str, Any]]:
    """Validate all docs content pages under a root directory."""
    root_path = Path(root)
    results: list[dict[str, Any]] = []
    for path in sorted([*root_path.rglob("*.md"), *root_path.rglob("*.mdx")]):
        validation = validate_docs_frontmatter(path)
        results.append(
            {
                "path": str(path),
                "valid": validation.valid,
                "errors": validation.errors,
            }
        )
    return results
