# v1.0.0 - 2026-02-26 - Security events collector (auth.log + fail2ban)
from __future__ import annotations

import ipaddress as ipaddr_mod
import logging
import os
import re
import sqlite3
import time
from datetime import datetime

import aiosqlite

from core.api.config import settings

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES = {"ssh_login", "ssh_failed", "ban_add", "console_login"}


class SecurityCollector:
    """Auth.log parser with inode-based rotation tracking + fail2ban SQLite reader."""

    AUTH_LOG_PATH = "/var/log/auth.log"
    F2B_DB_PATH = "/var/lib/fail2ban/fail2ban.sqlite3"

    SYSLOG_PREFIX = (
        r"(?P<month>\w{3})\s+(?P<day>\d{1,2})\s+(?P<time>\d{2}:\d{2}:\d{2})"
        r"\s+\S+\s+sshd\[\d+\]:\s+"
    )
    RE_ACCEPTED = re.compile(
        SYSLOG_PREFIX
        + r"Accepted\s+(?P<method>publickey|password)\s+for\s+(?P<user>\S+)"
        + r"\s+from\s+(?P<ip>\S+)\s+port\s+\d+"
    )
    RE_FAILED = re.compile(
        SYSLOG_PREFIX
        + r"Failed\s+(?P<method>password|publickey)\s+for\s+(?:invalid\s+user\s+)?"
        + r"(?P<user>\S+)\s+from\s+(?P<ip>\S+)\s+port\s+\d+"
    )

    def __init__(self) -> None:
        self._log_inode: int | None = None
        self._log_offset: int = 0
        self._db: aiosqlite.Connection | None = None

    async def start(self) -> None:
        """Initialize. DB writes go through write_db() (single-writer pattern)."""
        self._db = None

    async def stop(self) -> None:
        """Cleanup."""
        pass

    def read_new_ssh_events(self) -> list[dict]:
        """Read new lines since last check. Handles logrotate via inode tracking."""
        if not os.access(self.AUTH_LOG_PATH, os.R_OK):
            return []

        try:
            current_inode = os.stat(self.AUTH_LOG_PATH).st_ino
        except OSError:
            return []

        lines: list[str] = []

        # Detect log rotation: inode changed
        if self._log_inode is not None and current_inode != self._log_inode:
            rotated = self.AUTH_LOG_PATH + ".1"
            if os.path.exists(rotated):
                try:
                    if os.stat(rotated).st_ino == self._log_inode:
                        with open(rotated) as f:
                            f.seek(self._log_offset)
                            lines.extend(f.readlines())
                except OSError:
                    pass
            self._log_offset = 0

        # First run: skip to EOF
        if self._log_inode is None:
            self._log_inode = current_inode
            try:
                self._log_offset = os.stat(self.AUTH_LOG_PATH).st_size
            except OSError:
                self._log_offset = 0
            return []

        self._log_inode = current_inode

        try:
            with open(self.AUTH_LOG_PATH) as f:
                file_size = os.fstat(f.fileno()).st_size
                if file_size < self._log_offset:
                    self._log_offset = 0  # file truncated
                f.seek(self._log_offset)
                lines.extend(f.readlines())
                self._log_offset = f.tell()
        except OSError:
            return []

        events = []
        for line in lines:
            event = self._parse_ssh_line(line.strip())
            if event and event["event_type"] in VALID_EVENT_TYPES:
                events.append(event)
        return events

    def _parse_ssh_line(self, line: str) -> dict | None:
        """Parse a single auth.log line for SSH events."""
        m = self.RE_ACCEPTED.search(line)
        if m:
            return {
                "event_type": "ssh_login",
                "source_ip": m.group("ip"),
                "username": m.group("user"),
                "details": {"method": m.group("method")},
                "timestamp": self._parse_syslog_timestamp(
                    m.group("month"), m.group("day"), m.group("time")
                ),
            }

        m = self.RE_FAILED.search(line)
        if m:
            return {
                "event_type": "ssh_failed",
                "source_ip": m.group("ip"),
                "username": m.group("user"),
                "details": {"method": m.group("method")},
                "timestamp": self._parse_syslog_timestamp(
                    m.group("month"), m.group("day"), m.group("time")
                ),
            }

        return None

    @staticmethod
    def _parse_syslog_timestamp(month: str, day: str, time_str: str) -> int:
        """Parse syslog timestamp to Unix epoch (assumes current year)."""
        try:
            year = datetime.now().year
            dt = datetime.strptime(f"{year} {month} {day} {time_str}", "%Y %b %d %H:%M:%S")
            return int(dt.timestamp())
        except ValueError:
            return int(time.time())

    def read_fail2ban_bans(self) -> dict:
        """Direct read of fail2ban SQLite DB (read-only)."""
        if not os.access(self.F2B_DB_PATH, os.R_OK):
            return {"active_bans": [], "total_24h": 0}

        try:
            uri = f"file:{self.F2B_DB_PATH}?mode=ro"
            with sqlite3.connect(uri, uri=True, timeout=5) as conn:
                now = int(time.time())
                active = conn.execute(
                    "SELECT ip, jail, timeofban, bantime, bancount FROM bips "
                    "WHERE bantime = -1 OR timeofban + bantime > ?",
                    (now,),
                ).fetchall()
                total_24h = conn.execute(
                    "SELECT COUNT(*) FROM bans WHERE timeofban > ?",
                    (now - 86400,),
                ).fetchone()[0]

            return {
                "active_bans": [
                    {
                        "ip": self._mask_ip(r[0]),
                        "jail": r[1],
                        "timestamp": r[2],
                    }
                    for r in active
                ],
                "total_24h": total_24h,
            }
        except Exception:
            logger.exception("fail2ban read failed")
            return {"active_bans": [], "total_24h": 0}

    @staticmethod
    def _mask_ip(ip: str) -> str:
        """Mask IP for API responses. IPv4: /24, IPv6: /48."""
        try:
            addr = ipaddr_mod.ip_address(ip)
            if isinstance(addr, ipaddr_mod.IPv4Address):
                network = ipaddr_mod.IPv4Network(f"{ip}/24", strict=False)
                return str(network.network_address) + "/24"
            else:
                network = ipaddr_mod.IPv6Network(f"{ip}/48", strict=False)
                return str(network.network_address) + "/48"
        except ValueError:
            return "invalid"

    async def save_events_to_db(self, events: list[dict]) -> None:
        """Save security events to monitoring_events table."""
        if not events:
            return
        import json

        rows = [
            (
                e.get("timestamp", int(time.time())),
                e["event_type"],
                self._mask_ip(e.get("source_ip", "")),
                e.get("username"),
                json.dumps(e.get("details")) if e.get("details") else None,
            )
            for e in events
            if e["event_type"] in VALID_EVENT_TYPES
        ]
        if rows:
            from core.api.db import write_db
            async with write_db() as db:
                await db.executemany(
                    "INSERT INTO monitoring_events (timestamp, event_type, source_ip, username, details) "
                    "VALUES (?, ?, ?, ?, ?)",
                    rows,
                )

    async def get_ssh_summary_24h(self) -> dict:
        """Get SSH event summary for last 24 hours."""
        from core.api.db import acquire_db
        async with acquire_db() as db:
            cutoff = int(time.time()) - 86400
            cursor = await db.execute(
                "SELECT event_type, COUNT(*) FROM monitoring_events "
                "WHERE timestamp > ? AND event_type IN ('ssh_login', 'ssh_failed') "
                "GROUP BY event_type",
                (cutoff,),
            )
            rows = await cursor.fetchall()
            counts = {r[0]: r[1] for r in rows}

            cursor2 = await db.execute(
                "SELECT COUNT(DISTINCT source_ip) FROM monitoring_events "
                "WHERE timestamp > ? AND event_type IN ('ssh_login', 'ssh_failed')",
                (cutoff,),
        )
        unique = (await cursor2.fetchone())[0]

        return {
            "success_count": counts.get("ssh_login", 0),
            "failed_count": counts.get("ssh_failed", 0),
            "unique_ips": unique,
        }

    async def get_recent_events(self, event_type: str | None = None, limit: int = 20) -> list[dict]:
        """Get recent security events."""
        from core.api.db import acquire_db
        import json

        async with acquire_db() as db:
            if event_type:
                cursor = await db.execute(
                    "SELECT timestamp, event_type, source_ip, username, details "
                    "FROM monitoring_events WHERE event_type = ? "
                    "ORDER BY timestamp DESC LIMIT ?",
                    (event_type, limit),
                )
            else:
                cursor = await db.execute(
                    "SELECT timestamp, event_type, source_ip, username, details "
                    "FROM monitoring_events ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                )
        rows = await cursor.fetchall()
        return [
            {
                "timestamp": r[0],
                "event_type": r[1],
                "source_ip": r[2],
                "username": r[3],
                "details": json.loads(r[4]) if r[4] else None,
            }
            for r in rows
        ]


# Singleton
security_collector = SecurityCollector()
