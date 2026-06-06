# v1.1.0 - 2026-03-05 - Server monitoring API endpoints — add path jail to disk-tree (P0 security)
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from core.api.db import get_db
from core.api.models import (
    AlertInfo,
    BanInfo,
    CandleDatapoint,
    ConnectivityStatus,
    ContainerMetrics,
    KGStatus,
    MetricDatapoint,
    MonitoringSnapshot,
    SecurityData,
    SecurityEvent,
    DiskTreeNode,
    DiskTreeResponse,
    SecuritySummary,
    ServiceStatus,
    SSHSummary,
    SystemMetrics,
    UserInfo,
)
from core.api.security import get_current_user, get_current_user_or_agent
from core.api.services.metrics_collector import VALID_METRICS, metrics_collector
from core.api.services.security_collector import security_collector

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/monitoring", tags=["monitoring"])

# Alert thresholds
CPU_WARN = 80.0
RAM_WARN = 85.0
DISK_WARN = 90.0

# History query limits per range
HISTORY_SECONDS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
BUCKET_SECONDS = {"24h": 900, "7d": 3600, "30d": 14400}  # 15min, 1h, 4h

# Rate limiting
_rate_limit_store: dict[str, list[float]] = {}
RATE_LIMIT_PER_MIN = 30
RATE_LIMIT_WINDOW = 60.0
_RATE_LIMIT_MAX_KEYS = 1000


def _check_rate_limit(username: str) -> None:
    """Simple in-memory rate limiter."""
    now = time.time()
    timestamps = _rate_limit_store.get(username, [])
    cutoff = now - RATE_LIMIT_WINDOW
    timestamps = [t for t in timestamps if t > cutoff]
    if len(timestamps) >= RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "Rate limit exceeded")
    timestamps.append(now)
    _rate_limit_store[username] = timestamps
    # Evict stale entries periodically
    if len(_rate_limit_store) > _RATE_LIMIT_MAX_KEYS:
        stale = [k for k, v in _rate_limit_store.items() if not v or (now - v[-1]) > RATE_LIMIT_WINDOW]
        for k in stale:
            del _rate_limit_store[k]


@router.get("/current", response_model=MonitoringSnapshot)
async def get_current(
    request: Request,
    user: UserInfo = Depends(get_current_user_or_agent),
) -> MonitoringSnapshot:
    """Current server snapshot + sparkline data."""
    _check_rate_limit(user.username)

    # Collect services + connectivity + KG status (Phase 4 — agent-native parity)
    services_data, connectivity_data, kg_data = await asyncio.gather(
        metrics_collector.collect_services(),
        metrics_collector.collect_connectivity(),
        metrics_collector.collect_kg_status(),
    )

    # Build system metrics from sparkline buffer (latest values)
    sparklines = metrics_collector.get_all_sparklines()
    system_dict: dict[str, float] = {}
    for metric, points in sparklines.items():
        if points:
            system_dict[metric] = points[-1]["v"]

    # Also include non-sparkline system metrics from last collect
    # Get docker from last collect cycle (stored in sparkline context)
    docker_data = []
    try:
        last_snapshot = await _get_latest_snapshot()
        if last_snapshot:
            system_dict.update(last_snapshot.get("system", {}))
            docker_data = last_snapshot.get("docker", [])
    except Exception:
        pass

    system = SystemMetrics(**{k: system_dict.get(k, 0.0) for k in SystemMetrics.model_fields})

    # Alerts
    alerts = _compute_alerts(system)

    # Security summary
    ssh_summary = await security_collector.get_ssh_summary_24h()
    bans = security_collector.read_fail2ban_bans()

    security_summary = SecuritySummary(
        ssh_success_24h=ssh_summary.get("success_count", 0),
        ssh_failed_24h=ssh_summary.get("failed_count", 0),
        bans_active=len(bans.get("active_bans", [])),
    )

    # Sparklines as typed datapoints
    typed_sparklines = {
        metric: [MetricDatapoint(t=p["t"], v=p["v"]) for p in points]
        for metric, points in sparklines.items()
    }

    return MonitoringSnapshot(
        timestamp=int(time.time()),
        system=system,
        docker=[ContainerMetrics(**c) for c in docker_data if isinstance(c, dict)],
        network=ConnectivityStatus(**connectivity_data),
        services=[ServiceStatus(**s) for s in services_data],
        alerts=alerts,
        sparklines=typed_sparklines,
        security_summary=security_summary,
        kg=KGStatus(**kg_data),
    )


