"""Per-agent sessions: one virtual desktop, one browser profile, one log, one .env.

A "session" is the unit of isolation for running several agents in parallel on a
single Plasma desktop. Each one pins:

* a KWin virtual desktop (`control:<name>` unless told otherwise), so window
  commands can be scoped with `--desktop` and agents never see each other's
  windows;
* a state directory under `~/.local/share/control/sessions/<name>/`, holding a
  `session.json` record, a dedicated Chromium profile and a log file.

Sessions are picked up implicitly from `CONTROL_SESSION_ID`, so a terminal can
`export CONTROL_SESSION_ID=agent_1` once and every later `control` command in
that shell stays inside agent 1's desktop and profile.

Input is *not* isolated per session -- uinput is global hardware, so all agents
serialize on the single lock in locking.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import windows
from .locking import FileMutex

DEFAULT_STATE_DIR = Path.home() / ".local/share/control/sessions"
#: Prefix for auto-created virtual desktops, so `desktop list` shows who owns what.
DESKTOP_PREFIX = "control:"
_RECORD_NAME = "session.json"


class SessionError(RuntimeError):
    pass


def _allocation_lock() -> FileMutex:
    """Serializes desktop allocation between orchestrator processes.

    Two agents initialised at once for the same name would otherwise both see
    "no such desktop" and each create one. Kept separate from the input lock so
    session bookkeeping never delays another agent's keystrokes.
    """
    return FileMutex(state_dir() / ".allocate.lock", can_disable=False)


def state_dir() -> Path:
    """Root for session state (override with CONTROL_STATE_DIR, mostly for tests)."""
    return Path(os.environ.get("CONTROL_STATE_DIR") or DEFAULT_STATE_DIR)


def session_dir(name: str) -> Path:
    return state_dir() / name


def current_name() -> str | None:
    """Session this process belongs to, from CONTROL_SESSION_ID (None if unset)."""
    return os.environ.get("CONTROL_SESSION_ID") or None


def desktop_name_for(name: str) -> str:
    return f"{DESKTOP_PREFIX}{name}"


@dataclass
class Session:
    name: str
    desktop: int | None = None       # 1-based KWin desktop number
    desktop_id: str = ""             # KWin UUID; survives renumbering
    desktop_name: str = ""
    created_at: float = field(default_factory=time.time)
    pid: int = 0

    @property
    def dir(self) -> Path:
        return session_dir(self.name)

    @property
    def record_path(self) -> Path:
        return self.dir / _RECORD_NAME

    @property
    def profile_dir(self) -> Path:
        """Chromium profile, so agents don't fight over one set of cookies/logins."""
        return self.dir / "browser-profile"

    @property
    def log_file(self) -> Path:
        return self.dir / "control.log"

    @property
    def env_file(self) -> Path:
        """Optional per-session .env, loaded automatically when this session is active."""
        return self.dir / ".env"

    def to_dict(self) -> dict:
        data = asdict(self)
        data.update(
            dir=str(self.dir),
            profile_dir=str(self.profile_dir),
            log_file=str(self.log_file),
            env_file=str(self.env_file) if self.env_file.is_file() else None,
        )
        return data

    def save(self) -> "Session":
        self.dir.mkdir(parents=True, exist_ok=True)
        self.record_path.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        return self

    def log(self, message: str) -> None:
        """Append a line to this session's own log, so parallel agents don't interleave."""
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self.log_file.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} [{self.name}] {message}\n")


def load(name: str) -> Session | None:
    record = session_dir(name) / _RECORD_NAME
    if not record.is_file():
        return None
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    fields = {f for f in Session.__dataclass_fields__}
    return Session(**{k: v for k, v in data.items() if k in fields})


def list_sessions() -> list[Session]:
    root = state_dir()
    if not root.is_dir():
        return []
    found = (load(child.name) for child in sorted(root.iterdir()) if child.is_dir())
    return [s for s in found if s is not None]


