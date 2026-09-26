"""Tests for the input mutex: the thing that stops parallel agents from
interleaving their keystrokes on the single global input device.

Concurrency is simulated two ways: threads (which exercise the intra-process
RLock) and real subprocesses (which exercise the cross-process file lock --
`fcntl.flock` on POSIX, `msvcrt.locking` on Windows -- the case that actually
matters, since each agent is its own `control` process).
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from control import locking, platforms


@pytest.fixture
def lock_file(tmp_path, monkeypatch):
    path = tmp_path / "input.lock"
    monkeypatch.setenv("CONTROL_INPUT_LOCK", str(path))
    return path


class TestConfiguration:
    def test_lock_path_follows_env(self, lock_file):
        assert locking.lock_path() == lock_file

    def test_lock_path_defaults_to_the_scratch_dir(self, monkeypatch):
        monkeypatch.delenv("CONTROL_INPUT_LOCK", raising=False)
        assert locking.lock_path() == locking.DEFAULT_LOCK_PATH
        assert locking.DEFAULT_LOCK_PATH == platforms.scratch_dir("control_input.lock")

    @pytest.mark.skipif(os.name != "posix", reason="POSIX scratch dir")
    def test_lock_path_is_tmp_on_posix(self, monkeypatch):
        monkeypatch.delenv("CONTROL_INPUT_LOCK", raising=False)
        assert str(locking.DEFAULT_LOCK_PATH) == "/tmp/control_input.lock"

    @pytest.mark.skipif(os.name == "posix", reason="Windows scratch dir")
    def test_lock_path_is_under_the_windows_temp_dir(self, monkeypatch):
        monkeypatch.delenv("CONTROL_INPUT_LOCK", raising=False)
        # A literal "/tmp/control_input.lock" would be a *directory* named /tmp on
        # Windows, so the scratch dir has to come from the platform.
        assert locking.DEFAULT_LOCK_PATH.parent == platforms.runtime_dir()
        assert locking.DEFAULT_LOCK_PATH.name == "control_input.lock"

    def test_timeout_falls_back_when_env_is_garbage(self, monkeypatch):
        monkeypatch.setenv("CONTROL_INPUT_LOCK_TIMEOUT", "not-a-number")
        assert locking.default_timeout() == locking.DEFAULT_TIMEOUT

    def test_timeout_reads_env(self, monkeypatch):
        monkeypatch.setenv("CONTROL_INPUT_LOCK_TIMEOUT", "2.5")
        assert locking.default_timeout() == 2.5

    def test_locking_can_be_disabled(self, lock_file, monkeypatch):
        monkeypatch.setenv("CONTROL_INPUT_LOCK_DISABLED", "1")
        mutex = locking.FileMutex()
        with mutex:
            pass
        assert not lock_file.exists()


class TestReentrancy:
    def test_same_thread_can_nest(self, lock_file):
        mutex = locking.FileMutex()
        with mutex:
            with mutex:
                assert mutex.held
            assert mutex.held
        assert not mutex.held

    def test_release_without_acquire_is_an_error(self, lock_file):
        with pytest.raises(RuntimeError):
            locking.FileMutex().release()

    def test_lock_is_released_after_an_exception(self, lock_file):
        mutex = locking.FileMutex()
        with pytest.raises(ValueError):
            with mutex:
                raise ValueError("boom")
        assert not mutex.held
        with mutex:  # still acquirable
            assert mutex.held


class TestThreadConcurrency:
    def test_threads_do_not_overlap_in_the_critical_section(self, lock_file):
        """Two threads hammering the mutex must never be inside it at once."""
        mutex = locking.FileMutex()
        inside = 0
        overlaps = []
        bookkeeping = threading.Lock()

        def worker():
            nonlocal inside
            for _ in range(20):
                with mutex:
                    with bookkeeping:
                        inside += 1
                        if inside > 1:
                            overlaps.append(inside)
                    time.sleep(0.001)
                    with bookkeeping:
                        inside -= 1

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert overlaps == []
        assert inside == 0

    def test_actions_are_serialized_not_interleaved(self, lock_file):
        """Each thread's multi-step 'keystroke sequence' lands contiguously."""
        mutex = locking.FileMutex()
        transcript = []

        def agent(name: str):
            for _ in range(5):
                with mutex:
                    transcript.append(f"{name}-start")
                    time.sleep(0.002)
                    transcript.append(f"{name}-end")

        threads = [threading.Thread(target=agent, args=(n,)) for n in ("a1", "a2", "a3")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(transcript) == 30
        for start, end in zip(transcript[::2], transcript[1::2]):
            assert start.endswith("-start")
            assert end.endswith("-end")
            assert start.split("-")[0] == end.split("-")[0]

    def test_second_thread_times_out_while_first_holds_the_lock(self, lock_file):
        mutex = locking.FileMutex()
        error: list[BaseException] = []
        holding = threading.Event()
        release = threading.Event()

        def holder():
            with mutex:
                holding.set()
                release.wait(timeout=10)

        def latecomer():
            holding.wait(timeout=10)
            try:
                mutex.acquire(timeout=0.05)
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted below
                error.append(exc)

        threads = [threading.Thread(target=holder), threading.Thread(target=latecomer)]
        for t in threads:
            t.start()
        threads[1].join(timeout=10)
        release.set()
        for t in threads:
            t.join(timeout=10)

        assert len(error) == 1
        assert isinstance(error[0], locking.LockTimeout)


_CHILD = """
import os, sys, time
sys.path.insert(0, {src!r})
os.environ["CONTROL_INPUT_LOCK"] = {lock!r}
from control import locking
mutex = locking.FileMutex()
try:
    mutex.acquire(timeout=float(sys.argv[1]))
except locking.LockTimeout:
    print("TIMEOUT", flush=True)
    sys.exit(3)
print("ACQUIRED", flush=True)
time.sleep(float(sys.argv[2]))
mutex.release()
"""


def _child(lock_file, acquire_timeout: float, hold: float) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c",
         _CHILD.format(src=SRC_PATH, lock=str(lock_file)),
         str(acquire_timeout), str(hold)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


SRC_PATH = str((__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))


class TestProcessConcurrency:
    """The case that actually corrupts input: two `control` processes at once."""

    def test_second_process_waits_for_the_first(self, lock_file):
        holder = _child(lock_file, acquire_timeout=5, hold=0.6)
        assert holder.stdout.readline().strip() == "ACQUIRED"

        started = time.monotonic()
        waiter = _child(lock_file, acquire_timeout=10, hold=0)
        assert waiter.stdout.readline().strip() == "ACQUIRED"
        waited = time.monotonic() - started

        holder.wait(timeout=10)
        waiter.wait(timeout=10)
        # It had to sit out the remainder of the holder's 0.6s hold.
        assert waited > 0.2

    def test_second_process_times_out_rather_than_interleaving(self, lock_file):
        holder = _child(lock_file, acquire_timeout=5, hold=1.5)
        assert holder.stdout.readline().strip() == "ACQUIRED"

        waiter = _child(lock_file, acquire_timeout=0.1, hold=0)
        assert waiter.wait(timeout=10) == 3
        assert "TIMEOUT" in waiter.stdout.read()

        holder.wait(timeout=10)
