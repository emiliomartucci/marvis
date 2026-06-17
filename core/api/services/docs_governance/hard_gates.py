"""Deterministic docs governance hard gates.

These gates are opt-in per layer through `docs/.governance.yml`. Missing tools
or unknown gate names fail the gate explicitly instead of silently approving.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.api.services.docs_governance.config import layer_config, load_governance_config
from core.api.services.docs_governance.frontmatter_validator import validate_docs_frontmatter


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class GateReport:
    passed: bool
    results: list[GateResult] = field(default_factory=list)

    @property
    def failed(self) -> list[GateResult]:
        return [result for result in self.results if not result.passed]


_INTERNAL_URL_PATTERN = r"https?://(?:100\.\d+\.\d+\.\d+|localhost|127\.0\.0\.1)(?::\d+)?"
_PERSONAL_DOMAIN_PATTERN = os.environ.get(
    "DOCS_GATE_PERSONAL_DOMAIN_REGEX", r"\bllm\.example\.invalid\b"
)
_INTERNAL_PATH_PATTERN = r"(?:/data/projects/|/home/[^/]+/|/data/pir/)"
_INTERNAL_CODENAME_PATTERN = (
    r"\b(marvis-brain|marvisx-core|wedge\s+W\d+|criterion\s+§\d+|R\d{2,})\b"
)
_AGENT_TRANSPARENCY_PATTERN = r"\b(subagent|\d+/\d+\s+agent|multi-agent\s+transparency)\b"
_BARE_TAILSCALE_PATTERN = r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+\b"

SECURITY_LEAK_PATTERN_SOURCES: tuple[tuple[str, str, int], ...] = (
    ("internal_url", _INTERNAL_URL_PATTERN, 0),
    ("personal_gateway_domain", _PERSONAL_DOMAIN_PATTERN, re.IGNORECASE),
    ("internal_path", _INTERNAL_PATH_PATTERN, 0),
    ("internal_codename", _INTERNAL_CODENAME_PATTERN, re.IGNORECASE),
    ("agent_transparency", _AGENT_TRANSPARENCY_PATTERN, re.IGNORECASE),
    ("bare_tailscale_ip", _BARE_TAILSCALE_PATTERN, 0),
)
SECURITY_LEAK_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, flags)) for name, pattern, flags in SECURITY_LEAK_PATTERN_SOURCES
)
SECURITY_LEAK_GREP_PATTERN = "|".join(
    f"(?:{pattern})" for _name, pattern, _flags in SECURITY_LEAK_PATTERN_SOURCES
)


def check_frontmatter_valid(path: str | Path) -> GateResult:
    validation = validate_docs_frontmatter(path)
    return GateResult(
        "frontmatter_valid",
        validation.valid,
        ",".join(validation.errors),
    )


def check_no_security_leaks(text: str) -> GateResult:
    """Block internal endpoints, paths, codenames, and implementation traces."""
    matches = [
        name
        for name, pattern in SECURITY_LEAK_PATTERNS
        if pattern.search(text)
    ]
    return GateResult("no_security_leaks", not matches, ",".join(matches))


def check_schema_valid(payload: Mapping[str, Any]) -> GateResult:
    """Validate minimal schema-ish payloads from generated references."""
    ok = bool(payload) and not payload.get("errors")
    return GateResult("schema_valid", ok, str(payload.get("errors") or ""))


def check_no_breaking_removal(change_type: str) -> GateResult:
    return GateResult(
        "no_breaking_removal",
        change_type not in {"breaking_removal", "signature_change", "tool_removed"},
        change_type,
    )


def check_doc_detective_green(result: Mapping[str, Any]) -> GateResult:
    status = str(result.get("status") or "").upper()
    return GateResult("doc_detective_green", status == "PASS", status or "missing")


def check_vale_pass(result: Mapping[str, Any]) -> GateResult:
    errors = int(result.get("errors") or 0)
    return GateResult("vale_pass", errors == 0, f"errors={errors}")


def check_lychee_pass(result: Mapping[str, Any]) -> GateResult:
    failures = int(result.get("failures") or result.get("failed") or 0)
    return GateResult("lychee_pass", failures == 0, f"failures={failures}")


def check_kg_xref_valid(xrefs: Iterable[str], known_nodes: set[str]) -> GateResult:
    missing = sorted(str(node_id) for node_id in xrefs if str(node_id) not in known_nodes)
    return GateResult("kg_xref_valid", not missing, ",".join(missing[:10]))


def check_adr_consistency(result: Mapping[str, Any]) -> GateResult:
    conflicts = result.get("conflicts") or []
    return GateResult("adr_consistency", not conflicts, json.dumps(conflicts[:5]))


def check_coverage_no_regression(
    *,
    removed_nodes: Iterable[str],
    replacement_nodes: Iterable[str],
    baseline_documented_nodes: set[str],
) -> GateResult:
    """Fail if a PR removes the only documented coverage for a baseline node."""
    replacements = set(replacement_nodes)
    regressions = sorted(
        str(node_id)
        for node_id in removed_nodes
        if str(node_id) in baseline_documented_nodes and str(node_id) not in replacements
    )
    return GateResult(
        "documented_coverage_no_regression",
        not regressions,
        ",".join(regressions[:10]),
    )


def check_no_undocumented_new_function(
    *,
    new_function_qualified_names: set[str],
    documented_qualified_names: set[str],
) -> GateResult:
    """Fail if a newly added public function has no docs frontmatter claim."""
    missing = sorted(new_function_qualified_names - documented_qualified_names)
    return GateResult(
        "no_undocumented_new_function",
        not missing,
        ",".join(missing[:10]),
    )


def run_configured_hard_gates(
    *,
    layer: str,
    context: Mapping[str, Any],
    config_path: str | Path | None = None,
) -> GateReport:
    """Run only gates configured for a layer.

    Context keys intentionally mirror CI output names so callers can wire this
    without creating a synthetic object graph.
    """
    config = load_governance_config(config_path)
    gates = layer_config(layer, config).get("hard_gates") or []
    results: list[GateResult] = []
    text = str(context.get("text") or "")

    for gate in gates:
        if gate == "frontmatter_valid":
            path = context.get("path")
            if path:
                results.append(check_frontmatter_valid(path))
            else:
                results.append(GateResult("frontmatter_valid", False, "missing_path"))
        elif gate == "no_security_leaks":
            results.append(check_no_security_leaks(text))
        elif gate == "schema_valid":
            results.append(check_schema_valid(context.get("schema") or {}))
        elif gate == "no_breaking_removal":
            results.append(check_no_breaking_removal(str(context.get("change_type") or "")))
        elif gate == "doc_detective_green":
            results.append(check_doc_detective_green(context.get("doc_detective") or {}))
        elif gate == "vale_pass":
            results.append(check_vale_pass(context.get("vale") or {}))
        elif gate == "lychee_pass":
            results.append(check_lychee_pass(context.get("lychee") or {}))
        elif gate == "kg_xref_valid":
            results.append(
                check_kg_xref_valid(
                    context.get("xrefs") or [],
                    set(context.get("known_nodes") or []),
                )
            )
        elif gate == "adr_consistency":
            results.append(check_adr_consistency(context.get("adr") or {}))
        elif gate == "documented_coverage_no_regression":
            results.append(
                check_coverage_no_regression(
                    removed_nodes=context.get("removed_nodes") or [],
                    replacement_nodes=context.get("replacement_nodes") or [],
                    baseline_documented_nodes=set(context.get("baseline_documented_nodes") or []),
                )
            )
        elif gate == "no_undocumented_new_function":
            results.append(
                check_no_undocumented_new_function(
                    new_function_qualified_names=set(
                        context.get("new_function_qualified_names") or []
                    ),
                    documented_qualified_names=set(
                        context.get("documented_qualified_names") or []
                    ),
                )
            )
        else:
            results.append(GateResult(str(gate), False, "unknown_gate"))

    return GateReport(all(result.passed for result in results), results)
