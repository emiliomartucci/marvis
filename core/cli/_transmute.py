# v1.0.0 - 2026-05-28 - S0 RI-7 layer 1: deterministic mechanical transmute
"""Mechanical, deterministic layer of project *transmutation* (RI-7 layer 1).

This is the CLI half of the "plan deterministically, execute probabilistically,
validate strictly" split. It does ONLY the parts that must be reproducible and
non-negotiable; the intelligent content re-mapping (the agent's job, S-Transmute)
is OUT of this slice.

What it does:

1. **Scaffold** the Marvis structure into a *new* directory (separate from the
   source code): ``project.yaml`` + ``docs/`` + ``memory/`` + a seed
   ``context.md``. Derived from the source project (name / deduced type).
2. **Non-destructive guarantee (D3, regola ferrea):** a SHA-256 hash-inventory
   of the source tree is taken BEFORE the scaffold and re-verified AFTER. If a
   single hash changes, that is a hard error. Every write is confined to the new
   dir via a write-guard — the source tree is treated read-only and is NEVER a
   write target.
3. **Path registry + idempotency:** a ``.marvis-transmute.yaml`` manifest is
   written into the new dir recording ``source_roots[]`` (for the future in-place
   KG index — NOT built here), the reference hash-inventory, and a
   version/timestamp. A second run diffs against the manifest instead of blindly
   clobbering.

OUT of this slice (follow-ups, do NOT add here): in-place KG indexing of the
source code, the agent install-skill, the Hit@5 measurement harness.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Schema version of the manifest format — bump on breaking changes.
MANIFEST_VERSION = 1
MANIFEST_NAME = ".marvis-transmute.yaml"

# Directories never walked when building the source inventory or scaffolding:
# noise (VCS/build/caches) that would bloat the hash-inventory and the index.
_SKIP_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        "dist",
        "build",
        "target",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".idea",
        ".vscode",
    }
)

# The scaffold sub-directories created inside the NEW dir.
_SCAFFOLD_DIRS = ("docs", "memory")


class TransmuteError(RuntimeError):
    """A non-destructive invariant was violated, or a write escaped the new dir."""


# ---------------------------------------------------------------------------
# Hash-inventory (non-destructive verification)
# ---------------------------------------------------------------------------


def _iter_source_files(root: Path):
    """Yield every regular file under ``root``, skipping noise dirs and symlinks.

    Symlinks are NOT followed (a symlink escaping the root would let us read — and
    later wrongly attribute changes to — files outside the source tree).
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda p: p.name)
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir():
                if entry.name in _SKIP_DIRS:
                    continue
                stack.append(entry)
            elif entry.is_file():
                yield entry


def hash_inventory(root: Path) -> dict[str, str]:
    """Map ``relative_posix_path -> sha256-hex`` for every file under ``root``.

    Deterministic and stable: keyed on POSIX-relative paths, sorted by the caller
    when serialized. This is the reference used to prove the source is untouched.
    """
    root = root.resolve()
    inventory: dict[str, str] = {}
    for f in _iter_source_files(root):
        h = hashlib.sha256()
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 16), b""):
                h.update(chunk)
        rel = f.resolve().relative_to(root).as_posix()
        inventory[rel] = h.hexdigest()
    return inventory


def diff_inventory(
    before: dict[str, str], after: dict[str, str]
) -> dict[str, list[str]]:
    """Return ``{added, removed, modified}`` between two inventories."""
    before_keys = set(before)
    after_keys = set(after)
    added = sorted(after_keys - before_keys)
    removed = sorted(before_keys - after_keys)
    modified = sorted(k for k in before_keys & after_keys if before[k] != after[k])
    return {"added": added, "removed": removed, "modified": modified}


# ---------------------------------------------------------------------------
# Write-guard
# ---------------------------------------------------------------------------


class WriteGuard:
    """Confine every filesystem write to a single allowed directory (the new dir).

    The source tree (``forbidden_root``) is read-only by construction: any attempt
    to write at/under it — or anywhere outside ``allowed_root`` — raises. This is
    the allowlist that neutralizes "agent deletes the source" classes of bugs.
    """

    def __init__(self, allowed_root: Path, forbidden_root: Path) -> None:
        self.allowed_root = allowed_root.resolve()
        self.forbidden_root = forbidden_root.resolve()

    def _is_within(self, child: Path, parent: Path) -> bool:
        try:
            child.resolve().relative_to(parent)
            return True
        except ValueError:
            return False

    def check(self, target: Path) -> Path:
        """Validate ``target`` is inside the allowed dir; raise otherwise."""
        resolved = target if target.is_absolute() else (self.allowed_root / target)
        # Resolve the nearest existing ancestor so a not-yet-created file still
        # resolves to a real location (symlink-escape safe).
        probe = resolved
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        anchor = probe.resolve() / resolved.name if probe != resolved else resolved.resolve()
        if self._is_within(anchor, self.forbidden_root):
            raise TransmuteError(
                f"write-guard: refusing to write inside the source tree: {target}"
            )
        if not self._is_within(anchor, self.allowed_root):
            raise TransmuteError(
                f"write-guard: write outside the new dir is forbidden: {target}"
            )
        return resolved

    def mkdir(self, target: Path) -> None:
        self.check(target).mkdir(parents=True, exist_ok=True)

    def write_text(self, target: Path, text: str) -> None:
        path = self.check(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# Manifest (path registry + idempotency)
# ---------------------------------------------------------------------------


def build_manifest(
    *,
    slug: str,
    source_root: Path,
    inventory: dict[str, str],
    project_type: str,
) -> dict[str, Any]:
    """Construct the ``.marvis-transmute.yaml`` payload.

    ``source_roots`` is a LIST (multi-language repos = N roots) so a future
    in-place KG indexer can iterate it. We register one root here (the imported
    dir); the ``{path, language, exclude}`` shape is forward-compatible.
    """
    return {
        "manifest_version": MANIFEST_VERSION,
        "slug": slug,
        "transmuted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "project_type": project_type,
        # Path registry: maps the Marvis metadata dir <-> source code location(s).
        # The KG index of the code in-place is a FOLLOW-UP slice (not built here).
        "source_roots": [
            {
                "path": str(source_root.resolve()),
                "language": None,
                "exclude": sorted(_SKIP_DIRS),
            }
        ],
        # Reference hash-inventory: the proof-of-untouched baseline (D3).
        "source_inventory": {
            "algorithm": "sha256",
            "file_count": len(inventory),
            "files": dict(sorted(inventory.items())),
        },
    }


def load_manifest(new_dir: Path) -> dict[str, Any] | None:
    """Read an existing manifest from ``new_dir`` (or ``None`` if absent)."""
    import yaml

    path = new_dir / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def manifest_drift(
    manifest: dict[str, Any], current_inventory: dict[str, str]
) -> dict[str, list[str]]:
    """Diff the source against the inventory recorded in an existing manifest."""
    recorded = (manifest.get("source_inventory") or {}).get("files") or {}
    return diff_inventory(recorded, current_inventory)
