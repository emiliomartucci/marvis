from __future__ import annotations

from typing import Final


VALID_INBOX_TOPICS: Final[tuple[str, ...]] = (
    "ai-news",
    "ai-products",
    "tooling",
    "security-devtools",
    "pv-energy",
    "strategy-business",
    "policy-politics",
    "general",
)

VALID_INBOX_TREATMENTS: Final[tuple[str, ...]] = (
    "read",
    "save",
    "read_save",
    "ignore",
)

_TOPIC_SET = set(VALID_INBOX_TOPICS)
_TREATMENT_SET = set(VALID_INBOX_TREATMENTS)


def normalize_inbox_topic(value: str | None) -> str:
    if not value:
        return "general"
    topic = value.strip().lower()
    return topic if topic in _TOPIC_SET else "general"


def normalize_inbox_treatment(value: str | None) -> str:
    if not value:
        return "read"
    treatment = value.strip().lower()
    return treatment if treatment in _TREATMENT_SET else "read"


def infer_topic_from_metadata(
    metadata: dict[str, object],
    *,
    title: str | None = None,
    content: str | None = None,
) -> str:
    hint = metadata.get("topicHint")
    if isinstance(hint, str):
        return normalize_inbox_topic(hint)

    feed_url = metadata.get("feedUrl")
    if isinstance(feed_url, str):
        url = feed_url.lower()
        if any(
            token in url
            for token in (
                "arxiv",
                "importai",
                "latent.space",
                "deeplearning.ai",
                "alignmentfeed",
                "latentsignal",
            )
        ):
            return "ai-news"
        if any(
            token in url
            for token in (
                "anthropics/claude-code",
                "anomalyco/opencode",
                "simonwillison.net/atom/everything",
            )
        ):
            return "tooling"
        if any(token in url for token in ("pv-magazine", "renewablesnow", "pveurope")):
            return "pv-energy"
        if any(
            token in url
            for token in ("stratechery", "newcomer", "a16z", "notboring", "thediff")
        ):
            return "strategy-business"
        if any(token in url for token in ("ilpost", "linkiesta", "lavoce", "politico")):
            return "policy-politics"

    feed_name = metadata.get("feedName")
    if isinstance(feed_name, str):
        name = feed_name.lower()
        if any(
            token in name
            for token in ("claude", "opencode", "datasette", "tool", "simon willison")
        ):
            return "tooling"
        if any(token in name for token in ("security", "secret", "vuln")):
            return "security-devtools"
        if any(token in name for token in ("pv", "renewable", "energy")):
            return "pv-energy"
        if any(
            token in name for token in ("politico", "ilpost", "linkiesta", "lavoce")
        ):
            return "policy-politics"

    corpus = " ".join(filter(None, [title, content])).lower()
    if any(
        token in corpus
        for token in (
            "anthropic",
            "project glasswing",
            "mythos preview",
            "model preview",
            "model release",
            "llm release",
            "openai",
            "google ai",
        )
    ):
        return "ai-news"
    if any(
        token in corpus
        for token in (
            "claude code",
            "opencode",
            "datasette",
            "sqlite wal",
            "scan-for-secrets",
        )
    ):
        return "tooling"
    if any(
        token in corpus
        for token in ("supply chain", "vulnerability", "secret", "security", "axios")
    ):
        return "security-devtools"
    if any(
        token in corpus
        for token in (
            "glm",
            "gemma",
            "research",
            "paper",
            "benchmark",
            "model",
            "agentic",
        )
    ):
        return "ai-news"
    if any(
        token in corpus
        for token in ("product", "use case", "customer", "community", "feedback")
    ):
        return "ai-products"
    if any(
        token in corpus for token in ("solar", "pv", "renewable", "battery", "energy")
    ):
        return "pv-energy"
    if any(
        token in corpus
        for token in (
            "market",
            "startup",
            "funding",
            "strategy",
            "venture",
            "acquisition",
        )
    ):
        return "strategy-business"
    if any(
        token in corpus
        for token in (
            "politics",
            "regulation",
            "policy",
            "europe",
            "government",
            "election",
        )
    ):
        return "policy-politics"
    return "general"


def infer_treatment(topic: str, metadata: dict[str, object]) -> str:
    source_class = metadata.get("sourceClass")
    if source_class == "operational_email":
        return "save"
    if topic in {"ai-products", "strategy-business", "policy-politics"}:
        return "read_save"
    if topic in {
        "tooling",
        "security-devtools",
        "pv-energy",
    }:
        return "save"
    return "read"
