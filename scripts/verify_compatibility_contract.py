#!/usr/bin/env python3
"""Fail-closed verification of the OSS N/N-1 compatibility contract."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
from typing import Any


FIXTURE_SCHEMA = "marvis-compatibility-fixture/v1"
MANIFEST_SCHEMA = "marvis-compatibility-fixture-manifest/v1"
MATRIX_SCHEMA = "marvis-consumer-compatibility-matrix/v1"
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
    return failures


def _schema_breaks(expected: dict[str, Any], actual: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    old_shapes = expected["component_schema_compatibility"]
    new_shapes = actual["component_schema_compatibility"]
    for name, old in old_shapes.items():
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
        if not set(new.get("required", [])).issubset(set(old.get("required", []))):
            failures.append(f"schema_required_added:{name}")
        old_properties = old.get("property_sha256", {})
        new_properties = new.get("property_sha256", {})
        for prop, digest in old_properties.items():
            if new_properties.get(prop) != digest:
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
    if not consumer_view:
        failures.extend(_schema_breaks(expected, actual))
    return failures


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


def _validate_matrix(
    root: Path,
    matrix: dict[str, Any],
    *,
    n_consumer: dict[str, Any],
    previous: dict[str, Any],
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
        "n-consumer-contract.json",
        "n-minus-1-contract.json",
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
    if not HEX_64.fullmatch(payload_sha256):
        raise CompatibilityError("projection payload digest is invalid")
    if not HEX_64.fullmatch(consumer_manifest_sha256):
        raise CompatibilityError("projection consumer manifest digest is invalid")
    if current["source_ref"] != pinned_ref or consumer["source_ref"] != pinned_ref:
        raise CompatibilityError("N fixture source does not match engine pin")
    if current["contract_version"] != pinned_version or consumer["contract_version"] != pinned_version:
        raise CompatibilityError("N fixture version does not match engine pin")
    for name, path, digest_key in (
        ("N OpenAPI", root / "contracts/openapi/marvisx.json", "source_openapi_sha256"),
        ("N consumer OpenAPI", root / "contracts/openapi/marvisx-public-shared.json", "source_openapi_sha256"),
    ):
        fixture = current if name == "N OpenAPI" else consumer
        if _sha(path) != fixture[digest_key]:
            raise CompatibilityError(f"{name} bytes do not match fixture")
    if current.get("consumer_openapi_sha256") != consumer.get("source_openapi_sha256"):
        raise CompatibilityError("N fixture is not bound to the consumer OpenAPI bytes")

    old_to_new = provider_breaks(previous, current)
    if old_to_new:
        raise CompatibilityError(f"N breaks N-1 consumers: {old_to_new}")
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
    if provider_breaks(shared_consumer, previous, consumer_view=True):
        raise CompatibilityError("N-1 does not satisfy the shared N consumer operations")

    break_info = broken.get("deliberate_break")
    if not isinstance(break_info, dict) or break_info.get("baseline") != "n-minus-1-contract.json":
        raise CompatibilityError("deliberate break declaration missing")
    break_failures = provider_breaks(previous, broken)
    expected_break = (
        f"operation_removed:{str(break_info.get('method', '')).upper()} "
        f"{break_info.get('path', '')}"
    )
    if expected_break not in break_failures:
        raise CompatibilityError("deliberate breaking fixture did not fail as declared")

    matrix = _load(root / "contracts/compatibility/consumer-matrix-v1.json")
    _validate_matrix(root, matrix, n_consumer=consumer, previous=previous)
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
        "deliberate_breaks_detected": len(break_failures),
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