def init(name: str, desktop: int | str | None = None, desktop_name: str | None = None,
         activate: bool = False) -> Session:
    """Prepare (or re-attach to) the workspace for agent `name`.

    Idempotent: re-running it reuses the existing desktop and profile instead of
    creating a second one, so an orchestrator can call it unconditionally on
    every agent start.

    `desktop` pins an existing desktop number, or names one (created if
    missing); otherwise a desktop named `control:<name>` is created on demand.
    """
    if not name or "/" in name or name.startswith("."):
        raise SessionError(f"invalid session name {name!r}")

    with _allocation_lock():
        if desktop is not None and str(desktop).isdigit():
            target = windows.find_desktop(int(desktop))
            if target is None:
                raise SessionError(f"no virtual desktop number {desktop}")
        elif desktop is not None:
            target = windows.ensure_desktop(str(desktop))
        else:
            target = windows.ensure_desktop(desktop_name or desktop_name_for(name))

        existing = load(name)
        session = Session(
            name=name,
            desktop=target.number,
            desktop_id=target.id,
            desktop_name=target.name,
            created_at=existing.created_at if existing else time.time(),
            pid=os.getpid(),
        )
        session.save()
    session.profile_dir.mkdir(parents=True, exist_ok=True)
    session.log(f"session init on desktop {target.number} ({target.name!r})")
    if activate:
        windows.set_desktop(target.number)
    return session


def end(name: str, remove_desktop: bool = False) -> None:
    """Forget a session. Its browser profile and log are kept for inspection."""
    session = load(name)
    if session is None:
        raise SessionError(f"no such session {name!r}")
    if remove_desktop and session.desktop_name:
        target = windows.find_desktop(session.desktop_name)
        if target is not None:
            windows.remove_desktop(target.name)
    session.record_path.unlink(missing_ok=True)


def _list_live_desktops() -> list[windows.Desktop]:
    """Indirection so tests can keep session lookups off the real KWin."""
    return windows.list_desktops()


def current_desktop_number(session: Session) -> int | None:
    """The session's desktop number *now*, re-resolved through its KWin UUID.

    Numbers are positions: when another agent removes a desktop before ours,
    KWin renumbers everything after it, and a stale number would send this
    agent's windows onto someone else's desktop. The UUID doesn't move, so we
    look it up and refresh the record when the number drifted. If KWin can't be
    asked at all, the recorded number is the best we have.
    """
    if not session.desktop_id:
        return session.desktop
    try:
        desktops = _list_live_desktops()
    except (windows.DesktopError, OSError, subprocess.SubprocessError):
        return session.desktop
    match = next((d for d in desktops if d.id == session.desktop_id), None)
    if match is None:
        raise SessionError(
            f"desktop {session.desktop_name!r} of session {session.name!r} no longer "
            f"exists; recreate it with: control session init --name {session.name}"
        )
    if match.number != session.desktop:
        session.log(f"desktop renumbered {session.desktop} -> {match.number}")
        session.desktop = match.number
        session.save()
    return match.number


def resolve_desktop(explicit: int | None = None, name: str | None = None) -> int | None:
    """Which desktop a command should act on.

    Explicit `--desktop` wins; otherwise the active session's desktop; otherwise
    None, meaning "every desktop" -- the pre-existing, un-scoped behaviour.
    """
    if explicit is not None:
        return explicit
    session_name = name or current_name()
    if not session_name:
        return None
    session = load(session_name)
    return current_desktop_number(session) if session else None


async def status_async(include_windows: bool = True) -> dict:
    """Snapshot of who owns what: sessions, their desktops, and their windows.

    Queries run concurrently and never take the input lock, so an orchestrator
    can poll this while agents are busy typing.
    """
    sessions = list_sessions()
    active = current_name()

    desktops_task = windows.list_desktops_async()
    if include_windows:
        window_tasks = [
            windows.search_async("", desktop=s.desktop) if s.desktop else _no_windows()
            for s in sessions
        ]
        results = await asyncio.gather(desktops_task, *window_tasks)
        desktops, per_session = results[0], results[1:]
    else:
        desktops, per_session = await desktops_task, [[] for _ in sessions]

    by_number = {d.number: d for d in desktops}
    entries = []
    for session, wins in zip(sessions, per_session):
        desktop = by_number.get(session.desktop) if session.desktop else None
        entry = session.to_dict()
        entry.update(
            active=session.name == active,
            desktop_exists=desktop is not None,
            windows=[
                {"id": w.id, "title": w.title, "class": w.classname} for w in wins
            ],
        )
        entries.append(entry)
    return {
        "active_session": active,
        "sessions": entries,
        "desktops": [
            {"number": d.number, "position": d.position, "id": d.id, "name": d.name}
            for d in desktops
        ],
    }


async def _no_windows() -> list:
    return []


def status(include_windows: bool = True) -> dict:
    return asyncio.run(status_async(include_windows=include_windows))
