# v1.0.0 - 2026-05-27 - S2 F5: anonymous opt-in telemetry package
"""Anonymous, opt-in telemetry for the local MarvisX CLI.

Public surface: :func:`core.telemetry.client.emit`. Importing this package is
side-effect free (no network, no disk) — the consent gate runs inside ``emit``
before any I/O. See ``client.py`` for the gate-before-IO contract and ``schema.py``
for the strict per-event props whitelist (the no-PII guarantee, test-enforced).
"""
from __future__ import annotations
