"""Tests for the native Windows backend: platforms, winapi, the UI Automation
bridge, the PNG writer and the app catalog -- the code that only exists on
Windows (the POSIX-only tests live in test_windows.py, test_input.py, test_native.py).

Nothing here drives the desktop: the live calls are read-only (enumerate windows,
read a rectangle, read a process name) and the one test that writes to the
clipboard puts back what was there. Tests that need a window to exist skip
themselves when the runner has none, so the file is safe on a headless agent.
"""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import zlib

import pytest

from control import _png, apps, atspi, cli, env, input as inp, platforms, screenshot
from control import session as session_mod, sudo, windows
from control import winapi

pytestmark = pytest.mark.skipif(
    not platforms.IS_WINDOWS, reason="native Windows backend (Windows only)")

PLATFORM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(platforms.__file__)))

#: A second `control` process that takes the input lock and holds it, which is the
#: only way to prove the lock excludes *other* processes.
CHILD_HOLDS_LOCK = """
import sys, time
sys.path.insert(0, {platform_dir!r})
from control import locking
with locking.FileMutex():
    print("holding", flush=True)
    time.sleep(10)
"""


def foreground() -> int:
    """The foreground window handle, or 0 when this session has no desktop."""
    return winapi.foreground_window()


def needs_foreground() -> bool:
    return bool(foreground())


def has_desktops() -> bool:
    return winapi.virtual_desktops_supported()


def uia_usable() -> bool:
    if atspi._uia is None:
        return False
    usable, _why = atspi._uia.available()
    return usable


def uia_windows() -> bool:
    """True when UI Automation is up and at least one window publishes a tree."""
    return bool(uia_usable() and atspi._uia._app_windows())


# --- platforms: where state lives, per platform ----------------------------------------


