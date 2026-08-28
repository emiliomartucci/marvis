#!/bin/bash
# v1.6.0 - 2026-08-04 - Step 9b excuses a repo ast_parser scanned for zero source
#   files (default-floor repos only; an explicit-floor repo like marvisx stays
#   checked, since marvisx with no source is a broken checkout). Uses ast_parser's
#   own python_files+typescript_files count, never a re-derived scan glob.
# v1.5.0 - 2026-08-03 - Step 9b smoke gate is per-project and fail-closed:
#   every repo step 8 attempted must end with code nodes, and an empty
#   attempted set is a red run. The marvisx-only count let any external repo
#   index zero and still report success.
# v1.4.0 - 2026-06-22 - Tenant-aware full-rebuild: env-driven DB/projects/repo/venv + per-tenant watcher control + doc-store index step.
# v1.3.0 - 2026-05-22 - Re-include marvisx monorepo in step 8 code indexing (regression from v1.2.0); fix populate_temporal git cwd + populate_cross_project --marvisx-repo-root (was looking in /data/pir/core/.claude)
# v1.2.1 - 2026-05-15 - Hot-fix: pass --scan-patterns generic to ast_parser for external repos (marvisx-default api/* + console/src/* missed everything)
# v1.2.0 - 2026-05-15 - KG multi-repo: scan ~/repos/*/ external repos for code nodes (ast_parser --repo-root + --project)
# v1.1.0 - 2026-04-16 - KG Phase 6: add populate_artifacts --all-projects step
# v1.0.0 - 2026-04-15 - KG Phase 3: nightly full-rebuild safety net
#
# Usage:
#     scripts/kg_full_rebuild.sh                    # default scope all-projects
#     KG_REBUILD_LOG_DIR=/tmp scripts/kg_full_rebuild.sh
#
# ## Race resolution con kg-watcher (CRITICO — DOCUMENTATO OVUNQUE)
#
# Questo script PAUSA il watcher per-tenant prima di partire e lo riavvia
# alla fine. Senza questa coordination, watcher e rebuild scrivono entrambi
# su console.db → SQLite serializza con BEGIN IMMEDIATE → contention → il
# watcher rallenta o va in backoff timeout.
#
# COST NOTO: durante l'esecuzione (~5-15min su 68 progetti) eventuali file
# scritti in /data/projects/* NON sono auto-indicizzati real-time. Il rebuild
# stesso li raccoglie, quindi il graph resta consistente entro 24h. In pratica
# nessuno scrive di notte, quindi il gap e' invisibile.
#
# `trap EXIT` garantisce che il watcher venga riavviato anche su crash/timeout.
#
# ## Steps
#
# 1. Backup pre-rebuild via scripts/backup-db.sh (sqlite3 .backup atomic + integrity)
# 2. STOP per-tenant watcher unit
# 3. populate_artifacts (marvisx: commits + tasks + handoffs + knowledge)
# 4. populate_artifacts --all-projects --exclude-projects=marvisx
#    (handoff + knowledge_docs per gli altri progetti, metadata-only)
# 5. populate_cross_project --include-all-projects (~68 progetti scope esteso)
# 6. populate_temporal (touch_count_7d/30d aging)
# 7. populate_touch_counter (git blame counts)
# 8. populate_code multi-repo (marvisx + KG_EXTERNAL_REPOS_EXTRA)
# 9. reindex_documents --db "$DB_PATH"
# 10. restart per-tenant watcher unit
#
# Log: $KG_REBUILD_LOG_DIR/kg-full-rebuild-YYYYMMDDHHMMSS.log (default /tmp,
# keep-10 rotation).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB_PATH="${KG_DB_PATH:-${MARVIS_DB_PATH:-${PIR_DB_PATH:-${DB_PATH:-/data/pir/console.db}}}}"
LOG_DIR="${KG_REBUILD_LOG_DIR:-/tmp}"
TS=$(date -u +'%Y%m%d%H%M%S')
LOG_FILE="${LOG_DIR}/kg-full-rebuild-${TS}.log"
TENANT_ID="${TENANT_ID:-${PIR_TENANT_ID:-${MARVIS_INSTANCE:-}}}"
WATCHER_UNIT="${KG_WATCHER_UNIT:-${TENANT_ID:+marvis-kg-watcher@${TENANT_ID}.service}}"
WATCHER_UNIT="${WATCHER_UNIT:-pir-kg-watcher.service}"
SYSTEMD_SCOPE="${SYSTEMD_SCOPE:-system}"
PYTHON_BIN="${KG_PYTHON_BIN:-${MARVIS_VENV:-/data/pir/venv}/bin/python}"
# Phase 3 bug fix: populate_commits + populate_touch_counter chiamano `git log`.
# Lo script gira da /data/pir (WorkingDirectory del systemd unit) che NON e'
# un git repo. KG_REPO_ROOT punta al workspace marvisx (git repo monorepo).
KG_REPO_ROOT="${KG_REPO_ROOT:-${MARVIS_REPO_ROOT:-${HOME}/workspace}}"
PROJECTS_ROOT="${MARVIS_PROJECTS_ROOT:-${KG_PROJECTS_ROOT:-${KG_PROJECTS_DIR:-/data/projects}}}"
export MARVIS_PROJECTS_ROOT="$PROJECTS_ROOT"
WATCHER_WAS_ACTIVE=0
KG_EPHEMERAL_SRC=""

