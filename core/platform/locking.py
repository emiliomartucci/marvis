"""Cross-platform file locks for CLI-reachable shared runtime code."""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from typing import Iterator


class LockUnavailableError(RuntimeError):
    """Raised when a cross-process lock cannot be acquired safely."""


@contextmanager
def exclusive_file_lock(
    path: os.PathLike[str] | str,
    *,
    mode: int = 0o600,
    nofollow: bool = False,
) -> Iterator[None]:
    """Hold an exclusive cross-process lock until the context exits.

    POSIX keeps the existing ``flock`` + optional ``O_NOFOLLOW`` hardening.
    Windows uses the already-declared ``filelock`` runtime dependency, whose
    native backend locks through ``msvcrt``. Imports stay function-local so a
    platform never has to import another platform's standard-library module.
    """
    lock_path = os.fspath(path)
    if os.name == "nt":
        if nofollow:
            try:
                is_symlink = os.path.islink(lock_path)
            except OSError as exc:
                raise LockUnavailableError(
                    f"lock path is not inspectable: {lock_path}"
                ) from exc
            if is_symlink:
                raise LockUnavailableError(
                    os.strerror(errno.ELOOP) + f": {lock_path}"
                )
        try:
            from filelock import FileLock

            lock = FileLock(lock_path, mode=mode, timeout=-1)
            lock.acquire()
        except (ImportError, OSError, TimeoutError) as exc:
            raise LockUnavailableError(f"lock is unavailable: {lock_path}") from exc
        try:
            yield
        finally:
            lock.release()
        return

    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if nofollow and hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        import fcntl

        descriptor = os.open(lock_path, flags, mode)
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, mode)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except (ImportError, OSError) as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise LockUnavailableError(f"lock is unavailable: {lock_path}") from exc
    assert descriptor is not None
    try:
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
