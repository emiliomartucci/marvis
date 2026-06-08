#!/usr/bin/env python3
"""v1.0.0 - 2026-04-22 - docs-code drift check livello B.

Phase 7.x hygiene (plan §Pilastro 3). Verifies:
- Check A: MCP tool count (docs vs runtime tools/list)
- Check B: edge_types enum sync nei 4 posti (Python EDGE_TYPES tuple,
  Python EdgeType Literal, JS edgeTypeEnum array, SQL CHECK).

Exit 0 all-OK, 1 drift (with diff on stderr).
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

DEFAULT_WORKSPACE = Path(os.environ.get("WORKSPACE") or Path(__file__).parent.parent)
DEFAULT_DB_PATH = Path(os.environ.get("PIR_DB_PATH") or "/data/pir/console.db")
DEFAULT_ENV_FILE = Path(os.environ.get("PIR_ENV_FILE") or "/data/pir/.env")
DEFAULT_TASKS_API_URL = "https://api.justaskmarvis.com/api/v1/tasks"
DEDUP_WINDOW_DAYS = 7
SAFE_VALUE_RE = re.compile(r"^[a-zA-Z0-9_./:-]+$")

# Allow override via WORKSPACE env var so CI/cron can point at production
# workspace even when the script itself lives in a worktree checkout.
REPO = DEFAULT_WORKSPACE.resolve()
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from core.api.services.docs_governance.hard_gates import (  # noqa: E402
    SECURITY_LEAK_GREP_PATTERN,
    SECURITY_LEAK_PATTERN_SOURCES,
    check_no_security_leaks,
)


def set_workspace(workspace: str | Path) -> None:
    """Update the inspected repository root for CLI callers."""
    global REPO
    REPO = Path(workspace).expanduser().resolve()
    if str(REPO) not in sys.path:
        sys.path.insert(0, str(REPO))


def _safe_open(path: Path, flags: int, mode: int = 0o600) -> int:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if path.parent.is_symlink() or path.is_symlink():
        raise OSError(f"refusing symlink path: {path}")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags | nofollow, mode)


@contextlib.contextmanager
def safe_log_redirect(log_path: Path) -> Iterator[None]:
    """Redirect stdout/stderr to a log opened with O_NOFOLLOW."""
    fd = _safe_open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND)
    log_file = os.fdopen(fd, "a", encoding="utf-8", buffering=1)
    try:
        with contextlib.redirect_stdout(log_file), contextlib.redirect_stderr(log_file):
            yield
    finally:
        log_file.close()


@contextlib.contextmanager
def nonblocking_file_lock(lock_path: Path) -> Iterator[bool]:
    """Acquire a non-blocking flock using a lock file opened with O_NOFOLLOW."""
    fd: int | None = None
    acquired = False
    try:
        fd = _safe_open(lock_path, os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except BlockingIOError:
            acquired = False
        yield acquired
    finally:
        if fd is not None:
            if acquired:
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _stable_items(diff_items: object) -> list[str]:
    if isinstance(diff_items, dict):
        return [json.dumps(diff_items, sort_keys=True, separators=(",", ":"))]
    if isinstance(diff_items, list):
        items: list[str] = []
        for item in diff_items:
            if isinstance(item, str):
                items.append(item)
            else:
                items.append(json.dumps(item, sort_keys=True, separators=(",", ":")))
        return sorted(items)
    return [str(diff_items)]


def compute_fingerprint(check_name: str, diff_items: object) -> str:
    """Deterministic hash of a drift check name and its diff payload."""
    payload = check_name + "|" + "|".join(_stable_items(diff_items))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sanitize_scalar(value: object) -> object:
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, str):
        if len(value) > 500 or ".." in value or not SAFE_VALUE_RE.fullmatch(value):
            return "<sanitized>"
        return value
    rendered = str(value)[:200]
    if ".." in rendered or not SAFE_VALUE_RE.fullmatch(rendered):
        return "<sanitized>"
    return rendered


def sanitize_drift_detail(detail: dict[str, object]) -> dict[str, object]:
    """Whitelist drift detail values before durable storage."""
    clean: dict[str, object] = {}
    for key, value in detail.items():
        clean_key = key if SAFE_VALUE_RE.fullmatch(key) else "sanitized_key"
        if isinstance(value, dict):
            clean[clean_key] = sanitize_drift_detail(value)
        elif isinstance(value, list):
            clean[clean_key] = [_sanitize_scalar(item) for item in value]
        else:
            clean[clean_key] = _sanitize_scalar(value)
    return clean


def open_db(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA cache_size = -64000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.row_factory = sqlite3.Row
    return conn


def _env_value_from_file(name: str, env_file: Path = DEFAULT_ENV_FILE) -> str | None:
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    prefix = f"{name}="
    export_prefix = f"export {name}="
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(export_prefix):
            value = line[len(export_prefix) :]
        elif line.startswith(prefix):
            value = line[len(prefix) :]
        else:
            continue
        return value.strip().strip('"').strip("'")
    return None


def _pir_api_token() -> str | None:
    return (
        os.environ.get("PIR_API_TOKEN")
        or _env_value_from_file("PIR_API_TOKEN")
        or os.environ.get("TASKS_API_TOKEN")
        or _env_value_from_file("TASKS_API_TOKEN")
    )


def _tasks_api_url() -> str:
    raw_url = (
        os.environ.get("PIR_TASKS_API_URL")
        or os.environ.get("PIR_API_URL")
        or DEFAULT_TASKS_API_URL
    )
    normalized = raw_url.rstrip("/")
    if normalized.endswith("/api/v1/tasks"):
        return normalized
    return f"{normalized}/api/v1/tasks"


def create_pir_task(check_name: str, drift_detail: dict[str, object]) -> str | None:
    """Create a Marvis task via curl and Bearer auth; return the task id if created."""
    token = _pir_api_token()
    if not token:
        print("[drift] WARN: PIR_API_TOKEN missing, task creation skipped", file=sys.stderr)
        return None

    detail_json = json.dumps(drift_detail, indent=2, sort_keys=True)
    body = {
        "title": f"Docs drift: {check_name}",
        "description": (
            "Drift detected by automated post-commit check.\n\n"
            f"Details:\n{detail_json[:8000]}\n-/data/projects/marvisx"
        ),
        "project": "marvisx",
        "priority": "high",
        "source": "session",
        "tags": ["docs-drift", check_name[:40]],
        "impact": 7,
        "confidence": 8,
        "ease": 5,
        "delegation": "hybrid",
        "completion_mode": "pr",
    }
    api_url = _tasks_api_url()
    curl_config = "\n".join(
        [
            'request = "POST"',
            f"url = {json.dumps(api_url)}",
            'header = "Content-Type: application/json"',
            f"header = {json.dumps(f'Authorization: Bearer {token}')}",
            'header = "X-Agent-Name: marvisx"',
            f"data = {json.dumps(json.dumps(body))}",
        ]
    )
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "--config",
            "-",
        ],
        input=curl_config,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print(f"[drift] WARN: task creation curl failed: {result.stderr[:300]}", file=sys.stderr)
        return None
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"[drift] WARN: task creation returned non-JSON: {result.stdout[:300]}", file=sys.stderr)
        return None
    task_id = response.get("id") or response.get("task_id")
    if not task_id:
        print(f"[drift] WARN: task creation response missing id: {result.stdout[:300]}", file=sys.stderr)
        return None
    return str(task_id)


def _expire_inactive_dedup_rows(
    conn: sqlite3.Connection,
    *,
    fingerprint: str,
) -> None:
    window = f"-{DEDUP_WINDOW_DAYS} days"
    conn.execute(
        """
        UPDATE docs_drift_history
        SET dedup_expires_at = NULL
        WHERE fingerprint = ?
          AND dedup_expires_at IS NOT NULL
          AND (
            last_seen_at <= datetime('now', ?)
            OR opened_task_id IN (
                SELECT id FROM tasks WHERE status IN ('completed', 'rejected', 'failed')
            )
          )
        """,
        (fingerprint, window),
    )


def dedup_check(
    check_name: str,
    fingerprint: str,
    drift_detail: dict[str, object],
    *,
    db_path: str | Path = DEFAULT_DB_PATH,
    task_creator: Callable[[str, dict[str, object]], str | None] = create_pir_task,
) -> dict[str, object]:
    """Race-safe dedup insert; create one Marvis task for a new active drift."""
    clean_detail = sanitize_drift_detail(drift_detail)
    conn = open_db(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        _expire_inactive_dedup_rows(conn, fingerprint=fingerprint)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO docs_drift_history
                (check_name, fingerprint, drift_detail)
            VALUES (?, ?, ?)
            """,
            (check_name, fingerprint, json.dumps(clean_detail, sort_keys=True)),
        )
        if cursor.rowcount == 0:
            conn.execute(
                """
                UPDATE docs_drift_history
                SET last_seen_at = CURRENT_TIMESTAMP,
                    dedup_expires_at = datetime('now', '+7 days')
                WHERE fingerprint = ?
                  AND dedup_expires_at IS NOT NULL
                """,
                (fingerprint,),
            )
            row = conn.execute(
                """
                SELECT opened_task_id
                FROM docs_drift_history
                WHERE fingerprint = ?
                  AND dedup_expires_at IS NOT NULL
                ORDER BY last_seen_at DESC
                LIMIT 1
                """,
                (fingerprint,),
            ).fetchone()
            conn.commit()
            existing_task_id = row["opened_task_id"] if row else None
            return {"action": "update", "existing_task_id": existing_task_id}

        history_id = int(cursor.lastrowid)
        conn.commit()

        task_id = task_creator(check_name, clean_detail)
        if task_id:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE docs_drift_history SET opened_task_id = ? WHERE id = ?",
                (task_id, history_id),
            )
            conn.commit()
        return {"action": "insert", "existing_task_id": task_id}
    finally:
        conn.close()