# --- helpers ---------------------------------------------------------------

log() {
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"
}

declare -a _SYSTEMCTL_ARGS=()
if [ "$SYSTEMD_SCOPE" = "user" ]; then
    _SYSTEMCTL_ARGS+=(--user)
elif [ "$SYSTEMD_SCOPE" != "system" ]; then
    echo "WARN: unknown SYSTEMD_SCOPE=$SYSTEMD_SCOPE — defaulting to system" >&2
fi

systemctl_unit() {
    systemctl "${_SYSTEMCTL_ARGS[@]}" "$@"
}

unit_is_active() {
    local unit="$1"
    systemctl_unit is-active --quiet "$unit"
}

unit_is_enabled() {
    local unit="$1"
    systemctl_unit is-enabled --quiet "$unit"
}

stop_watcher_if_needed() {
    if unit_is_active "$WATCHER_UNIT"; then
        WATCHER_WAS_ACTIVE=1
        log "==> [2/10] Stopping $WATCHER_UNIT (race resolve)"
        systemctl_unit stop "$WATCHER_UNIT" 2>&1 | tee -a "$LOG_FILE"
        return 0
    fi
    log "==> [2/10] $WATCHER_UNIT not active — skipping stop"
    return 0
}

restart_watcher_if_needed() {
    if [ "$WATCHER_WAS_ACTIVE" -eq 1 ]; then
        log "==> Restoring $WATCHER_UNIT (trap EXIT)"
        if unit_is_active "$WATCHER_UNIT"; then
            log "==> $WATCHER_UNIT already active; no restart needed"
        else
            systemctl_unit start "$WATCHER_UNIT" 2>&1 | tee -a "$LOG_FILE" || true
        fi
    fi
}

cleanup_on_exit() {
    restart_watcher_if_needed
    # Remove the ephemeral materialized source (if any). "niente copie locali":
    # no on-box git checkout of marvisx may survive the rebuild.
    if [ -n "$KG_EPHEMERAL_SRC" ] && [ -d "$KG_EPHEMERAL_SRC" ]; then
        rm -rf "$KG_EPHEMERAL_SRC"
    fi
}

if [ ! -x "$PYTHON_BIN" ]; then
    echo "ERROR: python executable missing: $PYTHON_BIN" >&2
    exit 1
fi

trap cleanup_on_exit EXIT

# --- start -----------------------------------------------------------------

mkdir -p "$LOG_DIR"

