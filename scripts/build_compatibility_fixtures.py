#!/usr/bin/env python3
"""Build deterministic, compact N/N-1 API and schema fixtures.

The committed fixtures retain every HTTP operation plus a digest for every
component schema.  They are small enough to review while remaining bound to
the exact full OpenAPI documents.  N-1 is read from an exact Git object; no
moving branch or hosted filesystem is accepted as input.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any


HTTP_METHODS = ("delete", "get", "head", "options", "patch", "post", "put")
FIXTURE_SCHEMA = "marvis-compatibility-fixture/v1"


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _required_parameters(path_item: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    parameters = [*path_item.get("parameters", []), *operation.get("parameters", [])]
    required: set[str] = set()
    for parameter in parameters:
        if not isinstance(parameter, dict) or not parameter.get("required"):
            continue
        required.add(f"{parameter.get('in', '')}:{parameter.get('name', '')}")
    return sorted(required)


def compact_spec(
    spec: dict[str, Any],
    *,
    contract_version: int,
    source_ref: str,
    source_bytes: bytes,
    consumer_bytes: bytes | None = None,
) -> dict[str, Any]:
    operations: dict[str, dict[str, Any]] = {}
    for path, raw_path_item in sorted(spec.get("paths", {}).items()):
        if not isinstance(raw_path_item, dict):
            continue
        methods: dict[str, Any] = {}
        for method in HTTP_METHODS:
            operation = raw_path_item.get(method)
            if not isinstance(operation, dict):
                continue
            responses = operation.get("responses", {})
            methods[method] = {
                "deprecated": bool(operation.get("deprecated", False)),
                "operation_id": operation.get("operationId"),
                "request_body_required": bool(
                    isinstance(operation.get("requestBody"), dict)
                    and operation["requestBody"].get("required")
                ),
                "required_parameters": _required_parameters(raw_path_item, operation),
                "response_codes": sorted(str(code) for code in responses),
            }
        if methods:
            operations[path] = methods

    schemas: dict[str, str] = {}
    raw_schemas = spec.get("components", {}).get("schemas", {})
    if isinstance(raw_schemas, dict):
        schemas = {
            name: _sha(_canonical(schema))
            for name, schema in sorted(raw_schemas.items())
        }

    fixture: dict[str, Any] = {
        "schema": FIXTURE_SCHEMA,
        "contract_version": contract_version,
        "source_ref": source_ref,
        "source_openapi_sha256": _sha(source_bytes),
        "openapi": spec.get("openapi"),
        "path_count": len(operations),
        "operation_count": sum(len(methods) for methods in operations.values()),
        "component_schema_count": len(schemas),
        "operations": operations,
        "component_schema_sha256": schemas,
    }
    if consumer_bytes is not None:
        fixture["consumer_openapi_sha256"] = _sha(consumer_bytes)
    return fixture


def _git_object(repo: Path, ref: str, path: str) -> bytes:
    if len(ref) != 40 or any(ch not in "0123456789abcdef" for ch in ref):
        raise SystemExit("N-1 ref must be a full lowercase 40-character commit SHA")
    return subprocess.check_output(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        stderr=subprocess.PIPE,
    )


def _write(path: Path, value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False).encode() + b"\n"
    path.write_bytes(raw)
    return _sha(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-spec", type=Path, required=True)
    parser.add_argument("--n-consumer-spec", type=Path, required=True)
    parser.add_argument("--n-source-ref", required=True)
    parser.add_argument("--n-minus-one-repo", type=Path, required=True)
    parser.add_argument("--n-minus-one-ref", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    n_raw = args.n_spec.read_bytes()
    n_consumer_raw = args.n_consumer_spec.read_bytes()
    previous_raw = _git_object(
        args.n_minus_one_repo,
        args.n_minus_one_ref,
        "contracts/openapi/marvisx.json",
    )
    n_spec = json.loads(n_raw)
    previous_spec = json.loads(previous_raw)

    current = compact_spec(
        n_spec,
        contract_version=3,
        source_ref=args.n_source_ref,
        source_bytes=n_raw,
        consumer_bytes=n_consumer_raw,
    )
    previous = compact_spec(
        previous_spec,
        contract_version=2,
        source_ref=args.n_minus_one_ref,
        source_bytes=previous_raw,
    )

    broken = deepcopy(previous)
    first_path = sorted(broken["operations"])[0]
    first_method = sorted(broken["operations"][first_path])[0]
    del broken["operations"][first_path][first_method]
    if not broken["operations"][first_path]:
        del broken["operations"][first_path]
    broken["deliberate_break"] = {
        "kind": "operation_removed",
        "path": first_path,
        "method": first_method,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    digests = {
        "n-contract.json": _write(args.output_dir / "n-contract.json", current),
        "n-minus-1-contract.json": _write(
            args.output_dir / "n-minus-1-contract.json", previous
        ),
        "deliberate-break.json": _write(
            args.output_dir / "deliberate-break.json", broken
        ),
    }
    _write(
        args.output_dir / "manifest.json",
        {
            "schema": "marvis-compatibility-fixture-manifest/v1",
            "files": digests,
        },
    )
    print(
        "compatibility fixtures written: "
        f"N={current['operation_count']} operations, "
        f"N-1={previous['operation_count']} operations"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