def extract_python_iterable(path: Path, var_name: str) -> set[str]:
    """Extract values from `VAR = tuple(...)` or `VAR = Literal[...]`."""
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        target = None
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == var_name):
            continue
        value = node.value
        # Literal[...]
        if isinstance(value, ast.Subscript) and getattr(value.value, "id", None) == "Literal":
            slice_val = value.slice
            elts = slice_val.elts if isinstance(slice_val, ast.Tuple) else [slice_val]
            return {e.value for e in elts if isinstance(e, ast.Constant)}
        # tuple/list/set literal
        if isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return {e.value for e in value.elts if isinstance(e, ast.Constant)}
    raise ValueError(f"{var_name} not found in {path}")


def extract_js_enum(path: Path, var_name: str) -> set[str]:
    """Extract from `const VAR = z.enum([...])` — strip line comments first."""
    src = path.read_text()
    src = re.sub(r"//[^\n]*", "", src)  # kieran-ts punto 5: strip comments pre-match
    pattern = rf"{re.escape(var_name)}\s*=\s*z\.enum\(\[([^\]]+)\]\)"
    m = re.search(pattern, src, re.DOTALL)
    if not m:
        raise ValueError(f"{var_name} not found in {path}")
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


def extract_sql_check(path: Path) -> set[str]:
    """Extract `CHECK(relation IN ('a','b',...))` from migration.

    Strips SQL line comments (`-- ...`) first so inline parens in comments like
    `-- code (Fase 1a)` don't terminate the capture group prematurely.
    """
    src = path.read_text()
    # Strip `-- ...` line comments (mirror of kieran-ts punto 5 for SQL).
    src = re.sub(r"--[^\n]*", "", src)
    m = re.search(
        r"CHECK\s*\(\s*relation\s+IN\s*\(([^)]+)\)", src, re.DOTALL | re.IGNORECASE
    )
    if not m:
        raise ValueError(f"CHECK(relation IN ...) not found in {path}")
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


