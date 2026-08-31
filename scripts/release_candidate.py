#!/usr/bin/env python3
"""Fail-closed public release policy, artifact manifest and registry readback.

The PR path validates and builds but cannot publish. A tag path must additionally
pass live GitHub-environment checks, a fresh owner readback of the private shared
source and PyPI trusted publisher, and an external 24-hour approval-watchdog
receipt before upload.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from email.parser import Parser
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from typing import Any, TYPE_CHECKING
import urllib.error
import urllib.parse
import urllib.request
import zipfile

if TYPE_CHECKING:
    from packaging.version import Version

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


POLICY_SCHEMA = "marvis-public-release-policy/v1"
MANIFEST_SCHEMA = "marvis-public-release-manifest/v1"
ACCEPTANCE_SCHEMA = "marvis-public-release-acceptance/v1"
EXTERNAL_RECEIPT_SCHEMA = "marvis-external-release-receipt/v1"
WATCHDOG_WRITE_AUTHORITY_SCHEMA = "marvis-watchdog-write-authority/v1"
POLICY_PATH = Path("contracts/release/public-release-v1.json")
WORKFLOW_PATH = Path(".github/workflows/release.yml")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
USES = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*([^#\s]+)")
GENERATED_RELEASE_PREFIXES = (
    "acceptance-readback/",
    "apps/desktop-ui/out/",
    "build/",
    "core/api/console_dist/",
    "gh-release-readback/",
    "marvisx_cli.egg-info/",
    "pypi-readback/",
    "release-artifact/",
)
GENERATED_TRACKED_DELETIONS = frozenset({"core/api/console_dist/.gitkeep"})
_FILE_CHUNK_BYTES = 1024 * 1024
_TRUSTED_PUBLISHER_RECEIPT_ENV = "MARVIS_PYPI_TRUSTED_PUBLISHER_RECEIPT"
_APPROVAL_WATCHDOG_RECEIPT_ENV = "MARVIS_APPROVAL_WATCHDOG_RECEIPT"
_SHARED_SOURCE_RECEIPT_ENV = "MARVIS_SHARED_SOURCE_OWNER_RECEIPT"


class ReleasePolicyError(RuntimeError):
    pass


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_FILE_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleasePolicyError(f"invalid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ReleasePolicyError(f"JSON root must be an object: {path}")
    return value


def _validated_manifest(path: Path) -> dict[str, Any]:
    manifest = _load_json(path)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ReleasePolicyError("unsupported release manifest")
    claimed = manifest.get("content_digest")
    unsigned = dict(manifest)
    unsigned.pop("content_digest", None)
    if claimed != _sha_bytes(_canonical(unsigned)):
        raise ReleasePolicyError("release manifest content digest mismatch")
    return manifest


def _manifest_artifacts(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = manifest.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise ReleasePolicyError("release manifest artifact inventory is empty")
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReleasePolicyError("release manifest artifact inventory is invalid")
        filename = row.get("filename")
        size = row.get("size")
        sha256 = row.get("sha256")
        if (
            not isinstance(filename, str)
            or not filename
            or PurePosixPath(filename).name != filename
            or filename in {".", ".."}
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(sha256, str)
            or not SHA256.fullmatch(sha256)
        ):
            raise ReleasePolicyError("release manifest artifact inventory is invalid")
        if filename in result:
            raise ReleasePolicyError("release manifest contains duplicate artifact names")
        result[filename] = row
    return result


def load_policy(root: Path) -> dict[str, Any]:
    policy = _load_json(root / POLICY_PATH)
    if policy.get("schema") != POLICY_SCHEMA:
        raise ReleasePolicyError("unsupported public release policy")
    return policy


def _candidate_state(
    policy: dict[str, Any], *, shared_source: dict[str, Any]
) -> dict[str, str]:
    """Validate whether the reviewed candidate may still enter release gates."""
    state = policy.get("candidate_state")
    if not isinstance(state, dict):
        raise ReleasePolicyError("candidate state is missing")
    status = state.get("status")
    if status == "active":
        if set(state) != {"status"}:
            raise ReleasePolicyError("active candidate state contains stale evidence")
        return {"status": "active"}
    expected_keys = {
        "status",
        "reason",
        "invalidated_by_shared_source_sha",
        "required_next_gate",
    }
    if status != "invalidated" or set(state) != expected_keys:
        raise ReleasePolicyError("candidate state is invalid")
    source_sha = str(state.get("invalidated_by_shared_source_sha") or "")
    if (
        state.get("reason") != "shared_source_advanced_after_release_foundation"
        or source_sha != shared_source["merge_sha"]
        or state.get("required_next_gate")
        != "merge_product_projection_then_refresh_release_foundation"
    ):
        raise ReleasePolicyError("candidate invalidation evidence is inconsistent")
    return {
        "status": "invalidated",
        "reason": str(state["reason"]),
        "invalidated_by_shared_source_sha": source_sha,
        "required_next_gate": str(state["required_next_gate"]),
    }


def candidate_state_report(root: Path) -> dict[str, Any]:
    """Return one validated, non-secret release-state classification."""
    root = root.resolve()
    policy = load_policy(root)
    shared_source = _shared_source_coordinates(root, policy)
    state = _candidate_state(policy, shared_source=shared_source)
    return {
        "status": state["status"],
        "state": state,
        "shared_source_sha": shared_source["merge_sha"],
    }


def _git(root: Path, *args: str, text: bool = True) -> str | bytes:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=text,
    )
    return result.stdout.strip() if text else result.stdout


def _project_version(root: Path) -> str:
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
            "project"
        ]
        return str(project["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ReleasePolicyError("pyproject version is not reviewable") from exc


def _validate_pyproject_version_only(root: Path, base: str) -> None:
    """Prove the release delta changes only project.version in pyproject."""
    try:
        base_document = tomllib.loads(str(_git(root, "show", f"{base}:pyproject.toml")))
        candidate_document = tomllib.loads(
            (root / "pyproject.toml").read_text(encoding="utf-8")
        )
        base_version = base_document["project"]["version"]
        candidate_document["project"]["version"] = base_version
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise ReleasePolicyError("pyproject release delta is not reviewable") from exc
    if candidate_document != base_document:
        raise ReleasePolicyError(
            "pyproject release delta changes behavior beyond project.version"
        )


def _parsed_versions(values: list[str]) -> list[Version]:
    from packaging.version import InvalidVersion, Version

    parsed: list[Version] = []
    for value in values:
        normalized = value.removeprefix("v")
        try:
            parsed.append(Version(normalized))
        except InvalidVersion:
            continue
    return parsed


def _require_candidate_above_history(
    policy: dict[str, Any], values: list[str], *, authority: str
) -> None:
    from packaging.version import InvalidVersion, Version

    try:
        candidate = Version(str(policy["candidate_version"]))
    except (KeyError, InvalidVersion) as exc:
        raise ReleasePolicyError("candidate version is invalid") from exc
    historical = _parsed_versions(values)
    if historical and candidate <= max(historical):
        raise ReleasePolicyError(
            f"candidate version is not above all {authority} identities"
        )


def _require_local_version_order(root: Path, policy: dict[str, Any]) -> None:
    candidate_tag = str(policy.get("candidate_tag") or "")
    tags = [
        line
        for line in str(_git(root, "tag", "--list", "v*")).splitlines()
        if line and line != candidate_tag
    ]
    _require_candidate_above_history(policy, tags, authority="Git tag")


def _path_allowed(path: str, allowlist: list[str]) -> bool:
    return any(path == item or (item.endswith("/") and path.startswith(item)) for item in allowlist)


def _changed_paths(root: Path, base: str, head: str) -> list[str]:
    raw = str(_git(root, "diff", "--name-only", f"{base}..{head}"))
    return sorted(line for line in raw.splitlines() if line)


def _require_release_controls_committed(root: Path, resolved_head: str) -> None:
    tracked_changes: dict[str, str] = {}
    for line in str(
        _git(root, "diff", "--name-status", resolved_head, "--")
    ).splitlines():
        fields = line.split("\t")
        if len(fields) < 2 or not fields[0] or any(not path for path in fields[1:]):
            raise ReleasePolicyError("tracked source-change inventory is invalid")
        for path in fields[1:]:
            tracked_changes[path] = fields[0]
    untracked = {
        line
        for line in str(
            _git(root, "ls-files", "--others", "--exclude-standard")
        ).splitlines()
        if line
    }
    unexpected_untracked = {
        path
        for path in untracked
        if not path.startswith(GENERATED_RELEASE_PREFIXES)
    }
    allowed_tracked_deletions = {
        path
        for path, status in tracked_changes.items()
        if status == "D" and path in GENERATED_TRACKED_DELETIONS
    }
    unexpected = sorted(
        (set(tracked_changes) - allowed_tracked_deletions) | unexpected_untracked
    )
    if unexpected:
        raise ReleasePolicyError(
            f"release source tree differs from the checked-out commit: {unexpected}"
        )


def _action_pins(workflow: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for value in USES.findall(workflow):
        if value.startswith("./"):
            continue
        if "@" not in value:
            raise ReleasePolicyError(f"action has no immutable ref: {value}")
        name, ref = value.rsplit("@", 1)
        if not SHA40.fullmatch(ref):
            raise ReleasePolicyError(f"action is not pinned to a full commit: {value}")
        prior = pins.setdefault(name, ref)
        if prior != ref:
            raise ReleasePolicyError(f"action uses two commits in one workflow: {name}")
    return pins


def _active_workflow_lines(workflow: str) -> set[str]:
    return {
        line.strip()
        for line in workflow.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _publisher_coordinates(policy: dict[str, Any]) -> dict[str, str]:
    publisher = policy.get("trusted_publisher") or {}
    keys = ("project", "owner", "repository", "workflow", "environment")
    result = {key: str(publisher.get(key) or "") for key in keys}
    if not all(result.values()):
        raise ReleasePolicyError("trusted-publisher coordinates are incomplete")
    return result


def publisher_coordinates_sha256(policy: dict[str, Any]) -> str:
    return _sha_bytes(_canonical(_publisher_coordinates(policy)))


def _simple_yaml_value(path: Path, key: str) -> str:
    pattern = re.compile(rf"^{re.escape(key)}:\s*([^#\s]+)\s*(?:#.*)?$")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ReleasePolicyError(f"shared-source engine pin is unreadable: {path}") from exc
    matches = [match.group(1) for line in lines if (match := pattern.match(line))]
    if len(matches) != 1:
        raise ReleasePolicyError(f"shared-source engine pin has no unique {key}")
    return matches[0]


def _shared_source_coordinates(root: Path, policy: dict[str, Any]) -> dict[str, Any]:
    source = policy.get("shared_source") or {}
    repository = str(source.get("repository") or "")
    candidate_sha = str(source.get("candidate_sha") or "")
    merge_sha = str(source.get("merge_sha") or "")
    pull_request = source.get("pull_request")
    engine_pin_value = str(source.get("engine_pin") or "")
    engine_pin = PurePosixPath(engine_pin_value)
    if (
        not repository
        or not isinstance(pull_request, int)
        or isinstance(pull_request, bool)
        or pull_request < 1
        or not SHA40.fullmatch(candidate_sha)
        or not SHA40.fullmatch(merge_sha)
        or engine_pin.is_absolute()
        or ".." in engine_pin.parts
        or engine_pin.name == ""
    ):
        raise ReleasePolicyError("shared-source coordinates are invalid")
    engine_pin_path = root / Path(*engine_pin.parts)
    if _simple_yaml_value(engine_pin_path, "engine_ref") != merge_sha:
        raise ReleasePolicyError("shared-source merge differs from the engine pin")
    return {
        "repository": repository,
        "pull_request": pull_request,
        "candidate_sha": candidate_sha,
        "merge_sha": merge_sha,
        "engine_pin": engine_pin_value,
        "engine_pin_sha256": _sha_file(engine_pin_path),
    }


def _shared_source_readback(
    root: Path,
    policy: dict[str, Any],
    *,
    token: str,
    api_url: str,
    now: datetime | None = None,
    receipt_raw: str | None = None,
) -> dict[str, Any]:
    del token, api_url
    expected = _shared_source_coordinates(root, policy)
    receipt = _external_receipt(
        receipt_raw
        if receipt_raw is not None
        else os.environ.get(_SHARED_SOURCE_RECEIPT_ENV),
        kind="shared_source_owner_readback",
    )
    expected_coordinates = {
        key: expected[key]
        for key in (
            "repository",
            "pull_request",
            "candidate_sha",
            "merge_sha",
            "engine_pin",
            "engine_pin_sha256",
        )
    }
    expected_coordinates_sha256 = _sha_bytes(_canonical(expected_coordinates))
    observed_coordinates = {key: receipt.get(key) for key in expected_coordinates}
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    verified_at = _parse_time(str(receipt.get("verified_at") or ""))
    merged_at = _parse_time(str(receipt.get("merged_at") or ""))
    age = (current - verified_at).total_seconds()
    expected_verifier = (
        f"github-user:{policy['github_environment']['required_reviewer_login']}"
    )
    if (
        receipt.get("status") != "verified"
        or receipt.get("state") != "closed"
        or observed_coordinates != expected_coordinates
        or receipt.get("coordinates_sha256") != expected_coordinates_sha256
        or receipt.get("verified_by") != expected_verifier
        or age < 0
        or age > 24 * 3600
        or merged_at > verified_at
    ):
        raise ReleasePolicyError(
            "shared-source owner readback is stale or differs from release policy"
        )
    return {
        **expected,
        "state": receipt["state"],
        "merged_at": receipt["merged_at"],
        "verified_at": receipt["verified_at"],
        "verified_by": receipt["verified_by"],
        "receipt_sha256": _sha_bytes(_canonical(receipt)),
    }


def _release_foundation_coordinates(
    root: Path, policy: dict[str, Any]
) -> dict[str, Any]:
    foundation = policy.get("release_foundation") or {}
    repository = str(foundation.get("repository") or "")
    candidate_sha = str(foundation.get("candidate_sha") or "")
    merge_sha = str(foundation.get("merge_sha") or "")
    pull_request = foundation.get("pull_request")
    expected_paths = foundation.get("expected_changed_paths")
    product_base = str(policy.get("plan_b_product_base_sha") or "")
    if (
        repository != str(policy.get("repository") or "")
        or not isinstance(pull_request, int)
        or isinstance(pull_request, bool)
        or pull_request < 1
        or not SHA40.fullmatch(candidate_sha)
        or not SHA40.fullmatch(merge_sha)
        or not SHA40.fullmatch(product_base)
        or not isinstance(expected_paths, list)
        or not expected_paths
        or any(not isinstance(path, str) or not path for path in expected_paths)
        or expected_paths != sorted(set(expected_paths))
    ):
        raise ReleasePolicyError("release-foundation coordinates are invalid")
    for path in expected_paths:
        parsed = PurePosixPath(path)
        if parsed.is_absolute() or ".." in parsed.parts:
            raise ReleasePolicyError("release-foundation path is invalid")
    for label, sha in (
        ("Plan B product base", product_base),
        ("release-foundation candidate", candidate_sha),
        ("release-foundation merge", merge_sha),
    ):
        try:
            resolved = str(_git(root, "rev-parse", "--verify", f"{sha}^{{commit}}"))
        except subprocess.CalledProcessError as exc:
            raise ReleasePolicyError(f"{label} commit is unavailable") from exc
        if resolved != sha:
            raise ReleasePolicyError(f"{label} commit identity is ambiguous")
    parents = str(_git(root, "rev-list", "--parents", "-n", "1", merge_sha)).split()
    if parents != [merge_sha, product_base, candidate_sha]:
        raise ReleasePolicyError(
            "release-foundation merge does not join the exact Plan B base and candidate"
        )
    observed_paths = _changed_paths(root, product_base, merge_sha)
    if observed_paths != expected_paths:
        raise ReleasePolicyError(
            "release-foundation changed paths differ from release policy"
        )
    return {
        "repository": repository,
        "pull_request": pull_request,
        "candidate_sha": candidate_sha,
        "merge_sha": merge_sha,
        "changed_paths": observed_paths,
    }


def _release_foundation_readback(
    root: Path,
    policy: dict[str, Any],
    *,
    token: str,
    api_url: str,
) -> dict[str, Any]:
    expected = _release_foundation_coordinates(root, policy)
    pull = _request_json(
        f"{api_url}/repos/{expected['repository']}/pulls/{expected['pull_request']}",
        token=token,
    )
    if (
        pull.get("state") != "closed"
        or not pull.get("merged_at")
        or (pull.get("head") or {}).get("sha") != expected["candidate_sha"]
        or pull.get("merge_commit_sha") != expected["merge_sha"]
    ):
        raise ReleasePolicyError(
            "release-foundation PR identity differs from release policy"
        )
    return {
        **expected,
        "state": pull.get("state"),
        "merged_at": pull.get("merged_at"),
    }


def _release_job_guards(workflow: str) -> dict[str, str]:
    guards: dict[str, str] = {}
    in_jobs = False
    current: str | None = None
    for raw in workflow.splitlines():
        if raw == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        job = re.match(r"^  ([a-z0-9-]+):\s*$", raw)
        if job:
            current = job.group(1)
            continue
        guard = re.match(r"^    if:\s*(.+?)\s*$", raw)
        if current and guard:
            guards[current] = guard.group(1)
    return guards


def _workflow_job_blocks(workflow: str) -> dict[str, str]:
    lines = workflow.splitlines()
    starts: list[tuple[str, int]] = []
    in_jobs = False
    for index, raw in enumerate(lines):
        if raw == "jobs:":
            in_jobs = True
            continue
        if not in_jobs:
            continue
        job = re.match(r"^  ([a-z0-9-]+):\s*$", raw)
        if job:
            starts.append((job.group(1), index))
    result: dict[str, str] = {}
    for offset, (name, start) in enumerate(starts):
        end = starts[offset + 1][1] if offset + 1 < len(starts) else len(lines)
        result[name] = "\n".join(lines[start:end])
    return result


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleasePolicyError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ReleasePolicyError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _external_receipt(raw: str | None, *, kind: str) -> dict[str, Any]:
    if not raw:
        raise ReleasePolicyError(f"external {kind} receipt is absent")
    try:
        receipt = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReleasePolicyError(f"external {kind} receipt is invalid") from exc
    if not isinstance(receipt, dict) or receipt.get("schema") != EXTERNAL_RECEIPT_SCHEMA:
        raise ReleasePolicyError(f"external {kind} receipt schema is invalid")
    if receipt.get("kind") != kind:
        raise ReleasePolicyError(f"external {kind} receipt kind is invalid")
    return receipt


def _external_receipts(
    *,
    trusted_publisher_raw: str | None = None,
    approval_watchdog_raw: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        _external_receipt(
            trusted_publisher_raw
            if trusted_publisher_raw is not None
            else os.environ.get(_TRUSTED_PUBLISHER_RECEIPT_ENV),
            kind="trusted_publisher_owner_readback",
        ),
        _external_receipt(
            approval_watchdog_raw
            if approval_watchdog_raw is not None
            else os.environ.get(_APPROVAL_WATCHDOG_RECEIPT_ENV),
            kind="approval_watchdog",
        ),
    )


def _strict_external_receipts(
    policy: dict[str, Any],
    *,
    release_source_sha: str,
    now: datetime | None = None,
    trusted_publisher_receipt: dict[str, Any] | None = None,
    approval_watchdog_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not SHA40.fullmatch(release_source_sha):
        raise ReleasePolicyError("release source SHA for external receipts is invalid")
    if trusted_publisher_receipt is None or approval_watchdog_receipt is None:
        trusted_publisher_receipt, approval_watchdog_receipt = _external_receipts()
    readback = trusted_publisher_receipt
    if readback.get("status") != "verified":
        raise ReleasePolicyError("PyPI trusted-publisher owner readback is not verified")
    if readback.get("coordinates_sha256") != publisher_coordinates_sha256(policy):
        raise ReleasePolicyError("trusted-publisher readback coordinates do not match policy")
    verified_at = _parse_time(str(readback.get("verified_at") or ""))
    age = (current - verified_at).total_seconds()
    if age < 0 or age > 24 * 3600:
        raise ReleasePolicyError("trusted-publisher owner readback is stale")
    if not readback.get("verified_by"):
        raise ReleasePolicyError("trusted-publisher readback has no verifier")

    watchdog = approval_watchdog_receipt
    if watchdog.get("status") != "ready" or not watchdog.get("receipt_ref"):
        raise ReleasePolicyError("external approval watchdog has no ready receipt")
    if watchdog.get("late_approval_upload_guard") is not True:
        raise ReleasePolicyError("late-approval upload guard is disabled")
    expected_watchdog = policy.get("approval_watchdog") or {}
    observed_watchdog = {key: watchdog.get(key) for key in expected_watchdog}
    if observed_watchdog != expected_watchdog:
        raise ReleasePolicyError("approval watchdog receipt coordinates do not match policy")
    if watchdog.get("target_head_sha") != release_source_sha:
        raise ReleasePolicyError("approval watchdog targets another release source")
    watchdog_verified_at = _parse_time(str(watchdog.get("verified_at") or ""))
    watchdog_age = (current - watchdog_verified_at).total_seconds()
    if watchdog_age < 0 or watchdog_age > 24 * 3600:
        raise ReleasePolicyError("approval watchdog receipt is stale")
    if not watchdog.get("verified_by"):
        raise ReleasePolicyError("approval watchdog receipt has no verifier")
    write_authority = watchdog.get("write_authority")
    if not isinstance(write_authority, dict):
        raise ReleasePolicyError("approval watchdog has no write-authority proof")
    if set(write_authority) != {
        "schema",
        "status",
        "repository",
        "canary_workflow",
        "environment",
        "target_head_sha",
        "nonce",
        "verified_at",
        "verified_by",
        "capabilities",
        "worker_attestation",
    }:
        raise ReleasePolicyError("watchdog write-authority proof shape is invalid")
    expected_canary_workflow = expected_watchdog.get(
        "write_authority_canary_workflow"
    )
    expected_verifier = (
        f"github-user:{policy['github_environment']['required_reviewer_login']}"
    )
    expected_proof_coordinates = {
        "schema": WATCHDOG_WRITE_AUTHORITY_SCHEMA,
        "status": "verified",
        "repository": expected_watchdog.get("repository"),
        "canary_workflow": expected_canary_workflow,
        "environment": expected_watchdog.get("environment"),
        "target_head_sha": release_source_sha,
        "verified_by": expected_verifier,
    }
    observed_proof_coordinates = {
        key: write_authority.get(key) for key in expected_proof_coordinates
    }
    if observed_proof_coordinates != expected_proof_coordinates:
        raise ReleasePolicyError("watchdog write-authority coordinates do not match")
    proof_nonce = write_authority.get("nonce")
    if not isinstance(proof_nonce, str) or re.fullmatch(
        r"[A-Za-z0-9_-]{16,64}", proof_nonce
    ) is None:
        raise ReleasePolicyError("watchdog write-authority nonce is invalid")
    proof_verified_at = _parse_time(
        str(write_authority.get("verified_at") or "")
    )
    proof_age = (current - proof_verified_at).total_seconds()
    if (
        proof_age < 0
        or proof_age > 24 * 3600
        or proof_verified_at > watchdog_verified_at
    ):
        raise ReleasePolicyError("watchdog write-authority proof is stale or unordered")
    capabilities = write_authority.get("capabilities")
    if not isinstance(capabilities, dict) or set(capabilities) != {
        "reject_pending_deployment",
        "cancel_workflow_run",
    }:
        raise ReleasePolicyError("watchdog write-authority capabilities are incomplete")
    reject_proof = capabilities["reject_pending_deployment"]
    cancel_proof = capabilities["cancel_workflow_run"]
    if not isinstance(reject_proof, dict) or not isinstance(cancel_proof, dict):
        raise ReleasePolicyError("watchdog write-authority capability proof is invalid")
    if set(reject_proof) != {
        "status",
        "mode",
        "nonce",
        "run_id",
        "run_url",
        "observed_state",
    } or set(cancel_proof) != {
        "status",
        "mode",
        "nonce",
        "run_id",
        "run_url",
        "observed_conclusion",
    }:
        raise ReleasePolicyError("watchdog write-authority capability proof is invalid")
    reject_run_id = reject_proof.get("run_id")
    cancel_run_id = cancel_proof.get("run_id")
    if any(
        isinstance(run_id, bool)
        or not isinstance(run_id, int)
        or run_id <= 0
        for run_id in (reject_run_id, cancel_run_id)
    ) or reject_run_id == cancel_run_id:
        raise ReleasePolicyError("watchdog write-authority run identity is invalid")
    expected_run_url = (
        f"https://github.com/{expected_watchdog.get('repository')}/actions/runs/"
    )
    if (
        reject_proof.get("status") != "verified"
        or reject_proof.get("mode") != "reject-pending-deployment"
        or reject_proof.get("nonce") != proof_nonce
        or reject_proof.get("observed_state") != "rejected"
        or reject_proof.get("run_url") != f"{expected_run_url}{reject_run_id}"
        or cancel_proof.get("status") != "verified"
        or cancel_proof.get("mode") != "cancel-workflow-run"
        or cancel_proof.get("nonce") != proof_nonce
        or cancel_proof.get("observed_conclusion") != "cancelled"
        or cancel_proof.get("run_url") != f"{expected_run_url}{cancel_run_id}"
    ):
        raise ReleasePolicyError("watchdog write-authority readback is invalid")
    worker_attestation = write_authority.get("worker_attestation")
    if not isinstance(worker_attestation, dict) or set(worker_attestation) != {
        "algorithm",
        "worker_version_id",
        "signature",
    }:
        raise ReleasePolicyError("watchdog write-authority attestation is invalid")
    attestation_version = worker_attestation.get("worker_version_id")
    attestation_signature = worker_attestation.get("signature")
    if (
        worker_attestation.get("algorithm") != "HMAC-SHA256"
        or not isinstance(attestation_version, str)
        or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", attestation_version) is None
        or not isinstance(attestation_signature, str)
        or SHA256.fullmatch(attestation_signature) is None
    ):
        raise ReleasePolicyError("watchdog write-authority attestation is invalid")
    write_authority_sha256 = _sha_bytes(_canonical(write_authority))
    if watchdog.get("write_authority_sha256") != write_authority_sha256:
        raise ReleasePolicyError("watchdog write-authority digest does not match")
    active_until = _parse_time(str(watchdog.get("active_until") or ""))
    required_until = current + timedelta(
        hours=int(policy["approval_deadline_hours"])
    )
    if active_until < required_until:
        raise ReleasePolicyError(
            "approval watchdog does not cover the full approval window"
        )
    worker_version = watchdog.get("worker_version")
    if not isinstance(worker_version, dict):
        raise ReleasePolicyError("approval watchdog has no Worker version identity")
    worker_version_id = worker_version.get("id")
    worker_version_timestamp = worker_version.get("timestamp")
    if not isinstance(worker_version_id, str) or not worker_version_id.strip():
        raise ReleasePolicyError("approval watchdog Worker version id is invalid")
    if not isinstance(worker_version_timestamp, str):
        raise ReleasePolicyError("approval watchdog Worker version timestamp is invalid")
    _parse_time(worker_version_timestamp)
    expected_receipt_prefix = (
        "cloudflare-worker://marvis-oss-release-watchdog-041/"
        f"{worker_version_id}/"
    )
    receipt_ref = str(watchdog["receipt_ref"])
    receipt_digest = receipt_ref.removeprefix(expected_receipt_prefix)
    if (
        not receipt_ref.startswith(expected_receipt_prefix)
        or not SHA256.fullmatch(receipt_digest)
    ):
        raise ReleasePolicyError(
            "approval watchdog receipt does not bind its Worker version"
        )
    summaries = {
        "trusted_publisher": {
            "sha256": _sha_bytes(_canonical(readback)),
            "verified_at": readback["verified_at"],
            "verified_by": readback["verified_by"],
            "coordinates_sha256": readback["coordinates_sha256"],
        },
        "approval_watchdog": {
            "sha256": _sha_bytes(_canonical(watchdog)),
            "verified_at": watchdog["verified_at"],
            "verified_by": watchdog["verified_by"],
            "receipt_ref": receipt_ref,
            "target_head_sha": watchdog["target_head_sha"],
            "active_until": watchdog["active_until"],
            "worker_version": worker_version,
            "write_authority_sha256": write_authority_sha256,
        },
    }
    return summaries


def _tag_target(root: Path, tag: str) -> str | None:
    value = str(_git(root, "tag", "--list", tag))
    if not value:
        return None
    return str(_git(root, "rev-list", "-n", "1", tag))


def _validate_tag_trigger(tag: str, trigger_ref: str | None) -> None:
    if trigger_ref is None:
        if os.environ.get("GITHUB_ACTIONS") == "true":
            raise ReleasePolicyError("tag build has no GitHub trigger ref")
        return
    expected = f"refs/tags/{tag}"
    if trigger_ref != expected:
        raise ReleasePolicyError(
            f"tag build was triggered by {trigger_ref}, expected {expected}"
        )


def validate_static(
    root: Path,
    *,
    head: str = "HEAD",
    tag_build: bool = False,
    trigger_ref: str | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    policy = load_policy(root)
    release_branch = str(policy.get("release_branch") or "")
    if not release_branch or release_branch.startswith("refs/"):
        raise ReleasePolicyError("release branch is invalid")
    base = str(policy.get("plan_b_product_base_sha") or "")
    if not SHA40.fullmatch(base):
        raise ReleasePolicyError("Plan B product base is not a full commit")
    release_foundation = _release_foundation_coordinates(root, policy)
    shared_source = _shared_source_coordinates(root, policy)
    candidate_state = _candidate_state(policy, shared_source=shared_source)
    if candidate_state["status"] == "invalidated":
        raise ReleasePolicyError(
            "release candidate is invalidated; merge the product projection "
            "and refresh the exact release foundation"
        )
    receipt_policy = policy.get("external_receipts") or {}
    if (
        receipt_policy.get("schema") != EXTERNAL_RECEIPT_SCHEMA
        or receipt_policy.get("trusted_publisher_variable")
        != _TRUSTED_PUBLISHER_RECEIPT_ENV
        or receipt_policy.get("approval_watchdog_variable")
        != _APPROVAL_WATCHDOG_RECEIPT_ENV
        or receipt_policy.get("shared_source_variable")
        != _SHARED_SOURCE_RECEIPT_ENV
    ):
        raise ReleasePolicyError("external receipt provider contract is invalid")
    resolved_head = str(_git(root, "rev-parse", head))
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base, resolved_head],
        capture_output=True,
    )
    if ancestor.returncode != 0:
        raise ReleasePolicyError("release source does not descend from exact Plan B merge")
    foundation_ancestor = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "merge-base",
            "--is-ancestor",
            release_foundation["merge_sha"],
            resolved_head,
        ],
        capture_output=True,
    )
    if foundation_ancestor.returncode != 0:
        raise ReleasePolicyError(
            "release source does not descend from exact release foundation"
        )
    _validate_pyproject_version_only(root, release_foundation["merge_sha"])
    _require_local_version_order(root, policy)

    allowlist = policy.get("allowed_release_delta")
    if not isinstance(allowlist, list) or not allowlist:
        raise ReleasePolicyError("release delta allowlist is empty")
    changed = _changed_paths(root, release_foundation["merge_sha"], resolved_head)
    disallowed = [path for path in changed if not _path_allowed(path, allowlist)]
    if disallowed:
        raise ReleasePolicyError(f"product behavior entered release-only delta: {disallowed}")

    version = _project_version(root)
    expected = str(policy.get("candidate_version") or "")
    tag = str(policy.get("candidate_tag") or "")
    if version != expected or tag != f"v{version}":
        raise ReleasePolicyError("candidate version/tag does not match pyproject")
    if tag_build:
        _validate_tag_trigger(tag, trigger_ref or os.environ.get("GITHUB_REF"))
    failed_versions = policy.get("historical_failed_versions")
    if (
        not isinstance(failed_versions, list)
        or not failed_versions
        or any(not isinstance(item, str) or not item for item in failed_versions)
        or len(set(failed_versions)) != len(failed_versions)
    ):
        raise ReleasePolicyError("historical failed version inventory is invalid")
    legacy_failed_version = str(policy.get("historical_failed_version") or "")
    if legacy_failed_version and legacy_failed_version not in failed_versions:
        raise ReleasePolicyError("historical failed version inventory is incomplete")
    if version in failed_versions:
        raise ReleasePolicyError("historical failed version cannot be reused")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if f"## [{version}]" not in changelog:
        raise ReleasePolicyError("candidate version is absent from CHANGELOG")
    if not (root / f"docs/releases/{version}.md").is_file():
        raise ReleasePolicyError("candidate release notes are missing")

    existing_target = _tag_target(root, tag)
    if existing_target is not None and (not tag_build or existing_target != resolved_head):
        raise ReleasePolicyError(f"candidate tag already exists at {existing_target}")
    if tag_build and existing_target != resolved_head:
        raise ReleasePolicyError("tag build does not resolve to release source")

    workflow_path = root / WORKFLOW_PATH
    workflow = workflow_path.read_text(encoding="utf-8")
    observed_pins = _action_pins(workflow)
    expected_pins = policy.get("action_pins") or {}
    if observed_pins != expected_pins:
        raise ReleasePolicyError(
            f"workflow action pins differ from policy: {observed_pins} != {expected_pins}"
        )
    node_image = str((policy.get("build") or {}).get("node_image") or "")
    if not re.fullmatch(r"node:[^@\s]+@sha256:[0-9a-f]{64}", node_image):
        raise ReleasePolicyError("Node build image is not digest pinned")
    required_fragments = (
        node_image,
        "pull_request:",
        "workflow_dispatch:",
        'tags: ["v*"]',
        "pip install --require-hashes -r requirements-release.lock",
        "python -m build --no-isolation",
        "release_candidate.py preflight",
        "release_candidate.py tag-preflight",
        "release_candidate.py pretag",
        "release_candidate.py publish-window",
        "installed-upgrade",
        "MARVIS_PYPI_TRUSTED_PUBLISHER_RECEIPT",
        "MARVIS_APPROVAL_WATCHDOG_RECEIPT",
        "MARVIS_SHARED_SOURCE_OWNER_RECEIPT",
        "GH_REPO: ${{ github.repository }}",
        "environment: pypi",
        "pypa/gh-action-pypi-publish@" + expected_pins.get("pypa/gh-action-pypi-publish", ""),
    )
    missing = [fragment for fragment in required_fragments if fragment not in workflow]
    if missing:
        raise ReleasePolicyError(f"release workflow contract fragments missing: {missing}")
    active_lines = _active_workflow_lines(workflow)
    required_lines = {
        "python scripts/test_release_candidate.py",
        'if [[ "$GITHUB_EVENT_NAME" == "push" && "$GITHUB_REF" == refs/tags/v* ]]; then',
        "if: ${{ github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v') }}",
        "python scripts/release_candidate.py tag-preflight \\",
        'desktop_source_sha="$(git rev-parse HEAD)"',
        'SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"',
        '-e MARVIS_CONSOLE_BUILD_ID="$desktop_source_sha" \\',
        '-e SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" \\',
        "bash -lc 'npm ci && npm run test:build-id && npm run audit:production && npm run build'",
        'export SOURCE_DATE_EPOCH="$(git show -s --format=%ct HEAD)"',
        "python scripts/release_candidate.py registry-download \\",
        "python scripts/release_candidate.py acceptance \\",
        'gh release upload "$GITHUB_REF_NAME" acceptance/release-acceptance.json',
        '"$python_bin" "$GITHUB_WORKSPACE/scripts/release_candidate.py" \\',
        "installed-upgrade \\",
        '"$marvis" doctor --offline',
        '"$marvis" hooks install',
        '"$marvis" mcp register',
        'assert hooks["fully_installed"] is True',
        'assert status["db_ok"] is True',
        'assert mcp["connected"] is True',
        'assert mcp["tool_count"] > 0',
        "MARVIS_PYPI_TRUSTED_PUBLISHER_RECEIPT: ${{ vars.MARVIS_PYPI_TRUSTED_PUBLISHER_RECEIPT }}",
        "MARVIS_APPROVAL_WATCHDOG_RECEIPT: ${{ vars.MARVIS_APPROVAL_WATCHDOG_RECEIPT }}",
        "MARVIS_SHARED_SOURCE_OWNER_RECEIPT: ${{ vars.MARVIS_SHARED_SOURCE_OWNER_RECEIPT }}",
        "GH_REPO: ${{ github.repository }}",
        "if: ${{ always() && github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v') && (needs.build.result != 'success' || needs.release-record.result != 'success' || needs.prepublish.result != 'success' || needs.pypi.result != 'success' || needs.accept.result != 'success' || needs.finalize.result != 'success') }}",
        'echo "::error::Refusing to mutate an already-final GitHub Release."',
        'echo "::error::The failed candidate exists on PyPI; owner verification and yank are required."',
    }
    missing_lines = sorted(required_lines - active_lines)
    if missing_lines:
        raise ReleasePolicyError(
            f"release workflow active contract lines missing: {missing_lines}"
        )
    if "pip install --quiet --upgrade build" in workflow:
        raise ReleasePolicyError("release workflow installs mutable build tooling")
    if '--force-reinstall "marvisx-cli==0.3.8"' in workflow:
        raise ReleasePolicyError("release workflow presents package reinstall as data rollback")
    tag_guard = "${{ github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v') }}"
    guards = _release_job_guards(workflow)
    for job in ("release-record", "prepublish", "pypi", "accept", "finalize"):
        if guards.get(job) != tag_guard:
            raise ReleasePolicyError(f"{job} is not confined to a tag-push event")
    contain_guard = guards.get("contain") or ""
    if not contain_guard.startswith("${{ always() && github.event_name == 'push'"):
        raise ReleasePolicyError("contain is not confined to a tag-push event")
    blocks = _workflow_job_blocks(workflow)
    for privileged_job in ("release-record", "pypi", "finalize"):
        block = blocks.get(privileged_job) or ""
        if "actions/checkout@" in block or "scripts/" in block or "pip install" in block:
            raise ReleasePolicyError(
                f"{privileged_job} executes checkout or candidate code with write authority"
            )
    accept_block = blocks.get("accept") or ""
    if "contents: write" in accept_block or "contents: read" not in accept_block:
        raise ReleasePolicyError("acceptance does not use a read-only repository token")
    window_pos = workflow.find("release_candidate.py publish-window")
    publisher_pos = workflow.find("pypa/gh-action-pypi-publish@")
    if window_pos < 0 or publisher_pos < 0 or window_pos > publisher_pos:
        raise ReleasePolicyError("post-approval window check does not precede upload")

    lock = (root / str((policy.get("build") or {}).get("requirements") or "")).read_text(
        encoding="utf-8"
    )
    for requirement in ("build==", "packaging==", "pyproject-hooks==", "setuptools==", "wheel=="):
        if requirement not in lock:
            raise ReleasePolicyError(f"release build lock missing {requirement}")
    if lock.count("--hash=sha256:") < 5:
        raise ReleasePolicyError("release build lock is not hash closed")

    external_blockers: list[str] = []
    if not os.environ.get(_TRUSTED_PUBLISHER_RECEIPT_ENV):
        external_blockers.append("trusted_publisher_owner_readback")
    if not os.environ.get(_APPROVAL_WATCHDOG_RECEIPT_ENV):
        external_blockers.append("external_approval_watchdog")
    if not os.environ.get(_SHARED_SOURCE_RECEIPT_ENV):
        external_blockers.append("shared_source_owner_readback")
    return {
        "schema": "marvis-public-release-static-preflight/v1",
        "product_base_sha": base,
        "release_foundation": release_foundation,
        "shared_source": shared_source,
        "release_source_sha": resolved_head,
        "release_branch": release_branch,
        "version": version,
        "tag": tag,
        "changed_paths": changed,
        "action_pins": observed_pins,
        "workflow_sha256": _sha_file(workflow_path),
        "policy_sha256": _sha_file(root / POLICY_PATH),
        "external_blockers": external_blockers,
        "tag_build": tag_build,
        "status": "static_green_external_gates_open" if external_blockers else "green",
    }


def _request_json(url: str, *, token: str | None = None) -> dict[str, Any]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "marvis-release-preflight"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            value = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise FileNotFoundError(url) from exc
        raise ReleasePolicyError(f"remote preflight failed ({exc.code}): {url}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleasePolicyError(f"remote preflight unreadable: {url}") from exc
    if not isinstance(value, dict):
        raise ReleasePolicyError(f"remote preflight returned non-object: {url}")
    return value


def _require_remote_absence(
    url: str, *, label: str, token: str | None = None
) -> None:
    try:
        _request_json(url, token=token)
    except FileNotFoundError:
        return
    raise ReleasePolicyError(f"{label} already exists")


def _candidate_pypi_url(policy: dict[str, Any]) -> str:
    package = urllib.parse.quote(str(policy["package"]), safe="")
    version = urllib.parse.quote(str(policy["candidate_version"]), safe="")
    return f"https://pypi.org/pypi/{package}/{version}/json"


def _registry_history_check(policy: dict[str, Any]) -> dict[str, Any]:
    package = urllib.parse.quote(str(policy["package"]), safe="")
    registry = _request_json(f"https://pypi.org/pypi/{package}/json")
    releases = registry.get("releases")
    if not isinstance(releases, dict):
        raise ReleasePolicyError("PyPI version history is unavailable")
    _require_candidate_above_history(
        policy,
        [str(version) for version in releases],
        authority="PyPI",
    )
    return registry


def _remote_release_branch_sha(
    policy: dict[str, Any], *, token: str, api_url: str
) -> str:
    repository = str(policy["repository"])
    branch = urllib.parse.quote(str(policy["release_branch"]), safe="")
    remote_branch = _request_json(
        f"{api_url}/repos/{repository}/branches/{branch}", token=token
    )
    remote_sha = str((remote_branch.get("commit") or {}).get("sha") or "")
    if not SHA40.fullmatch(remote_sha):
        raise ReleasePolicyError("remote release-branch head is invalid")
    return remote_sha


def _require_remote_release_source(
    policy: dict[str, Any],
    release_source_sha: str,
    *,
    token: str,
    api_url: str,
    allow_branch_advance: bool = False,
) -> str:
    remote_sha = _remote_release_branch_sha(policy, token=token, api_url=api_url)
    if remote_sha == release_source_sha:
        return remote_sha
    if not allow_branch_advance:
        raise ReleasePolicyError(
            "release source is not the exact remote release-branch head"
        )
    repository = str(policy["repository"])
    branch = urllib.parse.quote(str(policy["release_branch"]), safe="")
    source = urllib.parse.quote(release_source_sha, safe="")
    try:
        comparison = _request_json(
            f"{api_url}/repos/{repository}/compare/{source}...{branch}", token=token
        )
    except FileNotFoundError as exc:
        raise ReleasePolicyError(
            "release source is not contained in remote release-branch history"
        ) from exc
    merge_base_sha = str((comparison.get("merge_base_commit") or {}).get("sha") or "")
    if comparison.get("status") != "ahead" or merge_base_sha != release_source_sha:
        raise ReleasePolicyError(
            "release source is not contained in remote release-branch history"
        )
    return remote_sha


def _workflow_dispatch_proof(
    policy: dict[str, Any],
    release_source_sha: str,
    *,
    token: str,
    api_url: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    repository = str(policy["repository"])
    workflow = urllib.parse.quote(_publisher_coordinates(policy)["workflow"], safe="")
    branch = urllib.parse.quote(str(policy["release_branch"]), safe="")
    payload = _request_json(
        f"{api_url}/repos/{repository}/actions/workflows/{workflow}/runs"
        f"?event=workflow_dispatch&branch={branch}&status=completed&per_page=100",
        token=token,
    )
    rows = payload.get("workflow_runs")
    if not isinstance(rows, list):
        raise ReleasePolicyError("workflow-dispatch proof inventory is unavailable")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidates = []
    for run in rows:
        if not isinstance(run, dict):
            continue
        if (
            run.get("head_sha") != release_source_sha
            or run.get("event") != "workflow_dispatch"
            or run.get("conclusion") != "success"
            or run.get("head_branch") != policy["release_branch"]
            or run.get("path") != str(WORKFLOW_PATH)
            or (run.get("head_repository") or {}).get("full_name") != repository
        ):
            continue
        completed_at = _parse_time(str(run.get("updated_at") or ""))
        age = (current - completed_at).total_seconds()
        if 0 <= age <= int(policy["approval_deadline_hours"]) * 3600:
            candidates.append((completed_at, run))
    if not candidates:
        raise ReleasePolicyError(
            "no fresh successful workflow_dispatch preflight exists for the release SHA"
        )
    completed_at, run = max(candidates, key=lambda item: item[0])
    run_id = run.get("id")
    attempt = run.get("run_attempt")
    if not isinstance(run_id, int) or not isinstance(attempt, int):
        raise ReleasePolicyError("workflow-dispatch proof identity is invalid")
    return {
        "id": run_id,
        "run_attempt": attempt,
        "head_sha": run["head_sha"],
        "head_branch": run["head_branch"],
        "event": run["event"],
        "conclusion": run["conclusion"],
        "path": run["path"],
        "completed_at": completed_at.isoformat().replace("+00:00", "Z"),
        "html_url": run.get("html_url"),
    }


def _environment_check(policy: dict[str, Any], *, token: str, api_url: str) -> dict[str, Any]:
    repository = str(policy["repository"])
    expected = policy["github_environment"]
    environment = _request_json(
        f"{api_url}/repos/{repository}/environments/{expected['name']}", token=token
    )
    if environment.get("can_admins_bypass") is not expected["can_admins_bypass"]:
        raise ReleasePolicyError("GitHub environment administrative bypass policy is unsafe")
    required_rules = [
        rule
        for rule in environment.get("protection_rules", [])
        if rule.get("type") == "required_reviewers"
    ]
    if len(required_rules) != 1:
        raise ReleasePolicyError("GitHub environment reviewer rule is ambiguous")
    reviewer_logins = {
        str((item.get("reviewer") or {}).get("login") or "")
        for item in required_rules[0].get("reviewers", [])
    }
    if reviewer_logins != {expected["required_reviewer_login"]}:
        raise ReleasePolicyError(
            "GitHub environment must have exactly the required owner reviewer"
        )
    required_rule = required_rules[0]
    if required_rule.get("prevent_self_review") is not expected["prevent_self_review"]:
        raise ReleasePolicyError("GitHub environment self-review policy drift")
    if "deployment_branch_policy" not in expected:
        raise ReleasePolicyError("GitHub environment branch policy is unspecified")
    if environment.get("deployment_branch_policy") != expected["deployment_branch_policy"]:
        raise ReleasePolicyError("GitHub environment deployment-branch policy drift")
    return {
        "name": environment.get("name"),
        "can_admins_bypass": environment.get("can_admins_bypass"),
        "reviewers": sorted(reviewer_logins),
        "prevent_self_review": required_rule.get("prevent_self_review"),
        "deployment_branch_policy": environment.get("deployment_branch_policy"),
    }


def _remote_tag_sha(policy: dict[str, Any], *, token: str, api_url: str) -> str:
    repository = str(policy["repository"])
    tag = str(policy["candidate_tag"])
    try:
        commit = _request_json(
            f"{api_url}/repos/{repository}/commits/{tag}", token=token
        )
    except FileNotFoundError as exc:
        raise ReleasePolicyError("candidate tag is absent from GitHub") from exc
    sha = str(commit.get("sha") or "")
    if not SHA40.fullmatch(sha):
        raise ReleasePolicyError("candidate tag does not resolve to a GitHub commit")
    return sha


def _draft_release(
    policy: dict[str, Any], *, token: str, api_url: str
) -> dict[str, Any]:
    repository = str(policy["repository"])
    tag = str(policy["candidate_tag"])
    try:
        release = _request_json(
            f"{api_url}/repos/{repository}/releases/tags/{tag}", token=token
        )
    except FileNotFoundError as exc:
        raise ReleasePolicyError("contained draft GitHub Release is absent") from exc
    if release.get("tag_name") != tag:
        raise ReleasePolicyError("draft GitHub Release names a different tag")
    if release.get("draft") is not True or release.get("prerelease") is not True:
        raise ReleasePolicyError("GitHub Release became final before registry acceptance")
    return {
        "id": release.get("id"),
        "tag_name": release.get("tag_name"),
        "draft": release.get("draft"),
        "prerelease": release.get("prerelease"),
    }


def preflight(
    root: Path, *, token: str, api_url: str = "https://api.github.com"
) -> dict[str, Any]:
    report = validate_static(root)
    policy = load_policy(root)
    _require_release_controls_committed(root, report["release_source_sha"])
    external_receipts = _strict_external_receipts(
        policy, release_source_sha=report["release_source_sha"]
    )
    shared_source = _shared_source_readback(
        root, policy, token=token, api_url=api_url
    )
    release_foundation = _release_foundation_readback(
        root, policy, token=token, api_url=api_url
    )
    environment = _environment_check(policy, token=token, api_url=api_url)
    remote_sha = _require_remote_release_source(
        policy,
        report["release_source_sha"],
        token=token,
        api_url=api_url,
    )
    repository = str(policy["repository"])

    tag = urllib.parse.quote(str(policy["candidate_tag"]), safe="")
    _require_remote_absence(
        f"{api_url}/repos/{repository}/git/ref/tags/{tag}",
        label="candidate Git tag",
        token=token,
    )
    _require_remote_absence(
        f"{api_url}/repos/{repository}/releases/tags/{tag}",
        label="candidate GitHub Release",
        token=token,
    )
    _require_remote_absence(
        _candidate_pypi_url(policy),
        label="candidate PyPI version",
    )
    registry = _registry_history_check(policy)
    report.update(
        {
            "environment": environment,
            "external_receipts": external_receipts,
            "release_foundation": release_foundation,
            "shared_source": shared_source,
            "remote_release_branch_sha": remote_sha,
            "registry_latest": (registry.get("info") or {}).get("version"),
            "namespace": {
                "git_tag": "absent",
                "github_release": "absent",
                "pypi_version": "absent",
            },
            "status": "preflight_green",
        }
    )
    return report


def tag_preflight(
    root: Path, *, token: str, api_url: str = "https://api.github.com"
) -> dict[str, Any]:
    """Recheck a newly created tag before spending time on its artifact build."""

    report = validate_static(root, tag_build=True)
    policy = load_policy(root)
    _require_release_controls_committed(root, report["release_source_sha"])
    external_receipts = _strict_external_receipts(
        policy, release_source_sha=report["release_source_sha"]
    )
    shared_source = _shared_source_readback(
        root, policy, token=token, api_url=api_url
    )
    release_foundation = _release_foundation_readback(
        root, policy, token=token, api_url=api_url
    )
    environment = _environment_check(policy, token=token, api_url=api_url)
    remote_tag_sha = _remote_tag_sha(policy, token=token, api_url=api_url)
    if remote_tag_sha != report["release_source_sha"]:
        raise ReleasePolicyError("GitHub tag differs from the reviewed release source")
    remote_branch_sha = _require_remote_release_source(
        policy,
        report["release_source_sha"],
        token=token,
        api_url=api_url,
        allow_branch_advance=True,
    )
    dispatch_preflight = _workflow_dispatch_proof(
        policy,
        report["release_source_sha"],
        token=token,
        api_url=api_url,
    )
    repository = str(policy["repository"])
    tag = urllib.parse.quote(str(policy["candidate_tag"]), safe="")
    _require_remote_absence(
        f"{api_url}/repos/{repository}/releases/tags/{tag}",
        label="candidate GitHub Release",
        token=token,
    )
    _require_remote_absence(
        _candidate_pypi_url(policy),
        label="candidate PyPI version",
    )
    registry = _registry_history_check(policy)
    report.update(
        {
            "environment": environment,
            "external_receipts": external_receipts,
            "release_foundation": release_foundation,
            "shared_source": shared_source,
            "dispatch_preflight": dispatch_preflight,
            "remote_tag_sha": remote_tag_sha,
            "remote_release_branch_sha": remote_branch_sha,
            "registry_latest": (registry.get("info") or {}).get("version"),
            "namespace": {"github_release": "absent", "pypi_version": "absent"},
            "status": "tag_preflight_green",
        }
    )
    return report


def pretag(root: Path, *, token: str, api_url: str = "https://api.github.com") -> dict[str, Any]:
    report = validate_static(root, tag_build=True)
    policy = load_policy(root)
    _require_release_controls_committed(root, report["release_source_sha"])
    external_receipts = _strict_external_receipts(
        policy, release_source_sha=report["release_source_sha"]
    )
    shared_source = _shared_source_readback(
        root, policy, token=token, api_url=api_url
    )
    release_foundation = _release_foundation_readback(
        root, policy, token=token, api_url=api_url
    )
    environment = _environment_check(policy, token=token, api_url=api_url)
    remote_tag_sha = _remote_tag_sha(policy, token=token, api_url=api_url)
    if remote_tag_sha != report["release_source_sha"]:
        raise ReleasePolicyError("GitHub tag differs from the reviewed release source")
    remote_branch_sha = _require_remote_release_source(
        policy,
        report["release_source_sha"],
        token=token,
        api_url=api_url,
        allow_branch_advance=True,
    )
    dispatch_preflight = _workflow_dispatch_proof(
        policy,
        report["release_source_sha"],
        token=token,
        api_url=api_url,
    )
    release = _draft_release(policy, token=token, api_url=api_url)
    _require_remote_absence(
        _candidate_pypi_url(policy),
        label="candidate PyPI version",
    )
    registry = _registry_history_check(policy)
    report.update(
        {
            "environment": environment,
            "external_receipts": external_receipts,
            "release_foundation": release_foundation,
            "shared_source": shared_source,
            "dispatch_preflight": dispatch_preflight,
            "github_release": release,
            "remote_release_branch_sha": remote_branch_sha,
            "registry_latest": (registry.get("info") or {}).get("version"),
        }
    )
    report["status"] = "pretag_green"
    return report


def _distribution_metadata(path: Path) -> tuple[str, str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            names = [name for name in archive.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ReleasePolicyError("wheel metadata inventory invalid")
            metadata = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
    elif path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            members = []
            for item in archive.getmembers():
                member_path = PurePosixPath(item.name)
                if (
                    item.isfile()
                    and len(member_path.parts) == 2
                    and member_path.name == "PKG-INFO"
                    and ".." not in member_path.parts
                ):
                    members.append(item)
            if len(members) != 1:
                raise ReleasePolicyError("sdist root metadata inventory invalid")
            handle = archive.extractfile(members[0])
            if handle is None:
                raise ReleasePolicyError("sdist metadata unreadable")
            metadata = Parser().parsestr(handle.read().decode("utf-8"))
    else:
        raise ReleasePolicyError(f"unsupported distribution: {path.name}")
    return str(metadata.get("Name") or ""), str(metadata.get("Version") or "")


def _dispatch_preflight_from_report(
    path: Path,
    *,
    release_source_sha: str,
) -> dict[str, Any]:
    report = _load_json(path)
    dispatch = report.get("dispatch_preflight")
    if (
        report.get("status") != "tag_preflight_green"
        or report.get("release_source_sha") != release_source_sha
        or not isinstance(dispatch, dict)
        or dispatch.get("head_sha") != release_source_sha
        or dispatch.get("event") != "workflow_dispatch"
        or dispatch.get("conclusion") != "success"
    ):
        raise ReleasePolicyError("tag preflight report is not bound to the release SHA")
    return {**dispatch, "report_sha256": _sha_file(path)}


def build_manifest(
    root: Path,
    dist: Path,
    *,
    source_sha: str = "HEAD",
    dispatch_proof: Path | None = None,
    dispatch_preflight: dict[str, Any] | None = None,
) -> dict[str, Any]:
    resolved_source = str(_git(root, "rev-parse", source_sha))
    checked_out_source = str(_git(root, "rev-parse", "HEAD"))
    if not SHA40.fullmatch(resolved_source) or resolved_source != checked_out_source:
        raise ReleasePolicyError("manifest source is not the exact checked-out commit")
    tag_exists = _tag_target(root, load_policy(root)["candidate_tag"]) is not None
    static = validate_static(root, head=resolved_source, tag_build=tag_exists)
    policy = load_policy(root)
    if tag_exists or os.environ.get("GITHUB_ACTIONS") == "true":
        _require_release_controls_committed(root, resolved_source)
    artifacts = sorted(
        [*dist.glob("*.whl"), *dist.glob("*.tar.gz")], key=lambda item: item.name
    )
    if len(artifacts) != 2 or sum(path.suffix == ".whl" for path in artifacts) != 1:
        raise ReleasePolicyError("expected exactly one wheel and one source archive")
    rows = []
    for path in artifacts:
        if path.is_symlink():
            raise ReleasePolicyError(f"distribution is a symbolic link: {path.name}")
        name, version = _distribution_metadata(path)
        normalized_name = name.lower().replace("_", "-")
        if (
            normalized_name != str(policy["package"]).lower()
            or version != policy["candidate_version"]
        ):
            raise ReleasePolicyError(f"artifact metadata mismatch: {path.name}")
        rows.append({"filename": path.name, "size": path.stat().st_size, "sha256": _sha_file(path)})
    resolved_head = static["release_source_sha"]
    if dispatch_proof is not None and dispatch_preflight is not None:
        raise ReleasePolicyError("dispatch preflight was provided twice")
    if dispatch_proof is not None:
        dispatch_preflight = _dispatch_preflight_from_report(
            dispatch_proof,
            release_source_sha=resolved_head,
        )
    if tag_exists and dispatch_preflight is None:
        raise ReleasePolicyError("tag manifest has no prior dispatch-preflight proof")
    receipt_env_present = bool(
        os.environ.get(_TRUSTED_PUBLISHER_RECEIPT_ENV)
        or os.environ.get(_APPROVAL_WATCHDOG_RECEIPT_ENV)
    )
    external_receipts = (
        _strict_external_receipts(policy, release_source_sha=resolved_head)
        if tag_exists or receipt_env_present
        else None
    )
    delta = _git(
        root,
        "diff",
        "--binary",
        "--full-index",
        f"{static['release_foundation']['merge_sha']}..{resolved_head}",
        text=False,
    )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "package": policy["package"],
        "version": policy["candidate_version"],
        "tag": policy["candidate_tag"],
        "product_base_sha": policy["plan_b_product_base_sha"],
        "release_foundation": static["release_foundation"],
        "shared_source": static["shared_source"],
        "release_source_sha": resolved_head,
        "allowed_release_delta_sha256": _sha_bytes(delta),
        "changed_paths": static["changed_paths"],
        "workflow": {"path": str(WORKFLOW_PATH), "sha256": static["workflow_sha256"]},
        "policy": {"path": str(POLICY_PATH), "sha256": static["policy_sha256"]},
        "action_pins": static["action_pins"],
        "build": policy["build"],
        "dispatch_preflight": dispatch_preflight,
        "release_notes": {
            "path": f"docs/releases/{policy['candidate_version']}.md",
            "sha256": _sha_file(
                root / f"docs/releases/{policy['candidate_version']}.md"
            ),
        },
        "release_controls": {
            "release_branch": policy["release_branch"],
            "approval_deadline_hours": policy["approval_deadline_hours"],
            "github_environment": policy["github_environment"],
            "trusted_publisher": {
                "coordinates": _publisher_coordinates(policy),
            },
            "approval_watchdog": policy["approval_watchdog"],
            "external_receipts": external_receipts,
        },
        "artifacts": rows,
        "candidate_state_at_build": {
            "github_release": "not_created",
            "pypi": "not_uploaded",
        },
    }
    manifest["content_digest"] = _sha_bytes(_canonical(manifest))
    return manifest


def verify_manifest(root: Path, manifest_path: Path, dist: Path) -> dict[str, Any]:
    manifest = _validated_manifest(manifest_path)
    claimed = manifest["content_digest"]
    if manifest.get("workflow", {}).get("sha256") != _sha_file(root / WORKFLOW_PATH):
        raise ReleasePolicyError("release workflow changed after artifact build")
    if manifest.get("policy", {}).get("sha256") != _sha_file(root / POLICY_PATH):
        raise ReleasePolicyError("release policy changed after artifact build")
    expected = _manifest_artifacts(manifest)
    observed = {path.name: path for path in [*dist.glob("*.whl"), *dist.glob("*.tar.gz")]}
    if set(expected) != set(observed):
        raise ReleasePolicyError("artifact file set differs from release manifest")
    for filename, row in expected.items():
        path = observed[filename]
        if path.is_symlink():
            raise ReleasePolicyError(f"distribution is a symbolic link: {filename}")
        if path.stat().st_size != row.get("size") or _sha_file(path) != row.get("sha256"):
            raise ReleasePolicyError(f"artifact bytes differ from manifest: {filename}")
    checked_out_source = str(_git(root, "rev-parse", "HEAD"))
    if manifest.get("release_source_sha") != checked_out_source:
        raise ReleasePolicyError("release manifest is not bound to the checked-out commit")
    rebuilt = build_manifest(
        root,
        dist,
        source_sha=checked_out_source,
        dispatch_preflight=manifest.get("dispatch_preflight"),
    )
    if manifest != rebuilt:
        differing = sorted(
            key
            for key in set(manifest) | set(rebuilt)
            if manifest.get(key) != rebuilt.get(key)
        )
        raise ReleasePolicyError(
            f"release manifest identity differs from current candidate: {differing}"
        )
    allowed_files = set(expected)
    try:
        if manifest_path.parent.resolve() == dist.resolve():
            allowed_files.add(manifest_path.name)
    except OSError as exc:
        raise ReleasePolicyError("release manifest path is unreadable") from exc
    all_files = {
        path.relative_to(dist).as_posix()
        for path in dist.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    if all_files != allowed_files:
        raise ReleasePolicyError(
            f"release asset file set differs from manifest: {sorted(all_files)}"
        )
    return {"status": "verified", "files": len(expected), "content_digest": claimed}


def _registry_readback(
    manifest: dict[str, Any], *, attempts: int, delay_seconds: float
) -> tuple[dict[str, Any], dict[str, Any]]:
    expected = _manifest_artifacts(manifest)
    package = manifest.get("package")
    version = manifest.get("version")
    if (
        not isinstance(package, str)
        or not package
        or not isinstance(version, str)
        or not version
    ):
        raise ReleasePolicyError("release manifest registry identity is invalid")
    normalized = {
        name: {"size": row["size"], "sha256": row["sha256"]}
        for name, row in expected.items()
    }
    if attempts < 1 or delay_seconds < 0:
        raise ReleasePolicyError("registry retry policy is invalid")
    encoded_package = urllib.parse.quote(package, safe="")
    encoded_version = urllib.parse.quote(version, safe="")
    url = f"https://pypi.org/pypi/{encoded_package}/{encoded_version}/json"
    for attempt in range(1, attempts + 1):
        try:
            payload = _request_json(url)
        except FileNotFoundError:
            payload = {}
        urls = payload.get("urls")
        if not isinstance(urls, list) or any(not isinstance(row, dict) for row in urls):
            urls = []
        observed = {
            str(row.get("filename") or ""): {
                "size": row.get("size"),
                "sha256": (row.get("digests") or {}).get("sha256"),
                "yanked": bool(row.get("yanked")),
            }
            for row in urls
        }
        normalized_with_yank = {
            name: {**identity, "yanked": False}
            for name, identity in normalized.items()
        }
        info = payload.get("info") or {}
        package_matches = str(info.get("name") or "").lower().replace("_", "-") == str(
            manifest["package"]
        ).lower().replace("_", "-")
        version_matches = str(info.get("version") or "") == str(manifest["version"])
        if (
            len(urls) == len(expected)
            and observed == normalized_with_yank
            and package_matches
            and version_matches
        ):
            return (
                {
                    "status": "registry_verified",
                    "files": len(observed),
                    "version": manifest["version"],
                    "attempt": attempt,
                },
                payload,
            )
        if attempt < attempts:
            time.sleep(delay_seconds)
    raise ReleasePolicyError("PyPI file-set readback differs from immutable manifest")


def registry_verify(
    manifest_path: Path, *, attempts: int = 12, delay_seconds: float = 10.0
) -> dict[str, Any]:
    manifest = _validated_manifest(manifest_path)
    report, _ = _registry_readback(
        manifest, attempts=attempts, delay_seconds=delay_seconds
    )
    return report


def registry_download(
    manifest_path: Path,
    destination: Path,
    *,
    attempts: int = 12,
    delay_seconds: float = 10.0,
) -> dict[str, Any]:
    """Download the exact non-yanked PyPI bytes named by the immutable manifest."""

    manifest = _validated_manifest(manifest_path)
    expected = _manifest_artifacts(manifest)
    report, payload = _registry_readback(
        manifest, attempts=attempts, delay_seconds=delay_seconds
    )
    if destination.is_symlink():
        raise ReleasePolicyError("registry download destination is a symbolic link")
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ReleasePolicyError("registry download destination is not empty")
    remote_rows = {str(row.get("filename") or ""): row for row in payload.get("urls", [])}
    created: list[Path] = []
    try:
        for filename, identity in expected.items():
            remote = remote_rows[filename]
            url = str(remote.get("url") or "")
            parsed = urllib.parse.urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "files.pythonhosted.org":
                raise ReleasePolicyError(f"PyPI artifact URL is not canonical: {filename}")
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "marvis-release-acceptance"},
            )
            target = destination / filename
            part = destination / f".{filename}.part"
            size = 0
            digest = hashlib.sha256()
            with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
                final = urllib.parse.urlparse(response.geturl())
                if final.scheme != "https" or final.hostname != "files.pythonhosted.org":
                    raise ReleasePolicyError(f"PyPI artifact redirected off-host: {filename}")
                with part.open("xb") as handle:
                    while chunk := response.read(_FILE_CHUNK_BYTES):
                        size += len(chunk)
                        if size > identity["size"]:
                            raise ReleasePolicyError(
                                f"downloaded PyPI bytes differ from manifest: {filename}"
                            )
                        digest.update(chunk)
                        handle.write(chunk)
            if size != identity["size"] or digest.hexdigest() != identity["sha256"]:
                raise ReleasePolicyError(f"downloaded PyPI bytes differ from manifest: {filename}")
            part.replace(target)
            created.append(target)
    except KeyError as exc:
        for path in [*created, *destination.glob(".*.part")]:
            path.unlink(missing_ok=True)
        raise ReleasePolicyError("PyPI artifact URL inventory is incomplete") from exc
    except (OSError, ReleasePolicyError, urllib.error.URLError):
        for path in [*created, *destination.glob(".*.part")]:
            path.unlink(missing_ok=True)
        raise
    return {**report, "status": "registry_downloaded", "destination": str(destination)}


def verify_installed_upgrade(
    contract_root: Path,
    *,
    version: str,
    evidence_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the existing recovery proof against installed candidate modules only."""

    contract_root = contract_root.resolve()
    try:
        distribution = importlib.metadata.distribution("marvisx-cli")
    except importlib.metadata.PackageNotFoundError as exc:
        raise ReleasePolicyError("installed marvisx-cli candidate is absent") from exc
    installed_root = Path(distribution.locate_file("")).resolve()
    if installed_root == contract_root or installed_root.is_relative_to(contract_root):
        raise ReleasePolicyError("candidate distribution resolves inside the checkout")
    migrations = installed_root / "migrations"
    if not migrations.is_dir():
        raise ReleasePolicyError("installed candidate migrations are absent")

    verifier_path = contract_root / "scripts/verify_local_upgrade.py"
    spec = importlib.util.spec_from_file_location(
        "_marvis_release_upgrade_verifier",
        verifier_path,
    )
    if spec is None or spec.loader is None:
        raise ReleasePolicyError("upgrade verifier cannot be loaded")
    checkout_modules = [
        name for name in sys.modules if name == "core" or name.startswith("core.")
    ]
    if checkout_modules:
        raise ReleasePolicyError("candidate modules were imported before isolation")
    original_path = list(sys.path)
    verifier = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = verifier
    try:
        spec.loader.exec_module(verifier)
    except Exception:
        sys.modules.pop(spec.name, None)
        sys.path[:] = original_path
        raise

    try:
        filtered = []
        for entry in sys.path:
            resolved = Path(entry or os.getcwd()).resolve()
            if resolved == contract_root or resolved.is_relative_to(contract_root):
                continue
            filtered.append(entry)
        sys.path[:] = [str(installed_root), *filtered]

        contract = verifier._load_contract(
            contract_root / "contracts/compatibility/prior-distributions-v1.json"
        )
        prior = next((item for item in contract if item.version == version), None)
        if prior is None:
            raise ReleasePolicyError("requested prior version is not supported")
        try:
            with tempfile.TemporaryDirectory(prefix="marvis-prior-download-") as raw:
                artifact = verifier._download(prior, Path(raw))
                report = verifier.verify_upgrade(
                    installed_root,
                    artifact,
                    prior,
                    evidence_dir=evidence_dir,
                )
        except verifier.UpgradeVerificationError as exc:
            raise ReleasePolicyError(str(exc)) from exc
        product_modules = {
            name: module
            for name, module in sys.modules.items()
            if module is not None and (name == "core" or name.startswith("core."))
        }
        if not product_modules:
            raise ReleasePolicyError("upgrade proof imported no candidate product modules")
        for name, module in product_modules.items():
            origins: list[Path] = []
            module_file = getattr(module, "__file__", None)
            if module_file:
                origins.append(Path(module_file).resolve())
            spec = getattr(module, "__spec__", None)
            locations = getattr(spec, "submodule_search_locations", None)
            if locations:
                origins.extend(Path(item).resolve() for item in locations)
            if not origins or any(
                origin != installed_root and not origin.is_relative_to(installed_root)
                for origin in origins
            ):
                raise ReleasePolicyError(
                    f"upgrade proof imported {name} outside the installed wheel"
                )
    finally:
        sys.path[:] = original_path
        sys.modules.pop(spec.name, None)
        for name in [
            module_name
            for module_name in sys.modules
            if module_name == "core" or module_name.startswith("core.")
        ]:
            sys.modules.pop(name, None)

    return {
        **report,
        "candidate_import_origin": "installed_distribution",
        "candidate_distribution_version": distribution.version,
    }


