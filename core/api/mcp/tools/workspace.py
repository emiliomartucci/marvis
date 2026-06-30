"""Workspace file tools for hosted MCP Phase A."""
from __future__ import annotations

import json
import os
import sqlite3
from typing import Annotated, Any

from pydantic import Field

from core.api.mcp._adapter import LOCAL_CTX, raise_mcp_error
from core.api.services import workspace_tools as svc
from core.api.use_cases._errors import ServiceError


def _truthy_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _actor() -> svc.WorkspaceActor:
    return svc.WorkspaceActor(
        actor_id=LOCAL_CTX.user_id or LOCAL_CTX.username,
        tenant_id=os.environ.get("TENANT_ID", "tenant"),
        auth_mode="mcp",
        scopes=("read:data", "write:data"),
    )


def _policy(*, write: bool = False, shell: bool = False) -> svc.WorkspacePolicy:
    # Phase A portal-trust: authenticated tenant Bearer is full workspace admin.
    # Future OAuth/per-agent scope enforcement should narrow this policy at the
    # adapter boundary, while the service remains unchanged.
    return svc.WorkspacePolicy(
        can_read=True,
        can_write=True if write else True,
        can_shell=shell and _truthy_env("MARVIS_WORKSPACE_SHELL_ENABLED"),
        require_audit_for_writes=(write or shell) and _truthy_env("MARVIS_WORKSPACE_AUDIT_REQUIRED"),
    )


def _audit_db_path() -> str:
    return (os.environ.get("MARVIS_DB_PATH") or os.environ.get("PIR_DB_PATH") or "").strip()


def _workspace_audit_sink(event: svc.AuditEvent) -> None:
    db_path = _audit_db_path()
    if not db_path:
        raise RuntimeError("MARVIS_DB_PATH/PIR_DB_PATH is not configured")

    details = {
        "phase": event.phase,
        "tenant_id": event.tenant_id,
        "before_sha256": event.before_sha256,
        "after_sha256": event.after_sha256,
        "outcome": event.outcome,
        "metadata": event.metadata,
    }
    with sqlite3.connect(db_path, timeout=2.0) as conn:
        conn.execute(
            "INSERT INTO audit_log (action, user, resource_type, resource_id, details_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                f"workspace.{event.action}.{event.phase}",
                event.actor_id,
                "workspace_file",
                event.path,
                json.dumps(details, sort_keys=True),
            ),
        )


