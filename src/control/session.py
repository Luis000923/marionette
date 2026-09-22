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
import signal
import subprocess
import time
import uuid
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


# --- ephemeral sandboxes ------------------------------------------------------------------

#: How long windows get to close after the sandboxed command ends.
SANDBOX_CLOSE_TIMEOUT = 5.0
#: After --timeout: how long the command gets between SIGTERM and SIGKILL, and
#: how long an app gets between SIGTERM and SIGKILL once its window refused to close.
SANDBOX_TERM_GRACE = 5.0
SANDBOX_KILL_GRACE = 3.0
#: Exit status reported when --timeout stopped the command (as timeout(1) does).
TIMEOUT_EXIT = 124


def _windows_on(desktop: int) -> list[windows.Window]:
    """Windows whose home is exactly `desktop` -- never ones shown on all desktops
    (panels, docks, pinned windows), which `--desktop` queries also return."""
    found = []
    for win in windows.list_windows("", desktop=desktop):
        try:
            if win.get_desktop() == desktop:
                found.append(win)
        except windows.KdotoolError:
            continue  # gone meanwhile
    return found


def sandbox(command: list[str], name: str | None = None, keep_windows: bool = False,
            env: dict[str, str] | None = None, timeout: float | None = None) -> dict:
    """Run `command` as an agent on a throwaway virtual desktop, then tear it down.

    A fresh session (own desktop `control:<name>`, own browser profile) is
    created without switching to it; `command` runs with CONTROL_SESSION_ID set,
    so every window/app command it issues is scoped to that desktop. Afterwards
    the windows it opened there are closed and the desktop is removed.

    Only windows that did not exist before the run *and* live exactly on the
    sandbox desktop are closed. If one refuses to close (an unsaved-changes
    dialog...), the desktop is kept rather than removed -- removing it would
    move that window onto the user's desktop -- and so is the session record,
    so the desktop is never orphaned: `control session end --remove-desktop`
    finishes the job.

    With `timeout` (seconds), a command still running then is stopped in order:
    first the windows it opened are asked to close (apps get to save or ask),
    then its process group gets SIGTERM, and SIGKILL after SANDBOX_TERM_GRACE.
    The group includes apps started with gtk-launch, which runs them as its
    children. An app whose window still ignores the close is sent SIGTERM, then
    SIGKILL -- but only when that process owns no window outside the sandbox
    (a browser that opened one sandbox window keeps the user's other windows).
    """
    name = name or f"sandbox-{uuid.uuid4().hex[:6]}"
    session = init(name)
    before = set(windows.window_ids(""))
    run_env = dict(os.environ if env is None else env)
    run_env["CONTROL_SESSION_ID"] = name
    report: dict = {"name": name, "desktop": session.desktop, "desktop_name": session.desktop_name,
                    "timed_out": False}
    closed_on_timeout: list[str] = []
    try:
        report["exit_code"], report["timed_out"] = _run_command(
            command, run_env, timeout,
            on_timeout=lambda: closed_on_timeout.extend(_close_new_windows(session, before)))
    finally:
        report.update(_teardown(session, before, keep_windows, force=report["timed_out"],
                                already_closed=closed_on_timeout))
    return report


def _run_command(command: list[str], env: dict[str, str], timeout: float | None,
                 on_timeout=None) -> tuple[int, bool]:
    """(exit status, timed out). Without a timeout the command stays in our process
    group, so Ctrl+C and an interactive stdin reach it as usual. `on_timeout` runs
    before any signal is sent (the polite part of the shutdown)."""
    if timeout is None:
        return subprocess.run(command, env=env).returncode, False
    # Its own process group, so the whole tree it spawned can be signalled at once.
    proc = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        return proc.wait(timeout=timeout), False
    except subprocess.TimeoutExpired:
        try:
            if on_timeout is not None:
                on_timeout()
        finally:
            _stop_group(proc)
        return TIMEOUT_EXIT, True
    except BaseException:
        _stop_group(proc)  # Ctrl+C: its group no longer gets the terminal's SIGINT
        raise


def _signal_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _stop_group(proc: subprocess.Popen) -> None:
    """SIGTERM the command's process group, SIGKILL whatever is left after the grace."""
    if not _signal_group(proc.pid, signal.SIGTERM):
        return
    try:
        proc.wait(timeout=SANDBOX_TERM_GRACE)
    except subprocess.TimeoutExpired:
        pass
    # Also reaches children that outlived a leader which exited on SIGTERM.
    _signal_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=SANDBOX_KILL_GRACE)
    except subprocess.TimeoutExpired:
        pass


def _wait_gone(desktop: int, ids: set[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and any(w.id in ids for w in _windows_on(desktop)):
        time.sleep(0.2)


def _killable_pids(stubborn: list[windows.Window], ours: set[str]) -> set[int]:
    """Owners of `stubborn` windows that own no window outside the sandbox."""
    pids: dict[int, list[str]] = {}
    for win in stubborn:
        try:
            pid = win.pid()
        except windows.KdotoolError:
            continue
        if pid and pid not in (os.getpid(), 1):
            pids.setdefault(pid, [])
    for win_id in windows.window_ids(""):
        if win_id in ours:
            continue
        try:
            pid = windows.Window(win_id).pid()
        except windows.KdotoolError:
            continue
        if pid in pids:
            pids[pid].append(win_id)  # this process also shows something of the user's
    return {pid for pid, outside in pids.items() if not outside}


def _kill_apps(pids: set[int], desktop: int, ids: set[str]) -> list[int]:
    killed = []
    for sig, grace in ((signal.SIGTERM, SANDBOX_KILL_GRACE), (signal.SIGKILL, SANDBOX_KILL_GRACE)):
        alive = [w for w in _windows_on(desktop) if w.id in ids]
        if not alive:
            break
        for pid in pids:
            try:
                os.kill(pid, sig)
                if pid not in killed:
                    killed.append(pid)
            except (ProcessLookupError, PermissionError):
                pass
        _wait_gone(desktop, ids, grace)
    return killed


def _close_new_windows(session: Session, before: set[str]) -> list[str]:
    """Ask every window the run opened on the sandbox desktop to close; wait for them."""
    ours = [w for w in _windows_on(session.desktop) if w.id not in before]
    closed = []
    for win in ours:
        try:
            win.close()
            closed.append(win.id)
        except windows.KdotoolError:
            pass
    _wait_gone(session.desktop, {w.id for w in ours}, SANDBOX_CLOSE_TIMEOUT)
    return closed


def _teardown(session: Session, before: set[str], keep_windows: bool, force: bool = False,
              already_closed: list[str] | None = None) -> dict:
    # Windows closed when the timeout hit still count as the run's.
    closed: list[str] = list(already_closed or [])
    ours = [w for w in _windows_on(session.desktop) if w.id not in before]
    our_ids = {w.id for w in ours} | set(closed)
    killed: list[int] = []
    if not keep_windows:
        closed += _close_new_windows(session, before)
        if force:
            stubborn = [w for w in _windows_on(session.desktop) if w.id in our_ids]
            if stubborn:
                killed = _kill_apps(_killable_pids(stubborn, our_ids), session.desktop, our_ids)
    left = [w.to_dict() for w in _windows_on(session.desktop)]
    remove = not left
    if remove:
        end(session.name, remove_desktop=True)
    else:
        session.log(f"sandbox teardown left {len(left)} window(s); desktop kept")
    return {"closed": closed, "killed_pids": killed, "left": left, "desktop_removed": remove,
            "session_kept": not remove}