_MIGRATION_VERSION_RE = re.compile(r"^(\d+)_")


def latest_edge_migration() -> Path:
    """Find last migration whose CHECK(relation) declaration is canonical.

    Considers both the legacy `*_kg_edge_*.sql` family and the broader
    `*_kg_*.sql` family (e.g. mig 132 `132_kg_pr_modifies.sql`), then picks
    the highest version prefix. The previous implementation stopped at the
    first matching glob, which masked migrations like 132 behind the
    older 085 entry.
    """
    migrations_dir = REPO / "migrations"
    seen: dict[int, Path] = {}
    for pattern in ("*_kg_edge_*.sql", "*_kg_*.sql"):
        for candidate in migrations_dir.glob(pattern):
            if candidate.stem.endswith("_down"):
                continue
            m = _MIGRATION_VERSION_RE.match(candidate.name)
            if not m:
                continue
            version = int(m.group(1))
            # Only keep the migration if it actually contains a relation CHECK.
            try:
                extract_sql_check(candidate)
            except ValueError:
                continue
            seen[version] = candidate
    if not seen:
        raise FileNotFoundError("No forward kg_edge migration found")
    return seen[max(seen)]


def extract_ts_string_array(path: Path, field_name: str) -> set[str]:
    """Extract `field_name: ['a', 'b', ...]` literal array from TS source.

    Used for `apps/docs/lib/kg-fetcher.ts` `edge_types:` static array.
    Tolerant of single or double quotes and trailing commas.
    """
    src = path.read_text()
    pattern = rf"{re.escape(field_name)}\s*:\s*\[([^\]]+)\]"
    m = re.search(pattern, src, re.DOTALL)
    if not m:
        raise ValueError(f"{field_name}: [...] not found in {path}")
    return set(re.findall(r"['\"]([a-z_]+)['\"]", m.group(1)))


