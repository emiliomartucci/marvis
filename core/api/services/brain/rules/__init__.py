# Brain v1 — Drift rule subpackage (sub-02 §6 C1).
# Each rule module exposes `build_signals(snapshot, *, run_id, now) -> list[DriftSignal]`.
# Registration order maps DR1..DR7. The orchestrator (drift.py) iterates this
# tuple — adding a rule requires appending it here AND in the matrices below.
#
# Layering invariant: rules MUST consume the CycleSnapshot — never raw-SQL
# brain_digest_events / brain_journal_entries / substrate tables.
from __future__ import annotations

import os

from core.api.services.brain.rules.dr1_activity_without_status import (
    build_signals as dr1_build_signals,
)
from core.api.services.brain.rules.dr2_decision_without_adr import (
    build_signals as dr2_build_signals,
)
from core.api.services.brain.rules.dr3_stale_open_loop import (
    build_signals as dr3_build_signals,
)
from core.api.services.brain.rules.dr4_docs_governance_drift import (
    build_signals as dr4_build_signals,
)
from core.api.services.brain.rules.dr5_playbook_changed import (
    build_signals as dr5_build_signals,
)
from core.api.services.brain.rules.dr6_external_update_unpropagated import (
    build_signals as dr6_build_signals,
)
from core.api.services.brain.rules.dr7_claimed_decision_gap import (
    build_signals as dr7_build_signals,
)
from core.api.services.brain.rules.dr8_direction_misalignment import (
    build_signals as dr8_build_signals,
)
from core.api.services.brain.rules.dr9_task_superseded import (
    build_signals as dr9_build_signals,
)

# CE4 §11.5: deterministic per-rule axis assignment. Drift author MUST update
# the matrix below — and the test_dr_axis_matrix invariant — when adding rules.
DR_AXIS_MATRIX = {
    "DR1": "context",   # activity happens, status doesn't capture it
    "DR2": "intent",    # meeting decision, ADR missing
    "DR3": "intent",    # committed intent, unexecuted
    "DR4": "context",   # passive doc decay
    "DR5": "context",   # procedure on the ground diverged
    "DR6": "context",   # world changed, our docs/code didn't
    "DR7": "intent",    # claim-and-act gap
    "DR8": "intent",    # declared direction vs observed execution
    "DR9": "intent",    # task declared open vs observed resolution (merged PR / done handoff)
}


# Public registry — (rule_id, builder). Order is canonical execution order.
REGISTERED_RULES = (
    ("DR1", dr1_build_signals),
    ("DR2", dr2_build_signals),
    ("DR3", dr3_build_signals),
    ("DR4", dr4_build_signals),
    ("DR5", dr5_build_signals),
    ("DR6", dr6_build_signals),
    ("DR7", dr7_build_signals),
    ("DR8", dr8_build_signals),
    ("DR9", dr9_build_signals),
)


# DR8 default-off (audit 2026-08-03): with the knowledge_form classifier
# returning 'unknown' for nearly every event, DR8 degrades to an activity
# counter — one medium signal per active project per cycle at fixed
# confidence — and the noise buries the actionable DR9 signals.
# Override with MARVIS_BRAIN_DISABLED_RULES (comma-separated rule ids;
# an empty string runs every registered rule).
_DEFAULT_DISABLED_RULES = "DR8"


def disabled_rule_ids() -> frozenset[str]:
    raw = os.environ.get("MARVIS_BRAIN_DISABLED_RULES")
    if raw is None:
        raw = _DEFAULT_DISABLED_RULES
    return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())


def active_rules():
    # Resolved at call time so the env override works without re-import.
    disabled = disabled_rule_ids()
    return tuple(entry for entry in REGISTERED_RULES if entry[0] not in disabled)


__all__ = [
    "DR_AXIS_MATRIX",
    "REGISTERED_RULES",
    "active_rules",
    "disabled_rule_ids",
]
