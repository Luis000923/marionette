"""Non-interactive privilege escalation for the handful of setup tasks that need it
(e.g. managing the ydotoold service, fixing uinput group membership).

The password is read from the CONTROL_SUDO_PASSWORD env var (see .env.example) and
piped to `sudo -S` over stdin -- it is never passed as a CLI argument or logged, since
CLI arguments are visible to any other user via `ps`.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .env import load_env


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

    Raises SudoError if CONTROL_SUDO_PASSWORD is unset (loading .env first if present).
    """
    load_env()
    password = os.environ.get("CONTROL_SUDO_PASSWORD")
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
