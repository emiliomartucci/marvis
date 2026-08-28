# v1.18.0 - 2026-05-16 - KG PR-Impact PRE phase: function cap + branch stale + replay buffer + pr_impact_enabled
from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.platform import current_user, db_default_path

# Prod (Hetzner/Docker) ships the vec0 loadable at this path; it stays the
# final fallback so prod keeps working unchanged.
_VEC0_PATH_PROD_DEFAULT = "/data/pir/lib/vec0"
_DEV_JWT_SECRET = "dev-secret-change-in-production"


def _default_vec0_path() -> str:
    """Resolve the vec0 loadable path for sqlite-vec.

    Order:
      1. ``$VEC0_PATH`` env — handled by pydantic-settings (overrides this
         default_factory when set), so it is NOT re-read here.
      2. The installed ``sqlite_vec`` package's bundled loadable
         (``sqlite_vec.loadable_path()``) — the OSS clean-install path
         (`pip install sqlite-vec`), platform-correct (.so / .dylib).
      3. ``/data/pir/lib/vec0`` — prod fallback.
    """
    try:
        import sqlite_vec  # type: ignore

        return str(sqlite_vec.loadable_path())
    except Exception:  # noqa: BLE001 — missing/old package → prod fallback
        return _VEC0_PATH_PROD_DEFAULT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # ignora env vars non dichiarate
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    pir_env: str = Field(
        default="development",
        validation_alias=AliasChoices("MARVIS_ENV", "PIR_ENV"),
    )
    # Free-form tenant identifier. Default "core" (OSS). Deploy bundles set
    # DEPLOY_MODE to their own tenant slug via .env; no tenant names are
    # hard-coded in core.
    deploy_mode: str = Field(default="core", alias="DEPLOY_MODE")
    pir_instance: str = Field(
        default="prod",
        validation_alias=AliasChoices("MARVIS_INSTANCE", "PIR_INSTANCE"),
    )
    marvisx_phase: Literal["A", "B", "C", "D"] = Field(
        default="A", alias="MARVISX_PHASE"
    )
    pir_canary_banner: bool = Field(
        default=False,
        validation_alias=AliasChoices("MARVIS_CANARY_BANNER", "PIR_CANARY_BANNER"),
    )
    pir_jwt_secret: str = Field(
        default=_DEV_JWT_SECRET,
        validation_alias=AliasChoices("MARVIS_JWT_SECRET", "PIR_JWT_SECRET"),
    )
    trusted_proxy_cidrs: list[str] = Field(
        # The host-install preset runs Caddy on the same machine and proxies to
        # the API over loopback.  Trust only those exact peers by default; the
        # Compose template adds exact /32 proxy container addresses explicitly.
        default_factory=lambda: ["127.0.0.1/32", "::1/128"],
        alias="TRUSTED_PROXY_CIDRS",
    )
    pir_admin_password_hash: str = Field(
        default="",
        validation_alias=AliasChoices(
            "MARVIS_ADMIN_PASSWORD_HASH", "PIR_ADMIN_PASSWORD_HASH"
        ),
    )
    db_path: str = Field(
        # Fallback when no MARVIS_DB_PATH/PIR_DB_PATH/DB_PATH env is set: the
        # platformdirs data root (was a CWD-relative "console.db" that opened a
        # different DB per launch directory — split-brain, and on Windows a
        # nonsense root). The env aliases still win via pydantic; this factory
        # runs ONLY when none are set.
        default_factory=lambda: str(db_default_path()),
        validation_alias=AliasChoices("MARVIS_DB_PATH", "PIR_DB_PATH", "DB_PATH"),
    )
    db_backup_dir: str = Field(
        # Where pre-migration / pre-rebuild console.db snapshots land. Empty (the
        # OSS default) keeps them next to the DB. In a managed deployment set this
        # to a roomy data volume: the snapshots are full DB copies (>1GB) and a
        # couple of them fill a small root disk — the recurring 90%-full cause.
        default="",
        validation_alias=AliasChoices("MARVIS_DB_BACKUP_DIR", "PIR_DB_BACKUP_DIR"),
    )
    cookie_domain: str | None = Field(default=None, alias="COOKIE_DOMAIN")
    # report_bug (transport C). Operator holds the SECRET and verifies HMAC;
    # each tenant sender holds only its own derived TOKEN + the operator URL.
    bugreport_ingest_secret: str = Field(
        default="", validation_alias=AliasChoices("MARVIS_BUGREPORT_INGEST_SECRET")
    )
    bugreport_ingest_url: str = Field(
        default="", validation_alias=AliasChoices("MARVIS_BUGREPORT_INGEST_URL")
    )
    bugreport_ingest_token: str = Field(
        default="", validation_alias=AliasChoices("MARVIS_BUGREPORT_INGEST_TOKEN")
    )
    bugreport_rate_limit_per_hour: int = Field(
        default=10, validation_alias=AliasChoices("MARVIS_BUGREPORT_RATE_LIMIT_PER_HOUR")
    )
    cors_origins_prod: list[str] = Field(
        default_factory=list,
        alias="CORS_ORIGINS_PROD",
    )
    cors_origins_dev: list[str] = ["http://localhost:3000", "http://localhost:3001"]
    console_base_url: str | None = Field(default=None, alias="CONSOLE_BASE_URL")
    jwt_expiry_hours: int = 24
    ws_ticket_ttl_seconds: int = 30
    tasks_api_token: str = ""
    agent_token_auth_mode: Literal["compatibility", "strict"] = Field(
        default="compatibility", alias="AGENT_TOKEN_AUTH_MODE"
    )
    agent_token_default_lifetime_hours: int = Field(
        default=720, ge=1, alias="AGENT_TOKEN_DEFAULT_LIFETIME_HOURS"
    )
    agent_token_max_lifetime_hours: int = Field(
        default=2160, ge=1, alias="AGENT_TOKEN_MAX_LIFETIME_HOURS"
    )
    agent_token_max_overlap_minutes: int = Field(
        default=1440, ge=0, alias="AGENT_TOKEN_MAX_OVERLAP_MINUTES"
    )
    sqlite_busy_timeout_ms: int = 30000  # 30s — backfill/reindex can hold lock >15s
    db_pool_size: int = 8  # bounded connection pool size
    auto_hibernate_enabled: bool = True
    tasks_rate_limit_per_min: int = 60
    kg_deep_rate_limit_per_min: int = (
        30  # max deep KG bundle requests per user per minute
    )
    inbox_sources_json: str = "{}"
    inbox_max_title_chars: int = 500
    inbox_max_content_chars: int = 20000
    inbox_max_metadata_bytes: int = 20000
    inbox_digest_scheduler_interval_seconds: int = 3600
    brain_scheduler_interval_seconds: int = 3600
    # Fase 2 console-slim: gate the two in-process schedulers at task creation
    # (mirrors canary_mode). Default True = byte-identical to prior behavior;
    # set the env var to false on a box that no longer surfaces inbox/brain.
    inbox_digest_scheduler_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "MARVIS_INBOX_DIGEST_SCHEDULER_ENABLED",
            "PIR_INBOX_DIGEST_SCHEDULER_ENABLED",
        ),
    )
    brain_scheduler_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "MARVIS_BRAIN_SCHEDULER_ENABLED", "PIR_BRAIN_SCHEDULER_ENABLED"
        ),
    )
    brain_run_off_peak_only: bool = Field(
        default=False, alias="BRAIN_RUN_OFF_PEAK_ONLY"
    )
    # Warehouse consolidation pass (full-store learning dedup, ships DORMANT).
    # Unlike memory_ops M2 (same-cycle only), this scans the WHOLE learnings
    # warehouse and PROPOSES consolidation of cross-cycle duplicates. Default
    # FALSE — a human enables it after reviewing. Daily cadence guard.
    brain_warehouse_consolidation_enabled: bool = Field(
        default=False, alias="BRAIN_WAREHOUSE_CONSOLIDATION_ENABLED"
    )
    brain_warehouse_consolidation_interval_seconds: int = Field(
        default=86400, alias="BRAIN_WAREHOUSE_CONSOLIDATION_INTERVAL_SECONDS"
    )

    # KG temporal recency pass (Fase D producer): proposes re-verifying aging,
    # never-verified live nodes. Gated by temporal_memory_enabled (no separate
    # enable flag — the flag IS the gate). Daily cadence guard.
    brain_temporal_recency_interval_seconds: int = Field(
        default=86400, alias="BRAIN_TEMPORAL_RECENCY_INTERVAL_SECONDS"
    )

    # GitHub Webhook
    github_webhook_secret: str = ""

    # KG PR-Impact View (planned 2026-05-16, sub-01/02/03 sequential implementation)
    # Source-of-truth single owner per setting: vedi docs/plans/2026-05-16-feat-kg-pr-impact-view-plan.md §9.10
    # Magic numbers cross-sub extracted per Emilio decision (D-MAGIC-NUMBERS-SETTINGS)
    pr_impact_enabled: Literal["off", "shadow", "on"] = Field(
        default="shadow", alias="PR_IMPACT_ENABLED"
    )
    function_cap_default: int = Field(
        default=800, alias="FUNCTION_CAP_DEFAULT"
    )  # PR-impact response: top-N functions per priority ranking (sub-02 §2.1)
    function_cap_max_deep_link: int = Field(
        default=50, alias="FUNCTION_CAP_MAX_DEEP_LINK"
    )  # sub-04 deep-link ?op= max highlighted nodes
    kg_branch_stale_days: int = Field(
        default=30, alias="KG_BRANCH_STALE_DAYS"
    )  # branch is_stale threshold (sub-02 §2.2)
    pr_events_replay_maxlen: int = Field(
        default=20, alias="PR_EVENTS_REPLAY_MAXLEN"
    )  # in-memory WS ring buffer per project_slug (sub-02 §3.2)
    telegram_bot_token: str | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_owner_chat_id: str | None = Field(
        default=None, alias="TELEGRAM_OWNER_CHAT_ID"
    )

    # Plan 3 tenant scaffolding. Defaults stay off for marvis-personal/core.
    multi_tenant_enabled: bool = Field(default=False, alias="MULTI_TENANT_ENABLED")
    per_user_api_key_enabled: bool = Field(
        default=False, alias="PER_USER_API_KEY_ENABLED"
    )
    byok_fernet_secret: str | None = Field(
        default=None, alias="BYOK_FERNET_SECRET"
    )
    fernet_salt_version: str = Field(default="v1", alias="FERNET_SALT_VERSION")
    uid_isolation_enabled: bool = Field(default=False, alias="UID_ISOLATION_ENABLED")
    uid_pool_size: int = Field(default=0, alias="UID_POOL_SIZE")
    uid_pool_prefix: str = Field(default="tenant", alias="UID_POOL_PREFIX")
    uid_pool_base: int = Field(default=10100, alias="UID_POOL_BASE")
    # Git run-as user for deployments where the API service account differs from
    # the user that owns the git repositories. When set and != the current
    # process user, git runs via `sudo -u <user>` and chown targets `<user>:<user>`.
    # Empty default (single-user / self-hosted): git runs directly and chown is
    # skipped — the API process already owns the repos. See services/runas.py.
    git_runas_user: str = Field(default="", alias="GIT_RUNAS_USER")
    force_password_change_on_first_login: bool = Field(
        default=False, alias="FORCE_PASSWORD_CHANGE_ON_FIRST_LOGIN"
    )
    # Extra env-var names a tenant exposes into its user tmux panes, on top of
    # the generic core defaults. JSON array form, e.g.
    #   TENANT_ENV_WHITELIST=["DATABASE_URL","DEPLOY_MODE","TENANT_SLUG"]
    # The tenant overlay (deploy/<tenant>/, tenants/<tenant>/config.env) ships
    # this. No tenant-specific whitelist is hardcoded in core.
    tenant_env_whitelist: list[str] = Field(
        default_factory=list, alias="TENANT_ENV_WHITELIST"
    )

    # Agents whose `hybrid` self-improvement proposals always require human
    # review (auto-approval veto). JSON array form, e.g.
    #   SELF_IMPROVEMENT_AGENTS=["agent-a","agent-b"]
    # Default empty: OSS core ships no internal agent names hardcoded. The deploy
    # bundle (.env) sets the names so the safety veto stays armed on our prod /
    # tenant installs. Empty list = no veto (auto-approval proceeds per the other
    # rules), which is safe for a fresh OSS install that has no self-improvement
    # agent. See services/auto_approval.py::ApprovalPolicy.evaluate.
    self_improvement_agents: list[str] = Field(
        default_factory=list, alias="SELF_IMPROVEMENT_AGENTS"
    )

    # Static agent identities accepted for X-Agent-Name attribution on top of the
    # DB `users` table (the authoritative source via get_valid_agent_names) and
    # the generic system identities. JSON array form, e.g.
    #   STATIC_AGENT_IDENTITIES=["agent-a","agent-b"]
    # Default empty: OSS core hardcodes no tenant agent names. Agents still
    # authenticate via their DB row; this list only covers the legacy shared
    # TASKS_API_TOKEN attribution path + the scope-enforcement gate in rbac.py,
    # which do not query the DB. The deploy bundle (.env) sets the names.
    # See security.py::_VALID_AGENT_NAMES and rbac.py::require_scope.
    static_agent_identities: list[str] = Field(
        default_factory=list, alias="STATIC_AGENT_IDENTITIES"
    )

    # Monitoring
    monitoring_metrics_interval: int = (
        60  # was 10 — caused DB lock every ~30min via metrics_collector.save_to_db
    )
    monitoring_docker_interval: int = (
        60  # Docker stats interval (separate from system metrics)
    )
    monitoring_security_interval: int = 60
    monitoring_retention_raw_hours: int = 24
    monitoring_retention_candles_days: int = 30
    monitoring_retention_events_days: int = 30
    monitoring_rate_limit_per_min: int = 30

    # Finder
    finder_root: str = "/data"
    workspace_root: str | None = Field(
        default=None, alias="WORKSPACE_ROOT"
    )
    repo_share_root: str | None = Field(
        default=None, alias="REPO_SHARE_ROOT"
    )
    runtime_home: str | None = Field(default=None, alias="RUNTIME_HOME")
    agents_base: str | None = Field(
        default=None, alias="AGENTS_BASE"
    )
    constitution_path: str | None = Field(
        default=None,
        alias="CONSTITUTION_PATH",
    )
    finder_max_upload_bytes: int = 50_000_000  # 50MB
    finder_max_edit_bytes: int = 1_000_000  # 1MB
    finder_max_view_bytes: int = 10_000_000  # 10MB
    finder_hidden_patterns: list[str] = [
        ".ssh",
        ".gnupg",
        ".env",
        "*.key",
        "*.pem",
        "node_modules",
        ".npm",
        "__pycache__",
        ".cache",
        "proc",
        "sys",
        "dev",
        "run",
    ]
    finder_symlink_whitelist: list[str] = Field(
        default_factory=list,
        alias="FINDER_SYMLINK_WHITELIST",
    )

    # Web Push (VAPID)
    vapid_private_key: str = ""
    vapid_public_key: str = ""

    # SSO / WorkOS AuthKit
    workos_api_key: str = ""
    workos_client_id: str = ""
    workos_cookie_password: str = ""  # 32+ chars for state cookie encryption
    sso_enabled: bool = False  # feature flag for gradual rollout
    expose_openapi: bool = (
        True  # expose /docs and /openapi.json (auth-protected in prod)
    )
    openai_api_key: str = ""
    anthropic_api_key: str = ""  # used by inbox_tldr and other Anthropic SDK callers

    # MAC-Phase 0.5: LiteLLM gateway endpoint. Example forms:
    #   http://<tailnet-host>:4000/v1
    #   https://llm.<your-domain>/v1
    # The deploy bundle (e.g. deploy/marvis-personal/.env) provides the actual value.
    llm_gateway_base_url: str = Field(default="", alias="LLM_GATEWAY_BASE_URL")
    llm_gateway_enforce_public_base_url: bool = Field(
        default=False, alias="LLM_GATEWAY_ENFORCE_PUBLIC_BASE_URL"
    )
    llm_gateway_api_key: SecretStr | None = Field(
        default=None, alias="LLM_GATEWAY_API_KEY"
    )
    ingest_llm_gateway_api_key: SecretStr | None = Field(
        default=None, alias="INGEST_LLM_GATEWAY_API_KEY"
    )
    ingest_llm_gateway_agent_name: str = Field(
        default="marvisx-ingester", alias="INGEST_LLM_GATEWAY_AGENT_NAME"
    )
    newsletter_llm_gateway_api_key: SecretStr | None = Field(
        default=None, alias="NEWSLETTER_LLM_GATEWAY_API_KEY"
    )
    newsletter_llm_gateway_agent_name: str = Field(
        default="newsletter-digest", alias="NEWSLETTER_LLM_GATEWAY_AGENT_NAME"
    )
    # Inbox TLDR migration flags. Both default false → behaviour unchanged
    # (Sonnet only). Setting shadow_mode=true logs cloud + local in parallel
    # without affecting the user response. Setting use_local=true serves the
    # local response and falls back to cloud only on gateway failure.
    inbox_tldr_use_local: bool = Field(default=False, alias="INBOX_TLDR_USE_LOCAL")
    inbox_tldr_shadow_mode: bool = Field(default=False, alias="INBOX_TLDR_SHADOW_MODE")
    inbox_tldr_local_model: Literal["tier-think", "tier-fast"] = Field(
        default="tier-think", alias="INBOX_TLDR_LOCAL_MODEL"
    )
    inbox_deep_research_local_model: Literal[
        "tier-think", "tier-fast", "tier-write"
    ] = Field(
        default="tier-fast", alias="INBOX_DEEP_RESEARCH_LOCAL_MODEL"
    )
    inbox_deep_research_llm_gateway_api_key: SecretStr | None = Field(
        default=None, alias="INBOX_DEEP_RESEARCH_LLM_GATEWAY_API_KEY"
    )
    inbox_deep_research_repair_model: Literal[
        "tier-think", "tier-fast", "tier-write"
    ] = Field(
        default="tier-fast", alias="INBOX_DEEP_RESEARCH_REPAIR_MODEL"
    )
    inbox_deep_research_local_max_tokens: int = Field(
        default=2000, alias="INBOX_DEEP_RESEARCH_LOCAL_MAX_TOKENS"
    )
    inbox_deep_research_local_timeout_seconds: float = Field(
        default=300.0, alias="INBOX_DEEP_RESEARCH_LOCAL_TIMEOUT_SECONDS"
    )
    ingest_parser_max_concurrency: int = Field(
        default=1, alias="INGEST_PARSER_MAX_CONCURRENCY"
    )
    ingest_local_parser_max_concurrency: int = Field(
        default=1, alias="INGEST_LOCAL_PARSER_MAX_CONCURRENCY"
    )
    ingest_ocr_max_concurrency: int = Field(
        default=3, alias="INGEST_OCR_MAX_CONCURRENCY"
    )
    ingest_docparse_max_concurrency: int = Field(
        default=1, alias="INGEST_DOCPARSE_MAX_CONCURRENCY"
    )
    ingest_transcribe_max_concurrency: int = Field(
        default=1, alias="INGEST_TRANSCRIBE_MAX_CONCURRENCY"
    )
    ingest_vision_max_concurrency: int = Field(
        default=1, alias="INGEST_VISION_MAX_CONCURRENCY"
    )
    # Optional AUX proxy URL for non-chat gateway endpoints (/v1/ocr,
    # /v1/audio/transcriptions). If unset, consumers derive it from
    # LLM_GATEWAY_BASE_URL when the private Mac tailnet URL uses :4000.
    llm_gateway_aux_base_url: str = Field(default="", alias="LLM_GATEWAY_AUX_BASE_URL")
    ingest_transcribe_timeout_seconds: float = Field(
        default=900.0, alias="INGEST_TRANSCRIBE_TIMEOUT_SECONDS"
    )
    ingest_transcribe_ffmpeg_timeout_seconds: float = Field(
        default=900.0, alias="INGEST_TRANSCRIBE_FFMPEG_TIMEOUT_SECONDS"
    )
    ingest_transcribe_max_bytes: int = Field(
        default=500 * 1024 * 1024, alias="INGEST_TRANSCRIBE_MAX_BYTES"
    )
    ingest_transcribe_language: str = Field(
        default="", alias="INGEST_TRANSCRIBE_LANGUAGE"
    )
    ingest_ocr_timeout_seconds: float = Field(
        default=300.0, alias="INGEST_OCR_TIMEOUT_SECONDS"
    )
    ingest_ocr_max_bytes: int = Field(
        default=500 * 1024 * 1024, alias="INGEST_OCR_MAX_BYTES"
    )
    ingest_docparse_enabled: bool = Field(
        default=False, alias="INGEST_DOCPARSE_ENABLED"
    )
    ingest_docparse_pdfs_enabled: bool = Field(
        default=True, alias="INGEST_DOCPARSE_PDFS_ENABLED"
    )
    ingest_docparse_images_enabled: bool = Field(
        default=True, alias="INGEST_DOCPARSE_IMAGES_ENABLED"
    )
    ingest_docparse_mode: Literal["fast", "standard", "precise"] = Field(
        default="standard", alias="INGEST_DOCPARSE_MODE"
    )
    ingest_docparse_mode_override: Literal["", "fast", "standard", "precise"] = Field(
        default="", alias="INGEST_DOCPARSE_MODE_OVERRIDE"
    )
    ingest_docparse_timeout_seconds: float = Field(
        default=900.0, alias="INGEST_DOCPARSE_TIMEOUT_SECONDS"
    )
    ingest_docparse_max_bytes: int = Field(
        default=500 * 1024 * 1024, alias="INGEST_DOCPARSE_MAX_BYTES"
    )
    ingest_vision_images_enabled: bool = Field(
        default=False, alias="INGEST_VISION_IMAGES_ENABLED"
    )
    ingest_vision_timeout_seconds: float = Field(
        default=120.0, alias="INGEST_VISION_TIMEOUT_SECONDS"
    )
    ingest_vision_max_bytes: int = Field(
        default=20 * 1024 * 1024, alias="INGEST_VISION_MAX_BYTES"
    )
    ingest_vision_max_tokens: int = Field(
        default=900, alias="INGEST_VISION_MAX_TOKENS"
    )
    ingest_docx_max_bytes: int = Field(
        default=25 * 1024 * 1024, alias="INGEST_DOCX_MAX_BYTES"
    )
    ingest_llm_classifier_model: Literal["tier-fast"] = Field(
        default="tier-fast", alias="INGEST_LLM_CLASSIFIER_MODEL"
    )
    ingest_llm_provider: Literal["local", "gateway", "mac", "tier-fast"] = Field(
        default="local", alias="INGEST_LLM_PROVIDER"
    )
    todos_llm_provider: Literal[
        "local", "gateway", "mac", "tier-fast", "none", "off", "disabled"
    ] = Field(default="local", alias="TODOS_LLM_PROVIDER")
    ingest_llm_classifier_timeout_seconds: float = Field(
        default=30.0, alias="INGEST_LLM_CLASSIFIER_TIMEOUT_SECONDS"
    )
    ingest_llm_classifier_max_concurrency: int = Field(
        default=4, alias="INGEST_LLM_CLASSIFIER_MAX_CONCURRENCY"
    )
    ingest_llm_classifier_max_attempts: int = Field(
        default=3, alias="INGEST_LLM_CLASSIFIER_MAX_ATTEMPTS"
    )
    ingest_llm_classifier_retry_after_cap_seconds: float = Field(
        default=30.0, alias="INGEST_LLM_CLASSIFIER_RETRY_AFTER_CAP_SECONDS"
    )
    ingest_llm_gateway_priority: Literal[
        "interactive", "batch", "background"
    ] = Field(default="batch", alias="INGEST_LLM_GATEWAY_PRIORITY")
    ingest_llm_gateway_initial_poll_delay_seconds: int = Field(
        default=1, alias="INGEST_LLM_GATEWAY_INITIAL_POLL_DELAY_SECONDS"
    )

    # Phase 7.0: KG lens default for HTTP surface (MCP default is separate: MARVIS_MCP_DEEP_DEFAULT)
    kg_http_deep_default: bool = (
        False  # pydantic-settings maps KG_HTTP_DEEP_DEFAULT env var
    )

    # Track 2 #3a — STRUCTURAL graph-lane in the RRF fusion (DEFAULT OFF).
    # When False the fused ranking is byte-identical to today (5 lanes). When
    # True a 6th lane (seeded 1-hop edge-weighted KG expansion) joins the blend.
    # graph_lane_hops / graph_lane_fanout / graph_lane_weight / graph_lane_seeds
    # tune the lane without code changes. Edge-type subset = all 15 by default.
    graph_lane_enabled: bool = Field(
        default=False, alias="MARVIS_GRAPH_LANE"
    )
    graph_lane_hops: int = Field(default=1, alias="MARVIS_GRAPH_LANE_HOPS")
    graph_lane_fanout: int = Field(default=25, alias="MARVIS_GRAPH_LANE_FANOUT")
    graph_lane_seeds: int = Field(default=10, alias="MARVIS_GRAPH_LANE_SEEDS")
    graph_lane_weight: float = Field(
        default=0.12, alias="MARVIS_GRAPH_LANE_WEIGHT"
    )
    # #13: when a graph read finds its index stale/behind HEAD, surface the
    # reindex as an EXPLICIT next action (command + auto_reindex marker). Default
    # off; even when on, a read NEVER fires the (heavy, repo-re-parsing) KG
    # reindex itself — re-indexing stays an explicit operation.
    graph_autoreindex_on_drift: bool = Field(
        default=False, alias="MARVIS_GRAPH_AUTOREINDEX_ON_DRIFT"
    )

    # Track 2 #1 — bi-temporal memory READ path (DEFAULT OFF). When False every
    # learnings read is byte-identical to today (no invalid_at filter, no as_of):
    # the temporal helper emits NO extra SQL. When True the default learnings
    # reads (check/list/get + the learnings retrieval lane in search) add
    # ``AND invalid_at IS NULL`` — a MECHANICAL, BINARY exclusion (superseded rows
    # never reach the LLM; never a down-weight, which would still let the model
    # cite the stale value). An explicit ``as_of=<ISO>`` relaxes the filter to the
    # point-in-time window ``valid_from <= as_of AND (invalid_at IS NULL OR
    # invalid_at > as_of)`` for audit. S3/S4/S5 (write path / LLM tiebreak / dream
    # cycle) reuse this SAME flag. Requires migration 148 columns when enabled.
    temporal_memory_enabled: bool = Field(
        default=False, alias="MARVIS_TEMPORAL_MEMORY"
    )

    # answer-ready claims (DEFAULT OFF). When False every reasoning-tool read is
    # byte-identical to today (no `claims` key). When True, project_impact/
    # graph_impact append a `claims[]` block: server-computed, relation-typed
    # counts with provenance — the agent reports the value instead of re-counting
    # raw edges (faithfulness). Separate from temporal_memory so grounding can be
    # enabled without the freshness surface. See plan 2026-06-08-feat-answer-ready-claims.
    kg_claims_enabled: bool = Field(
        default=False, alias="MARVIS_KG_CLAIMS"
    )

    # Memory-freshness v2a Phase 1 (B-fix, DEFAULT OFF). The documents_fts
    # INSERT/UPDATE triggers (migration 136) write file_path into the content
    # column, so every doc written AFTER the one-time migration backfill has NO
    # body in the lexical lane (prod 2026-06-09: 100% of recent docs degraded
    # → the BM25 lane sees OLD bodies but never NEW ones — a direct stale-
    # retrieval mechanism). When True, every documents upsert path additionally
    # overwrites the trigger-degraded FTS row with the real title+body
    # (refresh_documents_fts_row). When False the refresh helper is a no-op and
    # behavior is byte-identical to today. The one-shot backfill script
    # (scripts/backfill_documents_fts.py) repairs historical rows; in prod the
    # flag must flip ON at backfill time and stay on, otherwise the next doc
    # update re-degrades its row. See plan
    # 2026-06-09-feat-memory-freshness-v2a-retrieval.
    fts_bodies_enabled: bool = Field(
        default=False, alias="MARVIS_FTS_BODIES"
    )

    # Fase 2 mielinizzazione (plan 2026-08-16, v3) — outcome-anchored salience
    # reinforcement. TRI-STATE flag (R4): "off" = STRUCTURAL branch, the
    # pre-existing search path runs verbatim (no new query, byte-identical
    # scores AND response shape); "shadow" = ledger writes + memory_feedback
    # tool + nudge active, ranking contribution ZERO (read path identical to
    # off); "on" = effective salience enters the documents-hit ranking
    # post-fusion (U2). All numeric knobs are config, not code — post-deploy
    # calibration is part of the contract (KTD10).
    reinforcement_mode: Literal["off", "shadow", "on"] = Field(
        default="off", alias="MARVIS_REINFORCEMENT"
    )
    # Exponential decay half-life for ledger boosts, in days (R2: contribution
    # = weight · 2^(−age_days/half_life), computed in application code on the
    # candidate set only).
    reinforcement_half_life_days: float = Field(
        default=30.0, alias="MARVIS_REINFORCEMENT_HALF_LIFE_DAYS"
    )
    # Cap on the TOTAL boost contribution per doc: effettiva = base +
    # clamp(Σ, 0, cap). The lower clamp at 0 is the floor guarantee (KTD6:
    # misled without positives = no-op on ranking; floor = salience_base).
    reinforcement_cap_total: float = Field(
        default=0.3, alias="MARVIS_REINFORCEMENT_CAP_TOTAL"
    )
    # Per-boost weights (R8): agent < human, both calibratable post-deploy.
    reinforcement_weight_agent: float = Field(
        default=0.05, alias="MARVIS_REINFORCEMENT_WEIGHT_AGENT"
    )
    reinforcement_weight_human: float = Field(
        default=0.15, alias="MARVIS_REINFORCEMENT_WEIGHT_HUMAN"
    )
    # ISO timestamp; the read path ignores boosts CREATED BEFORE this epoch —
    # a ledger reset without DELETE (R4 emended v3).
    reinforcement_boost_epoch: str | None = Field(
        default=None, alias="MARVIS_REINFORCEMENT_BOOST_EPOCH"
    )
    # Anti-gaming caps on boost accounting (R7), enforced by the U3 feedback
    # gate against salience_boosts (accepted rows) with rejections recorded in
    # boost_rejects (mig 174; the legacy mig-046 boost_log stays with the REST
    # rate-limit): max accepted agent boosts per authenticated principal per
    # sliding hour; max per doc per day per principal; max DISTINCT principals
    # per doc per day.
    reinforcement_agent_hourly_cap: int = Field(
        default=3, alias="MARVIS_REINFORCEMENT_AGENT_HOURLY_CAP"
    )
    reinforcement_agent_doc_daily_cap: int = Field(
        default=1, alias="MARVIS_REINFORCEMENT_AGENT_DOC_DAILY_CAP"
    )
    reinforcement_doc_distinct_daily_cap: int = Field(
        default=3, alias="MARVIS_REINFORCEMENT_DOC_DISTINCT_DAILY_CAP"
    )
    # R10: N distinct misled (distinct principals, or same principal on ≥2
    # days) within the dedup window → supersede/contradiction proposal (U4).
    reinforcement_misled_threshold: int = Field(
        default=2, alias="MARVIS_REINFORCEMENT_MISLED_THRESHOLD"
    )
    # R13 tripwire: share of total effective-salience boost mass held by the
    # top decile of boosted docs above which telemetry flags concentration.
    reinforcement_top_decile_share_threshold: float = Field(
        default=0.5, alias="MARVIS_REINFORCEMENT_TOP_DECILE_SHARE_THRESHOLD"
    )

    # Memory-freshness v2a Phase 2 (A-span, DEFAULT OFF). When False the search
    # read path never touches the chunks/vec_chunks sidecars and the response is
    # byte-identical to today (no span_* fields populated). When True (and
    # MARVIS_CHUNKING populated the sidecars), the semantic lane max-pools
    # chunk-KNN hits into the doc ranking and each file-backed hit carries the
    # winning chunk's span expanded to line boundaries ±12 lines (span_text /
    # span_path / span_line_start / span_line_end) so the agent can answer FROM
    # the search result without a follow-up Read. Write-side chunking stays
    # gated by MARVIS_CHUNKING (env, read per-call). See plan
    # 2026-06-09-feat-memory-freshness-v2a-retrieval.
    search_spans_enabled: bool = Field(
        default=False, alias="MARVIS_SEARCH_SPANS"
    )

    # Track 2 #3c — community summaries in the brief (GraphRAG global-search,
    # DEFAULT OFF). When False the brief is byte-identical to today: the
    # communities/community_summary modules are a flag-gated library + seam,
    # never imported into session_brief. When True (off-host wiring), the KG is
    # partitioned (deterministic label propagation, NO LLM) and per-community
    # summaries enter the brief AFTER #2 so they inherit span-citation + NLI
    # verification (a summary is itself a claim to ground, not free text).
    community_summaries_enabled: bool = Field(
        default=False, alias="MARVIS_COMMUNITY_SUMMARIES"
    )

    # Track 2 #2: span-citation + grounding-verification layer for the brief.
    # DEFAULT OFF — the layer exists as a flag-gated library; when off the
    # session_brief output is byte-for-byte unchanged (the seam is never entered).
    # See core/api/services/grounding/. Wiring the NLI head (MiniCheck) is a
    # separate, deliberate, off-host step (NoopVerifier is the default).
    brief_citations_enabled: bool = Field(
        default=False, alias="MARVIS_BRIEF_CITATIONS"
    )

    # WIP limit: max tasks in_progress per project (doc/none tasks count as 0.5)
    wip_max_in_progress: int = 3

    # sqlite-vec: env VEC0_PATH → sqlite_vec.loadable_path() → prod fallback.
    vec0_path: str = Field(
        default_factory=_default_vec0_path,
        validation_alias=AliasChoices("VEC0_PATH"),
    )

    # LifeOS
    lifeos_data_dir: str = ""
    lifeos_git_repo_dir: str = ""
    lifeos_git_sync_interval: int = 30

    # Brain v1.2 Wave 3.1 — LLM polish layer (master switch + sub-switches).
    # Tier-write Gemma 3 12B QAT via Mac Gateway tenant `marvisx-brain`.
    # Master defaults True so narrative_polished is populated out-of-the-box;
    # sub-switches stay True (journal/finding_summary/finding_reasoning).
    # Disable master via BRAIN_LLM_POLISH_ENABLED=false to fall back to deterministic.
    brain_llm_polish_enabled: bool = Field(
        default=True, alias="BRAIN_LLM_POLISH_ENABLED"
    )
    # P1: pick the Brain LLM backend. "gateway" (default, unchanged) = the HTTP
    # Mac-gateway client (requires BRAIN_LLM_GATEWAY_API_KEY). "claude_cli" = run
    # `claude -p` headless on the user's Claude Code subscription (NO API key) —
    # see core/api/services/brain/llm/claude_cli.py.
    brain_llm_provider: Literal["gateway", "claude_cli"] = Field(
        default="gateway", alias="BRAIN_LLM_PROVIDER"
    )
    # Path/name of the `claude` binary for the claude_cli provider.
    marvis_claude_bin: str = Field(default="claude", alias="MARVIS_CLAUDE_BIN")
    brain_llm_journal_polish_enabled: bool = Field(
        default=True, alias="BRAIN_LLM_JOURNAL_POLISH_ENABLED"
    )
    brain_llm_finding_summary_enabled: bool = Field(
        default=True, alias="BRAIN_LLM_FINDING_SUMMARY_ENABLED"
    )
    brain_llm_finding_reasoning_enabled: bool = Field(
        default=True, alias="BRAIN_LLM_FINDING_REASONING_ENABLED"
    )
    brain_llm_gateway_base_url: str = Field(
        default="",
        alias="BRAIN_LLM_GATEWAY_BASE_URL",
    )
    brain_llm_gateway_api_key: SecretStr | None = Field(
        default=None, alias="BRAIN_LLM_GATEWAY_API_KEY"
    )
    brain_llm_tenant: str = Field(
        default="marvisx-brain", alias="BRAIN_LLM_TENANT"
    )
    brain_llm_model: str = Field(
        default="tier-write", alias="BRAIN_LLM_MODEL"
    )
    brain_llm_polish_timeout_seconds: int = Field(
        default=30, alias="BRAIN_LLM_POLISH_TIMEOUT_SECONDS"
    )
    brain_llm_polish_cache_ttl_seconds: int = Field(
        default=3600, alias="BRAIN_LLM_POLISH_CACHE_TTL_SECONDS"
    )
    # Wave 3.1 polish UX (Emilio 2026-05-19): default DISABLED. Quando True,
    # il polish path forza il LLM a citare ≥1 evidence_ref dal whitelist e
    # rifiuta narrative "not grounded" — ha bloccato ~50% delle entries
    # (~72% scope=company) senza prevenire hallucination reale (il body_json
    # passato al LLM è già ground truth deterministico). Lasciato come escape
    # hatch operatore via .env BRAIN_LLM_GROUNDING_STRICT=true.
    brain_llm_grounding_strict: bool = Field(
        default=False, alias="BRAIN_LLM_GROUNDING_STRICT"
    )
    brain_llm_retry_max_attempts: int = Field(
        default=3, alias="BRAIN_LLM_RETRY_MAX_ATTEMPTS"
    )
    brain_llm_retry_backoff_seconds: float = Field(
        default=1.0, alias="BRAIN_LLM_RETRY_BACKOFF_SECONDS"
    )
    brain_llm_semaphore_size: int = Field(
        default=8, alias="BRAIN_LLM_SEMAPHORE_SIZE"
    )

    @model_validator(mode="after")
    def validate_security_boundary(self) -> "Settings":
        """Reject ambiguous proxy policy and weak production signing keys."""
        for cidr in self.trusted_proxy_cidrs:
            try:
                network = ipaddress.ip_network(cidr, strict=True)
            except ValueError as exc:
                raise ValueError(
                    "TRUSTED_PROXY_CIDRS must contain canonical IPv4/IPv6 host routes"
                ) from exc
            if network.prefixlen != network.max_prefixlen:
                raise ValueError(
                    "TRUSTED_PROXY_CIDRS accepts only exact IPv4 /32 or IPv6 /128 peers"
                )

        if self.pir_env.strip().lower() == "production":
            secret = self.pir_jwt_secret
            if secret == _DEV_JWT_SECRET or len(secret.encode("utf-8")) < 32:
                raise ValueError(
                    "production JWT secret must be explicitly configured with "
                    "at least 32 UTF-8 bytes"
                )
        if self.agent_token_default_lifetime_hours > self.agent_token_max_lifetime_hours:
            raise ValueError(
                "AGENT_TOKEN_DEFAULT_LIFETIME_HOURS cannot exceed "
                "AGENT_TOKEN_MAX_LIFETIME_HOURS"
            )
        if self.agent_token_auth_mode == "strict" and self.tasks_api_token:
            raise ValueError(
                "TASKS_API_TOKEN must be removed before AGENT_TOKEN_AUTH_MODE=strict"
            )
        return self

    @property
    def cors_origins(self) -> list[str]:
        if self.pir_env == "production":
            return self.cors_origins_prod
        return self.cors_origins_dev

    @property
    def is_production(self) -> bool:
        return self.pir_env == "production"

    @property
    def is_marvis_personal(self) -> bool:
        return self.deploy_mode == "marvis-personal"

    @property
    def phase_index(self) -> int:
        return ["A", "B", "C", "D"].index(self.marvisx_phase)

    @property
    def effective_console_base_url(self) -> str:
        return self.console_base_url or "http://localhost:3000"

    @property
    def effective_workspace_root(self) -> str:
        return self.workspace_root or os.getcwd()

    @property
    def effective_repo_share_root(self) -> str:
        return self.repo_share_root or self.effective_workspace_root

    @property
    def effective_runtime_home(self) -> str:
        return self.runtime_home or os.path.expanduser("~")

    @property
    def effective_agents_base(self) -> str:
        return self.agents_base or f"{self.effective_runtime_home}/marvisx/data/agents"

    @property
    def effective_constitution_path(self) -> str:
        return (
            self.constitution_path
            or f"{self.effective_workspace_root}/.claude/rules/constitution.md"
        )

    @property
    def effective_git_runas_user(self) -> str:
        """Run-as user for git/chown, or '' to act as the current process.

        Returns '' when GIT_RUNAS_USER is unset or already matches the current
        OS user (no privilege hop needed)."""
        user = (self.git_runas_user or "").strip()
        if not user:
            return ""
        # current_user() is pwd.getpwuid(os.getuid()).pw_name on POSIX (identical
        # to the previous inline lookup) and getpass-based on Windows; the bare
        # `import pwd` here crashed `marvis project list` on Windows.
        if current_user() == user:
            return ""
        return user


