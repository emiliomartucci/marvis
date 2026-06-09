#!/bin/bash
# KG-HOOK-START
# KG-HOOK-VERSION=1.5.0
# v1.5.0 - 2026-05-11 - Docs Plan 7: non-blocking drift check post-commit
# v1.4.0 - 2026-05-10 - Docs Plan 0: index apps/docs documents_nodes as describes edges
# v1.3.0 - 2026-04-22 - KG Phase 7.2: incremental module<->file bridge sweep after ast_parser
# v1.2.1 - 2026-04-17 - Fix: python -> python3 (system binary is python3, `python` not found)
# v1.2.0 - 2026-04-14 - KG Fase 2: dispatch .md for cross-project populator + debounce + batch
# v1.0.0 - 2026-04-14 - KG Fase 1g: post-commit hook template (non-blocking)
# Knowledge Graph auto-update hook — updates graph_nodes/graph_edges in
# background after every local commit. Non-blocking by design: the commit is
# already complete when this runs, and `set +e` ensures we never fail the
# commit path. Parser runs detached with output redirected to /tmp/kg-parser-*.log.
set +e

# Idempotency multi-repo: only run if invoked from the MarvisX monorepo.
REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$REPO_ROOT" ]; then
    exit 0
fi
if [ ! -f "$REPO_ROOT/api/db.py" ] || [ ! -f "$REPO_ROOT/scripts/ast_parser.py" ]; then
    exit 0
fi

# Env override — lets developers disable without uninstalling.
if [ "${KG_HOOK_DISABLED:-0}" = "1" ]; then
    exit 0
fi

# Files touched by HEAD, split into code (.py/.ts/.tsx), project md (.md), and
# docs-site md/mdx pages carrying `documents_nodes` frontmatter.
# Fase 2: `.md` files need the cross-project populator (incremental-md mode).
ALL_FILES=$(git diff-tree --no-commit-id --name-only -r HEAD 2>/dev/null || true)
CODE_FILES=$(echo "$ALL_FILES" | grep -E '\.(py|ts|tsx)$' || true)
MD_FILES=$(echo "$ALL_FILES" | grep -E '\.md$' || true)
DOCS_CONTENT_FILES=$(echo "$ALL_FILES" | grep -E '^apps/docs/content/docs/.*\.(md|mdx)$' || true)

if [ -z "$CODE_FILES" ] && [ -z "$MD_FILES" ] && [ -z "$DOCS_CONTENT_FILES" ]; then
    exit 0
fi

# Rotate old logs: keep the 10 most recent, drop the rest.
ls -t /tmp/kg-parser-*.log 2>/dev/null | tail -n +11 | xargs -r rm 2>/dev/null

# Debounce queue: append to pending files, one background worker drains.
# PERF-5: a rapid-fire commit session (git rebase -i edit) should not launch
# N parallel parsers. Instead batch pending files and run once after 500ms of
# quiet. `flock -n` lets exactly one worker run at a time — others exit fast.
PENDING_CODE="/tmp/kg-pending-code.txt"
PENDING_MD="/tmp/kg-pending-md.txt"
PENDING_DOCS_CONTENT="/tmp/kg-pending-docs-content.txt"
LOCK="/tmp/kg-hook.lock"

if [ -n "$CODE_FILES" ]; then
    echo "$CODE_FILES" >> "$PENDING_CODE"
fi
if [ -n "$MD_FILES" ]; then
    echo "$MD_FILES" >> "$PENDING_MD"
fi
if [ -n "$DOCS_CONTENT_FILES" ]; then
    echo "$DOCS_CONTENT_FILES" >> "$PENDING_DOCS_CONTENT"
fi

LOG="/tmp/kg-parser-$(date +%s)-$$.log"

# Detached worker: debounce 500ms, then drain both queues.
(
    # Ensure single-writer: only one worker drains at a time. Others exit.
    flock -n 9 || exit 0
    # Small debounce window so rapid commits batch into one parser run.
    sleep 0.5
    cd "$REPO_ROOT" || exit 0

    if [ -s "$PENDING_CODE" ]; then
        CODE_LIST=$(sort -u "$PENDING_CODE" | tr '\n' ' ')
        : > "$PENDING_CODE"
        PYTHONPATH="$REPO_ROOT" python3 -m core.scripts.ast_parser --incremental $CODE_LIST \
            >> "$LOG" 2>&1
        # Phase 7.2: after AST re-index the matching file/module nodes are
        # fresh; run the bridge populator on the same paths so their
        # `resolves_to` edges exist. Non-fatal: if the migration hasn't been
        # applied yet or the DB is busy, we log and continue (commit is
        # already done at this point, we MUST NOT fail the hook).
        PYTHONPATH="$REPO_ROOT" python3 -m core.scripts.populate_module_file_bridge \
            --incremental $CODE_LIST \
            >> "$LOG" 2>&1 \
            || echo "[kg-post-commit] bridge incremental non-fatal error (continuing)" >> "$LOG"
    fi

    if [ -s "$PENDING_MD" ]; then
        # Currently the cross-project populator rescans everything — incremental
        # mode per-file is Fase 2.x. For now we only run if marvisx monorepo
        # (we're already in REPO_ROOT) to keep the hook cheap; the populator
        # has its own "skip-*" flags to stay under budget.
        : > "$PENDING_MD"
        PYTHONPATH="$REPO_ROOT" python3 -m core.scripts.populate_cross_project \
            --skip-similar-to >> "$LOG" 2>&1
    fi

    if [ -s "$PENDING_DOCS_CONTENT" ]; then
        DOCS_LIST=$(sort -u "$PENDING_DOCS_CONTENT" | tr '\n' ' ')
        : > "$PENDING_DOCS_CONTENT"
        PYTHONPATH="$REPO_ROOT" python3 -m core.scripts.docs_frontmatter_describes \
            --paths $DOCS_LIST \
            >> "$LOG" 2>&1 \
            || echo "[kg-post-commit] docs describes incremental non-fatal error (continuing)" >> "$LOG"
    fi
) 9>"$LOCK" < /dev/null > /dev/null 2>&1 &
disown $! 2>/dev/null || true

# Docs governance drift check: post-commit-only V1. This is intentionally
# detached and non-fatal; the Python script owns O_NOFOLLOW log/lock opening.
DRIFT_WORKSPACE="${MARVIS_WORKSPACE_ROOT:-${HOME}/workspace}"
DRIFT_RUNTIME="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}/pir-docs-drift"
if mkdir -p "$DRIFT_RUNTIME" 2>/dev/null; then
    chmod 0700 "$DRIFT_RUNTIME" 2>/dev/null || true
    DRIFT_LOG="$DRIFT_RUNTIME/drift-$(date +%Y%m%d).log"
    DRIFT_LOCK="$DRIFT_RUNTIME/drift.lock"
    (
        cd "$DRIFT_WORKSPACE" || exit 0
        PYTHONPATH="$DRIFT_WORKSPACE" /usr/bin/python3 \
            "$DRIFT_WORKSPACE/scripts/_drift_check.py" \
            --workspace "$DRIFT_WORKSPACE" \
            --source post-commit \
            --log-file "$DRIFT_LOG" \
            --lock-file "$DRIFT_LOCK"
    ) < /dev/null > /dev/null 2>&1 &
    disown $! 2>/dev/null || true
fi

exit 0
# KG-HOOK-END
# test comment Fri Apr 17 12:31:39 PM UTC 2026