def _workflow_run_readback(
    policy: dict[str, Any],
    release_source_sha: str,
    *,
    token: str,
    run_id: str,
    api_url: str,
) -> dict[str, Any]:
    if not str(run_id).isdigit():
        raise ReleasePolicyError("workflow run id is invalid")
    repository = str(policy["repository"])
    run = _request_json(
        f"{api_url}/repos/{repository}/actions/runs/{run_id}", token=token
    )
    if run.get("head_sha") != release_source_sha:
        raise ReleasePolicyError("workflow run SHA differs from immutable manifest")
    if run.get("path") != str(WORKFLOW_PATH):
        raise ReleasePolicyError("workflow identity differs from release policy")
    if run.get("event") != "push":
        raise ReleasePolicyError("publication run is not a tag push")
    if run.get("head_branch") != policy["candidate_tag"]:
        raise ReleasePolicyError("publication run was triggered by another tag")
    if (run.get("head_repository") or {}).get("full_name") != repository:
        raise ReleasePolicyError("publication run belongs to another repository")
    return run


def build_acceptance_receipt(
    root: Path,
    manifest_path: Path,
    registry_dist: Path,
    github_dist: Path,
    *,
    token: str,
    run_id: str,
    now: datetime | None = None,
    api_url: str = "https://api.github.com",
) -> dict[str, Any]:
    """Bind registry bytes and passed product-smoke gates before finalization."""
    manifest = _validated_manifest(manifest_path)
    source_sha = str(manifest.get("release_source_sha") or "")
    if not SHA40.fullmatch(source_sha):
        raise ReleasePolicyError("release manifest source is invalid")
    verify_manifest(root, manifest_path, registry_dist)
    verify_manifest(root, github_dist / manifest_path.name, github_dist)
    registry = registry_verify(manifest_path, attempts=1, delay_seconds=0)
    policy = load_policy(root)
    external_receipts = _strict_external_receipts(
        policy, release_source_sha=source_sha, now=now
    )
    shared_source = _shared_source_readback(
        root, policy, token=token, api_url=api_url
    )
    release_foundation = _release_foundation_readback(
        root, policy, token=token, api_url=api_url
    )
    if manifest.get("shared_source") != _shared_source_coordinates(root, policy):
        raise ReleasePolicyError("release manifest shared-source identity changed")
    if manifest.get("release_foundation") != _release_foundation_coordinates(
        root, policy
    ):
        raise ReleasePolicyError("release manifest foundation identity changed")
    remote_tag_sha = _remote_tag_sha(policy, token=token, api_url=api_url)
    if remote_tag_sha != source_sha:
        raise ReleasePolicyError("candidate tag moved before acceptance receipt")
    remote_branch_sha = _require_remote_release_source(
        policy,
        source_sha,
        token=token,
        api_url=api_url,
        allow_branch_advance=True,
    )
    release = _draft_release(policy, token=token, api_url=api_url)
    run = _workflow_run_readback(
        policy,
        source_sha,
        token=token,
        run_id=run_id,
        api_url=api_url,
    )
    dispatch_preflight = _workflow_dispatch_proof(
        policy,
        source_sha,
        token=token,
        api_url=api_url,
        now=now,
    )
    manifest_dispatch = manifest.get("dispatch_preflight") or {}
    if (
        manifest_dispatch.get("id") != dispatch_preflight["id"]
        or manifest_dispatch.get("run_attempt") != dispatch_preflight["run_attempt"]
        or manifest_dispatch.get("head_sha") != source_sha
    ):
        raise ReleasePolicyError("manifest dispatch-preflight proof changed")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    receipt = {
        "schema": ACCEPTANCE_SCHEMA,
        "created_at": current.isoformat().replace("+00:00", "Z"),
        "repository": policy["repository"],
        "package": manifest["package"],
        "version": manifest["version"],
        "tag": manifest["tag"],
        "product_base_sha": manifest["product_base_sha"],
        "release_foundation": release_foundation,
        "shared_source": shared_source,
        "release_source_sha": source_sha,
        "remote_release_branch_sha": remote_branch_sha,
        "remote_tag_sha": remote_tag_sha,
        "workflow_run": {
            "id": str(run_id),
            "path": run.get("path"),
            "event": run.get("event"),
            "head_branch": run.get("head_branch"),
            "head_sha": run.get("head_sha"),
        },
        "candidate_manifest": {
            "filename": manifest_path.name,
            "sha256": _sha_file(manifest_path),
            "content_digest": manifest["content_digest"],
        },
        "dispatch_preflight": dispatch_preflight,
        "external_receipts": external_receipts,
        "registry_readback": {
            **registry,
            "artifacts": manifest["artifacts"],
        },
        "github_release_readback": release,
        "passed_gates": [
            "github_release_asset_bytes",
            "pypi_registry_file_set",
            "pypi_registry_download_bytes",
            "wheel_clean_install_and_local_journey",
            "sdist_clean_install_and_local_journey",
            "supported_prior_backup_upgrade_restore_before_pin",
        ],
        "terminal_state": "accepted_ready_for_github_release_finalization",
    }
    receipt["content_digest"] = _sha_bytes(_canonical(receipt))
    return receipt


