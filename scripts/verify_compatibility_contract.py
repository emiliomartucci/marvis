#!/usr/bin/env python3
"""Fail-closed verification of the OSS N/N-1 compatibility contract."""
from __future__ import annotations

import argparse
import asyncio
import ast
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from build_compatibility_fixtures import compact_spec


FIXTURE_SCHEMA = "marvis-compatibility-fixture/v1"
MANIFEST_SCHEMA = "marvis-compatibility-fixture-manifest/v1"
MATRIX_SCHEMA = "marvis-consumer-compatibility-matrix/v1"
MCP_INVENTORY_SCHEMA = "marvis-mcp-tool-inventory/v1"
TRUST_SCHEMA = "marvis-local-server-trust-matrix/v1"
HEX_64 = re.compile(r"^[0-9a-f]{64}$")
SHA_40 = re.compile(r"^[0-9a-f]{40}$")
HTTP_METHODS = frozenset({"delete", "get", "head", "options", "patch", "post", "put"})
EXPECTED_SURFACES = {
    "cli",
    "gui",
    "local_mcp",
    "http",
    "hooks",
    "package_install",
    "persisted_local_data",
}
EXPECTED_TRUST_CONTEXTS = {
    "local_cli_os_account",
    "local_mcp_agent",
    "same_shell_agent_as_human",
    "server_authenticated_human",
    "server_agent",
    "server_local_ctx_reuse",
    "server_legacy_human_flag",
}
ALLOWED_RESULTS = {"pass", "deny", "migration_required", "not_applicable"}