settings = Settings()


def apply_marvis_settings(*, force: bool = False) -> bool:
    """Apply the shared runtime settings through the canonical config surface."""
    from core.api.runtime_settings import apply_marvis_settings as apply_runtime_settings

    return apply_runtime_settings(force=force)


def _resolve_repo_parents() -> list[Path]:
    """Read ALLOWED_REPO_PARENTS from env (comma-separated absolute paths).

    Falls back to a derivation from settings.effective_workspace_root,
    MARVIS_PROJECTS_ROOT, MARVIS_REPOS_ROOT, and /data/projects/ when
    ALLOWED_REPO_PARENTS is unset, so generic deploys work out of the box.
    Personal/enterprise deploys override via .env.
    """
    raw = os.environ.get("ALLOWED_REPO_PARENTS", "").strip()
    if raw:
        return [Path(p.strip()).resolve() for p in raw.split(",") if p.strip()]
    workspace = Path(settings.effective_workspace_root).resolve()
    parents = [
        (workspace / "projects").resolve(),
        workspace,
        Path("/data/projects/").resolve(),
    ]
    projects_root = os.environ.get("MARVIS_PROJECTS_ROOT", "").strip()
    if projects_root:
        parents.append(Path(projects_root).expanduser().resolve())
    repos_root = os.environ.get("MARVIS_REPOS_ROOT", "").strip()
    if repos_root:
        parents.append(Path(repos_root).expanduser().resolve())

    unique: list[Path] = []
    for parent in parents:
        if parent not in unique:
            unique.append(parent)
    return unique


# Centralized allowlist for repo_path validation (Decision S14, was duplicated in git_ops + projects)
ALLOWED_REPO_PARENTS: list[Path] = _resolve_repo_parents()
