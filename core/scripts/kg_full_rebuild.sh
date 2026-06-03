#!/bin/bash
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
# Questo script PAUSA `pir-kg-watcher.service` prima di partire e lo riavvia
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
# ## Steps (Phase 6: aggiunto step 4, shift 5->8)
#
# 1. Backup pre-rebuild via scripts/backup-db.sh (sqlite3 .backup atomic + integrity)
# 2. STOP pir-kg-watcher.service
# 3. populate_artifacts (marvisx: commits + tasks + handoffs + knowledge)
# 4. populate_artifacts --all-projects --exclude-projects=marvisx
#    (Phase 6: handoff + knowledge_docs per i 67 progetti metadata-only.
#     Deve girare PRIMA di populate_cross_project che emette edges su
#     artifacts gia' indicizzati.)
# 5. populate_cross_project --include-all-projects (~68 progetti scope esteso)
# 6. populate_temporal (touch_count_7d/30d aging)
# 7. populate_touch_counter (git blame counts)
# 8. START pir-kg-watcher.service
#
# Log: $KG_REBUILD_LOG_DIR/kg-full-rebuild-YYYYMMDDHHMMSS.log (default /tmp,
# keep-10 rotation).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
DB_PATH="${KG_DB_PATH:-/data/pir/console.db}"
LOG_DIR="${KG_REBUILD_LOG_DIR:-/tmp}"
TS=$(date -u +'%Y%m%d%H%M%S')
LOG_FILE="${LOG_DIR}/kg-full-rebuild-${TS}.log"
WATCHER_UNIT="pir-kg-watcher.service"
PYTHON_BIN="${KG_PYTHON_BIN:-/data/pir/venv/bin/python}"
# Phase 3 bug fix: populate_commits + populate_touch_counter chiamano `git log`.
# Lo script gira da /data/pir (WorkingDirectory del systemd unit) che NON e'
# un git repo. KG_REPO_ROOT punta al workspace marvisx (git repo monorepo).
KG_REPO_ROOT="${KG_REPO_ROOT:-${HOME}/workspace}"
WATCHER_WAS_ACTIVE=0

# --- helpers ---------------------------------------------------------------

log() {
    echo "[$(date -u +'%Y-%m-%dT%H:%M:%SZ')] $*" | tee -a "$LOG_FILE"
}

restart_watcher_if_needed() {
    if [ "$WATCHER_WAS_ACTIVE" -eq 1 ]; then
        log "==> Restoring $WATCHER_UNIT (trap EXIT)"
        systemctl --user start "$WATCHER_UNIT" 2>&1 | tee -a "$LOG_FILE" || true
    fi
}

trap restart_watcher_if_needed EXIT

# --- start -----------------------------------------------------------------

mkdir -p "$LOG_DIR"
log "==> KG full-rebuild starting"
log "    db=$DB_PATH log=$LOG_FILE"

# Step 1: Backup pre-rebuild
log "==> [1/7] Backup DB pre-rebuild"
bash "$SCRIPT_DIR/backup-db.sh" "$DB_PATH" pre-rebuild 2>&1 | tee -a "$LOG_FILE"

# Step 2: Stop watcher (CRITICAL race resolve, see header comment)
if systemctl --user is-active --quiet "$WATCHER_UNIT" 2>/dev/null; then
    WATCHER_WAS_ACTIVE=1
    log "==> [2/8] Stopping $WATCHER_UNIT (race resolve)"
    systemctl --user stop "$WATCHER_UNIT" 2>&1 | tee -a "$LOG_FILE"
else
    log "==> [2/8] $WATCHER_UNIT not active — skipping stop"
fi

# KG_HOOK_DISABLED=1 prevents any auxiliary git post-commit hook from
# concurrently writing during the rebuild window.
export KG_HOOK_DISABLED=1

# Step 3: populate_artifacts (commits + tasks/PRs + handoffs + knowledge docs)
log "==> [3/8] populate_artifacts marvisx (--repo-root=$KG_REPO_ROOT)"
"$PYTHON_BIN" -m core.scripts.populate_artifacts --repo-root "$KG_REPO_ROOT" 2>&1 | tee -a "$LOG_FILE"

# Step 4 (Phase 6): populate_artifacts --all-projects per coprire i progetti
# metadata-only (tutti i progetti senza repo). Indicizza
# handoff + knowledge_docs (plan/brainstorm/audit/spike/...). Deve girare PRIMA
# di populate_cross_project che emette edges su artifacts gia' indicizzati.
log "==> [4/8] populate_artifacts --all-projects (Phase 6 cross-project coverage)"
"$PYTHON_BIN" -m core.scripts.populate_artifacts --all-projects --exclude-projects=marvisx 2>&1 | tee -a "$LOG_FILE"

