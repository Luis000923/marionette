"""Non-interactive privilege escalation for the handful of setup tasks that need it
(e.g. managing the ydotoold service, fixing uinput group membership).

The password (CONTROL_SUDO_PASSWORD, see .env.example) is read straight from the
.env file via `env.get_secret` and piped to `sudo -S` over stdin. It is never passed
as a CLI argument (visible to every user via `ps`), never logged, and never written
to `os.environ` -- so it doesn't show up in `/proc/<pid>/environ` and isn't inherited
by sudo or the command it runs.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .env import EnvPermissionError, get_secret, scrubbed_environ


class SudoError(RuntimeError):
    pass


@dataclass
class SudoResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_as_root(command: list[str], timeout: int = 30) -> SudoResult:
    """Run `command` under `sudo -S`, feeding CONTROL_SUDO_PASSWORD via stdin.

    Raises SudoError if CONTROL_SUDO_PASSWORD is unset, or if the .env holding it
    is readable by other users.
    """
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


# --- narrow, purpose-built wrappers for the setup steps that genuinely need root ---
# (mirrors the manual steps documented in scripts/install.sh / README)

def restart_ydotoold() -> SudoResult:
    """Restart the ydotoold system service (used when input.py can't reach the daemon)."""
    return run_as_root(["systemctl", "restart", "ydotoold.service"])


def ensure_uinput_group(username: str) -> SudoResult:
    """Add `username` to the ydotoold group so ydotool works without sudo (needs re-login)."""
    return run_as_root(["usermod", "-aG", "ydotoold", username])
