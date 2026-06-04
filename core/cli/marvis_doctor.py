# v1.0.0 - 2026-05-28 - S1b: `marvis doctor` — install-health self-diagnostic
"""``marvis doctor`` — diagnose the MarvisX OSS installation and print
actionable remediation for every failure.

Registered onto the SAME Typer ``app`` as ``marvis init`` via ``register(app)``,
exactly like ``marvis_hooks`` / ``marvis_mcp`` / ``marvis_runtime``.

Design goals (plan S1b + RI-5):
- **Exit 1 only on ERROR** — warnings do not block; any error does.
- **Every failure = paste-ready fix command** — no fix printed means no ticket
  opened, which is the opposite of what we want.
- **Data-file integrity via ``importlib.resources``** — count shipped
  migrations / hook scripts / telemetry files against an internal manifest to
  catch an incomplete wheel BEFORE the user hits a confusing 500 / missing
  migration error. Never uses ``__file__`` or relative paths.
- **Connectivity is isolated + short-timeout** — skippable with ``--offline``.
  Never hangs.
- **Idempotent, fast, no side effects** — safe to run repeatedly and in CI.

Check levels:
- ``ok``      — healthy
- ``warning`` — degraded / sub-optimal; non-blocking
- ``error``   — broken; blocks on exit 1

``--json`` emits a machine-readable array of check results to stdout; all
human output goes to stderr (Rich), keeping ``marvis doctor --json | jq``
clean.
"""
from __future__ import annotations

import importlib.resources
import json
import os
import platform
import shutil
import socket
import stat
import sys
from pathlib import Path
from typing import Any, Literal

import typer

from core.cli._runtime_ctx import console
from core.platform import projects_root_default

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_PANEL_DOCTOR = "Diagnostics"

# Granite embedding model — OSS is Granite-only (ibm-granite/granite-embedding-97m-multilingual-r2).
# MiniLM fallback has been removed; machines below the RAM floor receive a
# warning (not a silent model swap).
GRANITE_MODEL_ID = "ibm-granite/granite-embedding-97m-multilingual-r2"
GRANITE_MODEL_DIMS = 384

# TODO(S0): replace 4 with the measured floor once S0 benchmarks are complete.
MIN_RAM_GB: float = 4.0

# Connectivity probe target — lightweight; no credentials required.
_PROBE_HOST = "justaskmarvis.com"
_PROBE_PORT = 443
_PROBE_TIMEOUT = 5.0  # seconds

# ---------------------------------------------------------------------------
# Internal data-file manifest (what a COMPLETE wheel must ship)
# ---------------------------------------------------------------------------

# Minimum counts for shipped data files, keyed by (importlib package, glob pattern).
# ``marvis doctor`` counts the matching resources and fails if below the floor.
# These floors are intentionally conservative (well below actual counts) so a
# new migration or hook script does not trigger a spurious failure — but a
# truncated wheel (the incident that prompted this feature, learning 9e527cfa)
# will be caught.
_MANIFEST_CHECKS: list[tuple[str, str, int, str]] = [
    # (package,               glob,  min_count, human_label)
    ("migrations", "*.sql", 50, "SQL migrations"),
    ("core.scripts.install_hooks", "*.sh", 4, "governance hook scripts"),
    ("core.scripts.install_hooks", "*.py", 1, "safety_bridge.py"),
    ("core.telemetry", "*.py", 2, "telemetry module files"),
    ("projects._template", "*.yaml", 1, "project scaffold template"),
]

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

CheckLevel = Literal["ok", "warning", "error"]


