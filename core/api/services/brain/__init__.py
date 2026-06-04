# Brain v1 — Digest + Journal subpackage (sub-01).
# Layering: api/models/brain.py owns cross-boundary contracts.
# Service-private dataclasses live in api/services/brain/models.py.
# No LLM imports allowed at this layer (parent §9.3 invariant).
from __future__ import annotations

from core.api.services.brain.cycle import (
    canonical_evidence,
    cycle_cutoff_at,
    current_brain_cycle_key,
    derive_event_id,
    evidence_hash as compute_evidence_hash,
    make_event_id,
    persist_event,
    polish_pending_journals,
    polish_run_journals,
    publish_run_journals,
    update_run_status,
)
from core.api.services.brain.capabilities import KNOWLEDGE_GLYPHS, get_capabilities
from core.api.services.brain.events_reader import list_events_for_cycle
from core.api.services.brain.jobs import recompute_brain_cycle, run_brain_jobs_if_due
from core.api.services.brain.runs_reader import (
    fetch_single_run,
    get_pipeline_counters,
    list_runs,
)
from core.api.services.brain.ws_emitter import emit_phase_complete, get_hub

__all__ = [
    "KNOWLEDGE_GLYPHS",
    "canonical_evidence",
    "compute_evidence_hash",
    "current_brain_cycle_key",
    "cycle_cutoff_at",
    "derive_event_id",
    "emit_phase_complete",
    "fetch_single_run",
    "get_capabilities",
    "get_hub",
    "get_pipeline_counters",
    "list_events_for_cycle",
    "list_runs",
    "make_event_id",
    "persist_event",
    "polish_pending_journals",
    "polish_run_journals",
    "publish_run_journals",
    "recompute_brain_cycle",
    "run_brain_jobs_if_due",
    "update_run_status",
]
