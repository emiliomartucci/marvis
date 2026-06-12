# v1.5.0 - 2026-04-23 - PR4: shadow cost_equivalent fields (migration 089)
# v1.4.0 - 2026-04-22 - PR2: dual metrics fields + computed_field aliases
# v1.3.0 - 2026-04-22 - Add ConversationId/SessionId NewType aliases (PR1)
# v1.2.0 - 2026-03-13 - Add project_slug to SessionCreate for project-aware session creation
from __future__ import annotations

from typing import Literal, NewType

from pydantic import BaseModel, Field

# Type aliases for stronger signatures in provider/service layers. Runtime
# these are plain str (NewType has zero overhead); type checkers treat them
# as distinct so you can't swap a ConversationId for a SessionId accidentally.
ConversationId = NewType("ConversationId", str)
SessionId = NewType("SessionId", str)


class SessionInfo(BaseModel):
    name: str
    display_name: str | None = None
    pinned: bool = False
    sort_order: int = 0
    group_name: str | None = None
    project_slug: str | None = None
    session_uuid: str | None = None
    status: str | None = None  # active process: "claude", "bash", "vim", etc.
    created_at: str | None = None
    last_active: str | None = None
    attached: bool = False
    # Intelligence fields
    hibernated: bool = False
    conversation_id: str | None = None
    model: str | None = None
    launch_model: str | None = None
    permission_preset: str | None = None
    # Legacy aliases kept writable for routers/agent that populate them
    # directly; PR2 wires the dual `last_context_pct_real/_scaled` +
    # `last_cost_conversation_usd/_session_usd` fields below.
    last_context_pct: float | None = None
    last_cost_usd: float | None = None
    last_message_count: int | None = None
    auto_hibernate_minutes: int = 240
    activity_state: str | None = None  # "working", "idle", "needs_input", "active"
    # Process metrics (from ps)
    cpu_pct: float | None = None
    ram_mb: float | None = None
    # Time tracking
    working_seconds: int = 0
    created_epoch: float | None = None  # for frontend uptime calc
    # DevX
    agent_managed: bool = False
    # Ownership (migration 033)
    owner_id: str | None = None
    # Provider (migration 050)
    provider: str = "claude"
    # --- PR2 dual metrics (migration 087) ----------------------------------
    # Real = actual ratio vs context_window. Scaled = Claude's 100/84 alias
    # that matches the auto-compact banner. OpenCode has scaled=None.
    last_context_pct_real: float | None = None
    last_context_pct_scaled: float | None = None
    # Dual cost: conversation = this JSONL/SQLite session only;
    # session = cumulative across resume chain (session_conversations).
    last_cost_conversation_usd: float | None = None
    last_cost_session_usd: float | None = None
    last_cost_session_incomplete: bool = False
    last_input_tokens: int | None = None
    last_output_tokens: int | None = None
    last_reasoning_tokens: int | None = None
    working_seconds_msg: int | None = None
    metrics_refreshed_at: str | None = None
    pricing_version: str | None = None
    conversation_ids: list[str] = Field(default_factory=list)
    # --- PR4 shadow cost (migration 089) -----------------------------------
    # What the session WOULD cost at pay-per-token API rates. For Claude =
    # real cost (already pay-per-token). For OpenCode OAuth/free sessions
    # with real cost=0, this exposes the hypothetical API bill. None when
    # pricing unknown (fallback_strategy=skip).
    last_cost_conversation_equivalent_usd: float | None = None
    last_cost_session_equivalent_usd: float | None = None
    last_cost_equivalent_pricing_version: str | None = None


class SessionCreate(BaseModel):
    name: str = Field(
        ...,
        min_length=2,
        max_length=30,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,28}[a-zA-Z0-9]$",
    )
    project_slug: str | None = Field(
        None, max_length=65, pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"
    )
    provider: str | None = None
    model: str | None = Field(None, max_length=120)
    permission_preset: str | None = Field(None, max_length=40)
    theme_mode: Literal["light", "dark"] | None = None


class SessionInfoDelta(BaseModel):
    """Minimal payload (~9 fields, <2KB) for WS broadcast `session_renamed`.

    Excludes full SessionInfo (~50 fields incl. dual metrics, conversation_ids)
    to keep payload compact + avoid privacy leak cross-tab. Client refetches
    full SessionInfo on-demand if richer fields are needed.

    Plan 2026-05-21 WS broadcast session_renamed delta — closes post-rename
    stale sidebar.
    """

    name: str
    prev_name: str
    display_name: str | None = None
    provider: str | None = None
    model: str | None = None
    project_slug: str | None = None
    status: str | None = None
    activity_state: str | None = None
    updated_at: str  # ISO timestamp, used as client-side idempotency key


class SessionUpdate(BaseModel):
    """Unified PATCH body - all fields optional."""

    new_name: str | None = Field(
        None,
        min_length=2,
        max_length=30,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,28}[a-zA-Z0-9]$",
    )
    display_name: str | None = Field(None, max_length=100)
    pinned: bool | None = None
    group_name: str | None = Field(None, max_length=50)
    project_slug: str | None = Field(
        None, max_length=65, pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"
    )
    agent_managed: bool | None = None  # DevX: operator+ only
    owner_id: str | None = None  # admin only: explicit owner override


class SessionReorder(BaseModel):
    """PUT body for bulk reorder."""

    order: list[str]