# When KG_MATERIALIZE_SOURCE=1 (hosted "niente copie locali"), the git-dependent
# steps read an EPHEMERAL checkout materialized from the DEPLOYED release's
# already-verified CI bundle instead of a persistent on-box clone/mirror. The
# tree carries full history (non-shallow bundle) so `git log`/`git blame`
# succeed; cleanup_on_exit removes it on any exit.
if [ "${KG_MATERIALIZE_SOURCE:-0}" = "1" ]; then
    KG_EPHEMERAL_SRC="$(mktemp -d "${TMPDIR:-/tmp}/kg-src-XXXXXX")"
    log "==> Materializing ephemeral marvisx source from the deployed release bundle -> $KG_EPHEMERAL_SRC"
    "$PYTHON_BIN" -m core.hosted_deploy.source_materialize --into "$KG_EPHEMERAL_SRC" 2>&1 | tee -a "$LOG_FILE"
    KG_REPO_ROOT="$KG_EPHEMERAL_SRC"
fi

log "==> KG full-rebuild starting"
log "    db=$DB_PATH projects_root=$PROJECTS_ROOT repo_root=$KG_REPO_ROOT watcher=$WATCHER_UNIT"
log "    log=$LOG_FILE"
log "    systemd_scope=$SYSTEMD_SCOPE"

# Step 1: Backup pre-rebuild.
# Backups land in $MARVIS_DB_BACKUP_DIR when set (prod: a roomy data volume),
# otherwise next to the DB. backup-db.sh resolves the dir (env or the .env next
# to the DB) and rotates global keep-2.
log "==> [1/10] Backup DB pre-rebuild"
BACKUP_DIR="${MARVIS_DB_BACKUP_DIR:-}" bash "$SCRIPT_DIR/backup-db.sh" "$DB_PATH" pre-rebuild 2>&1 | tee -a "$LOG_FILE"

# Step 2: Stop watcher (CRITICAL race resolve, see header comment)
stop_watcher_if_needed

# KG_HOOK_DISABLED=1 prevents any auxiliary git post-commit hook from
# concurrently writing during the rebuild window.
export KG_HOOK_DISABLED=1

# Step 3: populate_artifacts (commits + tasks/PRs + handoffs + knowledge docs)
log "==> [3/10] populate_artifacts marvisx (--repo-root=$KG_REPO_ROOT)"
"$PYTHON_BIN" -m core.scripts.populate_artifacts --db "$DB_PATH" --repo-root "$KG_REPO_ROOT" 2>&1 | tee -a "$LOG_FILE"

# Step 4 (Phase 6): populate_artifacts --all-projects per coprire i progetti
# metadata-only (tutti i progetti senza repo). Indicizza handoff +
# knowledge_docs (plan/brainstorm/audit/spike/...). Deve girare PRIMA di
# populate_cross_project, che emette edges su artifacts gia' indicizzati.
log "==> [4/10] populate_artifacts --all-projects (Phase 6 cross-project coverage)"
"$PYTHON_BIN" -m core.scripts.populate_artifacts --db "$DB_PATH" --all-projects --exclude-projects=marvisx 2>&1 | tee -a "$LOG_FILE"

# Step 5: populate_cross_project --include-all-projects (Phase 3 scope esteso)
log "==> [5/10] populate_cross_project --include-all-projects (--marvisx-repo-root=$KG_REPO_ROOT)"
"$PYTHON_BIN" -m core.scripts.populate_cross_project --db "$DB_PATH" --projects-root "$PROJECTS_ROOT" --include-all-projects --marvisx-repo-root "$KG_REPO_ROOT" 2>&1 | tee -a "$LOG_FILE"

SKIP_CODE_STEPS="${KG_SKIP_CODE_STEPS:-0}"
if [ "$SKIP_CODE_STEPS" != "1" ] && [ ! -d "$KG_REPO_ROOT/.git" ]; then
    SKIP_CODE_STEPS=1
    log "==> KG_REPO_ROOT=$KG_REPO_ROOT is not a git repo; skipping git-dependent temporal/touch/code steps"
elif [ "$SKIP_CODE_STEPS" = "1" ]; then
    log "==> KG_SKIP_CODE_STEPS=1; skipping git-dependent temporal/touch/code steps"
fi

