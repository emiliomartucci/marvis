from __future__ import annotations

import os
import resource
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any


ROLLING_WINDOW_SECONDS = 60.0
MAX_SAMPLES_PER_SERIES = 6_000


@dataclass(frozen=True)
class TimedValue:
    ts: float
    value: float
    metadata: dict[str, Any] = field(default_factory=dict)


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return ordered[index]


def _summary(values: list[float]) -> dict[str, float | int | None]:
    return {
        "count": len(values),
        "p50": _percentile(values, 0.50),
        "p95": _percentile(values, 0.95),
        "p99": _percentile(values, 0.99),
        "max": max(values) if values else None,
    }


class TerminalMetricsCollector:
    """In-memory terminal telemetry for the Phase 1 stress test.

    This collector is intentionally process-local and read-only from HTTP. It
    gives the Console dashboard enough signal for the Phase 1 gate without
    adding a JSONL writer or a client POST ingestion path.
    """

    def __init__(self, window_seconds: float = ROLLING_WINDOW_SECONDS):
        self.window_seconds = window_seconds
        self._lock = Lock()
        self._session_samples: dict[str, dict[str, deque[TimedValue]]] = defaultdict(
            lambda: {
                "pty_read_bytes": deque(maxlen=MAX_SAMPLES_PER_SERIES),
                "pty_write_duration_ms": deque(maxlen=MAX_SAMPLES_PER_SERIES),
                "fanout_duration_ms": deque(maxlen=MAX_SAMPLES_PER_SERIES),
                "websocket_ping_rtt_ms": deque(maxlen=MAX_SAMPLES_PER_SERIES),
            }
        )
        self._global_samples: dict[str, deque[TimedValue]] = defaultdict(
            lambda: deque(maxlen=MAX_SAMPLES_PER_SERIES)
        )
        self._websocket_sessions: dict[str, int] = defaultdict(int)
        self._live_pty_reader_count = 0
        self._client_event_batches = 0
        self._client_event_count = 0
        self._last_client_batch_at: float | None = None
        self._internet_probes: dict[str, dict[str, Any]] = {}

    def websocket_connected(self, session_name: str) -> None:
        with self._lock:
            self._websocket_sessions[session_name] += 1

    def websocket_disconnected(self, session_name: str) -> None:
        with self._lock:
            current = self._websocket_sessions.get(session_name, 0)
            if current <= 1:
                self._websocket_sessions.pop(session_name, None)
                return
            self._websocket_sessions[session_name] = current - 1

    def pty_reader_started(self) -> None:
        with self._lock:
            self._live_pty_reader_count += 1

    def pty_reader_stopped(self) -> None:
        with self._lock:
            self._live_pty_reader_count = max(0, self._live_pty_reader_count - 1)

    def record_pty_read_bytes(self, session_name: str, byte_count: int) -> None:
        self._append(session_name, "pty_read_bytes", float(byte_count))

    def record_pty_write_duration(self, session_name: str, duration_ms: float) -> None:
        self._append(session_name, "pty_write_duration_ms", duration_ms)

    def record_fanout_duration(
        self,
        session_name: str,
        duration_ms: float,
        *,
        connection_count: int,
    ) -> None:
        self._append(
            session_name,
            "fanout_duration_ms",
            duration_ms,
            metadata={"connection_count": connection_count},
        )

    def record_websocket_ping_rtt(self, session_name: str, duration_ms: float) -> None:
        self._append(session_name, "websocket_ping_rtt_ms", duration_ms)
        self._append_global("websocket_ping_rtt_ms", duration_ms)

    def record_event_loop_lag(self, duration_ms: float) -> None:
        self._append_global("event_loop_lag_ms", duration_ms)

    def record_client_event_batch(self, event_count: int) -> None:
        now = time.time()
        with self._lock:
            self._client_event_batches += 1
            self._client_event_count += event_count
            self._last_client_batch_at = now

    def record_internet_probe(
        self,
        *,
        target: str,
        ok: bool,
        duration_ms: float,
        status_code: int | None = None,
        bytes_received: int = 0,
        error: str | None = None,
    ) -> None:
        probe = {
            "target": target,
            "ok": ok,
            "timestamp": time.time(),
            "duration_ms": duration_ms,
            "status_code": status_code,
            "bytes_received": bytes_received,
            "bytes_per_sec": bytes_received / (duration_ms / 1000)
            if duration_ms > 0
            else None,
            "error": error,
        }
        with self._lock:
            self._internet_probes[target] = probe
        self._append_global(
            "internet_probe_duration_ms",
            duration_ms,
            metadata={"target": target, "ok": ok},
        )

    def record_capture_pane(
        self,
        *,
        session_name: str,
        duration_ms: float,
        outcome: str,
        bytes_captured: int = 0,
    ) -> None:
        """Record one tmux capture-pane invocation (cold->hot snapshot path)."""
        self._append_global(
            "capture_pane_duration_ms",
            duration_ms,
            metadata={
                "session_name": session_name,
                "outcome": outcome,
                "bytes": bytes_captured,
            },
        )

    def record_sessions_control_event(
        self,
        *,
        kind: str,
        duration_ms: float,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._append_global(
            f"sessions_{kind}_duration_ms",
            duration_ms,
            metadata=metadata,
        )

    def record_terminal_ticket_event(
        self,
        *,
        kind: str,
        session_name: str,
        duration_ms: float,
        outcome: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        event_metadata = dict(metadata or {})
        event_metadata.update(
            {
                "kind": kind,
                "session_name": session_name,
                "outcome": outcome,
                "duration_ms": duration_ms,
            }
        )
        self._append_global(
            f"terminal_ticket_{kind}_duration_ms",
            duration_ms,
            metadata=event_metadata,
        )
        for metric_name, metric_value in event_metadata.items():
            if metric_name == "duration_ms" or not metric_name.endswith("_ms"):
                continue
            if isinstance(metric_value, (int, float)):
                self._append_global(
                    f"terminal_ticket_{kind}_{metric_name}",
                    float(metric_value),
                    metadata=event_metadata,
                )

    def _append(
        self,
        session_name: str,
        metric: str,
        value: float,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        sample = TimedValue(ts=time.monotonic(), value=value, metadata=metadata or {})
        with self._lock:
            self._session_samples[session_name][metric].append(sample)

    def _append_global(
        self,
        metric: str,
        value: float,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        sample = TimedValue(ts=time.monotonic(), value=value, metadata=metadata or {})
        with self._lock:
            self._global_samples[metric].append(sample)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        with self._lock:
            sessions: dict[str, Any] = {}
            session_names = sorted(
                set(self._session_samples) | set(self._websocket_sessions)
            )
            for session_name in session_names:
                metrics = self._session_samples[session_name]
                pty_read = [
                    sample
                    for sample in metrics["pty_read_bytes"]
                    if sample.ts >= cutoff
                ]
                writes = [
                    sample.value
                    for sample in metrics["pty_write_duration_ms"]
                    if sample.ts >= cutoff
                ]
                fanouts = [
                    sample
                    for sample in metrics["fanout_duration_ms"]
                    if sample.ts >= cutoff
                ]
                websocket_ping_rtts = [
                    sample.value
                    for sample in metrics["websocket_ping_rtt_ms"]
                    if sample.ts >= cutoff
                ]
                fanout_values = [sample.value for sample in fanouts]
                fanout_connection_counts = [
                    int(sample.metadata["connection_count"])
                    for sample in fanouts
                    if isinstance(sample.metadata.get("connection_count"), int)
                ]

                sessions[session_name] = {
                    "pty_read_bytes_per_sec": sum(sample.value for sample in pty_read)
                    / self.window_seconds,
                    "pty_read_samples": len(pty_read),
                    "pty_write_duration_ms": _summary(writes),
                    "fanout_duration_ms": _summary(fanout_values),
                    "fanout_connection_count_max": max(fanout_connection_counts)
                    if fanout_connection_counts
                    else 0,
                    "websocket_ping_rtt_ms": _summary(websocket_ping_rtts),
                    "live_websocket_count": self._websocket_sessions.get(
                        session_name, 0
                    ),
                }

            live_websocket_count = sum(self._websocket_sessions.values())
            event_loop_lag = [
                sample.value
                for sample in self._global_samples["event_loop_lag_ms"]
                if sample.ts >= cutoff
            ]
            websocket_ping_rtt = [
                sample.value
                for sample in self._global_samples["websocket_ping_rtt_ms"]
                if sample.ts >= cutoff
            ]
            internet_probe_durations = [
                sample.value
                for sample in self._global_samples["internet_probe_duration_ms"]
                if sample.ts >= cutoff
            ]
            sessions_list_samples = [
                sample
                for sample in self._global_samples["sessions_list_duration_ms"]
                if sample.ts >= cutoff
            ]
            sessions_sync_samples = [
                sample
                for sample in self._global_samples["sessions_sync_duration_ms"]
                if sample.ts >= cutoff
            ]
            ticket_metric_names = {
                "issue_duration_ms": "terminal_ticket_issue_duration_ms",
                "issue_lock_wait_ms": "terminal_ticket_issue_lock_wait_ms",
                "issue_insert_ms": "terminal_ticket_issue_insert_ms",
                "issue_commit_ms": "terminal_ticket_issue_commit_ms",
                "consume_duration_ms": "terminal_ticket_consume_duration_ms",
                "consume_lock_wait_ms": "terminal_ticket_consume_lock_wait_ms",
                "consume_lookup_ms": "terminal_ticket_consume_lookup_ms",
                "consume_update_ms": "terminal_ticket_consume_update_ms",
                "consume_commit_ms": "terminal_ticket_consume_commit_ms",
            }
            ticket_samples = {
                name: [
                    sample
                    for sample in self._global_samples[metric]
                    if sample.ts >= cutoff
                ]
                for name, metric in ticket_metric_names.items()
            }
            ticket_outcomes: dict[str, int] = {}
            for kind in ("issue", "consume"):
                for sample in ticket_samples[f"{kind}_duration_ms"]:
                    outcome = sample.metadata.get("outcome")
                    if isinstance(outcome, str):
                        key = f"{kind}:{outcome}"
                        ticket_outcomes[key] = ticket_outcomes.get(key, 0) + 1
            cache_states: dict[str, int] = {}
            for sample in sessions_list_samples:
                state = sample.metadata.get("cache_state")
                if isinstance(state, str):
                    cache_states[state] = cache_states.get(state, 0) + 1
            capture_pane_samples = [
                sample
                for sample in self._global_samples["capture_pane_duration_ms"]
                if sample.ts >= cutoff
            ]
            capture_pane_outcomes: dict[str, int] = {}
            for sample in capture_pane_samples:
                outcome = sample.metadata.get("outcome")
                if isinstance(outcome, str):
                    capture_pane_outcomes[outcome] = (
                        capture_pane_outcomes.get(outcome, 0) + 1
                    )
            internet_probes = list(self._internet_probes.values())
            client_events = {
                "batches": self._client_event_batches,
                "events": self._client_event_count,
                "last_batch_at": self._last_client_batch_at,
            }
            from core.api.db import get_writer_lock_snapshot

            return {
                "timestamp": time.time(),
                "window_seconds": self.window_seconds,
                "live_websocket_count": live_websocket_count,
                "live_pty_reader_count": self._live_pty_reader_count,
                "process": process_snapshot(),
                "writer_lock": get_writer_lock_snapshot(self.window_seconds),
                "network": {
                    "event_loop_lag_ms": _summary(event_loop_lag),
                    "websocket_ping_rtt_ms": _summary(websocket_ping_rtt),
                    "internet_probe_duration_ms": _summary(internet_probe_durations),
                    "internet_probes": internet_probes,
                },
                "sessions_control": {
                    "list_duration_ms": _summary(
                        [sample.value for sample in sessions_list_samples]
                    ),
                    "sync_duration_ms": _summary(
                        [sample.value for sample in sessions_sync_samples]
                    ),
                    "cache_state_counts": cache_states,
                    "last_list_event": sessions_list_samples[-1].metadata
                    if sessions_list_samples
                    else None,
                    "last_sync_event": sessions_sync_samples[-1].metadata
                    if sessions_sync_samples
                    else None,
                },
                "terminal_ticket": {
                    **{
                        name: _summary([sample.value for sample in samples])
                        for name, samples in ticket_samples.items()
                    },
                    "outcome_counts": ticket_outcomes,
                    "last_issue_event": ticket_samples["issue_duration_ms"][-1].metadata
                    if ticket_samples["issue_duration_ms"]
                    else None,
                    "last_consume_event": ticket_samples["consume_duration_ms"][
                        -1
                    ].metadata
                    if ticket_samples["consume_duration_ms"]
                    else None,
                },
                "capture_pane": {
                    "duration_ms": _summary(
                        [sample.value for sample in capture_pane_samples]
                    ),
                    "outcome_counts": capture_pane_outcomes,
                    "last_event": capture_pane_samples[-1].metadata
                    if capture_pane_samples
                    else None,
                },
                "client_event_ingest": client_events,
                "sessions": sessions,
            }


def process_snapshot() -> dict[str, Any]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return {
        "pid": os.getpid(),
        "rss_bytes": _rss_bytes(usage.ru_maxrss),
        "max_rss_bytes": _rss_bytes(usage.ru_maxrss),
        "user_cpu_seconds": usage.ru_utime,
        "system_cpu_seconds": usage.ru_stime,
        "open_fd_count": _open_fd_count(),
        "thread_count": _thread_count(),
    }


def _rss_bytes(raw_maxrss: int) -> int:
    # Linux reports ru_maxrss in KiB; macOS reports bytes. Production is Linux,
    # but keeping the fallback makes local tests sane on either platform.
    return raw_maxrss if raw_maxrss > 10_000_000 else raw_maxrss * 1024


def _open_fd_count() -> int | None:
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return None


def _thread_count() -> int | None:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("Threads:"):
                    return int(line.split(":", 1)[1].strip())
    except (OSError, ValueError):
        return None
    return None
