# Brain v1.0.1 — Source collector subpackage (sub-01 §3 Sources v1).
# Each module owns one substrate (ingest / git+KG / PiR tasks / handoffs /
# learnings) and exposes a SourceCollector descriptor.
#
# register_all_collectors() is the single startup hook called from
# api/main.py lifespan BEFORE the first _periodic_brain_jobs tick. It is
# idempotent: prior collectors are cleared and the canonical five
# re-registered, so lifespan restarts never accrete duplicates.
from __future__ import annotations

from core.api.services.brain.digest_collector import (
    clear_registered_collectors,
    register_collector,
)
from core.api.services.brain.sources.git_kg import git_collector, kg_collector
from core.api.services.brain.sources.handoffs import handoffs_collector
from core.api.services.brain.sources.ingestor import ingestor_collector
from core.api.services.brain.sources.learnings import learnings_collector
from core.api.services.brain.sources.pir_tasks import pir_tasks_collector


def register_all_collectors() -> None:
    """Clear + register the six v1.0.1 source collectors. Idempotent.

    git_kg split in 2 separate collectors (git + kg) — CHECK constraint
    brain_source_watermarks.source_system accepts only single tokens
    ('git', 'kg', etc.), not composite 'git_kg'. Bugfix 2026-05-16.
    """
    clear_registered_collectors()
    register_collector(ingestor_collector)
    register_collector(git_collector)
    register_collector(kg_collector)
    register_collector(pir_tasks_collector)
    register_collector(handoffs_collector)
    register_collector(learnings_collector)


__all__ = [
    "git_collector",
    "kg_collector",
    "handoffs_collector",
    "ingestor_collector",
    "learnings_collector",
    "pir_tasks_collector",
    "register_all_collectors",
]
