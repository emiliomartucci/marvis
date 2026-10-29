"""Own the embedding worker process and a finite recovery budget.

The budget lives outside the worker, so terminating stuck native threads cannot
reset it. Service managers must not automatically restart this supervisor after
budget exhaustion. An operator can explicitly start a fresh recovery window.
"""

from __future__ import annotations

import argparse
import http.client
import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

logger = logging.getLogger("marvisx.embedding_supervisor")
EXHAUSTED = 78


def _health(url: str, timeout: float) -> dict:
    # This is a local control channel, never an environment-configured proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        payload = json.loads(response.read(65536))
    if not isinstance(payload, dict):
        raise ValueError("invalid health payload")
    return payload


def _stop_worker(process: subprocess.Popen, timeout: float) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=timeout)
    except ProcessLookupError:
        process.wait(timeout=timeout)


def supervise(
    command: list[str],
    *,
    health_url: str,
    max_restarts: int = 2,
    poll_interval: float = 1,
    request_timeout: float = 2,
    startup_timeout: float = 300,
    unresponsive_timeout: float = 15,
    stop_timeout: float = 5,
    restart_delay: float = 5,
) -> int:
    """Monitor cheap worker health; terminate, kill and reap before restarting."""
    stopped = threading.Event()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    for sig in previous:
        signal.signal(sig, lambda *_: stopped.set())
    try:
        for attempt in range(max_restarts + 1):
            if stopped.is_set():
                return 0
            process = subprocess.Popen(command, start_new_session=True)
            started = last_response = time.monotonic()
            ready = False
            reason = "worker_exited"
            try:
                while process.poll() is None and not stopped.is_set():
                    now = time.monotonic()
                    try:
                        health = _health(health_url, request_timeout)
                        last_response = time.monotonic()
                        if (
                            health.get("recovery_required")
                            or health.get("status") == "error"
                        ):
                            reason = "native_unavailable"
                            break
                        if health.get("status") in ("ready", "degraded"):
                            ready = True
                    except (
                        OSError,
                        ValueError,
                        urllib.error.URLError,
                        http.client.HTTPException,
                    ):
                        pass
                    now = time.monotonic()
                    if not ready and now - started > startup_timeout:
                        reason = "startup_deadline"
                        break
                    if ready and now - last_response > unresponsive_timeout:
                        reason = "worker_unresponsive"
                        break
                    stopped.wait(poll_interval)
            finally:
                _stop_worker(process, stop_timeout)
            if stopped.is_set():
                return 0
            logger.error(
                "embedding worker recovery reason=%s attempt=%d", reason, attempt + 1
            )
            if attempt < max_restarts:
                stopped.wait(restart_delay)
        logger.error("embedding recovery exhausted; explicit operator restart required")
        return EXHAUSTED
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if math.isfinite(value) and 0 < value <= 3600 else default


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("MARVIS_EMBEDDER_PORT", "8109"))
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    # Production has exactly two recovery attempts. Tests exercise the same
    # process owner with shorter deadlines and controlled synthetic workers.
    return supervise(
        [
            sys.executable,
            "-B",
            "-m",
            "uvicorn",
            "core.api.services.embedding_sidecar.app:app",
            "--host",
            args.host,
            "--port",
            str(args.port),
        ],
        health_url=f"http://127.0.0.1:{args.port}/healthz",
        startup_timeout=_seconds("EMBEDDER_STARTUP_TIMEOUT_SECONDS", 300),
        unresponsive_timeout=_seconds("EMBEDDER_UNRESPONSIVE_TIMEOUT_SECONDS", 15),
    )


if __name__ == "__main__":
    raise SystemExit(main())