def register(mcp) -> None:
    """Register Phase A workspace tools on the shared FastMCP instance."""

    @mcp.tool()
    async def read_file(
        path: Annotated[str, Field(min_length=1)],
        max_bytes: Annotated[int, Field(ge=1, le=2_000_000)] = svc.MAX_READ_BYTES,
    ) -> dict[str, Any]:
        """Read a UTF-8 text file from the tenant projects-root workspace.

        QUANDO USARLO: discovery hosted su file/documenti/progetti reali tramite path root-relative dentro MARVIS_PROJECTS_ROOT. CANONICALITY: questa e' prova hosted per report/piani.
        QUANDO NON USARLO: NOT per path assoluti, ../, symlink, .env/key/pem/.ssh/.git o file binari. NOT per leggere repo fuori projects-root.
        NEXT: grep per trovare riferimenti o write_file/edit con if_match_sha256 per modifiche.
        RESTITUISCE: content, sha256, size, truncation e freshness block {current_sha256,indexed_sha256,freshness_status,indexed_at}."""
        try:
            return svc.read_file(path, actor=_actor(), policy=_policy(), max_bytes=max_bytes)
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def directory_tree(
        path: str = "",
        max_depth: Annotated[int, Field(ge=0, le=10)] = 3,
        max_entries: Annotated[int, Field(ge=1, le=5_000)] = svc.MAX_TREE_ENTRIES,
    ) -> dict[str, Any]:
        """List a bounded directory tree under the tenant projects-root workspace.

        QUANDO USARLO: capire struttura progetti/repo/documenti hosted senza shell, con path root-relative e limiti espliciti.
        QUANDO NON USARLO: NOT per attraversare symlink o path secret; NOT come scanner infinito.
        RESTITUISCE: entries[{path,name,type,size_bytes,mtime}], entry_count, truncated, max_depth, max_entries."""
        try:
            return svc.directory_tree(
                path,
                actor=_actor(),
                policy=_policy(),
                max_depth=max_depth,
                max_entries=max_entries,
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def grep(
        pattern: Annotated[str, Field(min_length=1)],
        path: str = "",
        max_results: Annotated[int, Field(ge=1, le=1_000)] = svc.MAX_GREP_RESULTS,
        timeout_ms: Annotated[int, Field(ge=100, le=30_000)] = svc.GREP_TIMEOUT_MS,
    ) -> dict[str, Any]:
        """Search text in the tenant projects-root workspace using ripgrep when available.

        QUANDO USARLO: trovare riferimenti nei file hosted senza aprire shell; no match ritorna matched=false, non errore. CANONICALITY: usa i match come prova hosted, non grep locale.
        QUANDO NON USARLO: NOT per leggere secret path, symlink o file fuori projects-root. Se rg manca, installare ripgrep sul tenant host o usare read_file mirato.
        NEXT: read_file sul path matchato quando serve contesto completo.
        RESTITUISCE: matches[{path,line,text}], match_count, matched, truncated, timeout_ms."""
        try:
            return svc.grep(
                pattern,
                path=path,
                actor=_actor(),
                policy=_policy(),
                max_results=max_results,
                timeout_ms=timeout_ms,
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def write_file(
        path: Annotated[str, Field(min_length=1)],
        content: str,
        if_match_sha256: str | None = None,
        overwrite: bool = False,
        create_parent: bool = False,
    ) -> dict[str, Any]:
        """Atomically write a UTF-8 text file inside the tenant projects-root workspace.

        QUANDO USARLO: creare o aggiornare file hosted dopo read_file; per file esistenti passa if_match_sha256 oppure overwrite=true esplicito.
        QUANDO NON USARLO: NOT per path assoluti, ../, symlink, .env/key/pem/.ssh/.git, binari o contenuti oltre cap. NOT per bypassare PR/task policy.
        PROVA: sha256 nuovo e audit_status; verifica poi con read_file/grep.
        RESTITUISCE: sha256 nuovo, previous_sha256, created, audit_status e freshness block. Hash stale produce errore conflict con current_sha256."""
        try:
            return svc.write_file(
                path,
                content,
                actor=_actor(),
                policy=_policy(write=True),
                audit_sink=_workspace_audit_sink,
                if_match_sha256=if_match_sha256,
                overwrite=overwrite,
                create_parent=create_parent,
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def edit(
        path: Annotated[str, Field(min_length=1)],
        old_text: Annotated[str, Field(min_length=1)],
        new_text: str,
        if_match_sha256: str | None = None,
        replace_all: bool = False,
        expected_replacements: Annotated[int, Field(ge=0)] | None = None,
    ) -> dict[str, Any]:
        """Atomically find/replace text inside a hosted workspace file.

        QUANDO USARLO: modifiche chirurgiche dopo read_file, usando if_match_sha256 quando vuoi bloccare overwrite stale.
        QUANDO NON USARLO: NOT se old_text e' ambiguo senza replace_all=true; NOT per binari, secret path, symlink o file fuori projects-root.
        RESTITUISCE: sha256 nuovo, previous_sha256, replacements, audit_status e freshness. Match multipli non dichiarati producono ambiguous_edit."""
        try:
            return svc.edit(
                path,
                old_text,
                new_text,
                actor=_actor(),
                policy=_policy(write=True),
                audit_sink=_workspace_audit_sink,
                if_match_sha256=if_match_sha256,
                replace_all=replace_all,
                expected_replacements=expected_replacements,
            )
        except ServiceError as e:
            raise_mcp_error(e)

    @mcp.tool()
    async def run_bash(
        command: Annotated[str, Field(min_length=1, max_length=svc.MAX_BASH_COMMAND_CHARS)],
        cwd: str = "",
        timeout_ms: Annotated[int, Field(ge=100, le=svc.MAX_BASH_TIMEOUT_MS)] = svc.MAX_BASH_TIMEOUT_MS,
        max_output_bytes: Annotated[int, Field(ge=1, le=svc.MAX_BASH_OUTPUT_BYTES)] = svc.MAX_BASH_OUTPUT_BYTES,
    ) -> dict[str, Any]:
        """Run one guarded shell command inside the tenant projects-root workspace.

        QUANDO USARLO: hosted repo work che richiede comandi semplici come git status, test mirati o build brevi, dopo aver abilitato MARVIS_WORKSPACE_SHELL_ENABLED sul tenant.
        QUANDO NON USARLO: NOT per shell interattive, pipeline, rete/ssh/curl, sudo, path assoluti, ../, secret path o comandi lunghi. Non e' un filesystem jail kernel-level.
        RESTITUISCE: ok, returncode, stdout/stderr capped, timeout, cwd e safety metadata; returncode non-zero torna come ok=false, non come errore tool."""
        try:
            return svc.run_bash(
                command,
                cwd=cwd,
                actor=_actor(),
                policy=_policy(shell=True),
                audit_sink=_workspace_audit_sink,
                timeout_ms=timeout_ms,
                max_output_bytes=max_output_bytes,
            )
        except ServiceError as e:
            raise_mcp_error(e)