@router.get("/history", response_model=list[CandleDatapoint])
async def get_history(
    metric: str = Query(..., pattern=r"^[a-z_]+$"),
    range_: str = Query("24h", alias="range", pattern=r"^(24h|7d|30d)$"),
    user: UserInfo = Depends(get_current_user_or_agent),
    db: aiosqlite.Connection = Depends(get_db),
) -> list[CandleDatapoint]:
    """Candle data bucketed by range: 24h=15min, 7d=1h, 30d=4h."""
    _check_rate_limit(user.username)

    if metric not in VALID_METRICS:
        raise HTTPException(400, f"Invalid metric: {metric}")

    bucket = BUCKET_SECONDS[range_]
    cutoff = int(time.time()) - HISTORY_SECONDS[range_]

    cursor = await db.execute(
        """
        WITH ranked AS (
            SELECT
                (timestamp / :bucket) * :bucket AS bucket_ts,
                open, high, low, close,
                ROW_NUMBER() OVER (
                    PARTITION BY (timestamp / :bucket) * :bucket
                    ORDER BY timestamp ASC
                ) AS rn_asc,
                ROW_NUMBER() OVER (
                    PARTITION BY (timestamp / :bucket) * :bucket
                    ORDER BY timestamp DESC
                ) AS rn_desc
            FROM monitoring_candles
            WHERE metric = :metric AND timestamp > :cutoff AND metadata = ''
        )
        SELECT
            bucket_ts,
            MAX(CASE WHEN rn_asc = 1 THEN open END),
            MAX(high),
            MIN(low),
            MAX(CASE WHEN rn_desc = 1 THEN close END)
        FROM ranked
        GROUP BY bucket_ts
        ORDER BY bucket_ts ASC
        """,
        {"metric": metric, "cutoff": cutoff, "bucket": bucket},
    )
    rows = await cursor.fetchall()

    return [
        CandleDatapoint(t=row[0], o=row[1], h=row[2], l=row[3], c=row[4])
        for row in rows
    ]


_disk_tree_cache: dict[str, DiskTreeResponse] = {}
_disk_tree_cache_ts: dict[str, float] = {}
_DISK_TREE_TTL = 300.0
_DISK_EXCLUDE = {"/proc", "/sys", "/dev", "/run", "/tmp"}

# Path jail for disk-tree endpoint — only workspace and project data dirs allowed
_DISK_TREE_ALLOWED_ROOTS: list[Path] = [
    Path.home() / "workspace",
    Path("/data/projects"),
]


def _build_disk_tree(root_path: str = "/") -> DiskTreeResponse:
    """Blocking: runs du -d2 {root_path}. Depth is relative to root_path."""
    root_path = root_path.rstrip("/") or "/"
    try:
        result = subprocess.run(
            ["du", "-d", "2", "--block-size=1M", root_path],
            capture_output=True, text=True, timeout=30,
        )
    except subprocess.TimeoutExpired:
        return DiskTreeResponse(items=[], total_mb=0, free_mb=0)

    root_depth = len([p for p in root_path.split("/") if p])
    items: list[DiskTreeNode] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        try:
            size_mb = int(parts[0])
        except ValueError:
            continue
        path = parts[1].rstrip("/")
        if not path or path == root_path:
            continue
        if size_mb < 10:
            continue
        if any(path == ex or path.startswith(ex + "/") for ex in _DISK_EXCLUDE):
            continue
        # Depth relative to root_path: direct children = 1, grandchildren = 2
        relative_depth = path.count("/") - root_depth
        name = path.rsplit("/", 1)[-1] or path
        items.append(DiskTreeNode(path=path, name=name, size_mb=size_mb, depth=relative_depth))

    items.sort(key=lambda x: x.size_mb, reverse=True)
    items = items[:40]

    usage = shutil.disk_usage(root_path)
    total_mb = int(usage.total / (1024 * 1024))
    free_mb = int(usage.free / (1024 * 1024))
    return DiskTreeResponse(items=items, total_mb=total_mb, free_mb=free_mb)


