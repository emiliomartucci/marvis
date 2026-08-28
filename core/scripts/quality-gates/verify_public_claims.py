#!/usr/bin/env python3
"""Fail closed on inaccurate public license and audit claims."""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from email.parser import Parser
import json
from pathlib import Path
import re
import tarfile
from typing import Iterable
from zipfile import ZipFile

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 compatibility
    import tomli as tomllib  # type: ignore[no-redef]


_FORBIDDEN = {
    "unqualified audit claim": re.compile(r"\btamper[- ]evident\b", re.IGNORECASE),
    "inaccurate license claim": re.compile(
        r"\bopen[- ]source\b|\bOSI\b|(?<![-_A-Za-z0-9])OSS(?![-_A-Za-z0-9])",
        re.IGNORECASE,
    ),
    "inaccurate telemetry default": re.compile(
        r"\btelemetry\b[^\n]{0,120}\bopt[- ]out\b"
        r"|\btelemetry\b[^\n]{0,120}\bdefault[- ]on\b"
        r"|\bdefault\s*(?:→|->|:|=)\s*on\b",
        re.IGNORECASE,
    ),
}
_JS_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_JS_LINE_COMMENT = re.compile(r"(?m)^\s*//.*$")
_REPOSITORY_IDENTIFIERS = re.compile(
    r"(?i)(?:emiliomartucci/)?marvisx-oss|marvis@marvisx-oss|marvis-oss@(?:dev|[\w.+'-]+)"
)
_TEXT_SURFACES = (
    "CHANGELOG.md",
    "core/cli/README.md",
    "docs/install/INSTALL-SKILL.md",
)


@dataclass(frozen=True)
class ClaimViolation:
    surface: str
    rule: str
    excerpt: str


class PublicClaimError(RuntimeError):
    pass


def _repository_root() -> Path:
    """Return the repository root from this script's canonical package path."""
    return Path(__file__).resolve().parents[3]


def _sanitize_identifiers(text: str) -> str:
    return _REPOSITORY_IDENTIFIERS.sub("repository-identifier", text)


def _check_text(surface: str, text: str) -> list[ClaimViolation]:
    sanitized = _sanitize_identifiers(text)
    violations: list[ClaimViolation] = []
    for rule, pattern in _FORBIDDEN.items():
        match = pattern.search(sanitized)
        if match:
            start = max(0, match.start() - 45)
            end = min(len(sanitized), match.end() + 45)
            excerpt = " ".join(sanitized[start:end].split())
            violations.append(ClaimViolation(surface, rule, excerpt))
    return violations


def _public_readme(root: Path) -> Path:
    projection = root / "README.OSS.md"
    return projection if projection.is_file() else root / "README.md"


def _public_python_literals(root: Path) -> Iterable[tuple[str, str]]:
    for base in (root / "core/cli", root / "core/telemetry"):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            values = [
                node.value
                for node in ast.walk(tree)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            ]
            yield str(path.relative_to(root)), "\n".join(values)


def _ui_sources(root: Path) -> Iterable[tuple[str, str]]:
    for base in (root / "core/console/src", root / "apps/desktop-ui/src"):
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.suffix not in {".js", ".jsx", ".ts", ".tsx"} or not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            text = _JS_BLOCK_COMMENT.sub("", text)
            text = _JS_LINE_COMMENT.sub("", text)
            yield str(path.relative_to(root)), text


