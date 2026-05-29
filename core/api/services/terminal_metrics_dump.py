from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
import httpx

from core.api.services.terminal_metrics import TerminalMetricsCollector


logger = logging.getLogger(__name__)
DEFAULT_OUTPUT_DIR = Path(
    os.environ.get("TERMINAL_METRICS_OUTPUT_DIR", "/data/projects/marvisx/output")
)
JSONL_PREFIX = "terminal-metrics"
JSONL_RETENTION_DAYS = 30
EVENT_LOOP_LAG_INTERVAL_SECONDS = 1.0
NETWORK_PROBE_INTERVAL_SECONDS = 60.0
DUMP_INTERVAL_SECONDS = 300.0
INTERNET_PROBE_TARGETS = (
    {
        "name": "cloudflare_trace",
        "url": "https://www.cloudflare.com/cdn-cgi/trace",
    },
)


def terminal_metrics_path(
    *,
    now: datetime | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    current = now or datetime.now(timezone.utc)
    return output_dir / f"{JSONL_PREFIX}-{current:%Y-%m-%d}.jsonl"


async def append_terminal_metrics_record(
    record: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = terminal_metrics_path(output_dir=output_dir)
    payload = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **record,
    }
    async with aiofiles.open(path, "a", encoding="utf-8") as handle:
        await handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return path


async def append_terminal_client_events(
    payload: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    return await append_terminal_metrics_record(
        {
            "kind": "client_batch",
            "source": "console",
            "payload": payload,
        },
        output_dir=output_dir,
    )


def cleanup_terminal_metrics_files(
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    retention_days: int = JSONL_RETENTION_DAYS,
) -> int:
    if not output_dir.exists():
        return 0
    cutoff = time.time() - retention_days * 86400
    removed = 0
    for path in output_dir.glob(f"{JSONL_PREFIX}-*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


async def run_internet_probe(
    *,
    target: dict[str, str] | None = None,
    timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    probe_target = target or INTERNET_PROBE_TARGETS[0]
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=timeout_seconds,
            headers={"User-Agent": "MarvisX-terminal-metrics/1.0"},
        ) as client:
            response = await client.get(probe_target["url"])
        duration_ms = (time.perf_counter() - started) * 1000
        return {
            "target": probe_target["name"],
            "url": probe_target["url"],
            "ok": response.is_success,
            "status_code": response.status_code,
            "duration_ms": duration_ms,
            "bytes_received": len(response.content),
            "error": None,
        }
    except Exception as exc:
        duration_ms = (time.perf_counter() - started) * 1000
        return {
            "target": probe_target["name"],
            "url": probe_target["url"],
            "ok": False,
            "status_code": None,
            "duration_ms": duration_ms,
            "bytes_received": 0,
            "error": str(exc),
        }


async def sample_internet_probe(collector: TerminalMetricsCollector) -> dict[str, Any]:
    probe = await run_internet_probe()
    collector.record_internet_probe(
        target=probe["target"],
        ok=bool(probe["ok"]),
        duration_ms=float(probe["duration_ms"]),
        status_code=probe["status_code"],
        bytes_received=int(probe["bytes_received"]),
        error=probe["error"],
    )
    return probe


async def terminal_metrics_background_loop(
    collector: TerminalMetricsCollector,
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    dump_interval_seconds: float = DUMP_INTERVAL_SECONDS,
    network_interval_seconds: float = NETWORK_PROBE_INTERVAL_SECONDS,
    lag_interval_seconds: float = EVENT_LOOP_LAG_INTERVAL_SECONDS,
) -> None:
    cleanup_terminal_metrics_files(output_dir=output_dir)
    next_dump_at = time.monotonic() + dump_interval_seconds
    next_probe_at = time.monotonic()

    while True:
        expected_wake = time.monotonic() + lag_interval_seconds
        try:
            await asyncio.sleep(lag_interval_seconds)
        except asyncio.CancelledError:
            try:
                await append_terminal_metrics_record(
                    {
                        "kind": "server_snapshot",
                        "reason": "shutdown",
                        "snapshot": collector.snapshot(),
                    },
                    output_dir=output_dir,
                )
            except Exception:
                logger.warning("terminal metrics shutdown snapshot failed", exc_info=True)
            raise

        now = time.monotonic()
        collector.record_event_loop_lag(max(0.0, (now - expected_wake) * 1000))

        if now >= next_probe_at:
            try:
                probe = await sample_internet_probe(collector)
                await append_terminal_metrics_record(
                    {
                        "kind": "internet_probe",
                        "probe": probe,
                    },
                    output_dir=output_dir,
                )
            except Exception:
                logger.warning("terminal metrics internet probe failed", exc_info=True)
            next_probe_at = now + network_interval_seconds

        if now >= next_dump_at:
            try:
                await append_terminal_metrics_record(
                    {
                        "kind": "server_snapshot",
                        "reason": "periodic",
                        "snapshot": collector.snapshot(),
                    },
                    output_dir=output_dir,
                )
            except Exception:
                logger.warning("terminal metrics periodic dump failed", exc_info=True)
            next_dump_at = now + dump_interval_seconds