@router.get("/disk-tree", response_model=DiskTreeResponse)
async def get_disk_tree(
    user: UserInfo = Depends(get_current_user_or_agent),
    path: str = Query(default="/", description="Root path for disk usage scan"),
) -> DiskTreeResponse:
    """Disk usage by directory. Accepts ?path= for drill-down. Cached 5 minutes per path."""
    _check_rate_limit(user.username)
    # Resolve the requested path fully (expands ~, symlinks, .., .) to prevent traversal attacks
    resolved = Path(path.strip() or "/").expanduser().resolve()
    # Enforce allowlist: path must be equal to or a subdirectory of an allowed root
    allowed = any(
        resolved == allowed_root or resolved.is_relative_to(allowed_root)
        for allowed_root in _DISK_TREE_ALLOWED_ROOTS
    )
    if not allowed:
        allowed_display = ", ".join(str(r) for r in _DISK_TREE_ALLOWED_ROOTS)
        raise HTTPException(
            403,
            f"Path not allowed. Permitted roots: {allowed_display}",
        )
    root = str(resolved)
    now = time.time()
    if root in _disk_tree_cache and now - _disk_tree_cache_ts.get(root, 0) < _DISK_TREE_TTL:
        return _disk_tree_cache[root]
    result = await asyncio.to_thread(_build_disk_tree, root)
    _disk_tree_cache[root] = result
    _disk_tree_cache_ts[root] = now
    return result


@router.get("/security", response_model=SecurityData)
async def get_security(
    user: UserInfo = Depends(get_current_user),
) -> SecurityData:
    """Security data: SSH events, bans, console logins. Cookie-only auth."""
    _check_rate_limit(user.username)

    ssh_events = await security_collector.get_recent_events(limit=50)
    ssh_summary = await security_collector.get_ssh_summary_24h()
    bans_data = security_collector.read_fail2ban_bans()
    console_logins = await security_collector.get_recent_events(
        event_type="console_login", limit=20
    )

    return SecurityData(
        ssh_events=[SecurityEvent(**e) for e in ssh_events if e["event_type"] in ("ssh_login", "ssh_failed")],
        ssh_summary_24h=SSHSummary(**ssh_summary),
        active_bans=[BanInfo(**b) for b in bans_data.get("active_bans", [])],
        ban_count_24h=bans_data.get("total_24h", 0),
        console_logins=[SecurityEvent(**e) for e in console_logins],
    )


def _compute_alerts(system: SystemMetrics) -> list[AlertInfo]:
    """Compute alert badges from current values."""
    alerts = []
    if system.cpu_pct >= CPU_WARN:
        alerts.append(AlertInfo(metric="cpu_pct", value=system.cpu_pct, threshold=CPU_WARN))
    if system.ram_pct >= RAM_WARN:
        alerts.append(AlertInfo(metric="ram_pct", value=system.ram_pct, threshold=RAM_WARN))
    if system.disk_pct >= DISK_WARN:
        alerts.append(AlertInfo(metric="disk_pct", value=system.disk_pct, threshold=DISK_WARN))
    return alerts


async def _get_latest_snapshot() -> dict | None:
    """Get the latest full snapshot from the collector's last cycle.

    Uses the collector's internal state — no DB query needed.
    """
    # The collector stores last snapshot in sparkline buffers
    # For system metrics not in sparklines, we re-read from /proc (fast, virtual FS)
    try:
        system: dict[str, float] = {}
        system["cpu_pct"] = metrics_collector.collect_cpu()

        mem = metrics_collector.collect_memory()
        system.update(mem)

        disk = metrics_collector.collect_disk()
        system.update(disk)

        load = metrics_collector.collect_load()
        system.update(load)

        system["uptime_seconds"] = metrics_collector.collect_uptime()

        net = metrics_collector.collect_network()
        system.update(net)

        system["cpu_count"] = os.cpu_count() or 1

        docker = await metrics_collector.collect_docker()

        return {"system": system, "docker": docker}
    except Exception:
        logger.exception("Failed to get latest snapshot")
        return None
