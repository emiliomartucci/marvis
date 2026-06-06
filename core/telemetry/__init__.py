# v1.0.0 - 2026-05-27 - S2 F5: anonymous opt-out telemetry package
"""Anonymous, opt-out telemetry for the MarvisX OSS CLI.

Public surface: :func:`core.telemetry.client.emit`. Importing this package is
side-effect free (no network, no disk) — the opt-out gate runs inside ``emit``
before any I/O. See ``client.py`` for the gate-before-IO contract and ``schema.py``
for the strict per-event props whitelist (the no-PII guarantee, test-enforced).
"""
from __future__ import annotations