class CompatibilityError(RuntimeError):
    """The committed compatibility evidence is incomplete or inconsistent."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompatibilityError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise CompatibilityError(f"JSON root must be an object: {path}")
    return value


def _operations(fixture: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    raw = fixture.get("operations")
    if not isinstance(raw, dict):
        raise CompatibilityError("fixture operations must be an object")
    for path, methods in raw.items():
        if not isinstance(path, str) or not path.startswith("/") or not isinstance(methods, dict):
            raise CompatibilityError("invalid fixture operation path")
        for method, operation in methods.items():
            if method not in HTTP_METHODS or not isinstance(operation, dict):
                raise CompatibilityError(f"invalid operation: {method} {path}")
            result[(path, method)] = operation
    return result


def _validate_fixture(name: str, fixture: dict[str, Any]) -> None:
    if fixture.get("schema") != FIXTURE_SCHEMA:
        raise CompatibilityError(f"{name}: unsupported fixture schema")
    if not SHA_40.fullmatch(str(fixture.get("source_ref", ""))):
        raise CompatibilityError(f"{name}: source_ref must be an exact commit SHA")
    if not HEX_64.fullmatch(str(fixture.get("source_openapi_sha256", ""))):
        raise CompatibilityError(f"{name}: invalid source OpenAPI digest")
    operations = _operations(fixture)
    if fixture.get("path_count") != len({path for path, _method in operations}):
        raise CompatibilityError(f"{name}: path_count drift")
    if fixture.get("operation_count") != len(operations):
        raise CompatibilityError(f"{name}: operation_count drift")
    digests = fixture.get("component_schema_sha256")
    shapes = fixture.get("component_schema_compatibility")
    if not isinstance(digests, dict) or not isinstance(shapes, dict):
        raise CompatibilityError(f"{name}: schema compatibility evidence missing")
    if set(digests) != set(shapes) or fixture.get("component_schema_count") != len(digests):
        raise CompatibilityError(f"{name}: component schema inventory drift")
    if any(not HEX_64.fullmatch(str(value)) for value in digests.values()):
        raise CompatibilityError(f"{name}: invalid component schema digest")
    for operation in operations.values():
        refs = operation.get("schema_refs")
        request_refs = operation.get("request_schema_refs")
        response_refs = operation.get("response_schema_refs")
        if (
            not isinstance(refs, list)
            or refs != sorted(set(refs))
            or not all(isinstance(ref, str) and ref for ref in refs)
            or not isinstance(request_refs, list)
            or request_refs != sorted(set(request_refs))
            or not isinstance(response_refs, dict)
            or set(response_refs) != set(operation.get("response_codes", []))
        ):
            raise CompatibilityError(f"{name}: operation schema closure invalid")
        flattened = set(request_refs)
        for code_refs in response_refs.values():
            if (
                not isinstance(code_refs, list)
                or code_refs != sorted(set(code_refs))
                or not all(isinstance(ref, str) and ref for ref in code_refs)
            ):
                raise CompatibilityError(f"{name}: response schema closure invalid")
            flattened.update(code_refs)
        if refs != sorted(flattened):
            raise CompatibilityError(f"{name}: operation schema closure union drift")
        missing = set(refs) - set(shapes)
        if missing:
            raise CompatibilityError(
                f"{name}: operation references missing schemas: {sorted(missing)}"
            )


def _validate_mcp_inventory(name: str, inventory: dict[str, Any]) -> set[str]:
    if inventory.get("schema") != MCP_INVENTORY_SCHEMA:
        raise CompatibilityError(f"{name}: unsupported MCP inventory schema")
    if not SHA_40.fullmatch(str(inventory.get("source_ref", ""))):
        raise CompatibilityError(f"{name}: source_ref must be an exact commit SHA")
    tools = inventory.get("tools")
    if (
        not isinstance(tools, list)
        or not tools
        or not all(isinstance(tool, str) and tool for tool in tools)
        or tools != sorted(set(tools))
        or inventory.get("tool_count") != len(tools)
    ):
        raise CompatibilityError(f"{name}: MCP tool inventory drift")
    return set(tools)


def _runtime_mcp_tools() -> set[str]:
    try:
        from core.api.mcp.server import mcp

        async def collect() -> set[str]:
            return {tool.name for tool in await mcp.list_tools()}

        return asyncio.run(collect())
    except Exception as exc:  # noqa: BLE001 - compatibility gate fails closed
        raise CompatibilityError("current MCP tool inventory cannot be reconstructed") from exc


def _operation_breaks(
    expected: dict[str, Any], actual: dict[str, Any], *, consumer_view: bool = False
) -> list[str]:
    failures: list[str] = []
    if actual.get("operation_id") != expected.get("operation_id"):
        failures.append("operation_id_changed")
    old_required = set(expected.get("required_parameters", []))
    new_required = set(actual.get("required_parameters", []))
    if not new_required.issubset(old_required):
        failures.append("required_parameter_added")
    if actual.get("request_body_required") and not expected.get("request_body_required"):
        failures.append("request_body_became_required")
    old_success = {code for code in expected.get("response_codes", []) if str(code).startswith("2")}
    new_success = {code for code in actual.get("response_codes", []) if str(code).startswith("2")}
    if not (old_success & new_success):
        failures.append("no_shared_success_response")
    if not consumer_view and not set(expected.get("response_codes", [])).issubset(
        set(actual.get("response_codes", []))
    ):
        failures.append("response_code_removed")
    if not set(expected.get("request_schema_refs", [])).issubset(
        set(actual.get("request_schema_refs", []))
    ):
        failures.append("request_schema_reference_removed")
    actual_response_refs = actual.get("response_schema_refs", {})
    for code in sorted(old_success & new_success):
        if not set(expected.get("response_schema_refs", {}).get(code, [])).issubset(
            set(actual_response_refs.get(code, []))
        ):
            failures.append(f"response_schema_reference_removed:{code}")
    return failures


def _schema_breaks(
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    names: set[str] | None = None,
    direction: str = "provider_upgrade",
) -> list[str]:
    failures: list[str] = []
    old_shapes = expected["component_schema_compatibility"]
    new_shapes = actual["component_schema_compatibility"]
    selected = set(old_shapes) if names is None else names
    for name in sorted(selected):
        old = old_shapes.get(name)
        if old is None:
            failures.append(f"schema_evidence_missing:{name}")
            continue
        new = new_shapes.get(name)
        if new is None:
            failures.append(f"schema_removed:{name}")
            continue
        if old.get("kind") != new.get("kind"):
            failures.append(f"schema_kind_changed:{name}")
            continue
        if old.get("kind") == "opaque":
            if old.get("sha256") != new.get("sha256"):
                failures.append(f"schema_changed:{name}")
            continue
        if old.get("envelope_sha256") != new.get("envelope_sha256"):
            failures.append(f"schema_envelope_changed:{name}")
        old_properties = old.get("property_sha256", {})
        new_properties = new.get("property_sha256", {})
        old_required = set(old.get("required", []))
        new_required = set(new.get("required", []))
        if direction == "consumer_response":
            if not old_required.issubset(new_required):
                failures.append(f"schema_required_response_missing:{name}")
            compared_properties = set(old_properties) & set(new_properties)
        else:
            if not new_required.issubset(old_required):
                failures.append(f"schema_required_added:{name}")
            compared_properties = set(old_properties)
        for prop in sorted(compared_properties):
            if prop not in new_properties:
                failures.append(f"schema_property_missing:{name}.{prop}")
            elif new_properties[prop] != old_properties[prop]:
                failures.append(f"schema_property_changed:{name}.{prop}")
    return failures


def provider_breaks(
    expected: dict[str, Any], actual: dict[str, Any], *, consumer_view: bool = False
) -> list[str]:
    """Return breaks when ``actual`` must satisfy ``expected`` consumers."""

    failures: list[str] = []
    actual_ops = _operations(actual)
    for key, expected_operation in _operations(expected).items():
        actual_operation = actual_ops.get(key)
        label = f"{key[1].upper()} {key[0]}"
        if actual_operation is None:
            failures.append(f"operation_removed:{label}")
            continue
        failures.extend(
            f"{kind}:{label}"
            for kind in _operation_breaks(
                expected_operation, actual_operation, consumer_view=consumer_view
            )
        )
    schema_names = None
    if consumer_view:
        expected_ops = _operations(expected)
        request_names = {
            ref
            for operation in expected_ops.values()
            for ref in operation.get("request_schema_refs", [])
        }
        response_names: set[str] = set()
        for key, operation in expected_ops.items():
            actual_operation = actual_ops.get(key)
            if actual_operation is None:
                continue
            shared_success = {
                code
                for code in operation.get("response_codes", [])
                if str(code).startswith("2")
            } & {
                code
                for code in actual_operation.get("response_codes", [])
                if str(code).startswith("2")
            }
            for code in shared_success:
                response_names.update(
                    operation.get("response_schema_refs", {}).get(code, [])
                )
        failures.extend(
            _schema_breaks(
                expected,
                actual,
                names=request_names,
                direction="consumer_request",
            )
        )
        failures.extend(
            _schema_breaks(
                expected,
                actual,
                names=response_names,
                direction="consumer_response",
            )
        )
    else:
        failures.extend(_schema_breaks(expected, actual, names=schema_names))
    return list(dict.fromkeys(failures))


def _python_symbols(path: Path) -> set[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError) as exc:
        raise CompatibilityError(f"cannot inspect evidence: {path}") from exc
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }


def _validate_evidence(root: Path, reference: str) -> None:
    path_text = reference
    symbol: str | None = None
    if "::" in reference:
        path_text, symbol = reference.split("::", 1)
    elif reference.count(":") == 1:
        candidate, maybe_symbol = reference.split(":", 1)
        if candidate.endswith(".py"):
            path_text, symbol = candidate, maybe_symbol
    path = root / path_text
    if not path.exists():
        raise CompatibilityError(f"evidence path missing: {reference}")
    if symbol is not None:
        leaf = symbol.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
        if path.suffix != ".py" or leaf not in _python_symbols(path):
            raise CompatibilityError(f"evidence symbol missing: {reference}")


def _schema_property_values(
    spec: dict[str, Any], schema_name: str, property_name: str
) -> set[str]:
    try:
        value = spec["components"]["schemas"][schema_name]["properties"][property_name]
    except (KeyError, TypeError) as exc:
        raise CompatibilityError(
            f"response sanitizer schema property missing: {schema_name}.{property_name}"
        ) from exc

    collected: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for key in ("enum", "x-extensible-enum"):
                raw_values = node.get(key)
                if isinstance(raw_values, list):
                    collected.update(item for item in raw_values if isinstance(item, str))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    return collected


def _validate_provider_response_sanitizers(
    root: Path,
    matrix: dict[str, Any],
    *,
    provider_breaks_found: set[str],
    current_openapi: dict[str, Any],
    previous_openapi: dict[str, Any],
) -> set[str]:
    declarations = matrix.get("n_minus_1_provider_response_sanitizers")
    if not isinstance(declarations, list):
        raise CompatibilityError("N-1 provider response sanitizer inventory missing")

    rows = matrix.get("rows")
    if not isinstance(rows, list):
        raise CompatibilityError("consumer matrix rows missing")
    by_surface = {row.get("surface"): row for row in rows if isinstance(row, dict)}
    required_keys = {
        "failure",
        "schema",
        "property",
        "internal_value",
        "wire_value",
        "surfaces",
        "evidence",
    }
    declared: set[str] = set()

    for item in declarations:
        if not isinstance(item, dict) or set(item) != required_keys:
            raise CompatibilityError("invalid N-1 provider response sanitizer row")
        schema_name = item["schema"]
        property_name = item["property"]
        internal_value = item["internal_value"]
        wire_value = item["wire_value"]
        failure = item["failure"]
        if not all(
            isinstance(value, str) and value
            for value in (
                schema_name,
                property_name,
                internal_value,
                wire_value,
                failure,
            )
        ):
            raise CompatibilityError("response sanitizer values must be non-empty strings")
        expected_failure = f"schema_property_changed:{schema_name}.{property_name}"
        if failure != expected_failure or failure in declared:
            raise CompatibilityError("invalid or duplicate response sanitizer failure")
        if failure not in provider_breaks_found:
            raise CompatibilityError(f"response sanitizer declaration is stale: {failure}")
        if internal_value == wire_value:
            raise CompatibilityError("response sanitizer must change the internal value")

        surfaces = item["surfaces"]
        if (
            not isinstance(surfaces, list)
            or surfaces != sorted(set(surfaces))
            or not surfaces
            or not set(surfaces).issubset(EXPECTED_SURFACES)
        ):
            raise CompatibilityError(f"response sanitizer has invalid surfaces: {failure}")
        for surface in surfaces:
            row = by_surface.get(surface)
            if not isinstance(row, dict) or row.get("n_minus_1_consumer_n_contract") != "pass":
                raise CompatibilityError(
                    f"{surface}: response sanitizer cannot certify a non-passing N-1 row"
                )

        evidence = item["evidence"]
        if (
            not isinstance(evidence, list)
            or evidence != sorted(set(evidence))
            or len(evidence) < 2
        ):
            raise CompatibilityError(f"response sanitizer evidence invalid: {failure}")
        for reference in evidence:
            _validate_evidence(root, str(reference))

        previous_values = _schema_property_values(
            previous_openapi, schema_name, property_name
        )
        current_values = _schema_property_values(
            current_openapi, schema_name, property_name
        )
        if (
            wire_value not in previous_values
            or internal_value in previous_values
            or internal_value not in current_values
        ):
            raise CompatibilityError(
                f"response sanitizer mapping is not bounded by N/N-1 schemas: {failure}"
            )

        if (schema_name, property_name) != ("SearchResponse", "semantic_reason"):
            raise CompatibilityError(f"unsupported runtime response sanitizer: {failure}")
        try:
            from core.api.models.search import SearchResponse

            normalized = SearchResponse(semantic_reason=internal_value).model_dump()
            preserved = SearchResponse(semantic_reason=wire_value).model_dump()
            if (
                normalized.get(property_name) != wire_value
                or preserved.get(property_name) != wire_value
            ):
                raise CompatibilityError(
                    f"runtime response sanitizer mapping failed: {failure}"
                )
            try:
                SearchResponse(semantic_reason="compatibility-gate-unsanitized")
            except Exception:  # noqa: BLE001 - rejection is the required proof
                pass
            else:
                raise CompatibilityError(
                    f"runtime response sanitizer accepts unknown values: {failure}"
                )
        except CompatibilityError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed on runtime proof
            raise CompatibilityError(
                f"runtime response sanitizer proof failed: {failure}"
            ) from exc

        declared.add(failure)

    if declared != provider_breaks_found:
        missing = sorted(provider_breaks_found - declared)
        stale = sorted(declared - provider_breaks_found)
        raise CompatibilityError(
            f"N-1 response sanitizer inventory drift: missing={missing}, stale={stale}"
        )
    return declared


def _validate_matrix(
    root: Path,
    matrix: dict[str, Any],
    *,
    n_consumer: dict[str, Any],
    previous: dict[str, Any],
    consumer_previous_breaks: set[str],
    current_mcp_tools: set[str],
    previous_mcp_tools: set[str],
) -> None:
    if matrix.get("schema") != MATRIX_SCHEMA or matrix.get("contract_window") != "N/N-1":
        raise CompatibilityError("consumer matrix header invalid")
    directions = matrix.get("required_directions")
    if not isinstance(directions, list) or len(directions) != len(set(directions)):
        raise CompatibilityError("consumer matrix directions invalid")
    rows = matrix.get("rows")
    if not isinstance(rows, list):
        raise CompatibilityError("consumer matrix rows missing")
    by_surface = {row.get("surface"): row for row in rows if isinstance(row, dict)}
    if set(by_surface) != EXPECTED_SURFACES or len(rows) != len(by_surface):
        raise CompatibilityError("consumer matrix surface inventory drift")
    for surface, row in by_surface.items():
        for direction in directions:
            if row.get(direction) not in ALLOWED_RESULTS:
                raise CompatibilityError(f"{surface}: invalid {direction} result")
        evidence = row.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise CompatibilityError(f"{surface}: evidence missing")
        for reference in evidence:
            _validate_evidence(root, str(reference))

    new_only = set(_operations(n_consumer)) - set(_operations(previous))
    declarations = matrix.get("n_only_operations")
    if not isinstance(declarations, list):
        raise CompatibilityError("n_only_operations inventory missing")
    declared: set[tuple[str, str]] = set()
    for item in declarations:
        if not isinstance(item, dict):
            raise CompatibilityError("invalid n_only_operations row")
        key = (str(item.get("path", "")), str(item.get("method", "")).lower())
        if key in declared or key not in new_only:
            raise CompatibilityError(f"invalid or duplicate N-only operation: {key}")
        declared.add(key)
        surfaces = item.get("surfaces")
        if not isinstance(surfaces, list) or not surfaces or not set(surfaces).issubset(EXPECTED_SURFACES):
            raise CompatibilityError(f"N-only operation has invalid surfaces: {key}")
        for surface in surfaces:
            if by_surface[surface].get("n_consumer_n_minus_1_contract") != "migration_required":
                raise CompatibilityError(f"{surface}: N-only operation lacks traced migration")
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise CompatibilityError(f"N-only operation lacks evidence: {key}")
        for reference in evidence:
            _validate_evidence(root, str(reference))
    if declared != new_only:
        missing = sorted(new_only - declared)
        raise CompatibilityError(f"unclassified N-only operations: {missing}")

    mcp_declarations = matrix.get("n_only_mcp_tools")
    if not isinstance(mcp_declarations, list):
        raise CompatibilityError("n_only_mcp_tools inventory missing")
    added_mcp_tools = current_mcp_tools - previous_mcp_tools
    removed_mcp_tools = previous_mcp_tools - current_mcp_tools
    declared_mcp_tools: set[str] = set()
    for item in mcp_declarations:
        if not isinstance(item, dict):
            raise CompatibilityError("invalid n_only_mcp_tools row")
        name = item.get("name")
        if not isinstance(name, str) or name in declared_mcp_tools or name not in added_mcp_tools:
            raise CompatibilityError(f"invalid or duplicate N-only MCP tool: {name}")
        declared_mcp_tools.add(name)
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise CompatibilityError(f"N-only MCP tool lacks evidence: {name}")
        for reference in evidence:
            _validate_evidence(root, str(reference))
    if declared_mcp_tools != added_mcp_tools:
        missing = sorted(added_mcp_tools - declared_mcp_tools)
        stale = sorted(declared_mcp_tools - added_mcp_tools)
        raise CompatibilityError(
            f"N-only MCP tool inventory drift: missing={missing}, stale={stale}"
        )
    local_mcp = by_surface["local_mcp"]
    if added_mcp_tools and local_mcp.get("n_consumer_n_minus_1_contract") != "migration_required":
        raise CompatibilityError("local_mcp: N-only MCP tool lacks traced migration")
    if removed_mcp_tools and local_mcp.get("n_minus_1_consumer_n_contract") != "migration_required":
        raise CompatibilityError("local_mcp: removed MCP tool lacks traced migration")

    schema_declarations = matrix.get("n_consumer_n_minus_1_schema_breaks")
    if not isinstance(schema_declarations, list):
        raise CompatibilityError("N/N-1 schema-break inventory missing")
    declared_breaks: set[str] = set()
    for item in schema_declarations:
        if not isinstance(item, dict):
            raise CompatibilityError("invalid N/N-1 schema-break row")
        failure = item.get("failure")
        if not isinstance(failure, str) or failure in declared_breaks:
            raise CompatibilityError("invalid or duplicate N/N-1 schema break")
        declared_breaks.add(failure)
        surfaces = item.get("surfaces")
        if (
            not isinstance(surfaces, list)
            or not surfaces
            or not set(surfaces).issubset(EXPECTED_SURFACES)
        ):
            raise CompatibilityError(f"schema break has invalid surfaces: {failure}")
        for surface in surfaces:
            if by_surface[surface].get("n_consumer_n_minus_1_contract") != "migration_required":
                raise CompatibilityError(
                    f"{surface}: declared schema break lacks traced migration"
                )
        evidence = item.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise CompatibilityError(f"schema break lacks evidence: {failure}")
        for reference in evidence:
            _validate_evidence(root, str(reference))
    if declared_breaks != consumer_previous_breaks:
        missing = sorted(consumer_previous_breaks - declared_breaks)
        stale = sorted(declared_breaks - consumer_previous_breaks)
        raise CompatibilityError(
            f"N/N-1 schema-break inventory drift: missing={missing}, stale={stale}"
        )


def _validate_trust(root: Path, trust: dict[str, Any]) -> None:
    if trust.get("schema") != TRUST_SCHEMA:
        raise CompatibilityError("trust matrix header invalid")
    rows = trust.get("rows")
    if not isinstance(rows, list):
        raise CompatibilityError("trust matrix rows missing")
    contexts = {row.get("context") for row in rows if isinstance(row, dict)}
    if contexts != EXPECTED_TRUST_CONTEXTS or len(rows) != len(contexts):
        raise CompatibilityError("trust matrix context inventory drift")
    if "not cryptographically separable" not in str(trust.get("same_shell_limit", "")):
        raise CompatibilityError("same-shell trust limitation missing")
    for row in rows:
        evidence = row.get("evidence")
        if not isinstance(evidence, str) or not evidence:
            raise CompatibilityError("trust matrix evidence missing")
        _validate_evidence(root, evidence)


def _simple_yaml_value(path: Path, key: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:\s*([^#\s]+)")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            return match.group(1)
    raise CompatibilityError(f"engine pin missing {key}")


def verify(root: Path, *, expected_source_ref: str | None = None) -> dict[str, Any]:
    fixtures = root / "contracts/compatibility/fixtures"
    manifest = _load(fixtures / "manifest.json")
    if manifest.get("schema") != MANIFEST_SCHEMA or not isinstance(manifest.get("files"), dict):
        raise CompatibilityError("fixture manifest invalid")
    expected_files = {
        "n-contract.json",
        "n-mcp-tools.json",
        "n-consumer-contract.json",
        "n-minus-1-contract.json",
        "n-minus-1-mcp-tools.json",
        "n-minus-1-openapi.json",
        "deliberate-break.json",
    }
    if set(manifest["files"]) != expected_files:
        raise CompatibilityError("fixture manifest inventory drift")
    for name, digest in manifest["files"].items():
        if not HEX_64.fullmatch(str(digest)) or _sha(fixtures / name) != digest:
            raise CompatibilityError(f"fixture digest mismatch: {name}")

    current = _load(fixtures / "n-contract.json")
    consumer = _load(fixtures / "n-consumer-contract.json")
    previous = _load(fixtures / "n-minus-1-contract.json")
    current_mcp_inventory = _load(fixtures / "n-mcp-tools.json")
    previous_mcp_inventory = _load(fixtures / "n-minus-1-mcp-tools.json")
    broken = _load(fixtures / "deliberate-break.json")
    for name, fixture in (
        ("N", current),
        ("N consumer", consumer),
        ("N-1", previous),
        ("deliberate break", broken),
    ):
        _validate_fixture(name, fixture)

    pin = root / "contracts/engine-pin.yaml"
    pinned_ref = expected_source_ref or _simple_yaml_value(pin, "engine_ref")
    pinned_version = int(_simple_yaml_value(pin, "contract_version"))
    payload_sha256 = _simple_yaml_value(pin, "projection_payload_sha256")
    consumer_manifest_sha256 = _simple_yaml_value(
        pin, "projection_consumer_manifest_sha256"
    )
    previous_ref = _simple_yaml_value(pin, "n_minus_1_ref")
    previous_openapi_sha256 = _simple_yaml_value(
        pin, "n_minus_1_openapi_sha256"
    )
    previous_mcp_tools_sha256 = _simple_yaml_value(
        pin, "n_minus_1_mcp_tools_sha256"
    )
    deliberate_break_sha256 = _simple_yaml_value(
        pin, "deliberate_break_fixture_sha256"
    )
    if not HEX_64.fullmatch(payload_sha256):
        raise CompatibilityError("projection payload digest is invalid")
    if not HEX_64.fullmatch(consumer_manifest_sha256):
        raise CompatibilityError("projection consumer manifest digest is invalid")
    if not SHA_40.fullmatch(previous_ref):
        raise CompatibilityError("N-1 source ref is invalid")
    if not HEX_64.fullmatch(previous_openapi_sha256):
        raise CompatibilityError("N-1 OpenAPI digest is invalid")
    if not HEX_64.fullmatch(previous_mcp_tools_sha256):
        raise CompatibilityError("N-1 MCP inventory digest is invalid")
    if not HEX_64.fullmatch(deliberate_break_sha256):
        raise CompatibilityError("deliberate-break fixture digest is invalid")
    if current["source_ref"] != pinned_ref or consumer["source_ref"] != pinned_ref:
        raise CompatibilityError("N fixture source does not match engine pin")
    if current["contract_version"] != pinned_version or consumer["contract_version"] != pinned_version:
        raise CompatibilityError("N fixture version does not match engine pin")
    if previous["source_ref"] != previous_ref or broken["source_ref"] != previous_ref:
        raise CompatibilityError("N-1 fixture source does not match the external pin")
    current_mcp_tools = _validate_mcp_inventory("N MCP", current_mcp_inventory)
    previous_mcp_tools = _validate_mcp_inventory("N-1 MCP", previous_mcp_inventory)
    if current_mcp_inventory["source_ref"] != pinned_ref:
        raise CompatibilityError("N MCP inventory source does not match engine pin")
    if previous_mcp_inventory["source_ref"] != previous_ref:
        raise CompatibilityError("N-1 MCP inventory source does not match engine pin")
    if _sha(fixtures / "n-minus-1-mcp-tools.json") != previous_mcp_tools_sha256:
        raise CompatibilityError("N-1 MCP inventory does not match the external pin")
    if _runtime_mcp_tools() != current_mcp_tools:
        raise CompatibilityError("N MCP inventory does not reconstruct from runtime")
    previous_openapi_path = fixtures / "n-minus-1-openapi.json"
    if (
        previous["source_openapi_sha256"] != previous_openapi_sha256
        or broken["source_openapi_sha256"] != previous_openapi_sha256
        or _sha(previous_openapi_path) != previous_openapi_sha256
    ):
        raise CompatibilityError("N-1 fixture OpenAPI does not match the external pin")
    if _sha(fixtures / "deliberate-break.json") != deliberate_break_sha256:
        raise CompatibilityError("deliberate-break fixture does not match the external pin")

    n_path = root / "contracts/openapi/marvisx.json"
    consumer_path = root / "contracts/openapi/marvisx-public-shared.json"
    n_raw = n_path.read_bytes()
    consumer_raw = consumer_path.read_bytes()
    try:
        current_openapi = json.loads(n_raw)
        consumer_openapi = json.loads(consumer_raw)
        rebuilt_current = compact_spec(
            current_openapi,
            contract_version=pinned_version,
            source_ref=pinned_ref,
            source_bytes=n_raw,
            consumer_bytes=consumer_raw,
        )
        rebuilt_consumer = compact_spec(
            consumer_openapi,
            contract_version=pinned_version,
            source_ref=pinned_ref,
            source_bytes=consumer_raw,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CompatibilityError("current OpenAPI bytes cannot be compacted") from exc
    if rebuilt_current != current:
        raise CompatibilityError("N fixture does not reconstruct from current OpenAPI bytes")
    if rebuilt_consumer != consumer:
        raise CompatibilityError(
            "N consumer fixture does not reconstruct from current OpenAPI bytes"
        )
    previous_raw = previous_openapi_path.read_bytes()
    try:
        previous_openapi = json.loads(previous_raw)
        rebuilt_previous = compact_spec(
            previous_openapi,
            contract_version=previous["contract_version"],
            source_ref=previous_ref,
            source_bytes=previous_raw,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CompatibilityError("N-1 OpenAPI bytes cannot be compacted") from exc
    if rebuilt_previous != previous:
        raise CompatibilityError(
            "N-1 fixture does not reconstruct from pinned OpenAPI bytes"
        )
    if current.get("consumer_openapi_sha256") != consumer.get("source_openapi_sha256"):
        raise CompatibilityError("N fixture is not bound to the consumer OpenAPI bytes")

    old_to_new = provider_breaks(previous, current)
    consumer_to_n = provider_breaks(consumer, current, consumer_view=True)
    if consumer_to_n:
        raise CompatibilityError(f"N does not satisfy its consumer contract: {consumer_to_n}")
    shared_consumer = dict(consumer)
    previous_ops = set(_operations(previous))
    shared_consumer["operations"] = {
        path: {
            method: operation
            for method, operation in methods.items()
            if (path, method) in previous_ops
        }
        for path, methods in consumer["operations"].items()
    }
    shared_consumer["operations"] = {
        path: methods for path, methods in shared_consumer["operations"].items() if methods
    }
    consumer_previous_breaks = set(
        provider_breaks(shared_consumer, previous, consumer_view=True)
    )

    break_info = broken.get("deliberate_break")
    if not isinstance(break_info, dict) or break_info.get("baseline") != "n-minus-1-contract.json":
        raise CompatibilityError("deliberate break declaration missing")
    break_path = break_info.get("path")
    break_method = str(break_info.get("method", "")).lower()
    expected_broken = deepcopy(rebuilt_previous)
    if (
        not isinstance(break_path, str)
        or break_path not in expected_broken["operations"]
        or break_method not in expected_broken["operations"][break_path]
    ):
        raise CompatibilityError("deliberate break target is absent from N-1")
    del expected_broken["operations"][break_path][break_method]
    if not expected_broken["operations"][break_path]:
        del expected_broken["operations"][break_path]
    expected_broken["path_count"] = len(expected_broken["operations"])
    expected_broken["operation_count"] = sum(
        len(methods) for methods in expected_broken["operations"].values()
    )
    expected_broken["deliberate_break"] = break_info
    if expected_broken != broken:
        raise CompatibilityError(
            "deliberate breaking fixture does not reconstruct from N-1"
        )
    break_failures = provider_breaks(previous, broken)
    expected_break = (
        f"operation_removed:{str(break_info.get('method', '')).upper()} "
        f"{break_info.get('path', '')}"
    )
    if expected_break not in break_failures:
        raise CompatibilityError("deliberate breaking fixture did not fail as declared")

    matrix = _load(root / "contracts/compatibility/consumer-matrix-v1.json")
    declared_response_sanitizers = _validate_provider_response_sanitizers(
        root,
        matrix,
        provider_breaks_found=set(old_to_new),
        current_openapi=current_openapi,
        previous_openapi=previous_openapi,
    )
    traced_schema_breaks = consumer_previous_breaks - declared_response_sanitizers
    _validate_matrix(
        root,
        matrix,
        n_consumer=consumer,
        previous=previous,
        consumer_previous_breaks=traced_schema_breaks,
        current_mcp_tools=current_mcp_tools,
        previous_mcp_tools=previous_mcp_tools,
    )
    _validate_trust(root, _load(root / "contracts/compatibility/trust-matrix-v1.json"))

    return {
        "source_ref": pinned_ref,
        "contract_version": pinned_version,
        "projection_payload_sha256": payload_sha256,
        "projection_consumer_manifest_sha256": consumer_manifest_sha256,
        "n_operations": current["operation_count"],
        "n_minus_1_operations": previous["operation_count"],
        "consumer_operations": consumer["operation_count"],
        "n_only_operations": len(set(_operations(consumer)) - set(_operations(previous))),
        "n_mcp_tools": len(current_mcp_tools),
        "n_minus_1_mcp_tools": len(previous_mcp_tools),
        "n_only_mcp_tools": len(current_mcp_tools - previous_mcp_tools),
        "deliberate_breaks_detected": len(break_failures),
        "declared_n_minus_1_schema_breaks": len(traced_schema_breaks),
        "declared_n_minus_1_response_sanitizers": len(
            declared_response_sanitizers
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--expected-source-ref")
    args = parser.parse_args(argv)
    try:
        result = verify(args.root.resolve(), expected_source_ref=args.expected_source_ref)
    except CompatibilityError as exc:
        print(f"compatibility contract: FAIL: {exc}")
        return 1
    print("compatibility contract: PASS " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