def extract_json_string_array(path: Path, key: str) -> set[str]:
    """Extract `"key": ["a", "b", ...]` array from a JSON file.

    Parses with `json` so the snapshot stays canonical (no comment leak).
    """
    data = json.loads(path.read_text())
    if key not in data:
        raise ValueError(f"key {key!r} not found in {path}")
    values = data[key]
    if not isinstance(values, list):
        raise ValueError(f"{path}:{key} is not a list (got {type(values).__name__})")
    return {v for v in values if isinstance(v, str)}


def check_a_tool_count() -> tuple[bool, str]:
    """Verify MCP tools/list count matches baseline 53.

    Run node from mcp-pir/ dir so node_modules resolution works (Zod is a
    local dep, not global).
    """
    # Baseline hardcoded (aggiornabile futuro via baseline file)
    BASELINE = 54  # Post Phase 7.x (graph_capabilities added)
    mcp_dir = REPO / "mcp-pir"
    proc = subprocess.run(
        ["node", "index.mjs"],
        input='{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n',
        capture_output=True,
        text=True,
        timeout=15,
        cwd=str(mcp_dir),
    )
    # MCP server may emit multiple JSON-RPC responses on stdout. Parse the
    # last non-empty line that has a `result` key.
    response = None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and "result" in candidate:
            response = candidate
            break
    if response is None:
        stderr_hint = proc.stderr[:200] if proc.stderr else "no stderr"
        return False, f"MCP tools/list no response. stderr: {stderr_hint}"
    try:
        count = len(response["result"]["tools"])
    except (KeyError, TypeError) as e:
        return False, f"MCP tools/list parse failed: {e}"
    if count != BASELINE:
        return False, f"MCP tool count drift: got {count}, baseline {BASELINE}"
    return True, f"MCP tool count OK: {count}"


def check_b_edge_types_sync() -> tuple[bool, str]:
    """Verify edge_types enum sync across 6 sources (mandate per
    docs/plans/sub/2026-05-16-kg-pr-impact-01-backend-data-pipeline.md §D7).
    """
    sources: dict[str, set[str]] = {}
    errors: list[str] = []
    try:
        sources["graph_service.py"] = extract_python_iterable(
            REPO / "api/services/graph_service.py", "EDGE_TYPES"
        )
    except Exception as e:
        errors.append(f"graph_service.py: {e}")
    try:
        sources["graph.py"] = extract_python_iterable(
            REPO / "api/routers/graph.py", "EdgeType"
        )
    except Exception as e:
        errors.append(f"graph.py: {e}")
    try:
        sources["mcp-pir/index.mjs"] = extract_js_enum(
            REPO / "mcp-pir/index.mjs", "edgeTypeEnum"
        )
    except Exception as e:
        errors.append(f"mcp-pir: {e}")
    try:
        sources["kg-fetcher.ts"] = extract_ts_string_array(
            REPO / "apps/docs/lib/kg-fetcher.ts", "edge_types"
        )
    except Exception as e:
        errors.append(f"kg-fetcher.ts: {e}")
    try:
        sources["kg-schema-snapshot.json"] = extract_json_string_array(
            REPO / "apps/docs/lib/kg-schema-snapshot.json", "edge_types"
        )
    except Exception as e:
        errors.append(f"kg-schema-snapshot.json: {e}")
    try:
        mig = latest_edge_migration()
        sources[mig.name] = extract_sql_check(mig)
    except Exception as e:
        errors.append(f"migration: {e}")
    if errors:
        return False, f"Extraction errors: {'; '.join(errors)}"
    # All sets must be equal
    reference = next(iter(sources.values()))
    for name, s in sources.items():
        if s != reference:
            diff_msg = []
            for name2, s2 in sources.items():
                diff_msg.append(f"  {name2}: {sorted(s2)}")
            return False, "edge_types drift:\n" + "\n".join(diff_msg)
    return True, f"edge_types sync OK across {len(sources)} sources ({len(reference)} values)"


def _grep_security_matches(text: str) -> bool:
    proc = subprocess.run(
        ["grep", "-Piq", SECURITY_LEAK_GREP_PATTERN],
        input=text,
        capture_output=True,
        text=True,
        timeout=5,
    )
    return proc.returncode == 0