class CheckResult:
    __slots__ = ("name", "level", "detail", "fix")

    def __init__(
        self,
        name: str,
        level: CheckLevel,
        detail: str,
        fix: str = "",
    ) -> None:
        self.name = name
        self.level = level
        self.detail = detail
        self.fix = fix  # paste-ready command, empty when not needed

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "name": self.name,
            "level": self.level,
            "detail": self.detail,
        }
        if self.fix:
            d["fix"] = self.fix
        return d


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register(app: typer.Typer) -> None:
    """Attach ``doctor`` onto an existing Typer app."""
    app.command("doctor", rich_help_panel=_PANEL_DOCTOR)(doctor_cmd)


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def _check_os() -> CheckResult:
    """OS family, arch, and Rosetta detection."""
    system = platform.system()
    machine = platform.machine()
    node = platform.node()

    rosetta = False
    if system == "Darwin":
        # Rosetta translates x86_64 binaries on Apple Silicon. When a Python
        # binary compiled for x86_64 runs under Rosetta, platform.machine()
        # returns "x86_64" while the native chip is "arm64". We detect this by
        # checking sysctl.
        try:
            import subprocess  # lazy — kept inside check body

            result = subprocess.run(  # noqa: S603
                ["sysctl", "-n", "sysctl.proc_translated"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            rosetta = result.stdout.strip() == "1"
        except Exception:  # noqa: BLE001
            pass

    detail = f"{system} / {machine} (node={node})"
    if rosetta:
        return CheckResult(
            name="os",
            level="warning",
            detail=f"{detail} — running under Rosetta (x86_64 on Apple Silicon)",
            fix=(
                "Install a native arm64 Python and reinstall marvisx-cli:\n"
                "  brew install python@3.12  # arm64 brew\n"
                "  uv tool install marvisx-cli"
            ),
        )
    return CheckResult(name="os", level="ok", detail=detail)


def _check_python() -> CheckResult:
    """Python version + which interpreter resolves on PATH."""
    version = sys.version_info
    interpreter = sys.executable
    on_path = shutil.which("python3") or shutil.which("python")
    major = version.major
    minor = version.minor
    micro = getattr(version, "micro", 0)

    detail = (
        f"Python {major}.{minor}.{micro} "
        f"({interpreter}); PATH python={on_path or 'not found'}"
    )

    if (major, minor) < (3, 10):
        return CheckResult(
            name="python_version",
            level="error",
            detail=f"Python {major}.{minor} is below the required 3.10",
            fix=(
                "Install Python 3.10+:\n"
                "  # macOS:   brew install python@3.12\n"
                "  # Linux:   apt install python3.12 (or use pyenv)\n"
                "Then reinstall:  uv tool install marvisx-cli"
            ),
        )
    return CheckResult(name="python_version", level="ok", detail=detail)


def _detect_install_manager() -> str:
    """Return 'uv', 'pipx', 'pip', or 'unknown'."""
    if shutil.which("uv"):
        # Check if marvisx-cli is in uv's tool list (best effort).
        return "uv"
    if shutil.which("pipx"):
        return "pipx"
    if shutil.which("pip") or shutil.which("pip3"):
        return "pip"
    return "unknown"


def _check_install_manager() -> CheckResult:
    """Detect the install manager (uv / pipx / pip)."""
    manager = _detect_install_manager()
    if manager == "unknown":
        return CheckResult(
            name="install_manager",
            level="warning",
            detail="Could not detect uv, pipx, or pip on PATH",
            fix=(
                "Install uv (recommended) and reinstall:\n"
                "  curl -LsSf https://astral.sh/uv/install.sh | sh\n"
                "  uv tool install marvisx-cli"
            ),
        )
    return CheckResult(
        name="install_manager",
        level="ok",
        detail=f"install manager detected: {manager}",
    )


def _check_cli_on_path() -> CheckResult:
    """Verify ``shutil.which('marvis')`` resolves and points to the expected install."""
    which = shutil.which("marvis")
    if which is None:
        return CheckResult(
            name="cli_on_path",
            level="error",
            detail="'marvis' not found on PATH",
            fix=(
                "Add the tool bin directory to your PATH.\n"
                "  # uv:    export PATH=\"$HOME/.local/bin:$PATH\"\n"
                "  # pipx:  pipx ensurepath\n"
                "Then reload your shell or run:  source ~/.bashrc  (or ~/.zshrc)"
            ),
        )

    # Check that the resolved binary is under the same interpreter prefix — a
    # mismatch means a stale PATH entry from an old install.
    expected_prefix = Path(sys.executable).resolve().parent.parent
    which_path = Path(which).resolve()
    try:
        which_path.relative_to(expected_prefix)
        same_prefix = True
    except ValueError:
        same_prefix = False

    if not same_prefix:
        return CheckResult(
            name="cli_on_path",
            level="warning",
            detail=(
                f"'marvis' found at {which} but it is outside the current "
                f"interpreter prefix {expected_prefix}"
            ),
            fix=(
                "The CLI on PATH may belong to a different install. "
                "Reinstall under the active interpreter:\n"
                f"  uv tool install marvisx-cli  # uses {sys.executable}"
            ),
        )
    return CheckResult(
        name="cli_on_path",
        level="ok",
        detail=f"marvis found at {which}",
    )


def _check_config_dir() -> CheckResult:
    """Config dir (~/.marvis) exists and is owner-only readable."""
    vault_dir = os.environ.get("MARVIS_VAULT_DIR")
    base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"

    if not base.exists():
        return CheckResult(
            name="config_dir",
            level="warning",
            detail=f"Config directory {base} does not exist (run 'marvis init' first)",
            fix="marvis init",
        )

    if os.name != "posix":
        # POSIX mode bits are meaningless on Windows: owner-only is an ACL concern
        # (not yet enforced — see core.platform.secure_path). Report honestly; do
        # NOT read S_IROTH (always 0 -> would pass "by accident") or prescribe a
        # chmod fix (a silent no-op on Windows).
        return CheckResult(
            name="config_dir",
            level="ok",
            detail=f"{base} exists (Windows: POSIX perms N/A; owner-only ACL not verified)",
        )

    try:
        mode = base.stat().st_mode
    except OSError as exc:
        return CheckResult(
            name="config_dir",
            level="error",
            detail=f"Cannot stat {base}: {exc}",
            fix=f"chmod 700 {base}",
        )

    # Should be drwx------ (0o700) — world-readable is a security warning.
    world_readable = bool(mode & (stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH))
    group_readable = bool(mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP))
    if world_readable or group_readable:
        return CheckResult(
            name="config_dir",
            level="warning",
            detail=f"{base} has overly permissive mode {oct(stat.S_IMODE(mode))}",
            fix=f"chmod 700 {base}",
        )

    return CheckResult(
        name="config_dir",
        level="ok",
        detail=f"{base} exists (mode {oct(stat.S_IMODE(mode))})",
    )


def _check_config_parseable() -> CheckResult:
    """settings.yaml is parseable if it exists."""
    vault_dir = os.environ.get("MARVIS_VAULT_DIR")
    base = Path(vault_dir).expanduser() if vault_dir else Path.home() / ".marvis"
    settings_path_env = os.environ.get("MARVIS_SETTINGS_PATH")
    cfg = Path(settings_path_env).expanduser() if settings_path_env else base / "settings.yaml"

    if not cfg.exists():
        return CheckResult(
            name="config_parseable",
            level="warning",
            detail=f"settings.yaml not found at {cfg} (run 'marvis init')",
            fix="marvis init",
        )

    try:
        import yaml  # lazy — only needed if file exists

        data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="config_parseable",
            level="error",
            detail=f"settings.yaml at {cfg} is not valid YAML: {exc}",
            fix=(
                f"Inspect and fix the file manually:\n"
                f"  cat {cfg}\n"
                f"Or re-run setup to overwrite it:\n"
                f"  marvis init --yes"
            ),
        )

    if not isinstance(data, dict):
        return CheckResult(
            name="config_parseable",
            level="error",
            detail=f"settings.yaml root is not a mapping (got {type(data).__name__})",
            fix=f"marvis init --yes  # overwrites {cfg}",
        )

    return CheckResult(
        name="config_parseable",
        level="ok",
        detail=f"settings.yaml at {cfg} is valid YAML",
    )


