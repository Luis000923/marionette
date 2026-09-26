"""Privileged setup steps, non-interactively.

Almost nothing in this tool needs root (that is the point of the Wayland design:
the user session, not a daemon, sends the input). The handful of setup and repair
tasks that do are wrapped here so they never prompt on a terminal nobody is
watching.

* **POSIX** -- `sudo -S`, fed the password from the `.env` (`CONTROL_SUDO_PASSWORD`,
  see `.env.example`) over stdin. It is never an argument (every user can read
  those through `ps`), never logged, and never written to `os.environ`, so it does
  not end up in `/proc/<pid>/environ` and is not inherited by `sudo` or the command
  it runs.
* **Windows** -- there is no `sudo` to feed: elevation is the UAC prompt, which the
  user answers by clicking, and the shell's `runas` verb is what raises it.
  Nothing this tool does on Windows needs it, so `run_as_root` is there for the
  rare setup step that does, and `elevate_self` re-runs the current command
  elevated.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import platforms
from .env import EnvPermissionError, get_secret, scrubbed_environ

if platforms.IS_WINDOWS:
    from . import winapi as _win
else:
    _win = None


class SudoError(RuntimeError):
    pass


@dataclass
class SudoResult:
    returncode: int
    stdout: str
    stderr: str
    #: Windows only: ShellExecute starts the elevated process detached, so there
    #: is no stream to capture and the caller is told what actually happened.
    note: str = field(default="")

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_as_root(command: list[str], timeout: int = 30) -> SudoResult:
    """Run `command` with administrative rights.

    Raises SudoError if the password is unset, or if the .env holding it is
    readable by other users.
    """
    if _win is not None:
        return _run_elevated_windows(command, timeout)
    try:
        password = get_secret("CONTROL_SUDO_PASSWORD")
    except EnvPermissionError as exc:
        raise SudoError(str(exc)) from exc
    if not password:
        raise SudoError(
            "CONTROL_SUDO_PASSWORD is not set; copy .env.example to .env and set it"
        )
    result = subprocess.run(
        ["sudo", "-S", *command],
        input=f"{password}\n",
        capture_output=True,
        text=True,
        timeout=timeout,
        # Even a password the user exported by hand must not leak to the child.
        env=scrubbed_environ(),
    )
    return SudoResult(returncode=result.returncode, stdout=result.stdout, stderr=result.stderr)


def _run_elevated_windows(command: list[str], timeout: int) -> SudoResult:
    """Start `command` through the shell's `runas` verb, i.e. behind a UAC prompt.

    ShellExecute cannot hand back the child's output (it does not even wait for
    it), so this returns a note instead of pretending to have captured a stream.
    An already-elevated process skips the prompt and runs the command directly,
    with real output.
    """
    if is_elevated():
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                env=scrubbed_environ())
        return SudoResult(result.returncode, result.stdout, result.stderr)
    try:
        _win.open_path_elevated(command)
    except _win.Win32Error as exc:
        raise SudoError(f"elevation was refused or failed: {exc}") from exc
    return SudoResult(0, "", "", note=(
        "started elevated through the UAC prompt; it runs detached, so its output "
        "is not captured here"))


def is_elevated() -> bool:
    """True when this process already holds an administrator token."""
    if _win is None:
        return False
    return _win.process_is_elevated()


def elevate_self() -> SudoResult:
    """Re-run this very command line with administrator rights (UAC prompt)."""
    if _win is None:
        raise SudoError("elevate_self is the Windows path; on Linux use run_as_root")
    import sys

    return _run_elevated_windows([sys.executable, "-m", "control", *sys.argv[1:]], 30)


# --- narrow, purpose-built wrappers for the setup steps that genuinely need root ---
# (mirrors the manual steps documented in scripts/install.sh / install.ps1 / README)

def restart_ydotoold() -> SudoResult:
    """Restart the ydotoold system service (used when input.py can't reach the daemon)."""
    _linux_only("ydotoold", "restart the input daemon")
    return run_as_root(["systemctl", "restart", "ydotoold.service"])


def ensure_uinput_group(username: str) -> SudoResult:
    """Add `username` to the ydotoold group so ydotool works without sudo (needs re-login)."""
    _linux_only("the ydotoold group", "let the session reach /dev/uinput")
    return run_as_root(["usermod", "-aG", "ydotoold", username])


def _linux_only(what: str, why: str) -> None:
    """Fail with the Windows answer instead of a confusing "not recognized"."""
    if _win is None:
        return
    raise SudoError(
        f"{what} does not exist on Windows: nothing here needs {why}, because Windows "
        "input comes from the session itself (SendInput) and the clipboard from "
        "user32. Re-run this command from an elevated shell only if a third-party "
        "setup step asks for it."
    )
