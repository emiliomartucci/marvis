"""Wizard onboarding shared module.

Pure Python state machine + per-step payloads + validation + BYOK vault.
Consumed by:
- `core/console/src/app/(welcome)` wizard route (via API endpoint Wave 2)
- `marvis init` CLI (task 70a51178)

The two surfaces produce byte-identical `settings.yaml` when given the same answers.
"""

from .state import (
    DbBackend,
    FirstProjectPayload,
    LlmProvider,
    LlmProviderPayload,
    ProjectType,
    StepId,
    StoragePayload,
    WelcomePayload,
    WizardState,
)
from .steps import (
    SKIPPABLE,
    STEP_ORDER,
    advance,
    finalize,
    go_back,
    next_step,
    previous_step,
    skip_current,
)
from .validation import (
    ValidationError,
    slugify,
    validate_first_project,
    validate_llm_provider,
    validate_storage,
    validate_welcome,
)

__all__ = [
    "DbBackend",
    "FirstProjectPayload",
    "LlmProvider",
    "LlmProviderPayload",
    "ProjectType",
    "SKIPPABLE",
    "STEP_ORDER",
    "StepId",
    "StoragePayload",
    "ValidationError",
    "WelcomePayload",
    "WizardState",
    "advance",
    "finalize",
    "go_back",
    "next_step",
    "previous_step",
    "skip_current",
    "slugify",
    "validate_first_project",
    "validate_llm_provider",
    "validate_storage",
    "validate_welcome",
]
