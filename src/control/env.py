"""Minimal .env loader (KEY=VALUE lines, `#` comments, blank lines ignored).

No extra dependency: this is a lightweight CLI tool and the format we need
(simple KEY=VALUE pairs, optionally quoted) doesn't warrant python-dotenv.

Two security rules live here:

* **Secrets never reach `os.environ`.** Keys in `SECRET_KEYS` (the elevation
  password) are skipped by `load_env()`; anything that needs one asks
  `get_secret()`, which reads the file on demand. Otherwise it would sit in this
  process's environment -- `/proc/<pid>/environ` on Linux, inherited by every
  child on Windows -- and reach the GUI apps and Chromium we start.
* **Permissive files are flagged.** On POSIX that is the mode bits: an .env
  readable by group/others triggers a warning, and a secret is never read from
  such a file (nor from one owned by another user). On Windows mode bits say
  nothing (every file reports 0666 whatever its ACLs are), so the check is
  whether the file lives in the user's own profile, which is private by
  default. `CONTROL_ENV_STRICT=1` turns the warning into a hard error.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

from . import platforms

# repo root: src/control/env.py -> src/control -> src -> repo root
_DEFAULT_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"

#: Keys that are only ever handed out by `get_secret()`, never exported.
SECRET_KEYS = frozenset({"CONTROL_SUDO_PASSWORD"})

#: Files loaded by `load_env()` in this process, most important first, so
#: `get_secret()` honours the same precedence as ordinary variables.
_loaded_paths: list[Path] = []
_warned: set[Path] = set()


class EnvPermissionError(PermissionError):
    """An .env file is too widely readable (or not ours) to be trusted."""


def default_path() -> Path:
    return _DEFAULT_ENV_PATH


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def strict_mode() -> bool:
    return os.environ.get("CONTROL_ENV_STRICT", "").lower() in ("1", "true", "yes")


def permission_problem(path: Path) -> str | None:
    """Why `path` is unsafe to hold secrets, or None if it's fine (0600, ours)."""
    st = path.stat()
    if platforms.IS_WINDOWS:
        return _windows_permission_problem(path)
    if st.st_uid != os.getuid():
        return f"{path} is owned by uid {st.st_uid}, not by you"
    if st.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} has mode {stat.S_IMODE(st.st_mode):04o}; fix it with: chmod 600 {path}"
    return None


def _windows_permission_problem(path: Path) -> str | None:
    """A file is private on Windows when it sits in the user's own profile.

    Every Windows file reports mode 0666 regardless of its ACLs, so the mode bits
    would either reject every .env or none. What actually decides it is where the
    file is: `%USERPROFILE%` and below inherit an ACL that only the user (and
    administrators) can read, while anything in a shared or world-writable place
    (a repo under `C:\\Users\\Public`, a synced folder) is not private at all.
    """
    try:
        home = Path(os.environ.get("USERPROFILE") or Path.home()).resolve()
        resolved = path.resolve()
    except OSError:  # pragma: no cover - a path we cannot even resolve
        return f"{path} cannot be resolved, so its privacy cannot be checked"
    if resolved == home or home in resolved.parents:
        return None
    return (f"{path} is outside your user profile, where it is not private; move it to "
            f"{home} or keep secrets out of it")


def _check_permissions(path: Path) -> None:
    problem = permission_problem(path)
    if problem is None:
        return
    if strict_mode():
        raise EnvPermissionError(problem)
    if path not in _warned:
        _warned.add(path)
        print(f"[WARN] insecure env file: {problem}", file=sys.stderr)


def load_env(path: Path | str | None = None, override: bool = False) -> dict[str, str]:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Silently does nothing if the file doesn't exist. Returns the parsed values
    that are *not* secret; `SECRET_KEYS` stay in the file (see `get_secret`).
    """
    env_path = Path(path) if path is not None else _DEFAULT_ENV_PATH
    if not env_path.is_file():
        return {}
    _check_permissions(env_path)
    resolved = env_path.resolve()
    if resolved not in _loaded_paths:
        # Loads happen in decreasing precedence (--env-file, session, repo);
        # `override=True` means "this one beats everything loaded before".
        if override:
            _loaded_paths.insert(0, resolved)
        else:
            _loaded_paths.append(resolved)
    values = {
        k: v for k, v in parse_env(env_path.read_text(encoding="utf-8")).items()
        if k not in SECRET_KEYS
    }
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
    return values


def get_secret(key: str, paths: list[Path | str] | None = None) -> str | None:
    """Read a secret without ever writing it to os.environ.

    Looks at a variable the user exported themselves first, then at the .env
    files this process loaded (same precedence), then at the repo .env. A file
    that fails the permission check is refused outright for secrets, even
    outside strict mode: a world-readable password is already leaked.
    """
    if os.environ.get(key):
        return os.environ[key]
    candidates = [Path(p) for p in paths] if paths is not None else [
        *_loaded_paths, _DEFAULT_ENV_PATH,
    ]
    for path in candidates:
        if not path.is_file():
            continue
        values = parse_env(path.read_text(encoding="utf-8"))
        if not values.get(key):
            continue
        problem = permission_problem(path)
        if problem is not None:
            raise EnvPermissionError(f"refusing to read {key}: {problem}")
        return values[key]
    return None


def scrubbed_environ() -> dict[str, str]:
    """os.environ minus secrets, for subprocesses that must not inherit them."""
    return {k: v for k, v in os.environ.items() if k not in SECRET_KEYS}


def _reset_for_tests() -> None:
    _loaded_paths.clear()
    _warned.clear()