def _check_resolved_paths() -> CheckResult:
    """The resolved DB + projects paths are absolute (not CWD-relative split-brain).

    A relative DB path opens a different SQLite file per launch directory — the
    Windows ``C:\\data`` / bare ``console.db`` class of bug. Reports the live
    paths so an agent reading ``doctor --json`` can detect a misconfigured root.
    """
    from core.api.config import settings

    db = Path(settings.db_path)
    projects = projects_root_default()
    detail = f"db={db} | projects={projects}"
    if not db.is_absolute():
        return CheckResult(
            name="resolved_paths",
            level="warning",
            detail=(
                f"DB path is relative ({db}) -> resolves against the current "
                f"directory (a different DB per launch dir). {detail}"
            ),
            fix="marvis init",
        )
    return CheckResult(name="resolved_paths", level="ok", detail=detail)


def _check_brain_schedule() -> CheckResult:
    """Agent-native hint: advertise ``marvis brain schedule --enable`` when no daily
    reflection timer is active on a platform that supports one. Reflection runs
    opportunistically (on use) regardless, so this is a suggestion, not an error.
    Ships only with the backend — never advertises a command that returns
    ``unsupported``. An agent reading ``doctor --json`` acts on the ``fix`` field."""
    from core.cli import _brain_schedule

    backend = _brain_schedule.detect_backend()
    if backend == "unsupported":
        return CheckResult(
            name="brain_schedule",
            level="ok",
            detail="no OS scheduler here; daily reflection runs opportunistically (on use)",
        )
    state = _brain_schedule.status()
    if state.get("enabled"):
        return CheckResult(
            name="brain_schedule",
            level="ok",
            detail=f"daily reflection scheduled ({backend})",
        )
    return CheckResult(
        name="brain_schedule",
        level="warning",
        detail=(
            f"no daily reflection timer active ({backend} available); reflection "
            "runs opportunistically until you schedule it"
        ),
        fix="marvis brain schedule --enable",
    )


