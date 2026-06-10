# v1.1.0 - 2026-03-18 - Offload Docker JSON parsing to thread + separate interval
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import time
from collections import deque
from pathlib import Path

import aiohttp
import aiosqlite

from core.api.config import settings

logger = logging.getLogger(__name__)

VALID_METRICS = {
    "cpu_pct", "ram_pct", "disk_pct",
    "load_1m", "load_5m", "load_15m",
    "net_rx_bps", "net_tx_bps",
    "uptime_seconds",
}
VALID_DOCKER_METRICS = {"cpu_pct", "memory_mb", "memory_pct"}


class MetricsCollector:
    """Stateful metrics collector with cross-cycle deltas and sparkline buffers.

    Lifecycle: call start() in lifespan startup, stop() in lifespan shutdown.
    """

    def __init__(self) -> None:
        self._prev_cpu_stats: tuple[int, int] | None = None
        self._prev_net_counters: dict[str, tuple[int, int]] | None = None
        self._sparkline_buffer: dict[str, deque[tuple[int, float]]] = {}
        self._max_sparkline_points = 30
        self._db: aiosqlite.Connection | None = None
        self._docker_session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        """Initialize persistent connections."""
        # DB writes now go through write_db() (single-writer pattern).
        # self._db kept for backward-compat reads only.
        self._db = None
        # Docker socket — optional, may not exist
        import os as _os
        if _os.access("/var/run/docker.sock", _os.R_OK):
            try:
                connector = aiohttp.UnixConnector(path="/var/run/docker.sock")
                self._docker_session = aiohttp.ClientSession(connector=connector)
            except Exception:
                logger.warning("Docker socket not available, container metrics disabled")
                self._docker_session = None
        else:
            logger.info("Docker socket not readable, container metrics disabled")
            self._docker_session = None

    async def stop(self) -> None:
        """Close persistent connections."""
        if self._docker_session:
            await self._docker_session.close()
            self._docker_session = None

    # --- CPU: cross-cycle delta ---

    def _read_proc_stat(self) -> tuple[int, int]:
        """Read /proc/stat. Returns (idle, total) cpu ticks."""
        text = Path("/proc/stat").read_text()
        first_line = text.split("\n")[0]
        parts = first_line.split()[1:]
        values = [int(v) for v in parts]
        idle = values[3] + values[4]  # idle + iowait
        total = sum(values)
        return (idle, total)

    def collect_cpu(self) -> float:
        """CPU % via cross-cycle delta. First call returns 0."""
        idle, total = self._read_proc_stat()
        if self._prev_cpu_stats is None:
            self._prev_cpu_stats = (idle, total)
            return 0.0
        prev_idle, prev_total = self._prev_cpu_stats
        self._prev_cpu_stats = (idle, total)
        delta_idle = idle - prev_idle
        delta_total = total - prev_total
        if delta_total == 0:
            return 0.0
        return round((1.0 - delta_idle / delta_total) * 100.0, 1)

    # --- Memory ---

    def collect_memory(self) -> dict[str, float]:
        """Memory from /proc/meminfo using MemAvailable."""
        text = Path("/proc/meminfo").read_text()
        info: dict[str, int] = {}
        for line in text.strip().split("\n"):
            parts = line.split()
            info[parts[0].rstrip(":")] = int(parts[1])
        total = info["MemTotal"]
        available = info["MemAvailable"]
        used = total - available
        return {
            "ram_pct": round(used / total * 100, 1),
            "ram_used_mb": round(used / 1024, 0),
            "ram_total_mb": round(total / 1024, 0),
        }

    # --- Disk ---

    def collect_disk(self) -> dict[str, float]:
        """Disk usage for root filesystem."""
        import shutil
        usage = shutil.disk_usage("/")
        return {
            "disk_pct": round(usage.used / usage.total * 100, 1),
            "disk_used_gb": round(usage.used / (1024**3), 1),
            "disk_total_gb": round(usage.total / (1024**3), 1),
        }

    # --- Load average ---

    def collect_load(self) -> dict[str, float]:
        """Load average from os.getloadavg()."""
        load = os.getloadavg()
        return {
            "load_1m": round(load[0], 2),
            "load_5m": round(load[1], 2),
            "load_15m": round(load[2], 2),
        }

    # --- Uptime ---

    def collect_uptime(self) -> float:
        """Server uptime in seconds."""
        return float(Path("/proc/uptime").read_text().split()[0])

    # --- Network: cross-cycle delta ---

    def _read_proc_net_dev(self) -> dict[str, tuple[int, int]]:
        """Parse /proc/net/dev. Returns {iface: (rx_bytes, tx_bytes)}."""
        text = Path("/proc/net/dev").read_text()
        result: dict[str, tuple[int, int]] = {}
        for line in text.strip().split("\n")[2:]:
            parts = line.split()
            iface = parts[0].rstrip(":")
            if iface == "lo":
                continue
            result[iface] = (int(parts[1]), int(parts[9]))
        return result

    def collect_network(self) -> dict[str, float]:
        """Network bandwidth as bytes/sec (delta between cycles)."""
        current = self._read_proc_net_dev()
        if self._prev_net_counters is None:
            self._prev_net_counters = current
            return {"net_rx_bps": 0.0, "net_tx_bps": 0.0}
        rx_delta = sum(
            max(0, current.get(iface, (0, 0))[0] - self._prev_net_counters.get(iface, (0, 0))[0])
            for iface in current
        )
        tx_delta = sum(
            max(0, current.get(iface, (0, 0))[1] - self._prev_net_counters.get(iface, (0, 0))[1])
            for iface in current
        )
        self._prev_net_counters = current
        interval = settings.monitoring_metrics_interval
        return {
            "net_rx_bps": round(rx_delta / interval, 1),
            "net_tx_bps": round(tx_delta / interval, 1),
        }

    # --- Docker: Engine API via Unix socket ---

    async def collect_docker(self) -> list[dict]:
        """Docker stats via Engine API. Parallel per-container."""
        if not self._docker_session:
            return []
        try:
            async with self._docker_session.get(
                "http://localhost/containers/json?all=true",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    return []
                body = await resp.read()
                containers = await asyncio.to_thread(json.loads, body)

            async def _get_stats(c: dict) -> dict | None:
                cid = c["Id"][:12]
                name = c["Names"][0].lstrip("/") if c.get("Names") else cid
                if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$", name):
                    return None
                state = c.get("State", "unknown")
                if state != "running":
                    return {
                        "name": name, "status": state,
                        "cpu_pct": 0.0, "memory_mb": 0.0,
                        "memory_limit_mb": 0.0, "memory_pct": 0.0,
                        "restart_count": c.get("RestartCount", 0),
                        "uptime_seconds": 0.0,
                    }
                try:
                    async with self._docker_session.get(
                        f"http://localhost/containers/{cid}/stats?stream=false",
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as stats_resp:
                        if stats_resp.status != 200:
                            return None
                        body = await stats_resp.read()
                        stats = await asyncio.to_thread(json.loads, body)
                    return self._parse_container_stats(name, state, stats, c)
                except Exception:
                    return None

            results = await asyncio.gather(
                *[_get_stats(c) for c in containers],
                return_exceptions=True,
            )
            return [r for r in results if isinstance(r, dict)]
        except Exception:
            logger.exception("Docker collection failed")
            return []

    def _parse_container_stats(self, name: str, state: str, stats: dict, container: dict) -> dict:
        """Parse Docker stats API response into flat dict."""
        cpu_delta = stats.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) - \
                    stats.get("precpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0)
        system_delta = stats.get("cpu_stats", {}).get("system_cpu_usage", 0) - \
                       stats.get("precpu_stats", {}).get("system_cpu_usage", 0)
        num_cpus = stats.get("cpu_stats", {}).get("online_cpus", 1) or 1
        cpu_pct = 0.0
        if system_delta > 0:
            cpu_pct = round((cpu_delta / system_delta) * num_cpus * 100.0, 1)

        mem_usage = stats.get("memory_stats", {}).get("usage", 0)
        mem_limit = stats.get("memory_stats", {}).get("limit", 1)
        mem_cache = stats.get("memory_stats", {}).get("stats", {}).get("cache", 0)
        mem_actual = mem_usage - mem_cache

        # Uptime from container start time
        created_str = container.get("Created", 0)
        uptime = 0.0
        if isinstance(created_str, (int, float)):
            uptime = time.time() - created_str

        return {
            "name": name,
            "status": state,
            "cpu_pct": cpu_pct,
            "memory_mb": round(mem_actual / (1024 * 1024), 1),
            "memory_limit_mb": round(mem_limit / (1024 * 1024), 1),
            "memory_pct": round(mem_actual / mem_limit * 100, 1) if mem_limit > 0 else 0.0,
            "restart_count": container.get("RestartCount", 0),
            "uptime_seconds": uptime,
        }

    # --- Services ---

    async def collect_services(self) -> list[dict]:
        """Collect service statuses (tmux sessions, systemd services)."""
        services = []

        # Marvis API — self (always running if this code executes)
        services.append({"name": "Marvis API", "status": "running", "details": None})

        # tmux sessions
        try:
            proc = await asyncio.create_subprocess_exec(
                "tmux", "list-sessions", "-F", "#{session_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                session_names = [s.strip() for s in stdout.decode().strip().split("\n") if s.strip()]
                services.append({
                    "name": "tmux",
                    "status": "running",
                    "details": f"{len(session_names)} sessions: {', '.join(session_names[:5])}",
                })
            else:
                services.append({"name": "tmux", "status": "stopped", "details": None})
        except Exception:
            services.append({"name": "tmux", "status": "unknown", "details": None})

        # Tailscale
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                import json
                ts_data = json.loads(stdout.decode())
                ts_ip = ts_data.get("Self", {}).get("TailscaleIPs", [""])[0]
                services.append({
                    "name": "Tailscale",
                    "status": "running",
                    "details": ts_ip or None,
                })
            else:
                services.append({"name": "Tailscale", "status": "stopped", "details": None})
        except Exception:
            services.append({"name": "Tailscale", "status": "unknown", "details": None})

        # KG Phase 4: pir-kg-watcher (user systemd unit). Doppia exposure
        # (services list + kg field) per backward compat + agent-native parity.
        kg_status = await self._systemctl_user_is_active("pir-kg-watcher.service")
        services.append({
            "name": "pir-kg-watcher",
            "status": kg_status,
            "details": None,
        })

        return services

    async def _systemctl_user_is_active(self, unit: str) -> str:
        """Returns one of: 'running', 'stopped', 'failed', 'unknown'.

        Uses `systemctl --user is-active <unit>` which exits 0 if active and
        prints the active state on stdout. Wraps timeout + exception so the
        full collect_services() never blocks on a stuck systemd query.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "systemctl", "--user", "is-active", unit,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
            text = stdout.decode().strip()
            if text == "active":
                return "running"
            if text == "inactive":
                return "stopped"
            if text == "failed":
                return "failed"
            return "unknown"
        except Exception:
            return "unknown"

    async def collect_kg_status(self) -> dict:
        """Phase 4: read kg_watcher_state row + watcher unit status.

        Returns a dict matching `KGStatus` shape (caller wraps in pydantic).
        Reads from console.db read-only pool (no write contention with the
        watcher daemon).
        """
        import json

        # 1. Liveness via systemctl
        unit_status = await self._systemctl_user_is_active("pir-kg-watcher.service")
        watcher_status_map = {
            "running": "active",
            "stopped": "inactive",
            "failed": "failed",
            "unknown": "unknown",
        }
        result: dict = {
            "watcher_status": watcher_status_map.get(unit_status, "unknown"),
            "last_flush_at": None,
            "recent_skipped": [],
            "recent_flushes": [],
            "total_flushes": 0,
            "total_files_processed": 0,
        }

        # 2. Rich data from kg_watcher_state. Use the read-only pool — the
        # daemon writes here from a separate process (BEGIN IMMEDIATE), and
        # the pool has PRAGMA query_only=ON so we never collide.
        # NB: `acquire_db` (standalone context manager) NOT `get_db` (FastAPI
        # Depends) — collect_kg_status runs from a router but as a plain
        # awaitable, not as a Depends-injected handler.
        from core.api.db import acquire_db

        try:
            async with acquire_db() as db:
                cursor = await db.execute(
                    "SELECT last_flush_at, recent_skipped, recent_flushes, "
                    "total_flushes, total_files_processed "
                    "FROM kg_watcher_state WHERE id = 1"
                )
                row = await cursor.fetchone()
                if row is not None:
                    last_flush, rs_json, rf_json, total_flushes, total_files = row
                    result["last_flush_at"] = last_flush
                    try:
                        result["recent_skipped"] = json.loads(rs_json or "[]")
                    except (TypeError, ValueError):
                        result["recent_skipped"] = []
                    try:
                        result["recent_flushes"] = json.loads(rf_json or "[]")
                    except (TypeError, ValueError):
                        result["recent_flushes"] = []
                    result["total_flushes"] = int(total_flushes or 0)
                    result["total_files_processed"] = int(total_files or 0)
        except Exception as e:
            logger.warning("collect_kg_status read failed: %s", e)

        return result

    # --- Connectivity ---

    async def collect_connectivity(self) -> dict:
        """Collect connectivity status."""
        result = {"tailscale": "unknown", "tailscale_ip": None, "cf_tunnel": "unknown"}

        # Check Tailscale
        try:
            proc = await asyncio.create_subprocess_exec(
                "tailscale", "status", "--json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0:
                import json
                ts_data = json.loads(stdout.decode())
                ips = ts_data.get("Self", {}).get("TailscaleIPs", [])
                result["tailscale"] = "connected"
                result["tailscale_ip"] = ips[0] if ips else None
            else:
                result["tailscale"] = "disconnected"
        except Exception:
            pass

        # Check Cloudflare Tunnel (via systemd or docker)
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker", "ps", "--filter", "name=cf-tunnel", "--format", "{{.Status}}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            if proc.returncode == 0 and stdout.decode().strip():
                result["cf_tunnel"] = "active"
            else:
                result["cf_tunnel"] = "inactive"
        except Exception:
            pass

        return result

    # --- Sparkline buffer ---

    def _update_sparkline(self, metric: str, ts: int, value: float) -> None:
        if metric not in self._sparkline_buffer:
            self._sparkline_buffer[metric] = deque(maxlen=self._max_sparkline_points)
        self._sparkline_buffer[metric].append((ts, value))

    def get_sparkline_data(self, metric: str) -> list[dict]:
        """Return last 30 datapoints for sparkline chart."""
        return [{"t": t, "v": v} for t, v in self._sparkline_buffer.get(metric, [])]

    def get_all_sparklines(self) -> dict[str, list[dict]]:
        """Return all sparkline data keyed by metric name."""
        return {
            metric: [{"t": t, "v": v} for t, v in points]
            for metric, points in self._sparkline_buffer.items()
        }

    # --- Main collection cycle ---

    async def collect_all(self, include_docker: bool = True) -> dict:
        """Collect all metrics. Returns snapshot dict."""
        ts = int(time.time())

        # Sync reads wrapped in to_thread
        cpu, memory, disk, load, uptime, network = await asyncio.gather(
            asyncio.to_thread(self.collect_cpu),
            asyncio.to_thread(self.collect_memory),
            asyncio.to_thread(self.collect_disk),
            asyncio.to_thread(self.collect_load),
            asyncio.to_thread(self.collect_uptime),
            asyncio.to_thread(self.collect_network),
            return_exceptions=True,
        )

        # Docker stats — optional (expensive: N HTTP requests + JSON parsing)
        docker = await self.collect_docker() if include_docker else []

        # Build snapshot
        system = {}
        if isinstance(cpu, float):
            system["cpu_pct"] = cpu
            self._update_sparkline("cpu_pct", ts, cpu)
        if isinstance(memory, dict):
            system.update(memory)
            self._update_sparkline("ram_pct", ts, memory["ram_pct"])
        if isinstance(disk, dict):
            system.update(disk)
            self._update_sparkline("disk_pct", ts, disk["disk_pct"])
        if isinstance(load, dict):
            system.update(load)
        if isinstance(uptime, float):
            system["uptime_seconds"] = uptime
        if isinstance(network, dict):
            system.update(network)
            self._update_sparkline("net_rx_bps", ts, network["net_rx_bps"])
            self._update_sparkline("net_tx_bps", ts, network["net_tx_bps"])

        system["cpu_count"] = os.cpu_count() or 1

        return {
            "timestamp": ts,
            "system": system,
            "docker": docker if isinstance(docker, list) else [],
            "network": network if isinstance(network, dict) else {},
        }

    async def save_to_db(self, metrics: dict) -> None:
        """Batch INSERT via dedicated writer (single-writer pattern)."""
        from core.api.db import write_db
        ts = metrics.get("timestamp", int(time.time()))
        rows: list[tuple] = []

        system = metrics.get("system", {})
        for key in ("cpu_pct", "ram_pct", "disk_pct", "load_1m", "load_5m", "load_15m",
                     "net_rx_bps", "net_tx_bps", "uptime_seconds"):
            if key in system and key in VALID_METRICS:
                rows.append((ts, key, system[key], ""))

        # Docker per-container metrics
        for container in metrics.get("docker", []):
            name = container.get("name", "")
            if container.get("status") == "running":
                for key in ("cpu_pct", "memory_mb", "memory_pct"):
                    val = container.get(key)
                    if val is not None:
                        rows.append((ts, f"docker_{key}", val, name))

        if rows:
            async with write_db(label="metrics_collector.save_to_db") as db:
                await db.executemany(
                    "INSERT INTO monitoring_metrics (timestamp, metric, value, metadata) VALUES (?, ?, ?, ?)",
                    rows,
                )

    # --- Aggregation ---

    async def aggregate_to_candles(self) -> int:
        """Aggregate raw metrics into 1-min candles via dedicated writer."""
        from core.api.db import write_db
        async with write_db(label="metrics_collector.aggregate_to_candles") as db:
            cursor = await db.execute("""
                WITH ranked AS (
                    SELECT
                        (timestamp / 60) * 60 AS minute_ts,
                        metric,
                        metadata,
                        value,
                        ROW_NUMBER() OVER (
                            PARTITION BY metric, (timestamp / 60) * 60, metadata
                            ORDER BY timestamp ASC
                        ) AS rn_asc,
                        ROW_NUMBER() OVER (
                            PARTITION BY metric, (timestamp / 60) * 60, metadata
                            ORDER BY timestamp DESC
                        ) AS rn_desc
                    FROM monitoring_metrics
                    WHERE timestamp > COALESCE(
                        (SELECT MAX(timestamp) FROM monitoring_candles), 0
                    )
                )
                INSERT OR IGNORE INTO monitoring_candles (timestamp, metric, open, high, low, close, metadata)
                SELECT
                    minute_ts,
                    metric,
                    MAX(CASE WHEN rn_asc = 1 THEN value END),
                    MAX(value),
                    MIN(value),
                    MAX(CASE WHEN rn_desc = 1 THEN value END),
                    metadata
                FROM ranked
                GROUP BY metric, minute_ts, metadata
            """)
            return cursor.rowcount

    # --- Cleanup ---

    async def cleanup_old_raw(self, hours: int = 24, batch_size: int = 5000) -> int:
        """Batched delete of old raw metrics via dedicated writer."""
        from core.api.db import write_db
        cutoff = int(time.time()) - (hours * 3600)
        total = 0
        while True:
            async with write_db(label="metrics_collector.cleanup_old_raw") as db:
                cursor = await db.execute(
                    "DELETE FROM monitoring_metrics WHERE rowid IN "
                    "(SELECT rowid FROM monitoring_metrics WHERE timestamp < ? LIMIT ?)",
                    (cutoff, batch_size),
                )
                deleted = cursor.rowcount
            total += deleted
            if deleted < batch_size:
                break
            await asyncio.sleep(0.1)
        return total

    async def cleanup_old_candles(self, days: int = 30, batch_size: int = 5000) -> int:
        """Batched delete of old candle data via dedicated writer."""
        from core.api.db import write_db
        cutoff = int(time.time()) - (days * 86400)
        total = 0
        while True:
            async with write_db(label="metrics_collector.cleanup_old_candles") as db:
                cursor = await db.execute(
                    "DELETE FROM monitoring_candles WHERE rowid IN "
                    "(SELECT rowid FROM monitoring_candles WHERE timestamp < ? LIMIT ?)",
                    (cutoff, batch_size),
                )
                deleted = cursor.rowcount
            total += deleted
            if deleted < batch_size:
                break
            await asyncio.sleep(0.1)
        return total

    async def cleanup_old_events(self, days: int = 30, batch_size: int = 5000) -> int:
        """Batched delete of old security events via dedicated writer."""
        from core.api.db import write_db
        cutoff = int(time.time()) - (days * 86400)
        total = 0
        while True:
            async with write_db(label="metrics_collector.cleanup_old_events") as db:
                cursor = await db.execute(
                    "DELETE FROM monitoring_events WHERE rowid IN "
                    "(SELECT rowid FROM monitoring_events WHERE timestamp < ? LIMIT ?)",
                    (cutoff, batch_size),
                )
                deleted = cursor.rowcount
            total += deleted
            if deleted < batch_size:
                break
            await asyncio.sleep(0.1)
        return total


# Singleton
metrics_collector = MetricsCollector()
