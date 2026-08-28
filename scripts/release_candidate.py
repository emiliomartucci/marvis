#!/usr/bin/env python3
"""Fail-closed public release policy, artifact manifest and registry readback.

The PR path validates and builds but cannot publish. A tag path must additionally
pass live GitHub-environment checks, a fresh owner readback of the PyPI trusted
publisher, and an external 24-hour approval-watchdog receipt before upload.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email.parser import Parser
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
import zipfile

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    import tomli as tomllib  # type: ignore[no-redef]


POLICY_SCHEMA = "marvis-public-release-policy/v1"
MANIFEST_SCHEMA = "marvis-public-release-manifest/v1"
POLICY_PATH = Path("contracts/release/public-release-v1.json")
WORKFLOW_PATH = Path(".github/workflows/release.yml")
SHA40 = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"(?m)^\s*(?:-\s*)?uses:\s*([^#\s]+)")


class ReleasePolicyError(RuntimeError):
    pass


def _sha_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha_file(path: Path) -> str:
    return _sha_bytes(path.read_bytes())


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


def load_policy(root: Path) -> dict[str, Any]:
    policy = _load_json(root / POLICY_PATH)
    if policy.get("schema") != POLICY_SCHEMA:
        raise ReleasePolicyError("unsupported public release policy")
    return policy


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


def _path_allowed(path: str, allowlist: list[str]) -> bool:
    return any(path == item or (item.endswith("/") and path.startswith(item)) for item in allowlist)


def _changed_paths(root: Path, base: str, head: str) -> list[str]:
    raw = str(_git(root, "diff", "--name-only", f"{base}..{head}"))
    return sorted(line for line in raw.splitlines() if line)


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


def _publisher_coordinates(policy: dict[str, Any]) -> dict[str, str]:
    publisher = policy.get("trusted_publisher") or {}
    keys = ("project", "owner", "repository", "workflow", "environment")
    result = {key: str(publisher.get(key) or "") for key in keys}
    if not all(result.values()):
        raise ReleasePolicyError("trusted-publisher coordinates are incomplete")
    return result


def publisher_coordinates_sha256(policy: dict[str, Any]) -> str:
    return _sha_bytes(_canonical(_publisher_coordinates(policy)))


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleasePolicyError("timestamp must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ReleasePolicyError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _strict_external_receipts(
    policy: dict[str, Any], *, now: datetime | None = None
) -> None:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    readback = ((policy.get("trusted_publisher") or {}).get("readback") or {})
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

    watchdog = policy.get("approval_watchdog") or {}
    if watchdog.get("status") != "ready" or not watchdog.get("receipt_ref"):
        raise ReleasePolicyError("external approval watchdog has no ready receipt")
    if watchdog.get("late_approval_upload_guard") is not True:
        raise ReleasePolicyError("late-approval upload guard is disabled")
    expected_watchdog = {
        "repository": policy.get("repository"),
        "workflow": _publisher_coordinates(policy)["workflow"],
        "environment": (policy.get("github_environment") or {}).get("name"),
        "candidate_tag": policy.get("candidate_tag"),
        "approval_deadline_hours": policy.get("approval_deadline_hours"),
    }
    observed_watchdog = {key: watchdog.get(key) for key in expected_watchdog}
    if observed_watchdog != expected_watchdog:
        raise ReleasePolicyError("approval watchdog receipt coordinates do not match policy")
    watchdog_verified_at = _parse_time(str(watchdog.get("verified_at") or ""))
    watchdog_age = (current - watchdog_verified_at).total_seconds()
    if watchdog_age < 0 or watchdog_age > 24 * 3600:
        raise ReleasePolicyError("approval watchdog receipt is stale")
    if not watchdog.get("verified_by"):
        raise ReleasePolicyError("approval watchdog receipt has no verifier")


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
    resolved_head = str(_git(root, "rev-parse", head))
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", base, resolved_head],
        capture_output=True,
    )
    if ancestor.returncode != 0:
        raise ReleasePolicyError("release source does not descend from exact Plan B merge")

    allowlist = policy.get("allowed_release_delta")
    if not isinstance(allowlist, list) or not allowlist:
        raise ReleasePolicyError("release delta allowlist is empty")
    changed = _changed_paths(root, base, resolved_head)
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
    if version == str(policy.get("historical_failed_version") or ""):
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
        "release_candidate.py pretag",
        "release_candidate.py publish-window",
        "environment: pypi",
        "pypa/gh-action-pypi-publish@" + expected_pins.get("pypa/gh-action-pypi-publish", ""),
    )
    missing = [fragment for fragment in required_fragments if fragment not in workflow]
    if missing:
        raise ReleasePolicyError(f"release workflow contract fragments missing: {missing}")
    if "pip install --quiet --upgrade build" in workflow:
        raise ReleasePolicyError("release workflow installs mutable build tooling")
    publish_guard = "startsWith(github.ref, 'refs/tags/v')"
    if workflow.count(publish_guard) < 3:
        raise ReleasePolicyError("tag-only release/publish/finalize guards are incomplete")
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
    if ((policy.get("trusted_publisher") or {}).get("readback") or {}).get("status") != "verified":
        external_blockers.append("trusted_publisher_owner_readback")
    if (policy.get("approval_watchdog") or {}).get("status") != "ready":
        external_blockers.append("external_approval_watchdog")
    return {
        "schema": "marvis-public-release-static-preflight/v1",
        "product_base_sha": base,
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


def _environment_check(policy: dict[str, Any], *, token: str, api_url: str) -> dict[str, Any]:
    repository = str(policy["repository"])
    expected = policy["github_environment"]
    environment = _request_json(
        f"{api_url}/repos/{repository}/environments/{expected['name']}", token=token
    )
    if environment.get("can_admins_bypass") is not expected["can_admins_bypass"]:
        raise ReleasePolicyError("GitHub environment administrative bypass policy is unsafe")
    reviewer_logins = {
        str((item.get("reviewer") or {}).get("login") or "")
        for rule in environment.get("protection_rules", [])
        if rule.get("type") == "required_reviewers"
        for item in rule.get("reviewers", [])
    }
    if expected["required_reviewer_login"] not in reviewer_logins:
        raise ReleasePolicyError("required owner reviewer is absent from GitHub environment")
    required_rule = next(
        (
            rule
            for rule in environment.get("protection_rules", [])
            if rule.get("type") == "required_reviewers"
        ),
        {},
    )
    if required_rule.get("prevent_self_review") is not expected["prevent_self_review"]:
        raise ReleasePolicyError("GitHub environment self-review policy drift")
    return {
        "name": environment.get("name"),
        "can_admins_bypass": environment.get("can_admins_bypass"),
        "reviewers": sorted(reviewer_logins),
        "prevent_self_review": required_rule.get("prevent_self_review"),
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
    _strict_external_receipts(policy)
    environment = _environment_check(policy, token=token, api_url=api_url)
    repository = str(policy["repository"])
    branch = urllib.parse.quote(str(policy["release_branch"]), safe="")
    remote_branch = _request_json(
        f"{api_url}/repos/{repository}/branches/{branch}", token=token
    )
    remote_sha = str((remote_branch.get("commit") or {}).get("sha") or "")
    if remote_sha != report["release_source_sha"]:
        raise ReleasePolicyError("release source is not the exact remote release-branch head")

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
    package = urllib.parse.quote(str(policy["package"]), safe="")
    version = urllib.parse.quote(str(policy["candidate_version"]), safe="")
    _require_remote_absence(
        f"https://pypi.org/pypi/{package}/{version}/json",
        label="candidate PyPI version",
    )
    report.update(
        {
            "environment": environment,
            "remote_release_branch_sha": remote_sha,
            "namespace": {
                "git_tag": "absent",
                "github_release": "absent",
                "pypi_version": "absent",
            },
            "status": "preflight_green",
        }
    )
    return report


def pretag(root: Path, *, token: str, api_url: str = "https://api.github.com") -> dict[str, Any]:
    report = validate_static(root, tag_build=True)
    policy = load_policy(root)
    _strict_external_receipts(policy)
    environment = _environment_check(policy, token=token, api_url=api_url)
    remote_tag_sha = _remote_tag_sha(policy, token=token, api_url=api_url)
    if remote_tag_sha != report["release_source_sha"]:
        raise ReleasePolicyError("GitHub tag differs from the reviewed release source")
    release = _draft_release(policy, token=token, api_url=api_url)
    registry = _request_json(f"https://pypi.org/pypi/{policy['package']}/json")
    if policy["candidate_version"] in (registry.get("releases") or {}):
        raise ReleasePolicyError("candidate version already exists on PyPI")
    report.update(
        {
            "environment": environment,
            "github_release": release,
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


def build_manifest(root: Path, dist: Path, *, source_sha: str = "HEAD") -> dict[str, Any]:
    tag_exists = _tag_target(root, load_policy(root)["candidate_tag"]) is not None
    static = validate_static(root, head=source_sha, tag_build=tag_exists)
    policy = load_policy(root)
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
    delta = _git(
        root,
        "diff",
        "--binary",
        "--full-index",
        f"{policy['plan_b_product_base_sha']}..{resolved_head}",
        text=False,
    )
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "package": policy["package"],
        "version": policy["candidate_version"],
        "tag": policy["candidate_tag"],
        "product_base_sha": policy["plan_b_product_base_sha"],
        "release_source_sha": resolved_head,
        "allowed_release_delta_sha256": _sha_bytes(delta),
        "changed_paths": static["changed_paths"],
        "workflow": {"path": str(WORKFLOW_PATH), "sha256": static["workflow_sha256"]},
        "policy": {"path": str(POLICY_PATH), "sha256": static["policy_sha256"]},
        "action_pins": static["action_pins"],
        "build": policy["build"],
        "release_controls": {
            "release_branch": policy["release_branch"],
            "approval_deadline_hours": policy["approval_deadline_hours"],
            "github_environment": policy["github_environment"],
            "trusted_publisher": {
                "coordinates": _publisher_coordinates(policy),
                "readback": policy["trusted_publisher"]["readback"],
            },
            "approval_watchdog": policy["approval_watchdog"],
        },
        "artifacts": rows,
        "publication": {"github_release": "not_created", "pypi": "not_uploaded"},
    }
    manifest["content_digest"] = _sha_bytes(_canonical(manifest))
    return manifest


def verify_manifest(root: Path, manifest_path: Path, dist: Path) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise ReleasePolicyError("unsupported release manifest")
    claimed = manifest.get("content_digest")
    unsigned = dict(manifest)
    unsigned.pop("content_digest", None)
    if claimed != _sha_bytes(_canonical(unsigned)):
        raise ReleasePolicyError("release manifest content digest mismatch")
    if manifest.get("workflow", {}).get("sha256") != _sha_file(root / WORKFLOW_PATH):
        raise ReleasePolicyError("release workflow changed after artifact build")
    if manifest.get("policy", {}).get("sha256") != _sha_file(root / POLICY_PATH):
        raise ReleasePolicyError("release policy changed after artifact build")
    rows = manifest.get("artifacts")
    if not isinstance(rows, list) or not rows:
        raise ReleasePolicyError("release manifest artifact inventory is empty")
    try:
        filenames = [str(row["filename"]) for row in rows]
    except (KeyError, TypeError) as exc:
        raise ReleasePolicyError("release manifest artifact inventory is invalid") from exc
    if len(set(filenames)) != len(filenames):
        raise ReleasePolicyError("release manifest contains duplicate artifact names")
    expected = {filename: row for filename, row in zip(filenames, rows, strict=True)}
    observed = {path.name: path for path in [*dist.glob("*.whl"), *dist.glob("*.tar.gz")]}
    if set(expected) != set(observed):
        raise ReleasePolicyError("artifact file set differs from release manifest")
    for filename, row in expected.items():
        path = observed[filename]
        if path.is_symlink():
            raise ReleasePolicyError(f"distribution is a symbolic link: {filename}")
        if path.stat().st_size != row.get("size") or _sha_file(path) != row.get("sha256"):
            raise ReleasePolicyError(f"artifact bytes differ from manifest: {filename}")
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


def registry_verify(
    manifest_path: Path, *, attempts: int = 12, delay_seconds: float = 10.0
) -> dict[str, Any]:
    manifest = _load_json(manifest_path)
    expected = {row["filename"]: row for row in manifest.get("artifacts", [])}
    normalized = {
        name: {"size": row["size"], "sha256": row["sha256"]}
        for name, row in expected.items()
    }
    if attempts < 1 or delay_seconds < 0:
        raise ReleasePolicyError("registry retry policy is invalid")
    observed: dict[str, dict[str, Any]] = {}
    url = f"https://pypi.org/pypi/{manifest['package']}/{manifest['version']}/json"
    for attempt in range(1, attempts + 1):
        try:
            payload = _request_json(url)
        except FileNotFoundError:
            payload = {}
        observed = {
            row["filename"]: {
                "size": row.get("size"),
                "sha256": (row.get("digests") or {}).get("sha256"),
            }
            for row in payload.get("urls", [])
        }
        if observed == normalized:
            return {
                "status": "registry_verified",
                "files": len(observed),
                "version": manifest["version"],
                "attempt": attempt,
            }
        if attempt < attempts:
            time.sleep(delay_seconds)
    raise ReleasePolicyError("PyPI file-set readback differs from immutable manifest")


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
    if not str(run_id).isdigit():
        raise ReleasePolicyError("workflow run id is invalid")
    policy = load_policy(root)
    _strict_external_receipts(policy, now=now)
    manifest = _load_json(manifest_path)
    repository = policy["repository"]
    run = _request_json(f"{api_url}/repos/{repository}/actions/runs/{run_id}", token=token)
    created = _parse_time(str(run.get("created_at") or ""))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age = (current - created).total_seconds()
    if age < 0 or age > int(policy["approval_deadline_hours"]) * 3600:
        raise ReleasePolicyError("release approval window expired")
    if run.get("head_sha") != manifest.get("release_source_sha"):
        raise ReleasePolicyError("workflow run SHA differs from immutable manifest")
    if run.get("path") != str(WORKFLOW_PATH):
        raise ReleasePolicyError("workflow identity differs from release policy")
    if run.get("event") != "push":
        raise ReleasePolicyError("publication run is not a tag push")
    if run.get("head_branch") != policy["candidate_tag"]:
        raise ReleasePolicyError("publication run was triggered by another tag")
    if (run.get("head_repository") or {}).get("full_name") != repository:
        raise ReleasePolicyError("publication run belongs to another repository")
    if _remote_tag_sha(policy, token=token, api_url=api_url) != manifest.get(
        "release_source_sha"
    ):
        raise ReleasePolicyError("candidate tag moved after artifact creation")
    _draft_release(policy, token=token, api_url=api_url)
    environment = _environment_check(policy, token=token, api_url=api_url)
    verify_manifest(root, manifest_path, dist)
    return {
        "status": "publication_window_green",
        "run_id": str(run_id),
        "age_seconds": int(age),
        "environment": environment,
    }


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest="command", required=True)
    static_parser = sub.add_parser("static")
    static_parser.add_argument("--tag-build", action="store_true")
    static_parser.add_argument("--trigger-ref")
    pretag_parser = sub.add_parser("pretag")
    pretag_parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    preflight_parser = sub.add_parser("preflight")
    preflight_parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    manifest_parser = sub.add_parser("manifest")
    manifest_parser.add_argument("--dist", type=Path, required=True)
    manifest_parser.add_argument("--output", type=Path, required=True)
    manifest_parser.add_argument("--source-sha", default=os.environ.get("GITHUB_SHA", "HEAD"))
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("--manifest", type=Path, required=True)
    verify_parser.add_argument("--dist", type=Path, required=True)
    registry_parser = sub.add_parser("registry")
    registry_parser.add_argument("--manifest", type=Path, required=True)
    window_parser = sub.add_parser("publish-window")
    window_parser.add_argument("--manifest", type=Path, required=True)
    window_parser.add_argument("--dist", type=Path, required=True)
    window_parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    window_parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID"))
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "static":
            result = validate_static(
                root,
                tag_build=args.tag_build,
                trigger_ref=args.trigger_ref,
            )
        elif args.command == "pretag":
            if not args.token:
                raise ReleasePolicyError("GITHUB_TOKEN is required for live pretag checks")
            result = pretag(root, token=args.token)
        elif args.command == "preflight":
            if not args.token:
                raise ReleasePolicyError("GITHUB_TOKEN is required for live preflight checks")
            result = preflight(root, token=args.token)
        elif args.command == "manifest":
            result = build_manifest(root, args.dist.resolve(), source_sha=args.source_sha)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(
                json.dumps(result, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        elif args.command == "verify":
            result = verify_manifest(root, args.manifest.resolve(), args.dist.resolve())
        elif args.command == "registry":
            result = registry_verify(args.manifest.resolve())
        else:
            if not args.token or not args.run_id:
                raise ReleasePolicyError("GITHUB_TOKEN and GITHUB_RUN_ID are required")
            result = publish_window(
                root,
                args.manifest.resolve(),
                args.dist.resolve(),
                token=args.token,
                run_id=args.run_id,
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
