#!/usr/bin/env python3
"""Execute every suite in the reviewed CI collection contract."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Any
import unittest
import xml.etree.ElementTree as ET


_DESELECTED_RE = re.compile(r"(?<!\d)(\d+) deselected(?:\b|$)")


def _load(root: Path) -> dict[str, Any]:
    return json.loads((root / "contracts/ci/collection-v1.json").read_text(encoding="utf-8"))


def _pytest_suite(root: Path, suite: dict[str, Any], report: Path) -> dict[str, int]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--strict-markers",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
        str(suite["path"]),
    ]
    result = subprocess.run(
        command,
        cwd=root,
        timeout=300,
        text=True,
        capture_output=True,
    )
    if result.stdout:
        print(result.stdout, end="")
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"required pytest suite failed: {suite['path']}")
    xml = ET.parse(report).getroot()
    nodes = [xml] if xml.tag == "testsuite" else list(xml.findall("testsuite"))
    totals = {
        key: sum(int(node.attrib.get(key, "0")) for node in nodes)
        for key in ("tests", "failures", "errors", "skipped")
    }
    if totals["skipped"] > int(suite.get("allowed_skips", 0)):
        raise RuntimeError(f"unexpected pytest skip in {suite['path']}")
    if totals["tests"] < int(suite["minimum_collected"]):
        raise RuntimeError(f"executed pytest count below floor in {suite['path']}")
    output = result.stdout + "\n" + result.stderr
    deselected = sum(int(value) for value in _DESELECTED_RE.findall(output))
    if deselected > int(suite.get("allowed_deselections", 0)):
        raise RuntimeError(f"unexpected pytest deselection in {suite['path']}")
    totals["deselected"] = deselected
    return totals


class _Result(unittest.TextTestResult):
    pass


def _unittest_suite(root: Path, suite: dict[str, Any]) -> dict[str, int]:
    absolute = root / str(suite["path"])
    sys.path.insert(0, str(absolute))
    try:
        tests = unittest.defaultTestLoader.discover(
            str(absolute), pattern=str(suite["pattern"])
        )
        runner = unittest.TextTestRunner(verbosity=2, resultclass=_Result)
        result = runner.run(tests)
    finally:
        sys.path.pop(0)
    if not result.wasSuccessful():
        raise RuntimeError(f"required unittest suite failed: {suite['path']}")
    if len(result.skipped) > int(suite.get("allowed_skips", 0)):
        raise RuntimeError(f"unexpected unittest skip in {suite['path']}")
    return {
        "tests": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
    }


def run(root: Path) -> dict[str, Any]:
    contract = _load(root)
    pytest_results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="marvis-ci-reports-") as raw:
        reports = Path(raw)
        for index, suite in enumerate(contract["pytest_suites"]):
            pytest_results[suite["path"]] = _pytest_suite(
                root, suite, reports / f"pytest-{index}.xml"
            )
    unittest_results = {
        suite["path"]: _unittest_suite(root, suite)
        for suite in contract["unittest_suites"]
    }
    return {"pytest": pytest_results, "unittest": unittest_results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    try:
        result = run(args.root.resolve())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"CI contract run: FAIL: {exc}")
        return 1
    print("CI contract run: PASS " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
