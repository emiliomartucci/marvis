"""WS ticket in-memory store: single-use, expiry, session-match (task 0a322d40).

Tickets moved off the SQLite _write_lock to an in-memory dict to kill the
8.5s p95 lock-wait on terminal open/connect. These tests lock the security
invariants: single-use, session binding, TTL expiry, unknown ticket.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.api import security


@pytest.fixture(autouse=True)
def _clear_ticket_store():
    security._ws_tickets.clear()
    yield
    security._ws_tickets.clear()


async def test_create_then_consume_ok():
    ticket = await security.create_ws_ticket("emilio", "Visio")
    assert ticket
    user = await security.consume_ws_ticket(ticket, "Visio")
    assert user == "emilio"


async def test_single_use_second_consume_rejected():
    ticket = await security.create_ws_ticket("emilio", "Visio")
    assert await security.consume_ws_ticket(ticket, "Visio") == "emilio"
    # second consume must fail (single-use)
    assert await security.consume_ws_ticket(ticket, "Visio") is None


async def test_session_mismatch_rejected():
    ticket = await security.create_ws_ticket("emilio", "Visio")
    assert await security.consume_ws_ticket(ticket, "OtherSession") is None
    # ticket not marked used by a mismatched attempt → still valid for right session
    assert await security.consume_ws_ticket(ticket, "Visio") == "emilio"


async def test_unknown_ticket_rejected():
    assert await security.consume_ws_ticket("does-not-exist", "Visio") is None


async def test_expired_ticket_rejected_and_purged():
    ticket = await security.create_ws_ticket("emilio", "Visio")
    # force expiry into the past
    security._ws_tickets[ticket]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    assert await security.consume_ws_ticket(ticket, "Visio") is None
    # expired ticket removed from store on consume
    assert ticket not in security._ws_tickets


async def test_cleanup_purges_expired_only():
    fresh = await security.create_ws_ticket("emilio", "A")
    stale = await security.create_ws_ticket("emilio", "B")
    security._ws_tickets[stale]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    purged = security.cleanup_expired_ws_tickets()
    assert purged == 1
    assert fresh in security._ws_tickets
    assert stale not in security._ws_tickets


async def test_timings_outcome_recorded():
    timings: dict = {}
    ticket = await security.create_ws_ticket("emilio", "Visio", timings=timings)
    assert timings.get("insert_ms") == 0.0
    timings2: dict = {}
    await security.consume_ws_ticket(ticket, "Visio", timings=timings2)
    assert timings2.get("outcome") == "ok"
