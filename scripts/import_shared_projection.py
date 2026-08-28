#!/usr/bin/env python3
"""Fail-closed importer/verifier for the marvisx public/shared projection.

U2 of the 2026-08-25 controlled engine synchronization plan. The exporter
(marvisx core/scripts/export/public_shared.py) produces a bundle:

    bundle/payload/<output_path>...      the shared bytes
    bundle/manifests/payload.json        marvis-public-shared-payload/v1
    bundle/manifests/oss.json            marvis-projection-candidate/v1

This tool is the consumer-side gate: it re-verifies every identity and every
byte INDEPENDENTLY of the exporter, classifies each payload path against
contracts/shared-ownership.yaml, and refuses to touch the worktree unless the
entire candidate is clean. The exporter's policy is an input, never proof —
the same class of trust that once let a generic Console artifact ship the
wrong UI contract behind a green CI (incident lineage 2026-07-24, see
scripts/validate_surfaces.py).

Modes:
    dry-run  (default)  verify + classify + write a deterministic report; the
                        worktree is never modified.
    apply               additionally require a clean worktree, write every
                        importable payload file byte-identically, and record a
                        verifiable backup for rollback. Never deletes.
    rollback           restore the exact pre-apply state from a backup
                        manifest and prove the restored tree digest.

Determinism (plan AE3): the same OSS base commit and the same payload bytes
produce a byte-identical report and the same proposed tree digest — no
timestamps, no host paths, sorted lists throughout.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
import unicodedata
from pathlib import Path, PurePosixPath

import yaml

REPORT_SCHEMA = "marvis-import-report/v1"
BACKUP_SCHEMA = "marvis-import-backup/v1"
PAYLOAD_MANIFEST_SCHEMA = "marvis-public-shared-payload/v1"
CONSUMER_MANIFEST_SCHEMA = "marvis-projection-candidate/v1"
CONSUMER_NAME = "oss"
OWNERSHIP_MAP_REL = "contracts/shared-ownership.yaml"
ENGINE_PIN_REL = "contracts/engine-pin.yaml"
OPENAPI_VERSION_REL = "contracts/openapi/VERSION"
SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ALLOWED_MODES = {"100644", "100755"}
# Hard cap independent of the map, so a map typo cannot lift it.
ABSOLUTE_MAX_FILE_BYTES = 20971520
# Bound hostile candidates before their bytes are retained for classification.
# The verified payload is ~12 MiB; raising either ceiling is a reviewed
# importer-policy change, never something a bundle can request for itself.
ABSOLUTE_MAX_PAYLOAD_BYTES = 268435456
ABSOLUTE_MAX_MANIFEST_BYTES = 8388608
MIGRATIONS_PREFIX = "migrations/"
MAP_PATH_RULES = ("managed_areas", "oss_owned_areas")
APPROVED_PRESERVE_KEY = "approved_preserve_paths"
MAP_DENY_RULES = (
    "forbidden_prefixes",
    "forbidden_components",
    "forbidden_suffixes",
    "forbidden_content_markers",
    "secret_patterns",
    "forbidden_imports",
)


class ImportRefused(RuntimeError):
    """A fail-closed precondition failed before any classification."""


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_blob_oid(data: bytes) -> str:
    """The git content-address of a blob, computed without a repository."""
    return hashlib.sha1(b"blob %d\x00%s" % (len(data), data)).hexdigest()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def git_bytes(repo: Path, *args: str) -> bytes:
    process = _git(repo, *args)
    if process.returncode != 0:
        raise ImportRefused(f"git {' '.join(args[:2])} failed: {process.stderr.decode(errors='replace').strip()}")
    return process.stdout


# ---------------------------------------------------------------------------
# Ownership map
# ---------------------------------------------------------------------------

def _validate_path_rules(rules: object, where: str, errors: list[str]) -> list[str]:
    if not isinstance(rules, list) or not rules:
        errors.append(f"{where}: must be a non-empty list of path rules")
        return []
    clean: list[str] = []
    for rule in rules:
        if not isinstance(rule, str) or not rule.strip():
            errors.append(f"{where}: rules must be non-empty strings")
            continue
        pure = PurePosixPath(rule)
        if pure.is_absolute() or "\\" in rule or any(part in {"", ".", ".."} for part in pure.parts):
            errors.append(f"{where}: unsafe rule {rule!r}")
            continue
        if rule in clean:
            errors.append(f"{where}: duplicate rule {rule!r}")
            continue
        clean.append(rule)
    return clean


def _validate_exact_paths(rules: object, where: str, errors: list[str]) -> list[str]:
    """Validate a possibly-empty list of exact repository paths."""
    if not isinstance(rules, list):
        errors.append(f"{where}: must be a list of exact paths")
        return []
    clean: list[str] = []
    for rule in rules:
        if not isinstance(rule, str) or not rule.strip() or rule.endswith("/"):
            errors.append(f"{where}: entries must be non-empty exact paths")
            continue
        pure = PurePosixPath(rule)
        if pure.is_absolute() or "\\" in rule or any(part in {"", ".", ".."} for part in pure.parts):
            errors.append(f"{where}: unsafe path {rule!r}")
            continue
        if rule in clean:
            errors.append(f"{where}: duplicate path {rule!r}")
            continue
        clean.append(rule)
    return clean


def load_ownership_map(path: Path) -> tuple[dict, list[str]]:
    """Load and validate the ownership map; every structural doubt is an error.

    The map is the boundary this repository fails closed on, so a map that
    cannot be interpreted strictly must never be interpreted loosely.
    """
    errors: list[str] = []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ImportRefused(f"cannot load ownership map {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ImportRefused("ownership map must be a mapping")
    if doc.get("schema") != "marvis-shared-ownership/v1":
        errors.append("ownership map: schema must be marvis-shared-ownership/v1")
    if not isinstance(doc.get("ownership_map_version"), int):
        errors.append("ownership map: ownership_map_version must be an integer")

    clean: dict[str, list[str]] = {}
    for key in MAP_PATH_RULES:
        clean[key] = _validate_path_rules(doc.get(key), f"ownership map: {key}", errors)
    approved_preserve = _validate_exact_paths(
        doc.get(APPROVED_PRESERVE_KEY),
        f"ownership map: {APPROVED_PRESERVE_KEY}",
        errors,
    )
    deny_raw = doc.get("deny")
    deny: dict[str, list[str]] = {}
    if not isinstance(deny_raw, dict):
        errors.append("ownership map: deny must be a mapping")
    else:
        for key in MAP_DENY_RULES:
            deny[key] = _validate_path_rules(deny_raw.get(key), f"ownership map: deny.{key}", errors)
    policy = doc.get("policy")
    if not isinstance(policy, dict):
        errors.append("ownership map: policy must be a mapping")
    else:
        if policy.get("never_delete") is not True:
            errors.append("ownership map: policy.never_delete must be true — the importer refuses to run otherwise")
        if policy.get("apply_requires_clean_worktree") is not True:
            errors.append(
                "ownership map: policy.apply_requires_clean_worktree must be true — the importer refuses to run otherwise"
            )
        if not isinstance(policy.get("max_file_bytes"), int) or not 0 < policy["max_file_bytes"] <= ABSOLUTE_MAX_FILE_BYTES:
            errors.append(f"ownership map: policy.max_file_bytes must be an int in (0, {ABSOLUTE_MAX_FILE_BYTES}]")

    # A managed rule that is shadowed verbatim by an oss_owned rule would make
    # every file under it unimportable while looking shared; nesting is only
    # meaningful as a STRICT sub-area (core/api/tests/ under core/api/).
    for owned in clean["oss_owned_areas"]:
        for managed in clean["managed_areas"]:
            if owned == managed:
                errors.append(f"ownership map: {owned!r} is both managed and oss-owned")
    for path in approved_preserve:
        if not any(
            path.startswith(rule) if rule.endswith("/") else path == rule
            for rule in clean["oss_owned_areas"]
        ):
            errors.append(
                f"ownership map: approved preserve path {path!r} is not OSS-owned"
            )
    if errors:
        raise ImportRefused("ownership map invalid: " + "; ".join(sorted(set(errors))))

    compiled = dict(clean)
    compiled.update(
        {
            # Secret patterns scan payload BYTES, so they must compile as
            # bytes patterns or every scan would raise instead of matching.
            f"deny_{key}": [re.compile(rule.encode("utf-8")) if key == "secret_patterns" else rule for rule in deny[key]]
            for key in MAP_DENY_RULES
        }
    )
    compiled["ownership_map_version"] = doc["ownership_map_version"]
    compiled["max_file_bytes"] = policy["max_file_bytes"]
    compiled[APPROVED_PRESERVE_KEY] = approved_preserve
    return compiled, deny


def _rule_matches(path: str, rule: str) -> bool:
    return path.startswith(rule) if rule.endswith("/") else path == rule


def managed_rule_for(path: str, managed: list[str]) -> str | None:
    matches = [rule for rule in managed if _rule_matches(path, rule)]
    if len(matches) > 1:
        raise ImportRefused(f"ownership map ambiguity: {path} matches managed rules {sorted(matches)}")
    return matches[0] if matches else None


def owned_rule_for(path: str, owned: list[str]) -> str | None:
    matches = sorted(rule for rule in owned if _rule_matches(path, rule))
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Bundle verification
# ---------------------------------------------------------------------------

def payload_digest(records: list[dict]) -> str:
    """Byte-exact mirror of the exporter's payload digest algorithm."""
    digest = hashlib.sha256()
    for record in sorted(records, key=lambda item: item["path"]):
        encoded = json.dumps(
            {
                "mode": record["mode"],
                "path": record["path"],
                "sha256": record["sha256"],
                "size": record["size"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _validate_output_path(path: object) -> str:
    if not isinstance(path, str) or not path:
        raise ImportRefused("payload entry output_path must be a non-empty string")
    if path.startswith("/") or "\\" in path:
        raise ImportRefused(f"unsafe output path: {path!r}")
    pure = PurePosixPath(path)
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise ImportRefused(f"unsafe output path: {path!r}")
    if unicodedata.normalize("NFC", path) != path:
        raise ImportRefused(f"output path is not NFC-normalized: {path!r}")
    if any(ord(character) < 32 for character in path):
        raise ImportRefused(f"output path contains control characters: {path!r}")
    return path


def _safe_relative(base: Path, relative: str) -> Path:
    """Join and prove the target neither is nor traverses a symlink."""
    target = base / relative
    current = base
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ImportRefused(f"symlink inside bundle: {relative}")
    # Containment against the resolved root: absolute() equality would false-
    # positive on symlinked system prefixes (macOS /var -> /private/var).
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        raise ImportRefused(f"bundle path resolves outside the payload root: {relative}") from None
    return target


def load_bundle(
    bundle: Path,
    expected: dict,
    *,
    max_file_bytes: int = ABSOLUTE_MAX_FILE_BYTES,
) -> dict:
    """Verify bundle structure, identities, and every payload byte.

    Returns the verified state used by all modes. Any inconsistency raises
    ImportRefused BEFORE the worktree is examined, so a bad candidate can
    never even be classified, let alone applied.
    """
    payload_root = bundle / "payload"
    manifests = bundle / "manifests"
    for required in (payload_root, manifests / "payload.json", manifests / "oss.json"):
        if not required.exists():
            raise ImportRefused(f"bundle is incomplete: {required.relative_to(bundle)} is missing")

    payload_manifest_path = manifests / "payload.json"
    consumer_manifest_path = manifests / "oss.json"
    try:
        for manifest_path in (payload_manifest_path, consumer_manifest_path):
            if manifest_path.stat().st_size > ABSOLUTE_MAX_MANIFEST_BYTES:
                raise ImportRefused(
                    f"bundle manifest exceeds {ABSOLUTE_MAX_MANIFEST_BYTES} bytes: "
                    f"{manifest_path.name}"
                )
        payload_manifest_bytes = payload_manifest_path.read_bytes()
        consumer_manifest_bytes = consumer_manifest_path.read_bytes()
        payload_manifest = json.loads(payload_manifest_bytes.decode("utf-8"))
        consumer_manifest = json.loads(consumer_manifest_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportRefused(f"bundle manifests unreadable: {exc}") from exc
    if not isinstance(payload_manifest, dict) or not isinstance(consumer_manifest, dict):
        raise ImportRefused("bundle manifests must be objects")
    if payload_manifest.get("schema") != PAYLOAD_MANIFEST_SCHEMA:
        raise ImportRefused(f"payload manifest schema must be {PAYLOAD_MANIFEST_SCHEMA}")
    if consumer_manifest.get("schema") != CONSUMER_MANIFEST_SCHEMA:
        raise ImportRefused(f"consumer manifest schema must be {CONSUMER_MANIFEST_SCHEMA}")
    if consumer_manifest.get("consumer") != CONSUMER_NAME:
        raise ImportRefused(f"consumer manifest is for {consumer_manifest.get('consumer')!r}, not {CONSUMER_NAME!r}")

    consumer_manifest_sha256 = sha256_bytes(consumer_manifest_bytes)
    if expected.get("consumer_manifest_sha256") != consumer_manifest_sha256:
        raise ImportRefused(
            "OSS consumer manifest digest "
            f"{consumer_manifest_sha256} != expected {expected.get('consumer_manifest_sha256')}"
        )

    source_sha = payload_manifest.get("source_sha")
    exporter_sha = payload_manifest.get("exporter_sha")
    exporter_identity = payload_manifest.get("exporter_identity_sha256")
    if not isinstance(source_sha, str) or not SHA40_RE.match(source_sha):
        raise ImportRefused("payload manifest source_sha must be a full 40-hex commit SHA")
    if not isinstance(exporter_sha, str) or not SHA40_RE.match(exporter_sha):
        raise ImportRefused("payload manifest exporter_sha must be a full 40-hex commit SHA")
    if not isinstance(exporter_identity, str) or not SHA256_RE.match(exporter_identity):
        raise ImportRefused("payload manifest exporter_identity_sha256 must be a 64-hex digest")
    # The operator must name the exact source commit: importing "whatever the
    # bundle happens to carry" is how a stale candidate slips in.
    if "source_sha" in expected and expected["source_sha"] != source_sha:
        raise ImportRefused(
            f"bundle source_sha {source_sha} != expected {expected['source_sha']}"
        )
    if expected.get("exporter_sha") != exporter_sha:
        raise ImportRefused(
            f"bundle exporter_sha {exporter_sha} != expected {expected['exporter_sha']}"
        )
    if expected.get("exporter_identity_sha256") != exporter_identity:
        raise ImportRefused(
            "bundle exporter identity "
            f"{exporter_identity} != expected {expected.get('exporter_identity_sha256')}"
        )

    files_raw = payload_manifest.get("files")
    if not isinstance(files_raw, list) or not files_raw:
        raise ImportRefused("payload manifest carries no files")

    seen: dict[str, dict] = {}
    casefold: dict[str, str] = {}
    records: list[dict] = []
    payload_bytes = 0
    for entry in files_raw:
        if not isinstance(entry, dict):
            raise ImportRefused("payload file entry must be an object")
        path = _validate_output_path(entry.get("output_path"))
        if path in seen:
            raise ImportRefused(f"duplicate output path in manifest: {path}")
        folded = path.casefold()
        if folded in casefold and casefold[folded] != path:
            raise ImportRefused(
                f"case-insensitive collision in manifest: {path} vs {casefold[folded]}"
            )
        casefold[folded] = path
        mode = entry.get("mode")
        if mode not in ALLOWED_MODES:
            raise ImportRefused(f"{path}: mode must be 100644 or 100755, not {mode!r}")
        declared_sha = entry.get("sha256")
        if not isinstance(declared_sha, str) or not SHA256_RE.match(declared_sha):
            raise ImportRefused(f"{path}: sha256 must be a 64-hex digest")
        declared_size = entry.get("size")
        if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size < 0:
            raise ImportRefused(f"{path}: size must be a non-negative integer")
        if declared_size > max_file_bytes:
            raise ImportRefused(f"{path}: exceeds max_file_bytes before payload read")
        payload_bytes += declared_size
        if payload_bytes > ABSOLUTE_MAX_PAYLOAD_BYTES:
            raise ImportRefused(
                f"bundle payload exceeds {ABSOLUTE_MAX_PAYLOAD_BYTES} bytes before payload read"
            )
        target = _safe_relative(payload_root, path)
        if not target.is_file():
            raise ImportRefused(f"payload file missing from bundle: {path}")
        if target.stat().st_size > max_file_bytes:
            raise ImportRefused(f"{path}: on-disk payload exceeds max_file_bytes before read")
        content = target.read_bytes()
        if len(content) != declared_size:
            raise ImportRefused(f"{path}: size mismatch (manifest {declared_size}, actual {len(content)})")
        actual_sha = sha256_bytes(content)
        if actual_sha != declared_sha:
            raise ImportRefused(f"{path}: payload bytes do not match manifest sha256")
        seen[path] = {
            "mode": mode,
            "sha256": actual_sha,
            "size": len(content),
            "source_path": entry.get("source_path"),
            "content": content,
        }
        records.append({"mode": mode, "path": path, "sha256": actual_sha, "size": len(content)})

    recomputed = payload_digest(records)
    if recomputed != payload_manifest.get("payload_sha256"):
        raise ImportRefused(
            f"recomputed payload digest {recomputed} != manifest {payload_manifest.get('payload_sha256')}"
        )
    if expected.get("payload_sha256") != recomputed:
        raise ImportRefused(
            f"payload digest {recomputed} != expected {expected['payload_sha256']}"
        )
    if payload_manifest.get("file_count") != len(seen):
        raise ImportRefused("payload manifest file_count does not match the file list")
    if consumer_manifest.get("payload_sha256") != recomputed:
        raise ImportRefused("consumer manifest binds a different payload digest than payload.json")
    for field, actual in (
        ("source_sha", source_sha),
        ("exporter_sha", exporter_sha),
        ("exporter_identity_sha256", exporter_identity),
    ):
        if consumer_manifest.get(field) != actual:
            raise ImportRefused(
                f"consumer manifest {field} does not match payload manifest"
            )
    if consumer_manifest.get("payload_file_count") != len(seen):
        raise ImportRefused("consumer manifest payload_file_count does not match payload")
    declared_imports = consumer_manifest.get("import_paths")
    if not isinstance(declared_imports, list):
        raise ImportRefused("consumer manifest import_paths must be a list")
    declared_preserved = consumer_manifest.get("preserved_overlap_paths")
    if not isinstance(declared_preserved, list):
        raise ImportRefused("consumer manifest preserved_overlap_paths must be a list")
    if not all(isinstance(path, str) for path in declared_imports + declared_preserved):
        raise ImportRefused("consumer manifest paths must all be strings")
    if len(declared_imports) != len(set(declared_imports)):
        raise ImportRefused("consumer manifest import_paths contains duplicates")
    if len(declared_preserved) != len(set(declared_preserved)):
        raise ImportRefused("consumer manifest preserved_overlap_paths contains duplicates")
    if consumer_manifest.get("import_file_count") != len(declared_imports):
        raise ImportRefused("consumer manifest import_file_count does not match import_paths")
    if set(declared_imports) & set(declared_preserved):
        raise ImportRefused("consumer import and preserved path sets overlap")
    declared_paths = set(declared_imports) | set(declared_preserved)
    unknown = sorted(declared_paths - set(seen))
    if unknown:
        raise ImportRefused(f"consumer manifest names unknown payload paths: {unknown[0]}")
    omitted = sorted(set(seen) - declared_paths)
    if omitted:
        raise ImportRefused(f"consumer manifest omits payload path: {omitted[0]}")

    # Unlisted bytes sitting in payload/ are smuggling until proven otherwise.
    listed_files = {str(item) for item in seen}
    on_disk: set[str] = set()
    for found in payload_root.rglob("*"):
        if found.is_file() and not found.is_symlink():
            on_disk.add(found.relative_to(payload_root).as_posix())
        elif found.is_symlink():
            raise ImportRefused(f"symlink inside bundle payload/: {found.relative_to(payload_root)}")
    extra = sorted(on_disk - listed_files)
    if extra:
        raise ImportRefused(f"bundle payload/ carries files absent from the manifest: {extra[0]}")

    return {
        "source_sha": source_sha,
        "exporter_sha": exporter_sha,
        "exporter_identity_sha256": exporter_identity,
        "payload_sha256": recomputed,
        "payload_manifest_sha256": sha256_bytes(payload_manifest_bytes),
        "consumer_manifest_sha256": consumer_manifest_sha256,
        "files": seen,
        "consumer_import_paths": sorted(set(declared_imports)),
        "consumer_preserved_paths": sorted(set(declared_preserved)),
    }


# ---------------------------------------------------------------------------
# Classification and deny scans
# ---------------------------------------------------------------------------

def scan_forbidden_imports(files: dict[str, dict], forbidden_roots: list[str]) -> tuple[list[str], list[dict]]:
    """AST-scan payload Python for Cloud/Enterprise module imports.

    An import inside a try block is an explicit optional seam (KTD10: the
    guarded core.hosted_lifecycle / workos integrations degrade when the
    module is absent) and is only reported. An unguarded one is a hard
    dependency on surfaces this product must not require.
    """
    violations: list[str] = []
    seams: list[dict] = []
    roots = tuple(sorted(forbidden_roots))

    def _matches_root(module: str | None) -> str | None:
        if not module:
            return None
        for root in roots:
            if module == root or module.startswith(root + "."):
                return root
        return None

    def _walk(node: ast.AST, guarded: bool, path: str, module: str) -> None:
        for child in ast.iter_child_nodes(node):
            # Children of a Try are the guarded ones; the Try's own imports in
            # `else`/handlers stay guarded too, which matches the exporter's
            # optional-import semantics closely enough for this consumer check.
            child_guarded = guarded or isinstance(child, ast.Try)
            if isinstance(child, ast.Import):
                for alias in child.names:
                    if _matches_root(alias.name):
                        if child_guarded:
                            seams.append({"path": path, "line": child.lineno, "module": alias.name})
                        else:
                            violations.append(
                                f"unguarded forbidden import {alias.name!r} at {path}:{child.lineno}"
                            )
            elif isinstance(child, ast.ImportFrom):
                target = child.module
                if child.level:
                    base = module.split(".")[:-1]
                    ascend = max(0, child.level - 1)
                    base = base[: len(base) - ascend] if ascend else base
                    target = ".".join(base + (target.split(".") if target else []))
                if _matches_root(target):
                    if child_guarded:
                        seams.append({"path": path, "line": child.lineno, "module": target})
                    else:
                        violations.append(
                            f"unguarded forbidden import {target!r} at {path}:{child.lineno}"
                        )
            _walk(child, child_guarded, path, module)

    for path in sorted(files):
        if not path.endswith(".py"):
            continue
        module = path[:-3].replace("/", ".")
        try:
            tree = ast.parse(files[path]["content"], filename=path)
        except (SyntaxError, ValueError) as exc:
            violations.append(f"payload Python does not parse: {path}: {exc}")
            continue
        _walk(tree, False, path, module)
    return violations, sorted(seams, key=lambda item: (item["path"], item["line"]))


def classify(
    bundle_state: dict,
    ownership: dict,
    repo: Path,
) -> dict:
    """Classify every payload path and collect every reason to refuse."""
    files = bundle_state["files"]
    tracked = git_ls_tree(repo)
    violations: list[str] = []
    blocked: list[dict] = []
    preserved_oss_owned: list[str] = []
    already_synced: list[str] = []
    would_overwrite: list[str] = []
    additions: list[str] = []
    deny = ownership

    for path in sorted(files):
        entry = files[path]
        pure = PurePosixPath(path)
        # Independent deny rules — the exporter already enforces its own copy,
        # but the consumer verifies against its own policy, not trust.
        for prefix in deny["deny_forbidden_prefixes"]:
            if path.startswith(prefix):
                violations.append(f"deny: forbidden prefix {prefix!r} selected {path}")
        for component in pure.parts:
            if component in deny["deny_forbidden_components"]:
                violations.append(f"deny: forbidden component {component!r} in {path}")
        for suffix in deny["deny_forbidden_suffixes"]:
            if path.endswith(suffix):
                violations.append(f"deny: forbidden suffix {suffix!r} on {path}")
        content = entry["content"]
        for marker in deny["deny_forbidden_content_markers"]:
            if marker.encode("utf-8") in content:
                violations.append(f"deny: forbidden content marker in {path}")
        for pattern in deny["deny_secret_patterns"]:
            if pattern.search(content):
                violations.append(f"deny: credential-shaped content in {path}")
        if entry["size"] > ownership["max_file_bytes"]:
            violations.append(f"deny: {path} exceeds max_file_bytes")

        # Ownership: oss_owned wins over managed (documented ordering), and a
        # path matching neither is an undeclared change to this repository.
        owned_rule = owned_rule_for(path, ownership["oss_owned_areas"])
        if owned_rule is not None:
            if path in ownership[APPROVED_PRESERVE_KEY]:
                preserved_oss_owned.append(path)
            else:
                blocked.append({"path": path, "rule": owned_rule, "owner": "marvis"})
            continue
        managed_rule = managed_rule_for(path, ownership["managed_areas"])
        if managed_rule is None:
            blocked.append({"path": path, "rule": None, "owner": "undeclared"})
            continue

        # A payload path that casefolds onto a different tracked file would
        # shadow it on case-insensitive filesystems (the default on macOS).
        for tracked_path in tracked:
            if tracked_path != path and tracked_path.casefold() == path.casefold():
                violations.append(
                    f"case-insensitive collision with tracked file {tracked_path}: {path}"
                )

        local = repo / path
        if local.exists():
            if sha256_bytes(local.read_bytes()) == entry["sha256"]:
                already_synced.append(path)
            else:
                would_overwrite.append(path)
        else:
            additions.append(path)

    import_violations, seams = scan_forbidden_imports(files, deny["deny_forbidden_imports"])
    violations.extend(import_violations)

    # Local files inside managed areas that the payload does not carry. The
    # importer never deletes; these are surfaced for maintainer review so a
    # dropped upstream file cannot silently strand here either.
    local_only = []
    payload_paths = set(files)
    for tracked_path in sorted(tracked):
        if tracked_path in payload_paths:
            continue
        if managed_rule_for(tracked_path, ownership["managed_areas"]) is None:
            continue
        if owned_rule_for(tracked_path, ownership["oss_owned_areas"]) is not None:
            continue
        local_only.append(tracked_path)

    # Migration history must be append-only: every migration tracked here must
    # arrive byte-identical or not be carried at all (an upstream rewrite of a
    # historical migration would fork every already-migrated local database).
    migration_state = {"tracked": 0, "identical_in_payload": 0, "absent_from_payload": 0, "changed_in_payload": 0}
    for tracked_path in sorted(tracked):
        if not tracked_path.startswith(MIGRATIONS_PREFIX):
            continue
        migration_state["tracked"] += 1
        if tracked_path not in payload_paths:
            migration_state["absent_from_payload"] += 1
        elif sha256_bytes((repo / tracked_path).read_bytes()) != files[tracked_path]["sha256"]:
            migration_state["changed_in_payload"] += 1
            violations.append(f"migration compatibility: {tracked_path} differs in payload")
        else:
            migration_state["identical_in_payload"] += 1

    return {
        "violations": violations,
        "blocked": blocked,
        "preserved_oss_owned": preserved_oss_owned,
        "already_synced": already_synced,
        "would_overwrite": would_overwrite,
        "additions": additions,
        "local_only_in_managed_area": local_only,
        "optional_integration_seams": seams,
        "migrations": migration_state,
    }


def git_ls_tree(repo: Path) -> list[str]:
    raw = git_bytes(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD")
    return [item for item in raw.decode("utf-8", errors="strict").split("\0") if item]


def worktree_dirty(repo: Path) -> list[str]:
    raw = git_bytes(repo, "status", "--porcelain=v1", "--untracked-files=all")
    return [line for line in raw.decode("utf-8", errors="replace").splitlines() if line.strip()]


def proposed_tree_digest(repo: Path, files: dict[str, dict], importable: set[str]) -> str:
    """Digest of the tree the import would produce (AE3 determinism).

    Built from git blob oids: HEAD content for everything the import does not
    touch, the payload bytes for everything it writes. Nothing is removed, so
    the projected tree is exactly HEAD plus the overlay.
    """
    raw = git_bytes(repo, "ls-tree", "-r", "-z", "HEAD")
    entries: dict[str, str] = {}
    for record in raw.decode("utf-8", errors="strict").split("\0"):
        if not record:
            continue
        metadata, separator, path = record.partition("\t")
        mode, _kind, oid = metadata.split(" ")
        entries[path] = f"{mode} {oid}"
    for path in sorted(importable):
        entry = files[path]
        entries[path] = f"{entry['mode']} {git_blob_oid(entry['content'])}"
    digest = hashlib.sha256()
    for path in sorted(entries):
        digest.update(f"{entries[path]} {path}\n".encode("utf-8"))
    return digest.hexdigest()


def read_compatibility(repo: Path, bundle: Path) -> dict:
    """Informational contract window: candidate openapi version vs the pin."""
    result: dict = {
        "pinned_contract_version": None,
        "candidate_contract_version": None,
        "candidate_predecessor_contract_version": None,
        "candidate_carries_openapi_contract": False,
    }
    pin_path = repo / ENGINE_PIN_REL
    try:
        pin = yaml.safe_load(pin_path.read_text(encoding="utf-8"))
        if isinstance(pin, dict) and isinstance(pin.get("contract_version"), int):
            result["pinned_contract_version"] = pin["contract_version"]
    except (OSError, yaml.YAMLError):
        pass
    version_path = bundle / "payload" / OPENAPI_VERSION_REL
    if version_path.is_file():
        try:
            version = yaml.safe_load(version_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            version = None
        if isinstance(version, dict):
            result["candidate_carries_openapi_contract"] = True
            if isinstance(version.get("contract_version"), int):
                result["candidate_contract_version"] = version["contract_version"]
            if isinstance(version.get("predecessor_contract_version"), int):
                result["candidate_predecessor_contract_version"] = version["predecessor_contract_version"]
    return result


def build_report(
    mode: str,
    status: str,
    identities: dict,
    payload_summary: dict,
    classification: dict,
    compatibility: dict,
    extra: dict | None = None,
) -> dict:
    report = {
        "schema": REPORT_SCHEMA,
        "mode": mode,
        "status": status,
        "identities": identities,
        "payload": payload_summary,
        "classification": {
            "already_synced": classification["already_synced"],
            "would_overwrite": classification["would_overwrite"],
            "additions": classification["additions"],
            "blocked_collisions": classification["blocked"],
            "preserved_oss_owned": classification["preserved_oss_owned"],
            "local_only_in_managed_area": classification["local_only_in_managed_area"],
            "optional_integration_seams": classification["optional_integration_seams"],
        },
        "violations": classification["violations"],
        "compatibility": compatibility,
    }
    if extra:
        report.update(extra)
    return report


# ---------------------------------------------------------------------------
# Apply and rollback
# ---------------------------------------------------------------------------

def _destination_is_safe(repo: Path, relative: str) -> Path:
    destination = repo / relative
    current = repo
    for part in PurePosixPath(relative).parts:
        current = current / part
        if current.is_symlink():
            raise ImportRefused(f"refusing to write through symlink: {relative}")
        if current.exists() and not current.is_dir() and current != destination:
            raise ImportRefused(f"destination parent is not a directory: {relative}")
    if destination.exists() and destination.is_symlink():
        raise ImportRefused(f"refusing to overwrite a symlink: {relative}")
    return destination


def apply_payload(
    repo: Path,
    bundle_state: dict,
    importable: list[str],
    backup_dir: Path,
    pre_apply_tree: str,
) -> dict:
    dirty = worktree_dirty(repo)
    if dirty:
        raise ImportRefused(f"apply requires a clean worktree (first issue: {dirty[0]})")
    if backup_dir.exists():
        raise ImportRefused(f"backup directory already exists: {backup_dir}")
    try:
        backup_dir.resolve().relative_to(repo.resolve())
    except ValueError:
        pass
    else:
        raise ImportRefused("backup directory must live outside the repository")

    files_root = backup_dir / "files"
    entries = []
    written = 0
    for path in sorted(importable):
        entry = bundle_state["files"][path]
        destination = _destination_is_safe(repo, path)
        if destination.exists():
            prior_bytes = destination.read_bytes()
            (files_root / path).parent.mkdir(parents=True, exist_ok=True)
            (files_root / path).write_bytes(prior_bytes)
            entries.append(
                {
                    "path": path,
                    "action": "overwritten",
                    "prior_sha256": sha256_bytes(prior_bytes),
                    "prior_executable": bool(destination.stat().st_mode & 0o111),
                }
            )
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            entries.append({"path": path, "action": "added", "prior_sha256": None, "prior_executable": False})
        destination.write_bytes(entry["content"])
        destination.chmod(0o755 if entry["mode"] == "100755" else 0o644)
        # Readback before declaring the write: applied bytes must equal the
        # verified payload bytes, not merely have been sent to disk.
        if sha256_bytes(destination.read_bytes()) != entry["sha256"]:
            raise ImportRefused(f"readback mismatch after write: {path}")
        expected_executable = entry["mode"] == "100755"
        if bool(destination.stat().st_mode & 0o111) != expected_executable:
            raise ImportRefused(f"readback mode mismatch after write: {path}")
        written += 1

    post_records = [
        {
            "mode": bundle_state["files"][path]["mode"],
            "path": path,
            "sha256": bundle_state["files"][path]["sha256"],
            "size": bundle_state["files"][path]["size"],
        }
        for path in importable
    ]
    imported_readback_digest = payload_digest(post_records)
    backup_manifest = {
        "schema": BACKUP_SCHEMA,
        "source_sha": bundle_state["source_sha"],
        "payload_sha256": bundle_state["payload_sha256"],
        "base_head": git_bytes(repo, "rev-parse", "HEAD").decode().strip(),
        "pre_apply_tree_sha256": pre_apply_tree,
        "post_apply_readback_digest": imported_readback_digest,
        "entries": entries,
    }
    (backup_dir / "backup.json").write_bytes(canonical_json(backup_manifest))
    return {
        "applied": {
            "files_written": written,
            "readback_imported_files_sha256": imported_readback_digest,
            "backup_manifest_sha256": sha256_bytes(canonical_json(backup_manifest)),
            "pre_apply_tree_sha256": pre_apply_tree,
            "backup_entries": len(entries),
        }
    }


def rollback_payload(repo: Path, backup_dir: Path, files: dict[str, dict]) -> dict:
    manifest_path = backup_dir / "backup.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ImportRefused(f"cannot read backup manifest: {exc}") from exc
    if manifest.get("schema") != BACKUP_SCHEMA:
        raise ImportRefused("backup manifest schema is not " + BACKUP_SCHEMA)
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise ImportRefused("backup manifest entries must be a list")

    # Refuse to roll back onto drifted state: every entry must still be in the
    # exact post-apply condition, otherwise restoring would destroy work.
    for entry in entries:
        path = entry["path"]
        current = repo / path
        if not current.is_file():
            raise ImportRefused(f"cannot roll back: {path} no longer exists")
        if sha256_bytes(current.read_bytes()) != files[path]["sha256"]:
            raise ImportRefused(f"cannot roll back: {path} changed after apply")
        expected_executable = files[path]["mode"] == "100755"
        if bool(current.stat().st_mode & 0o111) != expected_executable:
            raise ImportRefused(f"cannot roll back: {path} mode changed after apply")

    restored = removed = 0
    for entry in sorted(entries, key=lambda item: item["path"]):
        path = entry["path"]
        destination = repo / path
        if entry["action"] == "overwritten":
            prior = backup_dir / "files" / path
            if not prior.is_file():
                raise ImportRefused(f"backup bytes missing for {path}")
            destination.write_bytes(prior.read_bytes())
            destination.chmod(0o755 if entry["prior_executable"] else 0o644)
            restored += 1
        else:
            destination.unlink()
            removed += 1
    for path in sorted({PurePosixPath(entry["path"]).parent for entry in entries}, reverse=True):
        candidate = repo / path
        if path != PurePosixPath(".") and candidate.is_dir() and not any(candidate.iterdir()):
            candidate.rmdir()

    dirty = worktree_dirty(repo)
    if dirty:
        raise ImportRefused(
            f"rollback verification failed: worktree is not clean (first issue: {dirty[0]})"
        )
    restored_digest = proposed_tree_digest(repo, files, set())
    if restored_digest != manifest.get("pre_apply_tree_sha256"):
        raise ImportRefused(
            f"rollback verification failed: tree digest {restored_digest} != pre-apply {manifest.get('pre_apply_tree_sha256')}"
        )
    return {"rollback": {"files_restored": restored, "files_removed": removed, "tree_matches_pre_apply": True}}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    bundle = args.bundle.resolve()
    ownership_path = args.ownership_map.resolve()
    expected: dict = {}
    required_expectations = (
        ("expected_source_sha", "source_sha", "--expected-source-sha", SHA40_RE, "40-hex commit SHA"),
        ("expected_exporter_sha", "exporter_sha", "--expected-exporter-sha", SHA40_RE, "40-hex commit SHA"),
        (
            "expected_exporter_identity_sha256",
            "exporter_identity_sha256",
            "--expected-exporter-identity-sha256",
            SHA256_RE,
            "64-hex digest",
        ),
        ("expected_payload_sha256", "payload_sha256", "--expected-payload-sha256", SHA256_RE, "64-hex digest"),
        (
            "expected_consumer_manifest_sha256",
            "consumer_manifest_sha256",
            "--expected-consumer-manifest-sha256",
            SHA256_RE,
            "64-hex digest",
        ),
    )
    for attribute, key, flag, pattern, description in required_expectations:
        value = getattr(args, attribute, None)
        if not value:
            print(f"{flag} is required for {args.mode}", file=sys.stderr)
            return 2
        if not isinstance(value, str) or not pattern.fullmatch(value):
            print(f"{flag} must be a full {description}", file=sys.stderr)
            return 2
        expected[key] = value

    try:
        ownership, _ = load_ownership_map(ownership_path)
        bundle_state = load_bundle(
            bundle,
            expected,
            max_file_bytes=ownership["max_file_bytes"],
        )
        classification = classify(bundle_state, ownership, repo)
        dirty = worktree_dirty(repo)
        importable = sorted(
            set(bundle_state["consumer_import_paths"])
            - {item["path"] for item in classification["blocked"]}
            - set(classification["preserved_oss_owned"])
        )
        compatibility = read_compatibility(repo, bundle)
        identities = {
            "source_sha": bundle_state["source_sha"],
            "exporter_sha": bundle_state["exporter_sha"],
            "exporter_identity_sha256": bundle_state["exporter_identity_sha256"],
            "payload_sha256": bundle_state["payload_sha256"],
            "payload_manifest_sha256": bundle_state["payload_manifest_sha256"],
            "consumer_manifest_sha256": bundle_state["consumer_manifest_sha256"],
            "oss_base_head": git_bytes(repo, "rev-parse", "HEAD").decode().strip(),
            "ownership_map_version": ownership["ownership_map_version"],
            "ownership_map_sha256": sha256_bytes(ownership_path.read_bytes()),
        }
        payload_summary = {
            "file_count": len(bundle_state["files"]),
            "importable_file_count": len(importable),
            "blocked_file_count": len(classification["blocked"]),
            "preserved_oss_owned_file_count": len(classification["preserved_oss_owned"]),
            "bytes_total": sum(entry["size"] for entry in bundle_state["files"].values()),
        }
        compatibility["migrations"] = classification["migrations"]
        compatibility["worktree_dirty_entries"] = len(dirty)

        blocked_or_violated = bool(classification["violations"] or classification["blocked"])
        status = "blocked" if blocked_or_violated else "verified"
        extra: dict = {
            "digests": {
                "proposed_tree_sha256": proposed_tree_digest(repo, bundle_state["files"], set(importable))
            }
        }

        if args.mode == "apply":
            if blocked_or_violated:
                status = "blocked"
            else:
                pre_apply = proposed_tree_digest(repo, bundle_state["files"], set())
                extra.update(apply_payload(repo, bundle_state, importable, args.backup_dir.resolve(), pre_apply))
                status = "applied"
        elif args.mode == "rollback":
            extra.update(rollback_payload(repo, args.backup_dir.resolve(), bundle_state["files"]))
            status = "rolled_back"

        report = build_report(
            args.mode, status, identities, payload_summary, classification, compatibility, extra
        )
    except ImportRefused as exc:
        print(f"import refused: {exc}", file=sys.stderr)
        return 2

    report_bytes = canonical_json(report)
    if args.report:
        Path(args.report).write_bytes(report_bytes)
    print(f"status={status} report_sha256={sha256_bytes(report_bytes)}")
    if classification["violations"]:
        print(f"{len(classification['violations'])} violation(s):", file=sys.stderr)
        for violation in classification["violations"][:20]:
            print(f"  - {violation}", file=sys.stderr)
    if classification["blocked"]:
        print(f"{len(classification['blocked'])} blocked collision(s):", file=sys.stderr)
        for item in classification["blocked"][:20]:
            owner = item["owner"] if item["rule"] else "undeclared"
            print(f"  - {item['path']} (owner: {owner}, rule: {item['rule']})", file=sys.stderr)
    return 0 if status in {"verified", "applied", "rolled_back"} else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--ownership-map", type=Path, default=None)
    parser.add_argument("--mode", choices=("dry-run", "apply", "rollback"), default="dry-run")
    parser.add_argument("--expected-source-sha", default=None)
    parser.add_argument("--expected-exporter-sha", default=None)
    parser.add_argument("--expected-exporter-identity-sha256", default=None)
    parser.add_argument("--expected-payload-sha256", default=None)
    parser.add_argument("--expected-consumer-manifest-sha256", default=None)
    parser.add_argument("--backup-dir", type=Path, default=None)
    parser.add_argument("--report", type=Path, default=None)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.ownership_map is None:
        args.ownership_map = args.repo / OWNERSHIP_MAP_REL
    if args.mode == "apply" and args.backup_dir is None:
        print("--backup-dir is required for apply", file=sys.stderr)
        return 2
    if args.mode == "rollback" and args.backup_dir is None:
        print("--backup-dir is required for rollback", file=sys.stderr)
        return 2
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
