#!/usr/bin/env python3
"""Build one immutable public/shared payload for OSS and Enterprise consumers."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable


SHA_RE = re.compile(r"^[0-9a-f]{40}$")
POLICY_REL = "contracts/projections/public_shared_v1.json"
EXPORTER_MODULE_REL = "core/scripts/export/public_shared.py"
EXPORTER_PACKAGE_REL = "core/scripts/export/__init__.py"
REQUIRED_IDENTITY_FILES = {
    "contracts/projections/enterprise_consumer_v1.json",
    "contracts/projections/oss_consumer_v1.json",
    POLICY_REL,
    EXPORTER_PACKAGE_REL,
    EXPORTER_MODULE_REL,
}
# Identity inputs whose bytes must come from the executing interpreter itself,
# never from the caller-supplied repository: those bytes ARE the code producing
# the manifests, so only they prove the exporter SHA being attested.
EXECUTING_IDENTITY_FILES = {
    EXPORTER_MODULE_REL,
    EXPORTER_PACKAGE_REL,
}
HIGH_CONFIDENCE_SECRET_PATTERNS = (
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{40,}"),
    re.compile(rb"xox[baprs]-[A-Za-z0-9-]{20,}"),
)


class ProjectionError(RuntimeError):
    """A fail-closed projection contract violation."""


@dataclass(frozen=True)
class GitEntry:
    mode: str
    object_type: str
    oid: str
    source_path: str


@dataclass(frozen=True)
class PayloadFile:
    source_path: str
    output_path: str
    mode: str
    git_oid: str
    content: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


class _ImportCollector(ast.NodeVisitor):
    """Collect runtime imports while ignoring explicit optional/type-only blocks."""

    def __init__(self) -> None:
        self.imports: list[tuple[ast.AST, bool]] = []
        self._optional_depth = 0

    def visit_Try(self, node: ast.Try) -> None:
        catches_missing_import = any(
            handler.type is None
            or (
                isinstance(handler.type, ast.Name)
                and handler.type.id in {"ImportError", "ModuleNotFoundError"}
            )
            or (
                isinstance(handler.type, ast.Tuple)
                and any(
                    isinstance(item, ast.Name)
                    and item.id in {"ImportError", "ModuleNotFoundError"}
                    for item in handler.type.elts
                )
            )
            for handler in node.handlers
        )
        if catches_missing_import:
            self._optional_depth += 1
            for child in node.body:
                self.visit(child)
            self._optional_depth -= 1
            for handler in node.handlers:
                for child in handler.body:
                    self.visit(child)
            for child in (*node.orelse, *node.finalbody):
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        is_type_checking = (
            isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        ) or (
            isinstance(node.test, ast.Attribute)
            and node.test.attr == "TYPE_CHECKING"
        )
        if is_type_checking:
            for child in node.orelse:
                self.visit(child)
            return
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        self.imports.append((node, self._optional_depth > 0))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self.imports.append((node, self._optional_depth > 0))


def _python_module(path: str) -> tuple[str, bool] | None:
    if not path.endswith(".py"):
        return None
    parts = list(PurePosixPath(path[:-3]).parts)
    if not parts or parts[0] != "core":
        return None
    is_package = parts[-1] == "__init__"
    if is_package:
        parts.pop()
    return ".".join(parts), is_package


def _module_inventory(paths: Iterable[str]) -> tuple[set[str], set[str]]:
    modules: set[str] = set()
    packages: set[str] = set()
    for path in paths:
        parsed = _python_module(path)
        if parsed is None:
            continue
        module, is_package = parsed
        modules.add(module)
        if is_package:
            packages.add(module)
        parts = module.split(".")
        packages.update(".".join(parts[:index]) for index in range(1, len(parts)))
    return modules, packages


def _relative_module(
    current_module: str,
    current_is_package: bool,
    level: int,
    imported_module: str | None,
) -> str | None:
    base = current_module.split(".")
    if not current_is_package:
        base = base[:-1]
    ascend = max(0, level - 1)
    if ascend > len(base):
        return None
    if ascend:
        base = base[:-ascend]
    if imported_module:
        base.extend(imported_module.split("."))
    return ".".join(base) or None


def validate_import_closure(
    files: list[PayloadFile], *, source_python_paths: Iterable[str]
) -> None:
    """Require every non-optional internal Python import in the payload."""
    selected_modules, selected_packages = _module_inventory(
        item.output_path for item in files
    )
    source_modules, source_packages = _module_inventory(source_python_paths)
    available = selected_modules | selected_packages
    source_available = source_modules | source_packages
    failures: list[str] = []

    def require(target: str | None, owner: str, line: int) -> None:
        if target and target.startswith("core") and target not in available:
            failures.append(f"{owner}:{line} imports excluded module {target}")

    for item in files:
        parsed = _python_module(item.output_path)
        if parsed is None:
            continue
        current_module, current_is_package = parsed
        try:
            tree = ast.parse(item.content, filename=item.output_path)
        except (SyntaxError, ValueError) as exc:
            raise ProjectionError(
                f"selected Python source cannot be parsed: {item.output_path}"
            ) from exc
        collector = _ImportCollector()
        collector.visit(tree)
        for node, optional in collector.imports:
            if optional:
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    require(alias.name, item.output_path, node.lineno)
                continue
            base = (
                _relative_module(
                    current_module,
                    current_is_package,
                    node.level,
                    node.module,
                )
                if node.level
                else node.module
            )
            require(base, item.output_path, node.lineno)
            if base:
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    candidate = f"{base}.{alias.name}"
                    if candidate in source_available:
                        require(candidate, item.output_path, node.lineno)
    if failures:
        summary = "; ".join(sorted(set(failures))[:8])
        raise ProjectionError(f"projection is not import-closed: {summary}")


def _canonical_json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        raise ProjectionError(f"git command failed: {args[0] if args else 'git'}")
    return process.stdout


def _require_full_commit(repo: Path, sha: str, label: str) -> str:
    if not SHA_RE.fullmatch(sha):
        raise ProjectionError(f"{label} must be a full lowercase 40-character commit SHA")
    resolved = _git(repo, "rev-parse", "--verify", f"{sha}^{{commit}}").decode().strip()
    if resolved != sha:
        raise ProjectionError(f"{label} does not resolve to the declared commit")
    return resolved


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProjectionError(f"invalid projection contract: {path.name}") from exc
    if not isinstance(value, dict):
        raise ProjectionError(f"projection contract must be an object: {path.name}")
    return value


def _git_blob_at(repo: Path, commit: str, path: str) -> bytes:
    return _git(repo, "show", f"{commit}:{path}")


def verify_exporter_identity(
    repo: Path, exporter_sha: str, policy: dict[str, Any]
) -> str:
    identity_files = policy.get("identity_files")
    if not isinstance(identity_files, list) or not all(
        isinstance(path, str) for path in identity_files
    ):
        raise ProjectionError("identity_files must be a list of repository paths")
    if not REQUIRED_IDENTITY_FILES.issubset(identity_files):
        raise ProjectionError("exporter identity omits a required policy or implementation file")

    executing_module = Path(__file__).resolve()
    executing_package = executing_module.parent / "__init__.py"
    digest = hashlib.sha256()
    for relative in sorted(set(identity_files)):
        if relative == EXPORTER_MODULE_REL:
            local_path = executing_module
        elif relative == EXPORTER_PACKAGE_REL:
            local_path = executing_package
        else:
            local_path = repo / relative
        if relative in EXECUTING_IDENTITY_FILES:
            # The attested exporter SHA must describe the module this
            # interpreter is running, not merely files sitting in --repo.
            # Running a copy from checkout A against --repo B fails closed here
            # instead of attesting B's identity for A's code.
            if local_path.resolve() != (repo / relative).resolve():
                raise ProjectionError(
                    f"executing exporter is not the asserted repository's {relative}"
                )
        try:
            local_bytes = local_path.read_bytes()
        except OSError as exc:
            raise ProjectionError(f"exporter identity file is missing: {relative}") from exc
        committed_bytes = _git_blob_at(repo, exporter_sha, relative)
        if local_bytes != committed_bytes:
            raise ProjectionError(f"running exporter differs from exporter SHA: {relative}")
        encoded = relative.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(hashlib.sha256(local_bytes).digest())
    return digest.hexdigest()


def _parse_tree(repo: Path, source_sha: str) -> list[GitEntry]:
    raw = _git(repo, "ls-tree", "-rz", "--full-tree", source_sha)
    entries: list[GitEntry] = []
    for record in raw.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ProjectionError("malformed git tree record")
        try:
            mode, object_type, oid = metadata.decode("ascii").split(" ")
            source_path = raw_path.decode("utf-8")
        except (UnicodeDecodeError, ValueError) as exc:
            raise ProjectionError("git tree contains a non-UTF-8 or malformed path") from exc
        entries.append(GitEntry(mode, object_type, oid, source_path))
    return entries


def _matches_rule(path: str, rule_source: str) -> str | None:
    if rule_source.endswith("/"):
        return path[len(rule_source) :] if path.startswith(rule_source) else None
    return "" if path == rule_source else None


def _is_excluded(path: str, policy: dict[str, Any]) -> bool:
    if path in set(policy.get("exclude_paths") or []):
        return True
    if any(path.startswith(prefix) for prefix in policy.get("exclude_prefixes") or []):
        return True
    for scope, raw_rules in (policy.get("scoped_allowlists") or {}).items():
        if not path.startswith(scope):
            continue
        if not isinstance(raw_rules, dict):
            raise ProjectionError(f"invalid scoped allowlist: {scope}")
        paths = set(raw_rules.get("paths") or [])
        prefixes = raw_rules.get("prefixes") or []
        return path not in paths and not any(path.startswith(prefix) for prefix in prefixes)
    return False


def _validate_output_path(path: str, policy: dict[str, Any]) -> None:
    if not path or path.startswith("/") or "\\" in path:
        raise ProjectionError(f"invalid output path: {path!r}")
    pure = PurePosixPath(path)
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise ProjectionError(f"unsafe output path: {path}")
    if unicodedata.normalize("NFC", path) != path:
        raise ProjectionError(f"output path is not NFC-normalized: {path}")
    if any(ord(character) < 32 for character in path):
        raise ProjectionError("output path contains control characters")
    if any(path.startswith(prefix) for prefix in policy.get("forbidden_output_prefixes") or []):
        raise ProjectionError(f"forbidden output class selected: {path}")
    forbidden_components = set(policy.get("forbidden_path_components") or [])
    if any(part in forbidden_components for part in pure.parts):
        raise ProjectionError(f"forbidden output component selected: {path}")
    if any(path.endswith(suffix) for suffix in policy.get("forbidden_suffixes") or []):
        raise ProjectionError(f"forbidden output suffix selected: {path}")


def _read_blobs(repo: Path, entries: list[GitEntry]) -> list[bytes]:
    if not entries:
        return []
    query = b"".join(entry.oid.encode("ascii") + b"\n" for entry in entries)
    raw = _git(repo, "cat-file", "--batch", input_bytes=query)
    offset = 0
    blobs: list[bytes] = []
    for expected in entries:
        header_end = raw.find(b"\n", offset)
        if header_end < 0:
            raise ProjectionError("truncated git cat-file response")
        header = raw[offset:header_end].decode("ascii", errors="strict")
        fields = header.split(" ")
        if len(fields) != 3:
            raise ProjectionError("malformed git cat-file response")
        oid, object_type, size_text = fields
        if oid != expected.oid or object_type != "blob":
            raise ProjectionError("git object identity changed during export")
        size = int(size_text)
        start = header_end + 1
        end = start + size
        if end >= len(raw) or raw[end : end + 1] != b"\n":
            raise ProjectionError("truncated git blob response")
        blobs.append(raw[start:end])
        offset = end + 1
    if offset != len(raw):
        raise ProjectionError("unexpected trailing git blob data")
    return blobs


def select_payload(repo: Path, source_sha: str, policy: dict[str, Any]) -> list[PayloadFile]:
    raw_rules = policy.get("include_rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ProjectionError("include_rules must be a non-empty list")
    rules: list[dict[str, Any]] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            raise ProjectionError("include rule must be an object")
        source = raw_rule.get("source")
        destination = raw_rule.get("destination")
        if not isinstance(source, str) or not isinstance(destination, str):
            raise ProjectionError("include rule requires source and destination")
        rules.append(raw_rule)

    tree = _parse_tree(repo, source_sha)
    selected_entries: list[tuple[GitEntry, str, int]] = []
    matched_rule_indexes: set[int] = set()
    output_paths: set[str] = set()
    casefold_paths: dict[str, str] = {}
    for entry in tree:
        matches: list[tuple[int, dict[str, Any], str]] = []
        for index, rule in enumerate(rules):
            suffix = _matches_rule(entry.source_path, rule["source"])
            if suffix is not None:
                matches.append((index, rule, suffix))
        if not matches:
            continue
        if len(matches) != 1:
            raise ProjectionError(f"source path matches multiple include rules: {entry.source_path}")
        index, rule, suffix = matches[0]
        if _is_excluded(entry.source_path, policy):
            continue
        matched_rule_indexes.add(index)
        if entry.object_type != "blob" or entry.mode == "120000":
            raise ProjectionError(f"symlink or non-blob selected: {entry.source_path}")
        output_path = rule["destination"] + suffix
        _validate_output_path(output_path, policy)
        if output_path in output_paths:
            raise ProjectionError(f"two source files map to one output path: {output_path}")
        folded = output_path.casefold()
        if folded in casefold_paths and casefold_paths[folded] != output_path:
            raise ProjectionError(f"case-insensitive output collision: {output_path}")
        output_paths.add(output_path)
        casefold_paths[folded] = output_path
        selected_entries.append((entry, output_path, index))

    for index, rule in enumerate(rules):
        if rule.get("required") is True and index not in matched_rule_indexes:
            raise ProjectionError(f"required include path missing at source SHA: {rule['source']}")
    selected_entries.sort(key=lambda item: item[1])
    blobs = _read_blobs(repo, [item[0] for item in selected_entries])
    max_file_bytes = int(policy.get("max_file_bytes") or 0)
    payload: list[PayloadFile] = []
    markers = [str(marker).encode("utf-8") for marker in policy.get("forbidden_content_markers") or []]
    for (entry, output_path, _index), content in zip(selected_entries, blobs, strict=True):
        if max_file_bytes <= 0 or len(content) > max_file_bytes:
            raise ProjectionError(f"selected file exceeds size policy: {output_path}")
        if any(marker in content for marker in markers) or any(
            pattern.search(content) for pattern in HIGH_CONFIDENCE_SECRET_PATTERNS
        ):
            raise ProjectionError(f"secret-bearing content selected: {output_path}")
        payload.append(
            PayloadFile(
                source_path=entry.source_path,
                output_path=output_path,
                mode=entry.mode,
                git_oid=entry.oid,
                content=content,
            )
        )
    if not payload:
        raise ProjectionError("projection selected no payload files")
    validate_import_closure(
        payload,
        source_python_paths=(entry.source_path for entry in tree),
    )
    return payload


def _payload_digest(files: Iterable[PayloadFile]) -> str:
    digest = hashlib.sha256()
    for item in sorted(files, key=lambda file: file.output_path):
        record = {
            "mode": item.mode,
            "path": item.output_path,
            "sha256": item.sha256,
            "size": len(item.content),
        }
        encoded = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _path_matches(path: str, rule: str) -> bool:
    return path.startswith(rule) if rule.endswith("/") else path == rule


def build_consumer_manifest(
    policy: dict[str, Any], payload_paths: list[str], payload_digest: str
) -> dict[str, Any]:
    if policy.get("schema") != "marvis-projection-consumer/v1":
        raise ProjectionError("invalid consumer policy schema")
    consumer = policy.get("consumer")
    owned = policy.get("downstream_owned")
    preserve = policy.get("preserve_overlaps")
    if not isinstance(consumer, str) or not isinstance(owned, list) or not isinstance(preserve, list):
        raise ProjectionError("invalid consumer ownership policy")
    overlaps = sorted(
        path for path in payload_paths if any(_path_matches(path, rule) for rule in owned)
    )
    undeclared = [
        path for path in overlaps if not any(_path_matches(path, rule) for rule in preserve)
    ]
    if undeclared:
        raise ProjectionError(
            f"undeclared downstream ownership overlap for {consumer}: {undeclared[0]}"
        )
    imported = [path for path in payload_paths if path not in set(overlaps)]
    return {
        "schema": "marvis-projection-candidate/v1",
        "consumer": consumer,
        "payload_sha256": payload_digest,
        "payload_file_count": len(payload_paths),
        "import_file_count": len(imported),
        "preserved_overlap_paths": overlaps,
        "import_paths": imported,
    }


def _ensure_clean_source(repo: Path, output: Path) -> None:
    repo_resolved = repo.resolve()
    output_resolved = output.resolve()
    try:
        output_resolved.relative_to(repo_resolved)
    except ValueError:
        pass
    else:
        raise ProjectionError("output directory must be outside the source repository")
    status_output = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if status_output.strip():
        raise ProjectionError("source repository must be clean before candidate export")
    if output.exists() and any(output.iterdir()):
        raise ProjectionError("output directory must be absent or empty")


def export_projection(
    *, repo: Path, source_sha: str, exporter_sha: str, output: Path
) -> dict[str, Any]:
    repo = repo.resolve()
    _ensure_clean_source(repo, output)
    _require_full_commit(repo, source_sha, "source_sha")
    _require_full_commit(repo, exporter_sha, "exporter_sha")
    policy = _load_json(repo / POLICY_REL)
    if policy.get("schema") != "marvis-public-shared-projection/v1":
        raise ProjectionError("invalid public/shared projection schema")
    exporter_digest = verify_exporter_identity(repo, exporter_sha, policy)
    files = select_payload(repo, source_sha, policy)
    payload_digest = _payload_digest(files)
    payload_paths = [item.output_path for item in files]

    consumer_manifests: dict[str, dict[str, Any]] = {}
    for relative in policy.get("consumer_policies") or []:
        consumer_policy = _load_json(repo / relative)
        manifest = build_consumer_manifest(consumer_policy, payload_paths, payload_digest)
        manifest.update(
            {
                "source_sha": source_sha,
                "exporter_sha": exporter_sha,
                "exporter_identity_sha256": exporter_digest,
            }
        )
        consumer_manifests[manifest["consumer"]] = manifest
    if set(consumer_manifests) != {"oss", "enterprise"}:
        raise ProjectionError("exactly OSS and Enterprise consumer policies are required")

    payload_manifest = {
        "schema": "marvis-public-shared-payload/v1",
        "source_sha": source_sha,
        "exporter_sha": exporter_sha,
        "exporter_identity_sha256": exporter_digest,
        "payload_sha256": payload_digest,
        "file_count": len(files),
        "files": [
            {
                "git_oid": item.git_oid,
                "mode": item.mode,
                "output_path": item.output_path,
                "sha256": item.sha256,
                "size": len(item.content),
                "source_path": item.source_path,
            }
            for item in files
        ],
    }

    output.mkdir(parents=True, exist_ok=True)
    payload_root = output / "payload"
    manifest_root = output / "manifests"
    payload_root.mkdir()
    manifest_root.mkdir()
    for item in files:
        destination = payload_root / item.output_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(item.content)
        destination.chmod(0o755 if item.mode == "100755" else 0o644)
    (manifest_root / "payload.json").write_bytes(_canonical_json(payload_manifest))
    for consumer, manifest in sorted(consumer_manifests.items()):
        (manifest_root / f"{consumer}.json").write_bytes(_canonical_json(manifest))
    return {
        "source_sha": source_sha,
        "exporter_sha": exporter_sha,
        "exporter_identity_sha256": exporter_digest,
        "payload_sha256": payload_digest,
        "file_count": len(files),
        "consumers": sorted(consumer_manifests),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--exporter-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        result = export_projection(
            repo=args.repo,
            source_sha=args.source_sha,
            exporter_sha=args.exporter_sha,
            output=args.output,
        )
    except ProjectionError as exc:
        print(f"projection failed: {exc}", file=sys.stderr)
        return 2
    print(_canonical_json(result).decode("utf-8"), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