class SessionRename(BaseModel):
    new_name: str = Field(
        ...,
        min_length=2,
        max_length=30,
        pattern=r"^[a-zA-Z0-9][a-zA-Z0-9\-]{0,28}[a-zA-Z0-9]$",
    )


class SendMessageBody(BaseModel):
    text: str = Field(..., max_length=2000)


class SessionMetricsResponse(BaseModel):
    conversation_id: str | None = None
    model: str | None = None
    context_pct: float = 0.0
    cost_usd: float = 0.0
    message_count: int = 0
    duration_minutes: float = 0.0
    hibernated: bool = False
    auto_hibernate_minutes: int = 240
    # PR2 dual metrics (optional so legacy callers ignore them)
    context_pct_real: float | None = None
    context_pct_scaled: float | None = None
    cost_conversation_usd: float | None = None
    cost_session_usd: float | None = None
    cost_session_incomplete: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    working_seconds_msg: int | None = None
    pricing_version: str | None = None
    conversation_ids: list[str] = Field(default_factory=list)
    # PR4 shadow cost
    cost_conversation_equivalent_usd: float | None = None
    cost_session_equivalent_usd: float | None = None
    cost_equivalent_pricing_version: str | None = None


class HibernateRequest(BaseModel):
    """Optional: set auto_hibernate_minutes for this session."""

    auto_hibernate_minutes: int | None = None


class AgentSessionView(BaseModel):
    """Subset di SessionInfo ottimizzato per agent consumers."""

    name: str
    session_uuid: str | None = None
    project_slug: str | None = None
    status: str | None = None
    activity_state: str | None = None
    last_context_pct: float | None = None
    last_cost_usd: float | None = None
    cpu_pct: float | None = None
    ram_mb: float | None = None
    working_seconds: int = 0
    uptime_seconds: float | None = None
    hibernated: bool = False
    conversation_id: str | None = None
    model: str | None = None
    launch_model: str | None = None
    agent_managed: bool = False
    provider: str = "claude"


class SessionCatalogModel(BaseModel):
    id: str
    label: str
    description: str
    context_window: int | None = None
    supports_1m: bool = False
    recommended: bool = False
    experimental: bool = False
    note: str | None = None


class SessionPermissionPreset(BaseModel):
    id: str
    label: str
    badge: str
    description: str


class SessionCatalogProvider(BaseModel):
    id: str
    label: str
    default_model: str
    launch_root: Literal["project", "workspace"] = "workspace"
    models: list[SessionCatalogModel]
    permission_presets: list[SessionPermissionPreset] = []
    note: str | None = None


class SessionCatalogResponse(BaseModel):
    providers: list[SessionCatalogProvider]


class AgentSessionUpdate(BaseModel):
    """PATCH body per agenti — solo campi sicuri."""

    project_slug: str | None = Field(
        None, max_length=65, pattern=r"^[a-z0-9][a-z0-9&+_.\-]{0,62}$"
    )
    display_name: str | None = Field(None, max_length=100)
    agent_managed: bool | None = None  # DevX: operator+ only


class PaneResponse(BaseModel):
    """Output del pane tmux per un agente."""

    lines: list[str]
    activity_state: str  # needs_input | idle | working | active
    input_prompt: str | None = None


class InputBody(BaseModel):
    """Risposta safe a prompt di approvazione strumenti — whitelist hard-coded."""

    response: Literal["y", "n", "Allow", "Deny", "Enter", "Escape"]


class ExecBody(BaseModel):
    """Input arbitrario al terminale.

    Newline nel testo vengono strippati prima dell'invio (previene command splitting).
    raw=True: send_keys_raw (tmux key sequences, es. C-c, Escape). Audit usa hash+length.
    raw=False: send_keys (testo + Enter automatico).
    """

    text: str = Field(..., min_length=1, max_length=2000)
    raw: bool = False


# Session state events from provider hooks/plugins (PR1, plan 2026-04-26).
# Literal narrows accepted values at the boundary — payload arbitrari = 422.
SessionStateProvider = Literal["claude", "opencode"]
SessionStateEvent = Literal[
    # Claude Code hooks
    "PreToolUse",
    "Stop",
    "StopFailure",
    "PermissionRequest",
    "SessionStart",
    "SessionEnd",
    # OpenCode plugin events (canonical via session.status)
    "session.status:active",
    "session.status:idle",
    "session.status:error",
    "session.error",
    "session.idle",  # legacy alias for session.status:idle (deprecated upstream)
    "session.deleted",
    "permission.updated",
]


class SessionStateUpdate(BaseModel):
    """Event-driven session state update from a provider hook/plugin.

    `ts` is a client-emitted monotonic timestamp (ISO 8601). The server uses it
    as the last-write-wins key (`WHERE ... < ?`) to handle out-of-order arrival
    of bash-backgrounded curls hitting different uvicorn workers (julik R2).

    `state` is intentionally NOT a field: the server is the single source of
    truth for the event→canonical-state mapping (drop body.state, simplicity-7).
    """

    provider: SessionStateProvider
    event: SessionStateEvent
    conv_id: str | None = Field(None, max_length=128)
    ts: str = Field(
        ...,
        max_length=64,
        description="Client monotonic ISO 8601 timestamp, e.g. 2026-04-26T14:30:00.123456Z",
    )
