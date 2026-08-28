"""Single repository-authority contract for current and historical surfaces."""

from __future__ import annotations

import re


RETIRED_REPOSITORY_TOOLS = frozenset(
    {
        "create_branch",
        "register_branch",
        "submit_pr",
        "get_pr",
        "close_pr",
        "approve_pr",
        "request_pr_changes",
        "update_pr",
        "merge_pr",
        "revert_pr",
    }
)

REPOSITORY_TOOL_ROUTE = (
    "repository_owned_pr_lifecycle: This Marvis tool is retired. Git and the "
    "repository host own worktrees, branches, commits, pull requests, reviews, "
    "checks, and merges. Use the GitHub connector or repository-native git/gh, "
    "then update only the linked Marvis task from verified GitHub evidence. "
    "Call guide() for the current routing contract."
)

HISTORICAL_REPOSITORY_NOTICE = (
    "historical_repository_lifecycle: This evidence names one or more retired "
    "Marvis branch/PR tools. Do not call them. Git and the repository host own "
    "worktrees, branches, commits, pull requests, reviews, checks, and merges. "
    "Use GitHub or repository-native git/gh, update only the linked Marvis task "
    "from verified evidence, and call guide() for the current routing contract."
)

_RETIRED_REPOSITORY_TOKEN_RE = re.compile(
    rf"(?<![A-Za-z0-9_])(?:{'|'.join(sorted(RETIRED_REPOSITORY_TOOLS))})"
    rf"(?![A-Za-z0-9_])"
)


def historical_repository_notice(text: str | None) -> str | None:
    """Return the current authority route when evidence contains retired names."""
    if not isinstance(text, str) or _RETIRED_REPOSITORY_TOKEN_RE.search(text) is None:
        return None
    return HISTORICAL_REPOSITORY_NOTICE