if [ "$SKIP_CODE_STEPS" = "1" ]; then
    log "==> [6/10] populate_temporal skipped (tenant snapshot / no git repo)"
    log "==> [7/10] populate_touch_counter skipped (tenant snapshot / no git repo)"
    log "==> [8/10] populate_code skipped (tenant snapshot / no git repo)"
    log "==> [9b/10] smoke gate skipped with code steps"
else
    # Step 6: populate_temporal (aging)
    # run inside $KG_REPO_ROOT so internal `git log` succeeds. WorkingDirectory
    # is /data/pir which is not a git repo, so the step used to fail with
    # "fatal: not a git repository (or any of the parent directories): .git".
    log "==> [6/10] populate_temporal (cwd=$KG_REPO_ROOT for git log)"
    (cd "$KG_REPO_ROOT" && "$PYTHON_BIN" -m core.scripts.populate_temporal --db "$DB_PATH") 2>&1 | tee -a "$LOG_FILE"

    # Step 7: populate_touch_counter (git blame counts)
    log "==> [7/10] populate_touch_counter (--repo-root=$KG_REPO_ROOT)"
    "$PYTHON_BIN" -m core.scripts.populate_touch_counter --db "$DB_PATH" --repo-root "$KG_REPO_ROOT" 2>&1 | tee -a "$LOG_FILE"

    # Step 8 (v1.3.0): code indexing for marvisx monorepo + external repos. The
    # v1.2.0 refactor introduced the multi-repo loop but silently dropped marvisx
    # from the table, so since 2026-05-15 the monorepo code KG was never updated.
    # After the 2026-05-18 `core/` materialize this left ~28k orphan `api.*` nodes
    # (see docs/audits/2026-05-22-kg-orphan-api-nodes-diagnosis.md). marvisx is
    # now indexed alongside the externals via the same ast_parser path.
    # The monorepo self-entry is always indexed. Additional external repos are
    # deployment-specific and supplied via KG_EXTERNAL_REPOS_EXTRA as newline- or
    # space-separated "path=slug" pairs (e.g. "$HOME/repos/foo=foo").
    declare -A KG_EXTERNAL_REPOS=(
        ["${KG_REPO_ROOT}"]="marvisx"
    )
    for _pair in ${KG_EXTERNAL_REPOS_EXTRA:-}; do
        _repo_path="${_pair%%=*}"
        _repo_slug="${_pair#*=}"
        if [ -n "$_repo_path" ] && [ -n "$_repo_slug" ] && [ "$_repo_path" != "$_repo_slug" ]; then
            KG_EXTERNAL_REPOS["$_repo_path"]="$_repo_slug"
        fi
    done
    log "==> [8/10] populate_code multi-repo (marvisx + KG_EXTERNAL_REPOS_EXTRA)"
    # Slugs we actually handed to ast_parser. Step 9b verifies each one
    # produced nodes; a repo skipped for not being a git checkout is not in
    # here, so the gate never blames a repo the rebuild never touched.
    declare -a INDEXED_SLUGS=()
    # Slugs ast_parser positively reported as having zero source files. A
    # default-floor repo here is excused by the gate as not applicable, so a
    # docs/config repo added via KG_EXTERNAL_REPOS_EXTRA does not false-red.
    declare -a EMPTY_SOURCE=()
    for REPO_DIR in "${!KG_EXTERNAL_REPOS[@]}"; do
        PROJECT_SLUG="${KG_EXTERNAL_REPOS[$REPO_DIR]}"
        if [ ! -d "$REPO_DIR/.git" ]; then
            log "  skip $PROJECT_SLUG ($REPO_DIR not a git repo)"
            continue
        fi
        log "  index $PROJECT_SLUG ($REPO_DIR) -> ast_parser"
        # v1.2.1 fix: pass --scan-patterns generic for non-marvisx layouts
        # (default patterns 'api/*.py' 'console/src/*' wouldn't match queue-gateway/, services/)
        # Capture ast_parser's JSON summary from stdout (its logs go to stderr,
        # tee'd to the log). python_files+typescript_files is its own count of
        # what it scanned — the source of truth for "did this repo have anything
        # to index". Re-deriving the scan glob here would just drift from
        # ast_parser, which is the becf93a5 mistake one layer down.
        AST_JSON=$("$PYTHON_BIN" -m core.scripts.ast_parser --db "$DB_PATH" --repo-root "$REPO_DIR" --project "$PROJECT_SLUG" --workers 4 \
            --scan-patterns '**/*.py' '**/*.ts' '**/*.tsx' 2>>"$LOG_FILE") || true
        printf '%s\n' "$AST_JSON" | tail -12 | tee -a "$LOG_FILE" >/dev/null
        # Recorded even when ast_parser errors: a crash yields empty JSON, which
        # parses as "has source" (conservative) and stays checked, so a failed
        # parse still becomes a zero-node red.
        INDEXED_SLUGS+=("$PROJECT_SLUG")
        SRC=$(printf '%s' "$AST_JSON" | "$PYTHON_BIN" -c 'import sys, json
try:
    d = json.load(sys.stdin)
    n = int(d.get("python_files", 0)) + int(d.get("typescript_files", 0))
    print("EMPTY" if n == 0 else "HAS")
except Exception:
    print("HAS")')
        if [ "$SRC" = "EMPTY" ]; then
            log "  $PROJECT_SLUG: ast_parser scanned 0 source files -> not applicable"
            EMPTY_SOURCE+=("$PROJECT_SLUG")
        fi
    done

    # v1.5.0 smoke gate: prevent the regression from sneaking back, for every
    # repo rather than for marvisx alone. Before this, step 8 could index an
    # external repo into nothing and the run stayed green because the count was
    # filtered to project_id = 'marvisx'
    # (docs/audits/2026-05-22-kg-orphan-api-nodes-diagnosis.md F3 fixed the
    # monorepo case only). The gate also refuses an empty attempted set: a
    # rebuild that indexed no repo has nothing to be green about.
    # `set -o pipefail` is on, so the gate's exit status survives the tee.
    MARVISX_CODE_NODES_MIN="${KG_MARVISX_CODE_NODES_MIN:-10000}"
    log "==> [9b/10] smoke gate: per-project code nodes (marvisx floor=$MARVISX_CODE_NODES_MIN)"
    if ! "$PYTHON_BIN" -m core.scripts.kg_smoke_gate \
        --db "$DB_PATH" \
        --floor "marvisx=$MARVISX_CODE_NODES_MIN" \
        --attempted ${INDEXED_SLUGS[@]+"${INDEXED_SLUGS[@]}"} \
        --empty-source ${EMPTY_SOURCE[@]+"${EMPTY_SOURCE[@]}"} 2>&1 | tee -a "$LOG_FILE"; then
        log "    ERROR: smoke gate failed — check KG_EXTERNAL_REPOS mapping or ast_parser scan-patterns"
        exit 1
    fi
fi

# Step 9: documents mirror + embeddings (fix tenant hosted doc-store empty symptom).
log "==> [9/10] reindex_documents --db $DB_PATH"
"$PYTHON_BIN" -m core.scripts.reindex_documents --db "$DB_PATH" --projects-root "$PROJECTS_ROOT" 2>&1 | tee -a "$LOG_FILE"

# Step 10: Restart watcher (also done by trap EXIT, but explicit here on success)
if [ "$WATCHER_WAS_ACTIVE" -eq 1 ]; then
    log "==> [10/10] Restarting $WATCHER_UNIT"
    if unit_is_active "$WATCHER_UNIT"; then
        log "==> $WATCHER_UNIT already active; no restart needed"
    else
        systemctl_unit start "$WATCHER_UNIT" 2>&1 | tee -a "$LOG_FILE"
    fi
    WATCHER_WAS_ACTIVE=0  # prevent trap from double-starting
else
    log "==> [10/10] $WATCHER_UNIT was not active before rebuild"
fi

# Rotation: keep newest 10 logs
log "==> Rotating logs (keep-10 in $LOG_DIR)"
ls -1t "$LOG_DIR"/kg-full-rebuild-*.log 2>/dev/null | tail -n +11 | xargs -r rm -f

log "==> KG full-rebuild complete"
