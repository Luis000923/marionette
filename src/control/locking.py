"""Cross-process mutual exclusion, so several agents can drive one desktop safely.

Keyboard and mouse input goes through a single global device on both backends:
`ydotoold`'s `uinput` node on Wayland, the Win32 input queue on Windows. If two
`control` processes type or click at the same time, their events interleave into
one keystroke stream and both agents get garbage. Every input action therefore
takes `FileMutex` first, which combines:

* a byte-range lock on a lock file (`control_input.lock` under the platform's
  scratch dir) -- `fcntl.flock` on POSIX, `msvcrt.locking` on Windows. It guards
  against *other processes*, which is the case that actually corrupts input; and
* a `threading.RLock` -- both flock and `msvcrt.locking` are per open file
  description, so two threads in one process sharing a fd would both "hold" it
  otherwise.

The mutex is reentrant, so composite actions (drag, double click, key combos)
can hold it for the whole sequence without deadlocking on their own primitives.

State *queries* (window lists, screenshots, snapshots) deliberately never take
this lock: they don't touch the input device, so they must stay non-blocking even
while another agent is mid-keystroke.
"""
from __future__ import annotations

import errno
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import platforms

if platforms.IS_WINDOWS:
    import msvcrt
else:
    import fcntl

#: Kept under the platform's scratch dir: `%TEMP%\control_input.lock` on Windows,
#: `/tmp/control_input.lock` on Linux. Never inside the repo, which may be read-only.
DEFAULT_LOCK_PATH = platforms.scratch_dir("control_input.lock")
DEFAULT_TIMEOUT = 30.0
_POLL_INTERVAL = 0.01

#: errno/winerror values a "somebody else holds the lock" failure carries.
_BUSY_ERRNOS = (errno.EACCES, errno.EAGAIN, getattr(errno, "EDEADLK", errno.EACCES))
_BUSY_WINERRORS = (32, 33, 167)  # sharing violation, lock violation, lock failed


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
    """Reentrant mutex shared across processes via a file lock and across threads
    via an RLock."""

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
                self._lock_file(deadline, limit)
            self._depth += 1
            return True
        except BaseException:
            self._thread_lock.release()
            raise

    def _busy(self, exc: OSError) -> bool:
        """True when this failure only means "somebody else got there first"."""
        return exc.errno in _BUSY_ERRNOS or getattr(exc, "winerror", None) in _BUSY_WINERRORS

    def _try_lock(self, fd: int) -> None:
        """Take the cross-process lock on `fd`, or raise OSError if it is busy."""
        if platforms.IS_WINDOWS:
            # msvcrt.locking() works on a byte range starting at the file
            # pointer, and that range has to exist, so keep the first byte
            # around and always lock from offset 0.
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)  # the write left the pointer at 1
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self, fd: int) -> None:
        if platforms.IS_WINDOWS:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)

    def _lock_file(self, deadline: float, limit: float) -> None:
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        while True:
            try:
                self._try_lock(fd)
                self._fd = fd
                return
            except OSError as exc:
                if not self._busy(exc):
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
                self._unlock(self._fd)
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