def check_c_security_grep_runtime_sync() -> tuple[bool, str]:
    """Keep manual grep checks aligned with runtime no_security_leaks."""
    required_names = {
        "internal_url",
        "personal_gateway_domain",
        "internal_path",
        "internal_codename",
        "agent_transparency",
        "bare_tailscale_ip",
    }
    actual_names = {name for name, _pattern, _flags in SECURITY_LEAK_PATTERN_SOURCES}
    if actual_names != required_names:
        return False, (
            "security pattern name drift: "
            f"missing={sorted(required_names - actual_names)} "
            f"extra={sorted(actual_names - required_names)}"
        )

    samples = [
        ("internal_url", "http://127.0.0.1:4000"),
        ("personal_gateway_domain", "https://llm.example.invalid"),
        ("internal_path", "/home/user/workspace"),
        ("internal_codename", "marvis" + "-brain"),
        ("agent_transparency", "2/4" + " agent disclosure"),
        ("bare_tailscale_ip", "100.103.221.55"),
        ("clean", "Deploy the docs site for {{instance_name}}."),
    ]
    mismatches: list[str] = []
    for name, sample in samples:
        runtime_match = not check_no_security_leaks(sample).passed
        grep_match = _grep_security_matches(sample)
        if runtime_match != grep_match:
            mismatches.append(f"{name}: runtime={runtime_match} grep={grep_match}")

    if mismatches:
        return False, "security grep/runtime drift: " + "; ".join(mismatches)
    return True, f"security grep/runtime sync OK ({len(required_names)} patterns)"


CHECKS: tuple[tuple[str, str, Callable[[], tuple[bool, str]]], ...] = (
    ("tool_count", "A (tool count)", check_a_tool_count),
    ("edge_types_sync", "B (edge types)", check_b_edge_types_sync),
    (
        "security_grep_runtime_sync",
        "C (security grep/runtime)",
        check_c_security_grep_runtime_sync,
    ),
)


def _drift_detail(check_name: str, message: str, source: str) -> dict[str, object]:
    return {
        "check_name": check_name,
        "source": source,
        "workspace": str(REPO),
        "message": message,
        "lines": [line.strip() for line in message.splitlines() if line.strip()],
    }


def record_failed_checks(
    failures: list[tuple[str, str]],
    *,
    source: str,
    db_path: str | Path = DEFAULT_DB_PATH,
) -> None:
    for check_name, message in failures:
        detail = _drift_detail(check_name, message, source)
        fingerprint = compute_fingerprint(check_name, detail["lines"] or message)
        try:
            result = dedup_check(
                check_name,
                fingerprint,
                detail,
                db_path=db_path,
            )
        except Exception as exc:
            print(f"[drift] WARN: dedup failed for {check_name}: {exc}", file=sys.stderr)
            continue
        print(
            "[drift] "
            f"{result['action']} fingerprint={fingerprint} "
            f"task={result.get('existing_task_id')}",
            file=sys.stderr,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--workspace",
        default=str(REPO),
        help="Repository root to inspect. Defaults to WORKSPACE or this checkout.",
    )
    parser.add_argument(
        "--source",
        default="manual",
        choices=("manual", "post-commit"),
        help="Only post-commit records drift to docs_drift_history and Marvis.",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="SQLite DB path for drift history.",
    )
    parser.add_argument(
        "--log-file",
        default=None,
        help="Optional log path opened with O_NOFOLLOW before checks run.",
    )
    parser.add_argument(
        "--lock-file",
        default=None,
        help="Optional non-blocking flock path opened with O_NOFOLLOW.",
    )
    return parser


def run_checks(*, source: str, db_path: str | Path) -> int:
    ok = True
    print("docs-drift-check v1.0.0", file=sys.stderr)
    failures: list[tuple[str, str]] = []
    for check_id, check_name, check_fn in CHECKS:
        passed, msg = check_fn()
        status = "PASS" if passed else "FAIL"
        print(f"  [{status}] {check_name}: {msg}", file=sys.stderr)
        ok = ok and passed
        if not passed:
            failures.append((check_id, msg))
    if failures and source == "post-commit":
        record_failed_checks(failures, source=source, db_path=db_path)
    return 0 if ok else 1


def _main_with_lock(args: argparse.Namespace) -> int:
    if args.lock_file:
        with nonblocking_file_lock(Path(args.lock_file)) as acquired:
            if not acquired:
                print("[drift] lock busy; skipping this run", file=sys.stderr)
                return 0
            return run_checks(source=args.source, db_path=args.db_path)
    return run_checks(source=args.source, db_path=args.db_path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_workspace(args.workspace)
    if args.log_file:
        with safe_log_redirect(Path(args.log_file)):
            return _main_with_lock(args)
    return _main_with_lock(args)


if __name__ == "__main__":
    sys.exit(main())
