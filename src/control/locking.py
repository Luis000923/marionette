"""Cross-process mutual exclusion, so several agents can drive one desktop safely.

`ydotoold` writes to a single global kernel `uinput` device: if two `control`
processes type or click at the same time, their events interleave into one
keystroke stream and both agents get garbage. Every input action therefore takes
`FileMutex` first, which combines:

* `fcntl.flock` on a lock file (`/tmp/control_input.lock` by default) -- guards
  against *other processes*, which is the case that actually corrupts input; and
* a `threading.RLock` -- `flock` is per open file description, so two threads in
  one process sharing a fd would both "hold" it otherwise.

The mutex is reentrant, so composite actions (drag, double click, key combos)
can hold it for the whole sequence without deadlocking on their own primitives.

State *queries* (window lists, screenshots, snapshots) deliberately never take
this lock: they don't touch uinput, so they must stay non-blocking even while
another agent is mid-keystroke.
"""
from __future__ import annotations

import errno
import fcntl
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DEFAULT_LOCK_PATH = Path("/tmp/control_input.lock")
DEFAULT_TIMEOUT = 30.0
_POLL_INTERVAL = 0.01


class LockTimeout(TimeoutError):
    """Raised when the input mutex could not be acquired in time."""


def lock_path() -> Path:
    """Lock file for input serialization.

    Deliberately *not* per session: `uinput` is a single global device, so every
    agent on the machine has to queue on the same lock.
    """
    return Path(os.environ.get("CONTROL_INPUT_LOCK") or DEFAULT_LOCK_PATH)


def sibling_lock(name: str, timeout: float | None = None) -> "FileMutex":
    """A separate named mutex next to the input lock (e.g. desktop allocation).

    Never affected by CONTROL_INPUT_LOCK_DISABLED, and never the input lock
    itself, so bookkeeping can't delay another agent's keystrokes.
    """
    return FileMutex(lock_path().parent / f"control_{name}.lock", timeout=timeout,
                     can_disable=False)


def default_timeout() -> float:
    raw = os.environ.get("CONTROL_INPUT_LOCK_TIMEOUT")
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT


def locking_disabled() -> bool:
    """Escape hatch for single-agent use or debugging (CONTROL_INPUT_LOCK_DISABLED=1)."""
    return os.environ.get("CONTROL_INPUT_LOCK_DISABLED", "").lower() in ("1", "true", "yes")


class FileMutex:
    """Reentrant mutex shared across processes via `flock` and across threads via an RLock."""

    def __init__(self, path: Path | str | None = None, timeout: float | None = None,
                 can_disable: bool = True):
        self._path = Path(path) if path is not None else None
        self._timeout = timeout
        # CONTROL_INPUT_LOCK_DISABLED is about *input*; other mutexes opt out of it.
        self._can_disable = can_disable
        self._thread_lock = threading.RLock()
        self._fd: int | None = None
        self._depth = 0

    @property
    def path(self) -> Path:
        return self._path if self._path is not None else lock_path()

    @property
    def timeout(self) -> float:
        return self._timeout if self._timeout is not None else default_timeout()

    @property
    def held(self) -> bool:
        return self._depth > 0

    def _disabled(self) -> bool:
        return self._can_disable and locking_disabled()

    def acquire(self, timeout: float | None = None) -> bool:
        if self._disabled():
            return True
        limit = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + limit

        acquired = (
            self._thread_lock.acquire(timeout=limit) if limit > 0
            else self._thread_lock.acquire(blocking=False)
        )
        if not acquired:
            raise LockTimeout(f"input lock busy (another thread held it for >{limit}s)")
        try:
            if self._depth == 0:
                self._flock(deadline, limit)
            self._depth += 1
            return True
        except BaseException:
            self._thread_lock.release()
            raise

    def _flock(self, deadline: float, limit: float) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._fd = fd
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    os.close(fd)
                    raise
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise LockTimeout(
                        f"could not acquire {path} within {limit}s; "
                        "another control process is still sending input"
                    ) from exc
                time.sleep(_POLL_INTERVAL)

    def release(self) -> None:
        if self._disabled():
            return
        if self._depth == 0:
            raise RuntimeError("release() called on an unheld input lock")
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        self._thread_lock.release()

    def __enter__(self) -> "FileMutex":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

    @contextmanager
    def acquired(self, timeout: float | None = None) -> Iterator["FileMutex"]:
        self.acquire(timeout)
        try:
            yield self
        finally:
            self.release()
