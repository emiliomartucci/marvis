# v1.1.0 - 2026-04-15 - KG Phase 4: KGStatus model + MonitoringSnapshot.kg
# v1.0.0 - 2026-03-03 - Monitoring, metrics, security, and finder models
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, field_validator


# --- Monitoring Models ---

class MetricDatapoint(BaseModel):
    t: int
    v: float


class CandleDatapoint(BaseModel):
    t: int
    o: float
    h: float
    l: float
    c: float


class ContainerMetrics(BaseModel):
    name: str
    status: str
    cpu_pct: float = 0.0
    memory_mb: float = 0.0
    memory_limit_mb: float = 0.0
    memory_pct: float = 0.0
    restart_count: int = 0
    uptime_seconds: float = 0.0


class ServiceStatus(BaseModel):
    name: str
    status: str
    details: str | None = None


class ConnectivityStatus(BaseModel):
    tailscale: str = "unknown"
    tailscale_ip: str | None = None
    cf_tunnel: str = "unknown"


class SecurityEvent(BaseModel):
    timestamp: int
    event_type: str
    source_ip: str | None = None
    username: str | None = None
    details: dict | None = None


class SSHSummary(BaseModel):
    success_count: int = 0
    failed_count: int = 0
    unique_ips: int = 0


class BanInfo(BaseModel):
    ip: str
    jail: str
    timestamp: int


class AlertInfo(BaseModel):
    metric: str
    value: float
    threshold: float
    level: str = "warning"


class SystemMetrics(BaseModel):
    cpu_pct: float = 0.0
    ram_pct: float = 0.0
    ram_used_mb: float = 0.0
    ram_total_mb: float = 0.0
    disk_pct: float = 0.0
    disk_used_gb: float = 0.0
    disk_total_gb: float = 0.0
    load_1m: float = 0.0
    load_5m: float = 0.0
    load_15m: float = 0.0
    uptime_seconds: float = 0.0
    net_rx_bps: float = 0.0
    net_tx_bps: float = 0.0
    cpu_count: int = 1


class DiskTreeNode(BaseModel):
    path: str
    name: str
    size_mb: int
    depth: int


class DiskTreeResponse(BaseModel):
    items: list[DiskTreeNode]
    total_mb: int
    free_mb: int


class SecuritySummary(BaseModel):
    ssh_success_24h: int = 0
    ssh_failed_24h: int = 0
    bans_active: int = 0


class SkippedFileEntry(BaseModel):
    """KG Phase 4: structured skip entry from kg_watcher_state.recent_skipped."""

    file: str
    reason: str
    fix_hint: str | None = None
    kind: str | None = None
    task_id: str | None = None


class FlushEntry(BaseModel):
    """KG Phase 4: per-flush summary from kg_watcher_state.recent_flushes."""

    at: str  # ISO-8601 UTC string from the daemon (kept as str for pass-through)
    upsert_count: int = 0
    delete_count: int = 0
    files_processed: int = 0
    edges_written: int = 0
    duration_ms: float = 0.0


class KGStatus(BaseModel):
    """KG Phase 4: snapshot dello stato del kg-watcher daemon + safety net.

    Esposto via /api/v1/monitoring/current → mcp__marvis__get_monitoring per dare
    agli agent la possibilita' di rispondere a "stato del watcher?" senza
    parsare log o systemctl. Tre signal:

    - `watcher_status`: liveness derivata da systemctl --user is-active.
      Mappata a Literal per evitare drift textuale.
    - `last_flush_at`: timestamp ultimo flush (UTC tz-aware obbligatorio).
      None se nessun flush ancora avvenuto. Stale > 30min puo' indicare
      daemon stuck (sd_notify WatchdogSec lo ammazza prima, ma il monitoring
      lo cattura come segnale soft).
    - `recent_skipped` / `recent_flushes`: ring buffer 20 entries da
      kg_watcher_state. Fornisce contesto debug agent-accessible (es.
      "perche' il mio handoff non e' nel grafo?" → controlla recent_skipped).

    Drop volutamente (plan deepen):
    - pending_queue_size: zero diagnostic value, vive max 2s tra debounce
      e flush.
    - last_full_rebuild_at: derivabile da systemctl show -p ExecMainExitTimestamp
      pir-kg-full-rebuild.service. Esporlo in monitoring duplicherebbe.
    """

    watcher_status: Literal["active", "inactive", "failed", "disabled", "unknown"] = "unknown"
    last_flush_at: datetime | None = None
    recent_skipped: list[SkippedFileEntry] = Field(default_factory=list)
    recent_flushes: list[FlushEntry] = Field(default_factory=list)
    total_flushes: int = 0
    total_files_processed: int = 0

    @field_validator("last_flush_at")
    @classmethod
    def ensure_utc(cls, v: datetime | None) -> datetime | None:
        """Naive datetime → debug hell cross-timezone. Coerce to UTC tz-aware
        (kieran-python deepen review). Accept either naive (assumed UTC, daemon
        writes datetime('now') in SQLite which is UTC) or already-aware values."""
        if v is None:
            return v
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)


class MonitoringSnapshot(BaseModel):
    timestamp: int
    system: SystemMetrics
    docker: list[ContainerMetrics] = []
    network: ConnectivityStatus = ConnectivityStatus()
    services: list[ServiceStatus] = []
    alerts: list[AlertInfo] = []
    sparklines: dict[str, list[MetricDatapoint]] = {}
    security_summary: SecuritySummary = SecuritySummary()
    # KG Phase 4: default non-None KGStatus(watcher_status="unknown") evita
    # `if snapshot.kg is not None` sparsi nei consumer (plan deepen).
    kg: KGStatus = Field(default_factory=KGStatus)


class SecurityData(BaseModel):
    ssh_events: list[SecurityEvent] = []
    ssh_summary_24h: SSHSummary = SSHSummary()
    active_bans: list[BanInfo] = []
    ban_count_24h: int = 0
    console_logins: list[SecurityEvent] = []


# --- Finder Models ---

class FinderTreeNode(BaseModel):
    name: str
    path: str
    has_children: bool


class FinderListItem(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    modified: str
    mime_type: str | None
    extension: str | None


class FinderListResponse(BaseModel):
    items: list[FinderListItem]
    path: str
    parent: str | None


class FinderFileContent(BaseModel):
    content: str
    filename: str
    path: str
    size: int
    mime_type: str | None
    encoding: Literal["utf-8", "base64"]
    readonly: bool


class FinderFileUpdate(BaseModel):
    content: str