# Step 5: populate_cross_project --include-all-projects (Phase 3 scope esteso)
# v1.3.0: --marvisx-repo-root pinned to $KG_REPO_ROOT. Default resolves to
# scripts/.. which evaluates to /data/pir/core (the deploy tree, no .claude/)
# when invoked from systemd's WorkingDirectory=/data/pir, so the Fase 2.z
# infra-indexing pass found "no .claude/ artifacts" and silently skipped.
log "==> [5/8] populate_cross_project --include-all-projects (--marvisx-repo-root=$KG_REPO_ROOT)"
"$PYTHON_BIN" -m core.scripts.populate_cross_project --include-all-projects --marvisx-repo-root "$KG_REPO_ROOT" 2>&1 | tee -a "$LOG_FILE"

# Step 6: populate_temporal (aging)
# v1.3.0: run inside $KG_REPO_ROOT so internal `git log` succeeds. WorkingDirectory
# is /data/pir which is not a git repo, so the step used to fail with
# "fatal: not a git repository (or any of the parent directories): .git".
log "==> [6/8] populate_temporal (cwd=$KG_REPO_ROOT for git log)"
(cd "$KG_REPO_ROOT" && "$PYTHON_BIN" -m core.scripts.populate_temporal) 2>&1 | tee -a "$LOG_FILE"

# Step 7: populate_touch_counter (git blame counts)
log "==> [7/9] populate_touch_counter (--repo-root=$KG_REPO_ROOT)"
"$PYTHON_BIN" -m core.scripts.populate_touch_counter --repo-root "$KG_REPO_ROOT" 2>&1 | tee -a "$LOG_FILE"

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
log "==> [8/9] populate_code multi-repo (marvisx + KG_EXTERNAL_REPOS_EXTRA)"
for REPO_DIR in "${!KG_EXTERNAL_REPOS[@]}"; do
    PROJECT_SLUG="${KG_EXTERNAL_REPOS[$REPO_DIR]}"
    if [ ! -d "$REPO_DIR/.git" ]; then
        log "  skip $PROJECT_SLUG ($REPO_DIR not a git repo)"
        continue
    fi
    log "  index $PROJECT_SLUG ($REPO_DIR) -> ast_parser"
    # v1.2.1 fix: pass --scan-patterns generic for non-marvisx layouts
    # (default patterns 'api/*.py' 'console/src/*' wouldn't match queue-gateway/, services/)
    "$PYTHON_BIN" -m core.scripts.ast_parser --repo-root "$REPO_DIR" --project "$PROJECT_SLUG" --workers 4 \
        --scan-patterns '**/*.py' '**/*.ts' '**/*.tsx' 2>&1 | tail -5 | tee -a "$LOG_FILE"
done

# v1.3.0 smoke gate: prevent the regression from sneaking back. After step 8
# we expect at least N marvisx code nodes; below the threshold means
# KG_EXTERNAL_REPOS lost its marvisx entry again or ast_parser silently
# matched nothing. Threshold sized below current baseline so a slimmer
# repo doesn't trip false positives.
MARVISX_CODE_NODES_MIN="${KG_MARVISX_CODE_NODES_MIN:-10000}"
ACTUAL=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM graph_nodes WHERE project_id='marvisx' AND id LIKE 'py:%' AND deprecated_at IS NULL;" 2>/dev/null || echo 0)
log "==> [8b/9] smoke gate: marvisx code nodes = $ACTUAL (min=$MARVISX_CODE_NODES_MIN)"
if [ "$ACTUAL" -lt "$MARVISX_CODE_NODES_MIN" ]; then
    log "    ERROR: marvisx code KG looks empty — check KG_EXTERNAL_REPOS mapping or ast_parser scan-patterns"
    exit 1
fi

# Step 9: Restart watcher (also done by trap EXIT, but explicit here on success)
if [ "$WATCHER_WAS_ACTIVE" -eq 1 ]; then
    log "==> [9/9] Restarting $WATCHER_UNIT"
    systemctl --user start "$WATCHER_UNIT" 2>&1 | tee -a "$LOG_FILE"
    WATCHER_WAS_ACTIVE=0  # prevent trap from double-starting
fi

# Rotation: keep newest 10 logs
log "==> Rotating logs (keep-10 in $LOG_DIR)"
ls -1t "$LOG_DIR"/kg-full-rebuild-*.log 2>/dev/null | tail -n +11 | xargs -r rm -f

log "==> KG full-rebuild complete"
