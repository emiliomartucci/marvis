"""OS user for git/chown operations in multi-user deployments.

The API process may run as a service account distinct from the user that owns
the git repositories. When a run-as user is configured (``GIT_RUNAS_USER``)
and differs from the current process user, git runs via ``sudo -u <user>`` and
chown targets ``<user>:<user>``. When unset — the default, and the common
single-user / self-hosted case — git runs directly and chown is a no-op: the
API process already owns the files.
"""
from __future__ import annotations

import subprocess

from core.api.config import settings


def runas_user() -> str:
    """The user git/chown must run as, or ``''`` to act as the current process."""
    return settings.effective_git_runas_user


def git_command() -> list[str]:
    """``git`` argv, prefixed with ``sudo -u <user>`` when a run-as user is set."""
    user = runas_user()
    return ["sudo", "-u", user, "git"] if user else ["git"]


# Resolved once at import: uid and config are fixed for the process lifetime.
GIT_CMD: list[str] = git_command()


def chown_to_runas(*paths: object, recursive: bool = True) -> None:
    """chown the given paths to the run-as user. No-op when none is configured."""
    user = runas_user()
    if not user:
        return
    targets = [str(p) for p in paths if p]
    if not targets:
        return
    cmd = ["sudo", "chown", *(["-R"] if recursive else []), f"{user}:{user}", *targets]
    subprocess.run(cmd, capture_output=True)