class TestPlatformPaths:
    def test_windows_is_detected(self):
        assert platforms.IS_WINDOWS is True
        assert platforms.IS_POSIX is False

    def test_data_home_is_the_per_user_local_appdata(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
        assert platforms.data_home() == tmp_path / "AppData" / "Local"

    def test_data_home_falls_back_to_the_profile(self, monkeypatch):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
        assert platforms.data_home() == Path_home() / "AppData" / "Local"

    def test_runtime_dir_is_the_temp_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TEMP", str(tmp_path / "Temp"))
        assert platforms.runtime_dir() == tmp_path / "Temp"

    def test_state_dir_is_a_control_subdirectory(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert platforms.state_dir() == tmp_path / "control"

    def test_paths_read_with_backslashes(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Datos"))
        rendered = str(platforms.state_dir() / "sessions" / "agent_1")
        assert "\\" in rendered and "/" not in rendered

    def test_the_session_state_lives_under_the_state_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONTROL_STATE_DIR", str(tmp_path / "sessions"))
        assert session_mod.state_dir() == tmp_path / "sessions"
        assert session_mod.DEFAULT_STATE_DIR == platforms.state_dir() / "sessions"

    def test_screenshots_land_in_the_scratch_dir_by_default(self, monkeypatch):
        monkeypatch.delenv("CONTROL_SESSION_ID", raising=False)
        assert screenshot.DEFAULT_DIR == platforms.scratch_dir("control-screenshots")
        assert screenshot.output_dir() == screenshot.DEFAULT_DIR


def Path_home():
    from pathlib import Path

    return Path.home()


# --- the cross-process lock -------------------------------------------------------------


class TestWindowsFileLock:
    def test_the_lock_file_gains_the_byte_msvcrt_locks(self, tmp_path, monkeypatch):
        from control import locking

        monkeypatch.setenv("CONTROL_INPUT_LOCK", str(tmp_path / "input.lock"))
        with locking.FileMutex():
            # msvcrt.locking() only locks a range that exists, so the mutex keeps
            # the first byte around; a file that stayed empty would not be a lock.
            assert locking.lock_path().stat().st_size == 1

    def test_another_process_cannot_take_the_lock_at_the_same_time(self, tmp_path, monkeypatch):
        from control import locking

        monkeypatch.setenv("CONTROL_INPUT_LOCK", str(tmp_path / "input.lock"))
        child = subprocess.Popen(
            [sys.executable, "-c", CHILD_HOLDS_LOCK.format(platform_dir=PLATFORM_DIR)],
            stdout=subprocess.PIPE, text=True, cwd=str(tmp_path))
        try:
            assert child.stdout.readline().strip() == "holding", "the child never took it"
            with pytest.raises(locking.LockTimeout):
                locking.FileMutex().acquire(timeout=0.3)
        finally:
            child.terminate()
            child.wait(timeout=10)

    def test_the_lock_is_free_again_once_released(self, tmp_path, monkeypatch):
        from control import locking

        monkeypatch.setenv("CONTROL_INPUT_LOCK", str(tmp_path / "input.lock"))
        with locking.FileMutex():
            pass
        with locking.FileMutex(timeout=1):  # nobody is holding it any more
            pass

    def test_locking_can_still_be_turned_off(self, tmp_path, monkeypatch):
        from control import locking

        monkeypatch.setenv("CONTROL_INPUT_LOCK", str(tmp_path / "input.lock"))
        monkeypatch.setenv("CONTROL_INPUT_LOCK_DISABLED", "1")
        with locking.FileMutex():
            pass
        assert not (tmp_path / "input.lock").exists()


# --- winapi: the ctypes layer, read-only ------------------------------------------------


class TestWinApi:
    def test_the_foreground_window_is_a_live_handle(self):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        assert winapi.window_exists(hwnd)
        assert isinstance(winapi.window_text(hwnd), str)
        assert isinstance(winapi.window_class(hwnd), str)
        assert winapi.window_pid(hwnd) > 0

    def test_geometry_comes_back_as_a_rectangle(self):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        rect = winapi.window_rect(hwnd)
        assert rect.width > 0 and rect.height > 0
        assert rect.as_dict() == {"x": rect.left, "y": rect.top,
                                  "width": rect.width, "height": rect.height}
        assert "w=" in repr(rect)

    def test_the_virtual_screen_covers_at_least_one_monitor(self):
        assert winapi.virtual_screen_rect().width > 0
        assert winapi.virtual_screen_rect().height > 0

    def test_the_cursor_can_be_read(self):
        position = winapi.cursor_pos()
        assert position is None or (isinstance(position[0], int) and position[0] >= 0)

    def test_this_process_is_named_after_its_executable(self):
        assert winapi.process_name(os.getpid()) == "python"
        assert winapi.process_path(os.getpid()).lower().endswith(".exe")

    def test_unknown_processes_answer_empty_and_stay_answered(self):
        missing = 0x7FFFFFF0  # no such pid
        assert winapi.process_name(missing) == ""
        assert winapi.process_name(missing) == ""  # cached: no second syscall

    def test_this_process_is_alive_and_a_bogus_pid_is_not(self):
        assert winapi.process_alive(os.getpid())
        assert not winapi.process_alive(0x7FFFFFF0)

    def test_the_virtual_key_table_covers_every_key_name(self):
        missing = sorted(name for name in inp.KEYCODES if name not in winapi.VK)
        assert missing == []

    def test_extended_keys_are_the_ones_windows_needs_them_for(self):
        for name in ("enter", "delete", "up", "down", "left", "right", "kpenter"):
            assert name in winapi._EXTENDED_KEYS, name
        assert "a" not in winapi._EXTENDED_KEYS

    def test_numpad_enter_is_enter_plus_the_extended_flag(self):
        # SendInput has no numpad-Enter virtual key; the flag is what tells
        # KP_Enter from Return.
        assert winapi.VK["kpenter"] == winapi.VK["enter"]

    def test_acting_on_a_window_that_does_not_exist_is_a_typed_error(self):
        for call in (winapi.close_window, winapi.activate, winapi.capture_window,
                     winapi.window_rect):
            with pytest.raises(winapi.Win32Error):
                call(0x7FFFFFF0)

    def test_elevating_nothing_is_an_error_not_a_crash(self):
        with pytest.raises(winapi.Win32Error):
            winapi.open_path_elevated([])

    def test_errors_name_the_api_and_the_reason(self):
        error = winapi.Win32Error("boom", api="SetForegroundWindow", code=5)
        assert error.api == "SetForegroundWindow" and error.code == 5
        assert "SetForegroundWindow" in str(winapi._error("SetForegroundWindow"))

    def test_dpi_awareness_is_on_so_coordinates_are_physical_pixels(self):
        assert winapi.os_version()[0] >= 10  # and enabling it must not have raised

    def test_the_structs_have_the_layout_win32_expects(self):
        assert ctypes_sizeof(winapi.INPUT) >= 40  # x64: type + padding + union
        assert ctypes_sizeof(winapi.KEYBDINPUT) >= 24
        assert ctypes_sizeof(winapi.DROPFILES) == 20


def ctypes_sizeof(structure) -> int:
    import ctypes

    return ctypes.sizeof(structure)


# --- activation ---------------------------------------------------------------------------

class _FakeDesktop:
    """Just enough of a window manager to record what activate() asks for.

    `focus_after` is what the target thread's focus looks like once the window
    has been activated -- Windows resets it to the top-level window, and only a
    framework that remembers its own last child puts it back.
    """

    def __init__(self, focus_before: int, focus_after: int):
        self.focus = focus_before
        self.focus_after = focus_after
        self.foreground = 0x100
        self.raised: list[int] = []
        self.focused: list[int] = []
        self.attached: list[bool] = []

    def foreground_window(self) -> int:
        return self.foreground

    def set_foreground(self, hwnd) -> None:
        self.foreground = winapi._hwnd(hwnd)
        self.focus = self.focus_after

    def restore_focus(self, hwnd) -> None:
        self.focused.append(winapi._hwnd(hwnd))
        self.focus = winapi._hwnd(hwnd)

    def roots_to(self, root: int):
        return lambda hwnd, flag: root


def _stub_activate(monkeypatch, desktop: _FakeDesktop, target: int = 0x200) -> None:
    import ctypes

    monkeypatch.setattr(winapi, "window_exists", lambda hwnd: True)
    monkeypatch.setattr(winapi, "IsIconic", lambda hwnd: False)
    monkeypatch.setattr(winapi, "ShowWindow", lambda hwnd, how: None)
    monkeypatch.setattr(winapi, "foreground_window", desktop.foreground_window)
    monkeypatch.setattr(winapi, "SetForegroundWindow",
                        lambda hwnd: desktop.set_foreground(hwnd))
    monkeypatch.setattr(winapi, "BringWindowToTop",
                        lambda hwnd: desktop.raised.append(winapi._hwnd(hwnd)))
    monkeypatch.setattr(winapi, "SetFocus", lambda hwnd: desktop.restore_focus(hwnd))
    monkeypatch.setattr(winapi, "_thread_focus", lambda thread: desktop.focus)
    monkeypatch.setattr(winapi, "GetCurrentThreadId", lambda: 1)
    monkeypatch.setattr(winapi, "GetWindowThreadProcessId",
                        lambda hwnd, pid: 0x200 if winapi._hwnd(hwnd) == target else 0x100)
    monkeypatch.setattr(winapi, "GetAncestor", desktop.roots_to(target))
    monkeypatch.setattr(winapi, "AttachThreadInput",
                        lambda a, b, attach: desktop.attached.append(bool(attach)))
    assert ctypes.sizeof(winapi.GUITHREADINFO) >= 72  # x64: 2 DWORDs + 6 HWNDs + RECT


class TestActivateKeepsTheFocus:
    """`type --window` is only worth anything if the keys reach the focused child.

    Windows resets a thread's focus to its top-level window every time that
    window is activated. A bare Win32 app never puts the child back, so
    activate() reads the focus before and restores it after -- without that,
    typing works on a text field until that field happens to be what was
    focused, and then the keystrokes vanish into the window procedure.
    """

    def test_a_child_that_lost_the_focus_gets_it_back(self, monkeypatch):
        desktop = _FakeDesktop(focus_before=0x301, focus_after=0x200)
        _stub_activate(monkeypatch, desktop)
        assert winapi.activate(0x200) is True
        assert desktop.focused == [0x301]

    def test_an_app_that_restores_its_own_focus_is_left_alone(self, monkeypatch):
        desktop = _FakeDesktop(focus_before=0x301, focus_after=0x301)
        _stub_activate(monkeypatch, desktop)
        assert winapi.activate(0x200) is True
        assert desktop.focused == []

    def test_a_focus_belonging_to_another_window_is_not_stolen(self, monkeypatch):
        desktop = _FakeDesktop(focus_before=0x301, focus_after=0x200)
        _stub_activate(monkeypatch, desktop)
        monkeypatch.setattr(winapi, "GetAncestor", _FakeDesktop(0, 0).roots_to(0x999))
        winapi.activate(0x200)
        assert desktop.focused == []

    def test_no_remembered_focus_is_not_a_reason_to_fail(self, monkeypatch):
        desktop = _FakeDesktop(focus_before=0, focus_after=0x200)
        _stub_activate(monkeypatch, desktop)
        assert winapi.activate(0x200) is True
        assert desktop.focused == []

    def test_the_window_still_comes_to_the_front_first(self, monkeypatch):
        desktop = _FakeDesktop(focus_before=0x301, focus_after=0x200)
        _stub_activate(monkeypatch, desktop)
        winapi.activate(0x200)
        assert desktop.raised == [0x200]
        assert desktop.foreground == 0x200

    def test_the_input_queues_are_joined_and_always_released(self, monkeypatch):
        desktop = _FakeDesktop(focus_before=0x301, focus_after=0x200)
        _stub_activate(monkeypatch, desktop)
        winapi.activate(0x200)
        # both the foreground thread (to unlock the focus-stealing rule) and the
        # target thread (so SetFocus reaches the child) are joined, then released
        assert desktop.attached == [True, True, False, False]

    def test_a_failure_still_releases_the_input_queues(self, monkeypatch):
        desktop = _FakeDesktop(focus_before=0x301, focus_after=0x200)
        _stub_activate(monkeypatch, desktop)

        def boom(hwnd):
            raise OSError("SetForegroundWindow went wrong")
        monkeypatch.setattr(winapi, "SetForegroundWindow", boom)
        with pytest.raises(OSError):
            winapi.activate(0x200)
        assert desktop.attached == [True, True, False, False]

    def test_a_window_that_is_already_in_front_is_left_completely_alone(self, monkeypatch):
        desktop = _FakeDesktop(focus_before=0x301, focus_after=0x200)
        _stub_activate(monkeypatch, desktop)
        desktop.foreground = 0x200
        assert winapi.activate(0x200) is True
        assert desktop.raised == [] and desktop.focused == [] and desktop.attached == []

    def test_activating_the_foreground_window_really_puts_it_in_front(self):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no desktop in this session")
        assert winapi.activate(hwnd) is True
        assert winapi.foreground_window() == hwnd


class TestNothingFocused:
    """A session where nothing holds the foreground (locked screen, or the focused
    window was destroyed) is a real state, and Windows refuses to give the
    foreground to anyone in it -- so the tool has to name it, not blame
    focus-stealing prevention."""

    def test_anything_focused_agrees_with_the_foreground_window(self, monkeypatch):
        for hwnd in (0, 0x200):
            monkeypatch.setattr(winapi, "foreground_window", lambda: hwnd)
            assert windows.anything_focused() is bool(hwnd)

    def test_the_active_window_error_says_what_to_do_about_it(self, monkeypatch):
        monkeypatch.setattr(winapi, "foreground_window", lambda: 0)
        with pytest.raises(winapi.Win32Error) as caught:
            windows.active_window()
        message = str(caught.value)
        assert "click any window once" in message
        assert "locked" in message, "a locked screen is the other way to get here"

    def test_the_active_window_is_described_when_something_is_focused(self, monkeypatch):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        assert windows.active_window().id == str(hwnd)


# --- windows.py on top of winapi --------------------------------------------------------


class TestWindowsApi:
    def test_every_window_has_a_string_id_and_an_executable(self):
        for window in windows.list_windows():
            assert window.id.isdigit()
            assert window.classname
            assert window.process, "Windows can always name the owning executable"

    def test_the_search_text_includes_the_executable(self):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        window = windows._win_describe(hwnd)
        assert window.process in window.match_text()

    def test_a_window_can_be_found_by_its_own_id(self):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        assert windows.find_one(str(hwnd)) is not None

    def test_a_pattern_matches_the_title_the_class_or_the_executable(self):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        window = windows._win_describe(hwnd)
        for pattern in (window.title[:12], window.classname, window.process):
            if pattern:
                assert windows.search(pattern, limit=1), pattern

    def test_searching_for_something_absent_returns_nothing(self):
        assert windows.search("definitely-not-a-window-xyz-42") == []

    def test_the_shell_windows_are_not_listed_as_apps(self):
        for window in windows.list_windows():
            assert not winapi.is_shell_window(int(window.id))

    def test_a_pattern_that_is_not_a_regex_still_matches(self):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        title = winapi.window_text(hwnd)
        if len(title) < 4:
            pytest.skip("the foreground window has too short a title")
        assert windows.search(f"[{title[:5]}", limit=1) is not None

    def test_the_json_shape_has_a_process_only_where_there_is_one(self):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        data = windows._win_describe(hwnd).to_dict()
        assert set(data) == {"id", "title", "class", "desktop", "process"}
        # On Linux the same dict has no "process" key at all, so the JSON shape
        # only grows where there is something new to report.
        assert set(windows.Window(id="1", title="t", classname="c").to_dict()) == {
            "id", "title", "class", "desktop"}

    @pytest.mark.skipif(has_desktops(), reason="this Windows does have virtual desktops")
    def test_without_virtual_desktops_there_is_exactly_one(self):
        assert len(windows.list_desktops()) == 1
        assert windows.num_desktops() == 1
        assert windows.current_desktop() == 1
        assert windows.virtual_desktops_available() is False
        assert windows.virtual_desktops_hint()

    @pytest.mark.skipif(not has_desktops(), reason="this Windows has no virtual desktops")
    def test_with_virtual_desktops_they_are_1_based_and_have_ids(self):
        desktops = windows.list_desktops()
        assert desktops and all(desktop.id for desktop in desktops)
        assert desktops[0].number == desktops[0].position + 1

    def test_every_window_reports_a_known_desktop(self):
        hwnd = foreground()
        if not hwnd:
            pytest.skip("no foreground window in this session")
        assert windows._win_desktop_of(hwnd) in (1, None)

    def test_a_window_cannot_be_sent_to_a_desktop_that_does_not_exist(self):
        window = windows.Window(id="123", title="x", classname="y")
        with pytest.raises((windows.DesktopError, windows.Win32Error)):
            window.move_to_desktop(99)

    def test_windows_cannot_pin_a_window_to_every_desktop(self):
        with pytest.raises(windows.Win32Error, match="every virtual desktop"):
            windows.Window(id="123").move_to_desktop("all")

    def test_an_unknown_desktop_name_is_refused(self):
        with pytest.raises(windows.DesktopError):
            windows.resolve_desktop("no-such-desktop-anywhere")

    def test_the_current_desktop_is_inside_the_range_that_exists(self):
        assert 1 <= windows.current_desktop() <= windows.num_desktops()


# --- the PNG writer ---------------------------------------------------------------------


class TestPngWriter:
    RGB = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255, 0, 0, 0, 255, 255, 255])

    def test_a_tiny_image_encodes_to_a_png(self):
        blob = _png.encode(3, 2, self.RGB)
        assert blob.startswith(b"\x89PNG\r\n\x1a\n")
        assert b"IHDR" in blob and b"IDAT" in blob
        assert blob.endswith(_png._chunk(b"IEND", b""))

    def test_the_pixels_survive_the_round_trip(self):
        blob = _png.encode(3, 1, self.RGB[:9])
        start = blob.index(b"IDAT") + 4
        length = struct.unpack(">I", blob[start - 8:start - 4])[0]
        assert zlib.decompress(blob[start:start + length]) == b"\x00" + self.RGB[:9]

    def test_the_header_carries_the_geometry(self):
        blob = _png.encode(3, 2, self.RGB)
        width, height, depth, color_type = struct.unpack(">IIBB", blob[16:26])
        assert (width, height, depth, color_type) == (3, 2, 8, 2)  # 8-bit truecolour

    def test_every_chunk_carries_its_own_crc(self):
        blob = _png.encode(1, 1, b"\x00\x00\x00")
        offset = 8
        while offset < len(blob):
            length = struct.unpack(">I", blob[offset:offset + 4])[0]
            body = blob[offset + 4:offset + 8 + length]
            stored = struct.unpack(">I", blob[offset + 8 + length:offset + 12 + length])[0]
            assert stored == zlib.crc32(body) & 0xFFFFFFFF, blob[offset + 4:offset + 8]
            offset += 12 + length

    def test_a_wrong_buffer_size_is_refused(self):
        with pytest.raises(ValueError, match="expected"):
            _png.encode(2, 2, b"\x00" * 3)


# --- apps: the Start Menu catalog ---------------------------------------------------------


class TestWindowsApps:
    @pytest.fixture
    def menu(self, tmp_path, monkeypatch):
        base = tmp_path / "Programs"
        (base / "Google" / "Chrome").mkdir(parents=True)
        (base / "Google" / "Chrome" / "Chrome.lnk").write_bytes(b"")
        (base / "Notepad.lnk").write_bytes(b"")
        (base / "Vim - Shortcut.lnk").write_bytes(b"")
        monkeypatch.setattr(apps, "_app_dirs", lambda: [base])
        return base

    def test_the_start_menu_directories_are_where_windows_keeps_them(self, monkeypatch, tmp_path):
        monkeypatch.setenv("APPDATA", str(tmp_path / "user"))
        monkeypatch.setenv("ProgramData", str(tmp_path / "all"))
        dirs = apps._app_dirs()
        assert dirs[0] == tmp_path / "user" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        assert dirs[1] == tmp_path / "all" / "Microsoft" / "Windows" / "Start Menu" / "Programs"

    def test_ids_come_from_the_shortcut_path(self, menu):
        catalog = {app.app_id: app for app in apps.list_apps()}
        assert "notepad" in catalog
        # Google\Chrome\Chrome.lnk: the folder repeating the shortcut would make
        # the id "google.chrome.chrome", and nobody would ever type that.
        assert "google.chrome" in catalog
        assert catalog["google.chrome"].name == "Chrome"

    def test_a_shortcut_suffixed_copy_is_named_like_the_original(self, menu):
        assert "Vim" in {app.name for app in apps.list_apps()}

    def test_the_catalog_is_sorted_by_display_name(self, menu):
        names = [app.name for app in apps.list_apps()]
        assert names == sorted(names, key=str.lower)

    def test_a_suggestion_is_offered_for_a_wrong_id(self, menu):
        assert "google.chrome" in apps.suggest_ids("chrome")
        assert "google.chrome" in apps.suggest_ids("chorme")

    def test_a_lnk_suffix_does_not_invent_a_phantom_suggestion(self, menu):
        # "notepad.lnk" is the id that was typed: nothing else to suggest.
        assert apps.suggest_ids("notepad.lnk") == []

    def test_an_unknown_id_fails_with_a_hint_and_nothing_is_launched(self, monkeypatch):
        monkeypatch.setattr(apps, "_resolve_windows_app", lambda app_id: None)
        with pytest.raises(apps.AppLaunchError) as exc:
            apps.launch("definitely-not-installed-xyz")
        assert "control app list" in exc.value.hint

    def test_an_executable_on_the_path_is_found(self, monkeypatch, tmp_path):
        monkeypatch.setattr(apps, "_app_dirs", lambda: [])
        script = tmp_path / "fakeapp.exe"
        script.write_bytes(b"")
        monkeypatch.setenv("PATH", str(tmp_path))
        assert apps._resolve_windows_app("fakeapp") == script
        assert apps._resolve_windows_app("not-installed-at-all") is None

    def test_a_launched_app_does_not_inherit_secrets(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONTROL_SUDO_PASSWORD", "hunter2")
        seen = {}

        def fake_popen(args, **kwargs):
            seen["env"] = kwargs.get("env")
            seen["flags"] = kwargs.get("creationflags")
            return object()

        target = tmp_path / "secretapp.exe"
        target.write_bytes(b"")
        monkeypatch.setattr(subprocess, "Popen", fake_popen)
        monkeypatch.setattr(apps, "_resolve_windows_app", lambda app_id: target)
        apps.launch("secretapp")
        assert "CONTROL_SUDO_PASSWORD" not in seen["env"]
        # Detached, so closing the terminal that launched it does not close the app.
        assert seen["flags"] & subprocess.DETACHED_PROCESS


# --- text method selection, Windows rules -------------------------------------------------


class TestWindowsTextMethod:
    def test_accents_symbols_and_cjk_are_typed(self):
        for text in ("Hola señor", "a:b/c", "¿Qué tal?", "€5", "日本語", "100%", ""):
            assert inp.choose_text_method(text) == ("type", None)

    def test_emoji_are_pasted_because_a_key_event_cannot_carry_them(self):
        assert inp.choose_text_method("hi \U0001F600") == ("paste", None)
        assert inp.typeable("hi \U0001F600") is False
        assert inp.outside_bmp("hi \U0001F600") == ["\U0001F600"]

    def test_bmp_text_is_always_typeable(self):
        assert inp.typeable("áéíóúñ€日本") is True
        assert inp.outside_bmp("áéíóúñ€日本") == []

    def test_forcing_type_still_works_and_warns_about_the_astral_characters(self):
        assert inp.choose_text_method("hola señor", "type") == ("type", None)
        method, warning = inp.choose_text_method("hi \U0001F600", "type")
        assert method == "type" and "\U0001F600" in warning

    def test_pasting_is_always_available(self):
        assert inp.clipboard_available() is True
        assert inp.choose_text_method("hi \U0001F600", "paste") == ("paste", None)

    def test_windows_terminals_paste_with_ctrl_shift_v(self):
        assert inp.paste_keys_for("CASCADIA_HOSTING_WINDOW_CLASS") == ("ctrl", "shift", "v")
        assert inp.paste_keys_for("WindowsTerminal.exe") == ("ctrl", "shift", "v")
        assert inp.paste_keys_for("Notepad") == ("ctrl", "v")

    def test_the_clipboard_round_trips_and_is_put_back(self):
        saved = inp._save_clipboard()
        try:
            inp.copy_to_clipboard("hola señor ñ ¿qué?")
            assert inp.read_clipboard() == "hola señor ñ ¿qué?"
            files = inp.copy_files([__file__])
            assert len(files) == 1
            assert inp.read_clipboard("text/uri-list").endswith("\r\n")
        finally:
            if saved is None:
                inp.clear_clipboard()
            else:
                inp.copy_to_clipboard(saved[1], mime=saved[0])

    def test_a_uri_list_is_parsed_back_into_windows_paths(self, tmp_path):
        deck = tmp_path / "my deck.pptx"
        deck.write_bytes(b"x")
        parsed = inp._uri_to_path(deck.as_uri())
        assert parsed == str(deck)
        assert "\\" in parsed and not parsed.startswith("/")
        assert " " in parsed  # percent-decoded, not "my%20deck.pptx"

    def test_a_unc_file_uri_is_left_alone(self):
        assert inp._uri_to_path(r"C:\Windows\notepad.exe") == r"C:\Windows\notepad.exe"

    def test_an_empty_clipboard_reads_as_an_empty_string(self):
        saved = inp._save_clipboard()
        try:
            inp.clear_clipboard()
            assert inp.read_clipboard() == ""
        finally:
            if saved is not None and saved[0]:
                inp.copy_to_clipboard(saved[1], mime=saved[0])


# --- UI Automation -------------------------------------------------------------------------


class TestUiAutomation:
    def test_the_at_spi_role_names_still_filter_uia_control_types(self):
        """The documented --role filters work on both platforms."""
        from control import _uia

        for role, control_type in (("button", "Button"), ("push button", "Button"),
                                   ("check box", "CheckBox"), ("entry", "Edit"),
                                   ("textbox", "Edit"), ("page tab", "TabItem"),
                                   ("menu item", "MenuItem"), ("combo box", "ComboBox"),
                                   ("frame", "Window"), ("list item", "ListItem"),
                                   ("checkbox", "CheckBox"), ("window", "Window")):
            assert _uia._role_matches(atspi.normalize_role(role), control_type), role
        assert not _uia._role_matches(atspi.normalize_role("button"), "Edit")

    def test_a_uia_control_type_typed_directly_also_matches(self):
        from control import _uia

        assert _uia._role_matches("Button", "Button")
        assert _uia._role_matches("edit", "Edit")

    def test_the_bridge_is_selected_and_nothing_has_to_be_enabled(self, monkeypatch):
        monkeypatch.setattr(atspi, "is_enabled", lambda: True)
        monkeypatch.setattr(atspi, "set_enabled", lambda enabled: None)
        atspi.set_enabled(False)
        assert atspi.is_enabled() is True
        assert atspi._uia is not None

    def test_an_unknown_app_is_a_typed_error_with_a_hint(self, monkeypatch):
        monkeypatch.setattr(atspi._uia, "available", lambda: (True, None))
        monkeypatch.setattr(atspi._uia, "_app_windows", lambda: [])
        with pytest.raises(atspi.A11yAppNotFound) as exc:
            atspi.call("get", app="nothing-here")
        assert "control a11y apps" in exc.value.hint

    def test_an_unknown_pid_is_a_typed_error(self, monkeypatch):
        monkeypatch.setattr(atspi._uia, "available", lambda: (True, None))
        monkeypatch.setattr(atspi._uia, "_app_windows", lambda: [])
        with pytest.raises(atspi.A11yAppNotFound):
            atspi.call("get", pid=999999)

    def test_a_path_that_leads_nowhere_is_element_not_found(self, monkeypatch):
        monkeypatch.setattr(atspi._uia, "available", lambda: (True, None))
        monkeypatch.setattr(atspi._uia, "_app_windows", lambda: [])
        monkeypatch.setattr(atspi._uia, "_find_app", lambda request: object())
        monkeypatch.setattr(atspi._uia, "_children", lambda element: [])
        with pytest.raises(atspi.A11yElementNotFound):
            atspi.call("get", app="anything", path="/9/9")

    def test_filters_match_on_name_role_and_state(self):
        from control import _uia

        node = {"role": "Edit", "name": "Asunto", "text": "Reunión", "states_all": ["focused"],
                "actions": [], "path": "/0", "depth": 0}
        assert _uia._matches(node, {"role": atspi.normalize_role("entry")})
        assert _uia._matches(node, {"name": "asunto"})
        assert _uia._matches(node, {"text": "reun"})
        assert _uia._matches(node, {"states": ["focused"]})
        assert not _uia._matches(node, {"role": "button"})
        assert not _uia._matches(node, {"states": ["disabled"]})
        assert not _uia._matches(node, {"interactive": True})  # nothing to act on

    @pytest.mark.skipif(not uia_usable(), reason="UI Automation is not available here")
    def test_the_status_reports_which_backend_answers(self):
        info = atspi.status()
        assert info["backend"] == "uia"
        assert info["bus_enabled"] is True  # there is nothing to switch on Windows
        assert info["helper_python"]

    @pytest.mark.skipif(not uia_windows(), reason="no window publishes a tree here")
    def test_a_window_publishes_its_tree_with_roles_and_paths(self):
        found = atspi.call("apps")["apps"]
        if not found:
            pytest.skip("no application publishes a tree in this session")
        tree = atspi.call("tree", pid=found[0]["pid"], limit=25)
        assert tree["nodes"], "the first app with a tree should have elements"
        for node in tree["nodes"]:
            assert node["role"] and isinstance(node["depth"], int)
            assert node["path"] == "" or node["path"].startswith("/")

    @pytest.mark.skipif(not uia_windows(), reason="no window publishes a tree here")
    def test_a_password_field_is_never_read_back(self):
        for app in atspi.call("apps")["apps"]:
            for node in atspi.call("tree", pid=app["pid"], limit=200)["nodes"]:
                if "password" in node["states"]:
                    assert node.get("text") in ("", None)


# --- elevation, permissions, session names, CLI hints ---------------------------------------


class TestWindowsEnvironment:
    def test_a_file_in_the_user_profile_is_private(self, tmp_path, monkeypatch):
        from pathlib import Path

        home = tmp_path / "Profile"
        home.mkdir()
        monkeypatch.setenv("USERPROFILE", str(home))
        path = home / ".env"
        path.write_text("A=1\n")
        assert env.permission_problem(path) is None
        assert Path(home).resolve() in path.resolve().parents

    def test_a_file_outside_the_user_profile_is_flagged(self, tmp_path, monkeypatch):
        home = tmp_path / "Profile"
        (tmp_path / "Public").mkdir(parents=True)
        home.mkdir()
        monkeypatch.setenv("USERPROFILE", str(home))
        path = tmp_path / "Public" / ".env"
        path.write_text("A=1\n")
        problem = env.permission_problem(path)
        assert problem and "not private" in problem

    def test_session_names_that_windows_cannot_use_are_refused(self):
        for name in ("a/b", "a\\b", "a:b", "a*b", "a?b", 'a"b', "a<b", "a>b", "a|b", ".hidden", ""):
            with pytest.raises(session_mod.SessionError):
                session_mod.init(name)

    def test_a_plain_name_is_accepted_and_written_out(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CONTROL_STATE_DIR", str(tmp_path))
        monkeypatch.setattr(session_mod.windows, "ensure_desktop",
                            lambda name, position=None: windows.Desktop(0, "id", name))
        session = session_mod.init("agent_1")
        assert session.name == "agent_1"
        assert (session.dir / "session.json").is_file()
        assert session.profile_dir.name == "browser-profile"
        session.log("hello")
        assert (session.dir / "control.log").is_file()
        session_mod.end("agent_1")

    def test_the_sandbox_child_gets_its_own_process_group(self):
        kwargs = session_mod._own_group_kwargs()
        assert kwargs == {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        assert "start_new_session" not in kwargs

    def test_elevating_reports_whether_it_is_already_elevated(self):
        assert isinstance(sudo.is_elevated(), bool)

    def test_the_linux_only_setup_helpers_say_so_on_windows(self):
        for call in (sudo.restart_ydotoold, lambda: sudo.ensure_uinput_group("alice")):
            with pytest.raises(sudo.SudoError, match="Windows"):
                call()


class TestWindowsCliHints:
    def test_help_says_it_controls_windows(self, capsys):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["--help"])
        assert "Windows" in capsys.readouterr().out

    def test_every_native_error_type_has_a_hint(self):
        for error in (windows.WindowError, windows.DesktopError, windows.Win32Error,
                      inp.InputError, inp.ClipboardError, screenshot.ScreenshotError,
                      sudo.SudoError, atspi.AtspiError, env.EnvPermissionError):
            assert cli._ERROR_HINTS.get(error), error

    def test_the_top_level_help_names_the_native_backends(self, capsys):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["--help"])
        out = capsys.readouterr().out
        assert "CF_UNICODETEXT" in out and "wl-copy" not in out   # clip
        assert "UI Automation" in out and "AT-SPI" not in out     # a11y
        assert "Windows" in out

    def test_the_type_help_explains_the_unicode_typing(self, capsys):
        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["type", "--help"])
        assert "Unicode" in capsys.readouterr().out

    def test_the_sandbox_usage_error_shows_a_windows_command(self, capsys, monkeypatch):
        from .test_browser_ai import _main

        assert _main(["--json", "sandbox"], monkeypatch) == 1
        import json

        err = json.loads(capsys.readouterr().out)
        assert err["type"] == "usage_error" and "python" in err["hint"]

    def test_error_types_are_stable_identifiers(self):
        assert cli._snake_case(windows.Win32Error.__name__) == "win32_error"
        assert cli._snake_case(inp.SendInputError.__name__) == "send_input_error"
        assert cli._snake_case(windows.DesktopError.__name__) == "desktop_error"

    def test_a_missing_window_is_reported_as_json_with_a_hint(self, capsys, monkeypatch):
        from contextlib import redirect_stdout

        import io

        buffer = io.StringIO()
        args = cli.build_parser().parse_args(["--json", "win", "close", "no-such-window-xyz"])
        monkeypatch.setattr(cli.windows, "find_one", lambda pattern, desktop=None: None)
        with redirect_stdout(buffer):
            code = cli._report(args, "window_not_found", "no window matching 'x'", "list some")
            assert code is None
        import json

        assert json.loads(buffer.getvalue()) == {
            "error": "no window matching 'x'", "hint": "list some", "type": "window_not_found"}
