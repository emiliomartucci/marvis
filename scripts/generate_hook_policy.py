#!/usr/bin/env python3
"""Generate or verify the packaged hook rule engine from its canonical source."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets


SCHEMA = "marvis-hook-policy/v1"


class HookGenerationError(RuntimeError):
    pass


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _load(root: Path) -> tuple[Path, dict[str, object]]:
    path = root / "contracts/hooks/policy-v1.json"
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HookGenerationError("hook policy manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise HookGenerationError("unsupported hook policy manifest")
    return path, manifest


def _atomic_write(path: Path, raw: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    descriptor = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, path)
    if mode is not None:
        os.chmod(path, mode)


def generate(root: Path, *, write: bool) -> str:
    manifest_path, manifest = _load(root)
    canonical = root / str(manifest.get("canonical_source", ""))
    generated = root / str(manifest.get("generated_resource", ""))
    if not canonical.is_file():
        raise HookGenerationError("canonical hook source missing")
    raw = canonical.read_bytes()
    digest = _sha(raw)
    if write:
        _atomic_write(generated, raw, mode=canonical.stat().st_mode & 0o777)
        manifest["policy_sha256"] = digest
        manifest_raw = (
            json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
        ).encode("utf-8")
        _atomic_write(manifest_path, manifest_raw, mode=0o644)
    else:
        if not generated.is_file() or generated.read_bytes() != raw:
            raise HookGenerationError("packaged hook resource drifted from canonical source")
        if manifest.get("policy_sha256") != digest:
            raise HookGenerationError("hook policy digest drift")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        digest = generate(args.root.resolve(), write=args.write)
    except HookGenerationError as exc:
        print(f"hook generation: FAIL: {exc}")
        return 1
    print(f"hook generation: PASS sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