def verify_source(root: Path) -> dict[str, object]:
    root = root.resolve()
    readme = _public_readme(root)
    paths = [readme, *(root / relative for relative in _TEXT_SURFACES)]
    missing = [str(path.relative_to(root)) for path in paths if not path.is_file()]
    if missing:
        raise PublicClaimError(f"required public claim surfaces missing: {missing}")

    surfaces: list[tuple[str, str]] = [
        (str(path.relative_to(root)), path.read_text(encoding="utf-8"))
        for path in paths
    ]
    pyproject_path = root / "pyproject.toml"
    try:
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]
        metadata = "\n".join(
            [
                str(project["description"]),
                *(str(value) for value in project.get("keywords", [])),
            ]
        )
        license_value = project.get("license")
        license_text = (
            license_value.get("text")
            if isinstance(license_value, dict)
            else license_value
        )
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise PublicClaimError("package metadata is not reviewable") from exc
    if license_text != "BSL-1.1":
        raise PublicClaimError("package license must be exactly BSL-1.1")
    surfaces.append(("pyproject.toml:[project]", metadata))
    surfaces.extend(_public_python_literals(root))
    surfaces.extend(_ui_sources(root))

    violations = [
        violation
        for surface, text in surfaces
        for violation in _check_text(surface, text)
    ]
    readme_text = readme.read_text(encoding="utf-8").lower()
    for phrase in (
        "transactional append-only",
        "database-writer",
        "trusted checkpoint",
        "stored independently",
        "business source license 1.1",
    ):
        if phrase not in readme_text:
            violations.append(
                ClaimViolation(str(readme.relative_to(root)), "missing boundary", phrase)
            )
    if violations:
        details = "; ".join(
            f"{item.surface}: {item.rule}: {item.excerpt}" for item in violations
        )
        raise PublicClaimError(details)
    return {
        "source_surfaces": len(surfaces),
        "license": "BSL-1.1",
        "audit_claim": "transactional-append-only/checkpoint-relative",
    }


def _artifact_texts(path: Path) -> Iterable[tuple[str, str]]:
    if path.suffix == ".whl":
        with ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".dist-info/METADATA"):
                    yield name, archive.read(name).decode("utf-8", "replace")
        return
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile() or not (
                    member.name.endswith("/PKG-INFO")
                    or member.name.endswith("/core/cli/README.md")
                ):
                    continue
                extracted = archive.extractfile(member)
                if extracted is not None:
                    yield member.name, extracted.read(2_000_000).decode("utf-8", "replace")
        return
    raise PublicClaimError(f"unsupported artifact type: {path.name}")


def verify_artifact(path: Path) -> dict[str, object]:
    path = path.resolve()
    if not path.is_file():
        raise PublicClaimError(f"artifact missing: {path}")
    texts = list(_artifact_texts(path))
    if not texts:
        raise PublicClaimError(f"artifact has no reviewable public metadata: {path.name}")
    violations = [
        violation
        for surface, text in texts
        for violation in _check_text(f"{path.name}:{surface}", text)
    ]
    metadata_members = [
        (surface, text)
        for surface, text in texts
        if surface.endswith("/METADATA") or surface.endswith("/PKG-INFO")
    ]
    if not metadata_members:
        raise PublicClaimError(f"artifact has no package metadata: {path.name}")
    for surface, text in metadata_members:
        metadata = Parser().parsestr(text, headersonly=True)
        license_text = metadata.get("License-Expression") or metadata.get("License")
        if license_text != "BSL-1.1":
            violations.append(
                ClaimViolation(
                    f"{path.name}:{surface}",
                    "package license must be exactly BSL-1.1",
                    str(license_text or "<missing>"),
                )
            )
    if violations:
        details = "; ".join(
            f"{item.surface}: {item.rule}: {item.excerpt}" for item in violations
        )
        raise PublicClaimError(details)
    return {
        "artifact": path.name,
        "reviewed_members": len(texts),
        "license": "BSL-1.1",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=_repository_root())
    parser.add_argument("--artifact", type=Path, action="append", default=[])
    args = parser.parse_args(argv)
    try:
        result = {"source": verify_source(args.root)}
        result["artifacts"] = [verify_artifact(path) for path in args.artifact]
    except PublicClaimError as exc:
        print(f"Public claims: FAIL: {exc}")
        return 1
    print("Public claims: PASS " + json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