def _check_data_files() -> list[CheckResult]:
    """Count shipped data files via ``importlib.resources`` against the internal manifest.

    This is the key check that catches an incomplete wheel BEFORE the user
    sees a confusing error at runtime (learning 9e527cfa: the incident that
    prompted this entire check).

    Uses ``importlib.resources`` exclusively — never ``__file__`` or
    relative filesystem paths — so it works correctly from both a source
    checkout and an installed wheel.
    """
    results: list[CheckResult] = []
    for pkg, pattern, min_count, label in _MANIFEST_CHECKS:
        try:
            pkg_ref = importlib.resources.files(pkg)
            # Count all direct children matching the glob pattern.
            # importlib.resources Traversable: iterate contents.
            suffix = pattern.lstrip("*")  # e.g. "*.sql" -> ".sql"
            count = sum(
                1
                for item in pkg_ref.iterdir()
                if item.is_file() and item.name.endswith(suffix)
            )
        except (ModuleNotFoundError, FileNotFoundError, AttributeError) as exc:
            results.append(
                CheckResult(
                    name=f"data_files_{pkg.replace('.', '_')}_{suffix.lstrip('.')}",
                    level="error",
                    detail=f"Cannot access package '{pkg}' for {label}: {exc}",
                    fix=(
                        "The installation is incomplete. Reinstall from a fresh wheel:\n"
                        "  pip uninstall marvisx-cli -y\n"
                        "  uv tool install marvisx-cli"
                    ),
                )
            )
            continue

        name = f"data_files_{pkg.replace('.', '_')}_{suffix.lstrip('.')}"
        if count < min_count:
            results.append(
                CheckResult(
                    name=name,
                    level="error",
                    detail=(
                        f"Expected at least {min_count} {label} ({pattern}), "
                        f"found {count} in package '{pkg}' — wheel may be incomplete"
                    ),
                    fix=(
                        "Reinstall from a complete wheel:\n"
                        "  pip uninstall marvisx-cli -y\n"
                        "  uv tool install marvisx-cli"
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    name=name,
                    level="ok",
                    detail=f"{label}: {count} files found in '{pkg}' (min {min_count})",
                )
            )
    return results


def _check_connectivity(*, offline: bool) -> CheckResult:
    """TCP connectivity probe to justaskmarvis.com:443.

    Isolated (plain socket, no HTTP library), short timeout, skippable with
    ``--offline``. Never hangs.
    """
    if offline:
        return CheckResult(
            name="connectivity",
            level="ok",
            detail="skipped (--offline)",
        )

    try:
        with socket.create_connection(
            (_PROBE_HOST, _PROBE_PORT), timeout=_PROBE_TIMEOUT
        ):
            pass
        return CheckResult(
            name="connectivity",
            level="ok",
            detail=f"TCP {_PROBE_HOST}:{_PROBE_PORT} reachable",
        )
    except OSError as exc:
        return CheckResult(
            name="connectivity",
            level="warning",
            detail=f"Cannot reach {_PROBE_HOST}:{_PROBE_PORT}: {exc}",
            fix=(
                "Check your internet connection.\n"
                "To skip this check when working offline:  marvis doctor --offline"
            ),
        )


def _check_granite_model() -> list[CheckResult]:
    """Check whether the Granite embedding model is present in the HF cache and
    whether available RAM meets the documented floor.

    Granite is the OSS-only embedding model (ibm-granite/granite-embedding-97m-multilingual-r2,
    384-dim). MiniLM has been removed as a fallback; machines below MIN_RAM_GB
    receive an actionable warning instead of a silent model swap.

    Imports are lazy — this check must never trigger a model load.
    """
    results: list[CheckResult] = []

    # --- RAM check ---
    try:
        import psutil  # optional dependency — graceful if absent

        available_gb = psutil.virtual_memory().available / (1024**3)
        total_gb = psutil.virtual_memory().total / (1024**3)

        if available_gb < MIN_RAM_GB:
            results.append(
                CheckResult(
                    name="granite_ram",
                    level="warning",
                    detail=(
                        f"Available RAM {available_gb:.1f} GB / {total_gb:.1f} GB total "
                        f"is below the recommended floor ({MIN_RAM_GB} GB). "
                        f"Local Granite embedding may be slow or fail to load."
                        # TODO(S0): update MIN_RAM_GB once the S0 benchmark measures
                        # the actual peak RAM under granite-embedding-97m-multilingual-r2.
                    ),
                    fix=(
                        "Free memory by closing other applications, or run "
                        "MarvisX on a machine that meets the RAM requirement. "
                        f"Local Granite embedding needs roughly {MIN_RAM_GB} GB "
                        "available (MarvisX OSS is Granite-only by design)."
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    name="granite_ram",
                    level="ok",
                    detail=(
                        f"Available RAM {available_gb:.1f} GB / {total_gb:.1f} GB "
                        f"(floor {MIN_RAM_GB} GB)"
                    ),
                )
            )
    except ImportError:
        results.append(
            CheckResult(
                name="granite_ram",
                level="warning",
                detail="psutil not available — cannot check available RAM",
                fix="pip install psutil  # optional, enables RAM check",
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(
            CheckResult(
                name="granite_ram",
                level="warning",
                detail=f"RAM check skipped: {exc}",
            )
        )

    # --- Model cache check ---
    hf_home = Path(os.environ.get("HF_HOME", "~/.cache/huggingface")).expanduser()
    # HuggingFace Hub stores models under <HF_HOME>/hub/models--<org>--<name>/
    # snapshots/<revision>/ where "/" in the model id is replaced by "--". The
    # torch-free engine needs the ONNX graph + tokenizer, so check those files
    # exist (not just the dir — that was a false-green: the dir can exist with
    # only a partial download).
    model_dir_name = "models--" + GRANITE_MODEL_ID.replace("/", "--")
    model_cache = hf_home / "hub" / model_dir_name
    snapshots = model_cache / "snapshots"

    required = ["onnx/model.onnx", "tokenizer.json"]
    found_snapshot = None
    if snapshots.is_dir():
        for snap in snapshots.iterdir():
            if all((snap / rel).exists() for rel in required):
                found_snapshot = snap
                break

    pre_download_fix = (
        "Pre-download to avoid first-run latency (torch-free):\n"
        "  python -c \""
        "from huggingface_hub import snapshot_download; "
        f"snapshot_download('{GRANITE_MODEL_ID}', "
        "allow_patterns=['onnx/model.onnx','tokenizer.json','tokenizer_config.json',"
        "'special_tokens_map.json','config.json','1_Pooling/config.json',"
        "'config_sentence_transformers.json'])"
        "\""
    )

    if found_snapshot is not None:
        results.append(
            CheckResult(
                name="granite_model_cache",
                level="ok",
                detail=f"Granite ONNX graph + tokenizer found at {found_snapshot}",
            )
        )
    elif model_cache.exists():
        results.append(
            CheckResult(
                name="granite_model_cache",
                level="warning",
                detail=(
                    f"Granite model dir exists at {model_cache} but the ONNX graph "
                    f"+ tokenizer ({', '.join(required)}) are missing or incomplete. "
                    "They will be fetched automatically on first use."
                ),
                fix=pre_download_fix,
            )
        )
    else:
        results.append(
            CheckResult(
                name="granite_model_cache",
                level="warning",
                detail=(
                    f"Granite model not yet downloaded to {model_cache}. "
                    "The ONNX graph + tokenizer will be fetched automatically on "
                    "first use (one-time download)."
                ),
                fix=pre_download_fix,
            )
        )

    return results


# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _check_semantic_search() -> list[CheckResult]:
    """Semantic-search readiness: the sqlite-vec (vec0) load path + index budget.

    The Granite onnx model + RAM floor are checked by _check_granite_model; here
    we probe the two clean-install failure modes that silently disable
    search-by-meaning (F1) — sqlite3 extension loading + the sqlite-vec binary —
    and report the OOM-bound index budget knob (F2). Never loads the model.
    """
    import sqlite3

    results: list[CheckResult] = []

    # F1 probe 1 — the #1 clean-install failure: a Python whose sqlite3 was built
    # WITHOUT loadable-extension support (Apple's stock /usr/bin/python3, some
    # Homebrew / python.org builds) → enable_load_extension raises before vec0 can
    # ever load. Unrecoverable without a different interpreter → ERROR.
    ext_ok = False
    try:
        conn = sqlite3.connect(":memory:")
        try:
            conn.enable_load_extension(True)
            ext_ok = True
        finally:
            conn.close()
    except (AttributeError, sqlite3.OperationalError) as exc:
        results.append(
            CheckResult(
                name="semantic_vec0",
                level="error",
                detail=(
                    f"Python's sqlite3 has no loadable-extension support ({exc}) → "
                    "the vector index (vec0) cannot load; semantic search is "
                    "unavailable (keyword search still works)."
                ),
                fix=(
                    "Use a Python built with loadable sqlite extensions. uv ships "
                    "its own CPython that supports them:\n"
                    "  uv tool install --force marvisx-cli\n"
                    "On macOS do NOT run the runtime under /usr/bin/python3."
                ),
            )
        )
        return results
    except Exception as exc:  # noqa: BLE001
        results.append(
            CheckResult(
                name="semantic_vec0",
                level="warning",
                detail=f"Could not probe sqlite3 extension loading: {exc}",
            )
        )

    # F1 probe 2 — sqlite-vec present + its platform binary resolves. Missing on a
    # partial install → WARNING (a clean `uv tool install` declares it; reinstall
    # fixes it), not a hard ERROR.
    if ext_ok:
        try:
            import sqlite_vec

            loadable = sqlite_vec.loadable_path()
            if loadable and Path(loadable).exists():
                results.append(
                    CheckResult(
                        name="semantic_vec0",
                        level="ok",
                        detail=(
                            "vec0 loadable (sqlite3 extensions + sqlite-vec binary present)"
                        ),
                    )
                )
            else:
                results.append(
                    CheckResult(
                        name="semantic_vec0",
                        level="warning",
                        detail=(
                            "sqlite-vec is installed but its loadable binary does not "
                            f"resolve (loadable_path={loadable!r}) → semantic search unavailable."
                        ),
                        fix="uv tool install --force marvisx-cli  # reinstall the platform wheel",
                    )
                )
        except ImportError:
            results.append(
                CheckResult(
                    name="semantic_vec0",
                    level="warning",
                    detail=(
                        "sqlite-vec is not installed → no vector index → semantic "
                        "search falls back to keyword only."
                    ),
                    fix="uv tool install --force marvisx-cli  # sqlite-vec is a declared dependency",
                )
            )
        except Exception as exc:  # noqa: BLE001
            results.append(
                CheckResult(
                    name="semantic_vec0",
                    level="warning",
                    detail=f"sqlite-vec probe skipped: {exc}",
                )
            )

    # F2 — index memory budget. Embedding a long doc is bounded by
    # EMBEDDING_TOKEN_BUDGET (batch × seqlen). On a low-total-RAM machine, recommend
    # lowering it (and the per-doc cap) before `marvis project index`.
    budget = os.environ.get("EMBEDDING_TOKEN_BUDGET", "16384")
    total_gb: float | None = None
    try:
        import psutil

        total_gb = psutil.virtual_memory().total / (1024**3)
    except Exception:  # noqa: BLE001 — psutil optional; granite_ram already warns
        total_gb = None

    if total_gb is not None and total_gb < 8.0:
        results.append(
            CheckResult(
                name="semantic_index_memory",
                level="warning",
                detail=(
                    f"Total RAM {total_gb:.1f} GB is modest for indexing long docs "
                    f"(batch budget EMBEDDING_TOKEN_BUDGET={budget} tokens × hidden). "
                    "A very long doc can still spike memory during `project index`."
                ),
                fix=(
                    "Lower the budget before indexing:\n"
                    "  export EMBEDDING_TOKEN_BUDGET=8192\n"
                    "  export EMBEDDING_MAX_TOKENS=2048"
                ),
            )
        )
    else:
        ram_note = f" (total RAM {total_gb:.1f} GB)" if total_gb is not None else ""
        results.append(
            CheckResult(
                name="semantic_index_memory",
                level="ok",
                detail=(
                    f"Index memory budget EMBEDDING_TOKEN_BUDGET={budget} tokens"
                    f"{ram_note}"
                ),
            )
        )

    return results


def doctor_cmd(
    offline: bool = typer.Option(
        False,
        "--offline",
        help="Skip the connectivity check (useful in air-gapped environments).",
    ),
    json_out: bool = typer.Option(
        False,
        "--json",
        help="Emit check results as a JSON array to stdout (machine-readable).",
    ),
) -> None:
    """Diagnose the MarvisX install health and print actionable remediation.

    Exits 0 if all checks pass (warnings are non-blocking).
    Exits 1 if any check reports ERROR.
    """
    checks: list[CheckResult] = []

    # Resolve the user's settings.yaml into `settings` BEFORE any check reads a
    # path, so resolved_paths reports the configured db / projects_root instead
    # of the module defaults (the ~/Library/Application Support cosmetic bug).
    # Best-effort: a missing/unreadable settings.yaml leaves the defaults, which
    # is the correct state for a not-yet-initialized install.
    try:
        from core.api.runtime_settings import apply_marvis_settings

        apply_marvis_settings()
    except Exception:  # noqa: BLE001 — doctor must never crash on settings load
        pass

    checks.append(_check_os())
    checks.append(_check_python())
    checks.append(_check_install_manager())
    checks.append(_check_cli_on_path())
    checks.append(_check_config_dir())
    checks.append(_check_config_parseable())
    checks.append(_check_resolved_paths())
    checks.append(_check_brain_schedule())
    checks.extend(_check_data_files())
    checks.append(_check_connectivity(offline=offline))
    checks.extend(_check_granite_model())
    checks.extend(_check_semantic_search())

    has_error = any(c.level == "error" for c in checks)

    if json_out:
        sys.stdout.write(
            json.dumps([c.to_dict() for c in checks], indent=2, ensure_ascii=False)
        )
        sys.stdout.write("\n")
    else:
        _render_human(checks)

    if has_error:
        raise typer.Exit(1)


def _render_human(checks: list[CheckResult]) -> None:
    """Render the check results as a compact Rich table to stdout."""
    from rich.table import Table  # lazy — only for human output

    table = Table(
        title="marvis doctor",
        show_header=True,
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("Check", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail")

    level_style = {"ok": "green", "warning": "yellow", "error": "red"}
    level_icon = {"ok": "✓", "warning": "!", "error": "✗"}

    for c in checks:
        style = level_style[c.level]
        icon = level_icon[c.level]
        table.add_row(
            c.name,
            f"[{style}]{icon} {c.level.upper()}[/{style}]",
            c.detail,
        )

    console.print(table)

    # Print fix commands below the table for easy copy-paste.
    fixable = [c for c in checks if c.fix]
    if fixable:
        console.print()
        console.print("[bold]Remediation:[/bold]")
        for c in fixable:
            icon = level_icon[c.level]
            style = level_style[c.level]
            console.print(f"  [{style}]{icon}[/{style}] [bold]{c.name}[/bold]")
            for line in c.fix.splitlines():
                console.print(f"      {line}")

    has_error = any(c.level == "error" for c in checks)
    has_warning = any(c.level == "warning" for c in checks)

    console.print()
    if has_error:
        console.print("[red bold]✗ ERROR: one or more checks failed.[/red bold]")
    elif has_warning:
        console.print("[yellow]! WARNING: install is functional but has issues.[/yellow]")
    else:
        console.print("[green bold]✓ All checks passed.[/green bold]")

    console.print()
    console.print(
        "[dim]Tip: `marvis guide` explains how Marvis organizes work — point "
        "your agent at an existing folder to adopt it.[/dim]"
    )