def publish_window(
    root: Path,
    manifest_path: Path,
    dist: Path,
    *,
    token: str,
    run_id: str,
    now: datetime | None = None,
    api_url: str = "https://api.github.com",
) -> dict[str, Any]:
    manifest = _validated_manifest(manifest_path)
    source_sha = str(manifest.get("release_source_sha") or "")
    if not SHA40.fullmatch(source_sha):
        raise ReleasePolicyError("release manifest source is invalid")
    policy = load_policy(root)
    external_receipts = _strict_external_receipts(
        policy, release_source_sha=source_sha, now=now
    )
    shared_source = _shared_source_readback(
        root, policy, token=token, api_url=api_url
    )
    release_foundation = _release_foundation_readback(
        root, policy, token=token, api_url=api_url
    )
    if manifest.get("shared_source") != _shared_source_coordinates(root, policy):
        raise ReleasePolicyError("release manifest shared-source identity changed")
    if manifest.get("release_foundation") != _release_foundation_coordinates(
        root, policy
    ):
        raise ReleasePolicyError("release manifest foundation identity changed")
    run = _workflow_run_readback(
        policy,
        str(manifest.get("release_source_sha") or ""),
        token=token,
        run_id=run_id,
        api_url=api_url,
    )
    created = _parse_time(str(run.get("created_at") or ""))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - created).total_seconds()
    if age < 0 or age > int(policy["approval_deadline_hours"]) * 3600:
        raise ReleasePolicyError("release approval window expired")
    dispatch_preflight = _workflow_dispatch_proof(
        policy,
        str(manifest.get("release_source_sha") or ""),
        token=token,
        api_url=api_url,
        now=now,
    )
    if _remote_tag_sha(policy, token=token, api_url=api_url) != manifest.get(
        "release_source_sha"
    ):
        raise ReleasePolicyError("candidate tag moved after artifact creation")
    remote_branch_sha = _require_remote_release_source(
        policy,
        str(manifest.get("release_source_sha") or ""),
        token=token,
        api_url=api_url,
        allow_branch_advance=True,
    )
    _draft_release(policy, token=token, api_url=api_url)
    environment = _environment_check(policy, token=token, api_url=api_url)
    verify_manifest(root, manifest_path, dist)
    _require_remote_absence(
        _candidate_pypi_url(policy),
        label="candidate PyPI version",
    )
    registry = _registry_history_check(policy)
    return {
        "status": "publication_window_green",
        "run_id": str(run_id),
        "age_seconds": int(age),
        "environment": environment,
        "external_receipts": external_receipts,
        "release_foundation": release_foundation,
        "shared_source": shared_source,
        "dispatch_preflight": dispatch_preflight,
        "remote_release_branch_sha": remote_branch_sha,
        "registry_latest": (registry.get("info") or {}).get("version"),
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("state")
    static_parser = sub.add_parser("static")
    static_parser.add_argument("--tag-build", action="store_true")
    static_parser.add_argument("--trigger-ref")
    pretag_parser = sub.add_parser("pretag")
    pretag_parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    tag_preflight_parser = sub.add_parser("tag-preflight")
    tag_preflight_parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    preflight_parser = sub.add_parser("preflight")
    preflight_parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    manifest_parser = sub.add_parser("manifest")
    manifest_parser.add_argument("--dist", type=Path, required=True)
    manifest_parser.add_argument("--output", type=Path, required=True)
    manifest_parser.add_argument("--source-sha", default=os.environ.get("GITHUB_SHA", "HEAD"))
    manifest_parser.add_argument("--dispatch-proof", type=Path)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--dist", type=Path, required=True)
    registry_parser = sub.add_parser("registry")
    registry_parser.add_argument("--manifest", type=Path, required=True)
    registry_download_parser = sub.add_parser("registry-download")
    registry_download_parser.add_argument("--manifest", type=Path, required=True)
    registry_download_parser.add_argument("--dest", type=Path, required=True)
    acceptance_parser = sub.add_parser("acceptance")
    acceptance_parser.add_argument("--manifest", type=Path, required=True)
    acceptance_parser.add_argument("--registry-dist", type=Path, required=True)
    acceptance_parser.add_argument("--github-dist", type=Path, required=True)
    acceptance_parser.add_argument("--output", type=Path, required=True)
    acceptance_parser.add_argument(
        "--token", default=os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    )
    acceptance_parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    window_parser = sub.add_parser("publish-window")
    window_parser.add_argument("--manifest", type=Path, required=True)
    window_parser.add_argument("--dist", type=Path, required=True)
    window_parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    window_parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    installed_upgrade_parser = sub.add_parser("installed-upgrade")
    installed_upgrade_parser.add_argument("--version", default="0.3.8")
    installed_upgrade_parser.add_argument("--evidence-dir", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "state":
            result = candidate_state_report(root)
        elif args.command == "static":
            result = validate_static(
                root,
                tag_build=args.tag_build,
                trigger_ref=args.trigger_ref,
            )
        elif args.command == "pretag":
            if not args.token:
                raise ReleasePolicyError("GITHUB_TOKEN is required for live pretag checks")
            result = pretag(root, token=args.token)
        elif args.command == "tag-preflight":
            if not args.token:
                raise ReleasePolicyError(
                    "GITHUB_TOKEN is required for live tag-preflight checks"
                )
            result = tag_preflight(root, token=args.token)
        elif args.command == "preflight":
            if not args.token:
                raise ReleasePolicyError("GITHUB_TOKEN is required for live preflight checks")
            result = preflight(root, token=args.token)
        elif args.command == "manifest":
            result = build_manifest(
                root,
                args.dist.resolve(),
                source_sha=args.source_sha,
                dispatch_proof=(
                    args.dispatch_proof.resolve()
                    if args.dispatch_proof is not None
                    else None
                ),
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif args.command == "verify":
            result = verify_manifest(root, args.manifest.resolve(), args.dist.resolve())
        elif args.command == "registry":
            result = registry_verify(args.manifest.resolve())
        elif args.command == "registry-download":
            result = registry_download(
                args.manifest.resolve(), args.dest.resolve()
            )
        elif args.command == "acceptance":
            if not args.token or not args.run_id:
                raise ReleasePolicyError("GITHUB_TOKEN and GITHUB_RUN_ID are required")
            result = build_acceptance_receipt(
                root,
                args.manifest.resolve(),
                args.registry_dist.resolve(),
                args.github_dist.resolve(),
                token=args.token,
                run_id=args.run_id,
            )
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif args.command == "publish-window":
            if not args.token or not args.run_id:
                raise ReleasePolicyError("GITHUB_TOKEN and GITHUB_RUN_ID are required")
            result = publish_window(
                root,
                args.manifest.resolve(),
                args.dist.resolve(),
                token=args.token,
                run_id=args.run_id,
            )
        else:
            result = verify_installed_upgrade(
                root,
                version=args.version,
                evidence_dir=(
                    args.evidence_dir.resolve()
                    if args.evidence_dir is not None
                    else None
                ),
            )
    except (
        ReleasePolicyError,
        OSError,
        subprocess.SubprocessError,
        tarfile.TarError,
        zipfile.BadZipFile,
    ) as exc:
        print(f"release candidate: FAIL: {exc}", file=sys.stderr)
        return 1
    _print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
