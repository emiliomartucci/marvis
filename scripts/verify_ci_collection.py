#!/usr/bin/env python3
"""Verify that public CI cannot silently narrow the required test collection."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
from typing import Any
import unittest

import yaml


SCHEMA = "marvis-ci-collection-contract/v1"


class CollectionContractError(RuntimeError):
    pass


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionContractError("CI collection contract invalid") from exc
    if not isinstance(value, dict) or value.get("schema") != SCHEMA:
        raise CollectionContractError("CI collection contract unsupported")
    return value


def _pytest_nodes(root: Path, path: str) -> set[str]:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--strict-markers",
            "-p",
            "no:cacheprovider",
            path,
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise CollectionContractError(f"pytest collection failed for {path}: {result.stdout[-500:]}")
    return {
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith(path + "/") and "::" in line
    }


def _flatten(suite: unittest.TestSuite) -> list[unittest.TestCase]:
    result: list[unittest.TestCase] = []
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            result.extend(_flatten(item))
        else:
            result.append(item)
    return result


def _unittest_nodes(root: Path, path: str, pattern: str) -> set[str]:
    absolute = root / path
    sys.path.insert(0, str(absolute))
    try:
        suite = unittest.defaultTestLoader.discover(str(absolute), pattern=pattern)
        return {test.id() for test in _flatten(suite)}
    finally:
        sys.path.pop(0)


def _workflow(root: Path, contract: dict[str, Any]) -> None:
    spec = contract["workflow"]
    path = root / spec["file"]
    raw = path.read_text(encoding="utf-8")
    payload = yaml.safe_load(raw)
    jobs = payload.get("jobs") if isinstance(payload, dict) else None
    if not isinstance(jobs, dict) or spec["required_job"] not in jobs:
        raise CollectionContractError("required Python CI job missing")
    steps = jobs[spec["required_job"]].get("steps", [])
    command_lines = {
        line.strip()
        for step in steps
        if isinstance(step, dict)
        for line in str(step.get("run", "")).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    for line in spec["required_run_lines"]:
        if line not in command_lines:
            raise CollectionContractError(f"CI run line missing: {line}")
    setup_python = [
        step.get("with", {}).get("python-version")
        for step in steps
        if isinstance(step, dict)
        and str(step.get("uses", "")).startswith("actions/setup-python@")
        and isinstance(step.get("with"), dict)
    ]
    if setup_python != [contract["primary_python"]]:
        raise CollectionContractError("primary CI Python row drift")

    for additional in spec.get("additional_jobs", []):
        job_name = str(additional["job"])
        job = jobs.get(job_name)
        if not isinstance(job, dict):
            raise CollectionContractError(f"required CI job missing: {job_name}")
        defaults = job.get("defaults", {})
        working_directory = (
            defaults.get("run", {}).get("working-directory")
            if isinstance(defaults, dict) and isinstance(defaults.get("run"), dict)
            else None
        )
        if working_directory != additional.get("working_directory"):
            raise CollectionContractError(f"CI working directory drift: {job_name}")
        job_steps = job.get("steps", [])
        commands = "\n".join(
            str(step.get("run", "")) for step in job_steps if isinstance(step, dict)
        )
        command_lines = {
            line.strip()
            for line in commands.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        for line in additional.get("required_run_lines", []):
            if line not in command_lines:
                raise CollectionContractError(f"CI run line missing: {line}")
        env_run_line = str(additional["env_run_line"])
        env_steps = [
            step
            for step in job_steps
            if isinstance(step, dict)
            and env_run_line
            in {
                line.strip()
                for line in str(step.get("run", "")).splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            }
        ]
        if len(env_steps) != 1 or not isinstance(env_steps[0].get("env"), dict):
            raise CollectionContractError(f"CI environment step missing: {env_run_line}")
        step_env = {str(key): str(value) for key, value in env_steps[0]["env"].items()}
        for key, value in additional.get("required_step_env", {}).items():
            if step_env.get(key) != value:
                raise CollectionContractError(f"CI step environment drift: {key}")


def _platform_matrix(root: Path, contract: dict[str, Any]) -> None:
    spec = contract["platform_matrix"]
    raw = (root / spec["file"]).read_text(encoding="utf-8")
    for version in spec["python_versions"]:
        if f'"{version}"' not in raw:
            raise CollectionContractError(f"supported Python row missing: {version}")
    for operating_system in spec["operating_systems"]:
        if operating_system not in raw:
            raise CollectionContractError(f"supported OS row missing: {operating_system}")
    payload = yaml.safe_load(raw)
    jobs = payload.get("jobs") if isinstance(payload, dict) else {}
    for exclusion in spec["explicit_exclusions"]:
        job = jobs.get(exclusion["job"], {})
        if job.get("if") is not exclusion["condition"]:
            raise CollectionContractError(f"platform exclusion drift: {exclusion['job']}")
        if exclusion["reason_code"] not in raw:
            raise CollectionContractError(f"platform exclusion reason missing: {exclusion['job']}")


def verify(root: Path) -> dict[str, Any]:
    contract = _load(root / "contracts/ci/collection-v1.json")
    pytest_count = 0
    for suite in contract.get("pytest_suites", []):
        path = str(suite["path"])
        if not (root / path).is_dir():
            raise CollectionContractError(f"required pytest path missing: {path}")
        nodes = _pytest_nodes(root, path)
        if len(nodes) < int(suite["minimum_collected"]):
            raise CollectionContractError(f"pytest collection below floor for {path}")
        missing = set(suite.get("required_nodes", [])) - nodes
        if missing:
            raise CollectionContractError(f"required pytest nodes missing: {sorted(missing)}")
        pytest_count += len(nodes)

    unittest_count = 0
    for suite in contract.get("unittest_suites", []):
        nodes = _unittest_nodes(root, str(suite["path"]), str(suite["pattern"]))
        if len(nodes) < int(suite["minimum_collected"]):
            raise CollectionContractError("unittest collection below floor")
        for suffix in suite.get("required_node_suffixes", []):
            if not any(node.endswith(suffix) for node in nodes):
                raise CollectionContractError(f"required unittest node missing: {suffix}")
        unittest_count += len(nodes)

    markers = subprocess.run(
        [sys.executable, "-m", "pytest", "--markers"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    ).stdout
    for marker in contract.get("markers", {}).get("required", []):
        if f"@pytest.mark.{marker}" not in markers:
            raise CollectionContractError(f"required pytest marker missing: {marker}")

    _workflow(root, contract)
    _platform_matrix(root, contract)
    return {
        "pytest_collected": pytest_count,
        "unittest_collected": unittest_count,
        "required_job": contract["workflow"]["required_job"],
        "additional_jobs": [
            item["job"] for item in contract["workflow"].get("additional_jobs", [])
        ],
        "python_versions": contract["platform_matrix"]["python_versions"],
        "operating_systems": contract["platform_matrix"]["operating_systems"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        result = verify(args.root.resolve())
    except (CollectionContractError, OSError, subprocess.SubprocessError) as exc:
        print(f"CI collection: FAIL: {exc}")
        return 1
    print("CI collection: PASS " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
