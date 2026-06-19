# v0.1.0 - 2026-06-04 - Track 2 #3c: community summarizers (flag MARVIS_COMMUNITY_SUMMARIES, default OFF)
"""Per-community summarizers for the GraphRAG global-search layer (Track 2 #3c).

A community summary is the one-line "quadro d'insieme" GraphRAG global-search
(arXiv:2506.05690) surfaces per cluster — "cosa sta succedendo nel programma X".
The plan pins how a summary may be produced AND how it enters the brief:

* **Clustering is LLM-free** (see ``communities.py``); only the *summary* text
  may come from a model, and that model is a **local** one.
* The summary, once generated, enters the brief **after Track 2 #2** (span
  citations + NLI verification). A community summary is itself a *claim to
  ground*, not free text — it inherits span-citation + verification exactly like
  any other brief claim, so #2 closes the hallucination door on it too. A
  summarizer therefore returns a string of claims to be grounded downstream; it
  must NOT be trusted as pre-verified prose.

The default path (``NoopSummarizer``) is fully model-free and deterministic so
the layer can ship behind ``MARVIS_COMMUNITY_SUMMARIES`` (default OFF) without
pulling a model onto the shared host. Running a real local LLM summarizer is a
deliberate **off-host** step (learning a09b8754: a model run on the shared host
saturated CPU/RAM and cascaded sessions) — see ``LocalLLMSummarizer``.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from core.api.services.kg.communities import Community

# How many member labels to name in the extractive stub before eliding.
_STUB_LABEL_PREVIEW = 5


@runtime_checkable
class CommunitySummarizer(Protocol):
    """Strategy for turning a community + its node texts into a summary string.

    ``node_texts`` maps ``node_id -> short text`` (e.g. a node title/snippet).
    Implementations must tolerate missing keys (not every member need have text)
    and must return a single summary string. The return value is a *claim* that
    downstream #2 grounds + verifies; it is never trusted as final prose.
    """

    def summarize(
        self, community: Community, node_texts: Mapping[str, str]
    ) -> str: ...


class NoopSummarizer:
    """DEFAULT model-free extractive stub. Loads nothing, fully deterministic.

    Produces ``"Cluster of N nodes: <first few labels>"`` — a structural
    descriptor built only from the community membership and (when available) the
    provided node texts. No model, no network, no allocation beyond the string.
    Same community + same texts → same output, always.
    """

    def summarize(
        self, community: Community, node_texts: Mapping[str, str]
    ) -> str:
        size = community.size
        # Prefer human-ish node_texts when present, else the node ids; sorted
        # for determinism, capped to a short preview.
        previews = [
            node_texts.get(member) or member
            for member in sorted(community.members)
        ]
        shown = previews[:_STUB_LABEL_PREVIEW]
        head = ", ".join(shown)
        if len(previews) > _STUB_LABEL_PREVIEW:
            head = f"{head}, …"
        node_word = "node" if size == 1 else "nodes"
        return f"Cluster of {size} {node_word}: {head}"


class LocalLLMSummarizer:
    """STUB for an off-host local-LLM summarizer. ``__init__`` raises by design.

    Generating real community summaries with a local LLM is a deliberate
    **off-host** step and MUST NOT run a model on the shared host (learning
    a09b8754: a model run here saturated CPU/RAM and cascaded ~tmux sessions).
    Constructing this class on the shared host is therefore a hard error — it
    fails loud instead of silently importing torch/transformers and warming a
    model. The real implementation lives off-host (separate box / batch job);
    once it emits summaries, those strings enter the brief AFTER Track 2 #2 so
    they inherit span-citation + NLI verification (a summary is a claim to
    ground, not free text).

    No model library is imported at module load or here — the import graph of
    this module pulls neither ``torch`` nor ``transformers``.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise NotImplementedError(
            "LocalLLMSummarizer is an off-host step: a local LLM summary MUST "
            "NOT run a model on the shared host (learning a09b8754). Run it on "
            "a dedicated box / batch job; its summaries then enter the brief "
            "AFTER Track 2 #2 to inherit span-citation + verification. Use "
            "NoopSummarizer for the in-process, model-free default."
        )

    def summarize(
        self, community: Community, node_texts: Mapping[str, str]
    ) -> str:  # pragma: no cover - unreachable: __init__ always raises
        raise NotImplementedError
