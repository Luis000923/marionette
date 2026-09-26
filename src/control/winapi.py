"""Native Windows backend: windows, input, clipboard, screens, virtual desktops.

This is the Windows half of `windows.py`, `input.py`, `screenshot.py` and
`session.py` -- the equivalent of the kdotool/ydotool/spectacle/`gdbus` wrappers
the KDE build shells out to. Those tools don't exist on Windows, so the calls go
straight to user32/kernel32/gdi32/shell32/ole32 through `ctypes`: no extra
dependency, and coordinates, key names and window ids keep meaning what the rest
of the package already thinks they mean.

Deliberately stdlib-only. `pywin32` would be shorter, but this project is
`uv sync` and nothing else, and a desktop-control tool that can't start without a
compiled wheel is a worse tool. UI Automation (`control a11y`) is the one place
where ctypes stops being reasonable, and that lives in `_uia.py` behind
`comtypes`.

What maps to what:

* window listing, activation, geometry, state -- `EnumWindows`,
  `SetForegroundWindow`, `SetWindowPos`, `ShowWindow`, `GetWindowRect` plus the
  DWM extended frame bounds, so geometry excludes the drop shadow;
* mouse and keyboard -- `SendInput`, including `KEYEVENTF_UNICODE`, which types
  accents, ñ and emoji exactly, with no clipboard and no keyboard-layout games;
* the clipboard -- `CF_UNICODETEXT` and `CF_HDROP`, the native equivalent of
  wl-copy's `--type text/uri-list`;
* screenshots -- GDI `PrintWindow`/`BitBlt` into a DIB, plus a drag-select
  overlay for `shot region`;
* virtual desktops -- the `IVirtualDesktopManager` COM interface, so
  `desktop create`, `session init` and `win send` keep isolating agents per
  desktop, one Win32 virtual desktop each.

Win32 is unforgiving about types and about which thread owns what, so every
function is declared explicitly and every fallible call is checked against
`GetLastError` rather than returning a zero nobody looks at.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

if os.name != "nt":  # pragma: no cover - imported only by the Windows code paths
    raise ImportError(
        "control.winapi is the native Windows backend; it cannot be imported on "
        f"{sys.platform}. Use the kdotool/ydotool backend there instead."
    )

# --- DLL handles -------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
try:
    dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
except OSError:  # pragma: no cover - present on every supported Windows
    dwmapi = None

LRESULT = ctypes.c_ssize_t
LPVOID = ctypes.c_void_p
HDC = wintypes.HDC
HBITMAP = wintypes.HBITMAP
HGDIOBJ = wintypes.HGDIOBJ
HBRUSH = ctypes.c_void_p
HPEN = ctypes.c_void_p
HMENU = wintypes.HMENU
HMONITOR = ctypes.c_void_p
ULONG_PTR = ctypes.c_size_t


# --- errors -------------------------------------------------------------------------

#: GetLastError() values worth translating into something a human can act on.
_KNOWN_ERRORS = {
    5: "access denied -- the target window belongs to an app running as "
       "administrator, and Windows forbids input across that boundary",
    87: "invalid parameter",
    1400: "invalid window handle (it was closed in the meantime)",
}


class Win32Error(RuntimeError):
    """A Win32 call failed, or this Windows build cannot do what was asked."""

    def __init__(self, message: str, api: str = "", code: int | None = None):
        super().__init__(message)
        self.api = api
        self.code = code


def _error(api: str) -> Win32Error:
    code = ctypes.get_last_error()
    return Win32Error(f"{api} failed: {_KNOWN_ERRORS.get(code, f'error {code}')}",
                      api=api, code=code)


def _check(ok, api: str) -> None:
    if not ok:
        raise _error(api)


def _declare(lib, name: str, restype, *argtypes):
    """Bind a function with explicit types: ctypes' defaults are c_int and lie."""
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


# --- structures -----------------------------------------------------------------------

class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    def normalized(self) -> "RECT":
        return RECT(min(self.left, self.right), min(self.top, self.bottom),
                    max(self.left, self.right), max(self.top, self.bottom))

    def as_dict(self) -> dict[str, int]:
        return {"x": self.left, "y": self.top, "width": self.width, "height": self.height}

    def __repr__(self) -> str:
        return (f"RECT(x={self.left}, y={self.top}, w={self.width}, h={self.height})")


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class MSG(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD), ("pt", POINT), ("lPrivate", wintypes.DWORD)]


class GUID(ctypes.Structure):
    _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

    def __str__(self) -> str:
        first = f"{self.Data1:08X}-{self.Data2:04X}-{self.Data3:04X}-"
        tail = "".join(f"{b:02X}" for b in self.Data4)
        return f"{first}{tail[:4]}-{tail[4:]}"


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", wintypes.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class DROPFILES(ctypes.Structure):
    _fields_ = [("pFiles", wintypes.DWORD), ("pt", POINT), ("fNC", wintypes.BOOL),
                ("fWide", wintypes.BOOL)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", HDC), ("fErase", wintypes.BOOL), ("rcPaint", RECT),
                ("fRestore", wintypes.BOOL), ("fIncUpdate", wintypes.BOOL),
                ("rgbReserved", ctypes.c_byte * 32)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", LPVOID),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", RECT)]


class RTL_OSVERSIONINFOEXW(ctypes.Structure):
    _fields_ = [("dwOSVersionInfoSize", wintypes.DWORD), ("dwMajorVersion", wintypes.DWORD),
                ("dwMinorVersion", wintypes.DWORD), ("dwBuildNumber", wintypes.DWORD),
                ("dwPlatformId", wintypes.DWORD), ("szCSDVersion", wintypes.WCHAR * 128)]


# --- SendInput structures ---------------------------------------------------------------

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.POINTER(ULONG_PTR))]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.POINTER(ULONG_PTR))]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTUNION)]


# --- constants -------------------------------------------------------------------------

SW_SHOWNORMAL, SW_SHOWMINIMIZED, SW_MAXIMIZE = 1, 2, 3
SW_SHOW, SW_MINIMIZE, SW_RESTORE = 5, 6, 9
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20
SWP_NOSIZE, SWP_NOMOVE, SWP_NOZORDER = 0x0001, 0x0002, 0x0004
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x0010, 0x0040
GWL_EXSTYLE = -20
HWND_TOP, HWND_TOPMOST, HWND_NOTOPMOST = 0, -1, -2
GA_ROOT = 2

WM_DESTROY, WM_CLOSE, WM_PAINT, WM_QUIT = 0x0002, 0x0010, 0x000F, 0x0012
WM_ERASEBKGND, WM_KEYDOWN = 0x0014, 0x0100
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_RBUTTONDOWN = 0x0200, 0x0201, 0x0202, 0x0204

CS_HREDRAW, CS_VREDRAW = 0x0002, 0x0001
WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TOPMOST, WS_EX_TOOLWINDOW = 0x00080000, 0x00000008, 0x00000080
WS_EX_NOACTIVATE = 0x08000000
LWA_ALPHA = 0x00000002
NULL_BRUSH = 4
TRANSPARENT = 1

SRCCOPY, CAPTUREBLT = 0x00CC0020, 0x40000000
DIB_RGB_COLORS, BI_RGB = 0, 0
PW_RENDERFULLCONTENT = 0x00000002
DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS = 14, 9
MONITOR_DEFAULTTONEAREST = 0x00000002
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79

CF_UNICODETEXT, CF_HDROP = 13, 15
GMEM_MOVEABLE, GMEM_ZEROINIT = 0x0002, 0x0040
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
VK_ESCAPE = 0x1B
CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED = 1, 2
ERROR_CLASS_ALREADY_EXISTS = 1410
CLIPBOARD_RETRIES, CLIPBOARD_RETRY_DELAY = 25, 0.01

#: Virtual key codes, using exactly the canonical names of the Linux table in
#: `input.py`, so `control key --list`, its aliases and every existing command
#: keep working the same way on both platforms.
VK: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "pause": 0x13, "capslock": 0x14,
    "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "sysrq": 0x2C, "insert": 0x2D, "delete": 0x2E,
    "0": 0x30, "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34,
    "5": 0x35, "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39,
    "a": 0x41, "b": 0x42, "c": 0x43, "d": 0x44, "e": 0x45, "f": 0x46, "g": 0x47,
    "h": 0x48, "i": 0x49, "j": 0x4A, "k": 0x4B, "l": 0x4C, "m": 0x4D, "n": 0x4E,
    "o": 0x4F, "p": 0x50, "q": 0x51, "r": 0x52, "s": 0x53, "t": 0x54, "u": 0x55,
    "v": 0x56, "w": 0x57, "x": 0x58, "y": 0x59, "z": 0x5A,
    "super": 0x5B, "meta": 0x5B, "menu": 0x5D,
    "kp0": 0x60, "kp1": 0x61, "kp2": 0x62, "kp3": 0x63, "kp4": 0x64, "kp5": 0x65,
    "kp6": 0x66, "kp7": 0x67, "kp8": 0x68, "kp9": 0x69,
    "kpasterisk": 0x6A, "kpplus": 0x6B, "kpminus": 0x6D, "kpdot": 0x6E, "kpslash": 0x6F,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "rightshift": 0xA1, "rightctrl": 0xA3, "rightalt": 0xA5,
    "numlock": 0x90, "scrolllock": 0x91,
    "comma": 0xBC, "dot": 0xBE, "slash": 0xBF, "semicolon": 0xBA, "equal": 0xBB,
    "minus": 0xBD, "apostrophe": 0xDE, "grave": 0xC0, "backslash": 0xDC,
    "leftbrace": 0xDB, "rightbrace": 0xDD,
    "leftbracket": 0xDB, "rightbracket": 0xDD,
    "kpenter": 0x0D,  # Windows has no numpad-Enter virtual key; the extended
                      # flag in _EXTENDED_KEYS is what tells Enter from KP_Enter.
}
#: Keys Windows only recognises with the "extended" bit set. Without it the
#: numpad Enter arrives as the main one and the arrow keys as the numeric keypad.
_EXTENDED_KEYS = frozenset({"enter", "kpenter", "delete", "up", "down", "left", "right",
                            "home", "end", "pageup", "pagedown", "insert",
                            "rightctrl", "rightalt", "kpslash"})

BUTTON_FLAGS: dict[str, tuple[int, int]] = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}

#: Shell windows that are not applications: the desktop itself, the taskbar, and
#: the WorkerW layers behind it. Listing them would put three lines of noise in
#: every `win list` that no agent ever wants.
_SHELL_CLASSES = frozenset({
    "progman", "workerw", "shell_traywnd", "shell_scalebar", "systray", "button",
    "topmost_window_wrapper", "multitaskingviewframe", "windows.ui.core.corewindow",
    "edge_ui_input_top_wnd", "xaml_presence_listener",
})

# --- declared entry points ------------------------------------------------------------

IsWindow = _declare(user32, "IsWindow", wintypes.BOOL, wintypes.HWND)
IsWindowVisible = _declare(user32, "IsWindowVisible", wintypes.BOOL, wintypes.HWND)
IsIconic = _declare(user32, "IsIconic", wintypes.BOOL, wintypes.HWND)
IsZoomed = _declare(user32, "IsZoomed", wintypes.BOOL, wintypes.HWND)
EnumWindows = _declare(user32, "EnumWindows", wintypes.BOOL, LPVOID, wintypes.LPARAM)
GetWindowTextLengthW = _declare(user32, "GetWindowTextLengthW", ctypes.c_int, wintypes.HWND)
GetWindowTextW = _declare(user32, "GetWindowTextW", ctypes.c_int, wintypes.HWND,
                          wintypes.LPWSTR, ctypes.c_int)
GetClassNameW = _declare(user32, "GetClassNameW", ctypes.c_int, wintypes.HWND,
                         wintypes.LPWSTR, ctypes.c_int)
GetWindowThreadProcessId = _declare(user32, "GetWindowThreadProcessId", wintypes.DWORD,
                                    wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
GetWindowRect = _declare(user32, "GetWindowRect", wintypes.BOOL, wintypes.HWND, LPVOID)
GetForegroundWindow = _declare(user32, "GetForegroundWindow", wintypes.HWND)
GetShellWindow = _declare(user32, "GetShellWindow", wintypes.HWND)
SetForegroundWindow = _declare(user32, "SetForegroundWindow", wintypes.BOOL, wintypes.HWND)
BringWindowToTop = _declare(user32, "BringWindowToTop", wintypes.BOOL, wintypes.HWND)
GetGUIThreadInfo = _declare(user32, "GetGUIThreadInfo", wintypes.BOOL, wintypes.DWORD, LPVOID)
SetFocus = _declare(user32, "SetFocus", wintypes.HWND, wintypes.HWND)
GetAncestor = _declare(user32, "GetAncestor", wintypes.HWND, wintypes.HWND, wintypes.UINT)
AttachThreadInput = _declare(user32, "AttachThreadInput", wintypes.BOOL, wintypes.DWORD,
                             wintypes.DWORD, wintypes.BOOL)
ShowWindow = _declare(user32, "ShowWindow", wintypes.BOOL, wintypes.HWND, ctypes.c_int)
SetWindowPos = _declare(user32, "SetWindowPos", wintypes.BOOL, wintypes.HWND, wintypes.HWND,
                        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
GetWindowLongW = _declare(user32, "GetWindowLongW", ctypes.c_long, wintypes.HWND, ctypes.c_int)
PostMessageW = _declare(user32, "PostMessageW", wintypes.BOOL, wintypes.HWND, wintypes.UINT,
                        wintypes.WPARAM, wintypes.LPARAM)
PrintWindow = _declare(user32, "PrintWindow", wintypes.BOOL, wintypes.HWND, HDC, wintypes.UINT)
GetDC = _declare(user32, "GetDC", HDC, wintypes.HWND)
ReleaseDC = _declare(user32, "ReleaseDC", ctypes.c_int, wintypes.HWND, HDC)
MonitorFromWindow = _declare(user32, "MonitorFromWindow", HMONITOR, wintypes.HWND, wintypes.DWORD)
GetMonitorInfoW = _declare(user32, "GetMonitorInfoW", wintypes.BOOL, HMONITOR, LPVOID)
GetSystemMetrics = _declare(user32, "GetSystemMetrics", ctypes.c_int, ctypes.c_int)
GetCursorPos = _declare(user32, "GetCursorPos", wintypes.BOOL, LPVOID)
SetCursorPos = _declare(user32, "SetCursorPos", wintypes.BOOL, ctypes.c_int, ctypes.c_int)
SendInput = _declare(user32, "SendInput", wintypes.UINT, wintypes.UINT, LPVOID, ctypes.c_int)
SetProcessDPIAware = _declare(user32, "SetProcessDPIAware", wintypes.BOOL)
SetProcessDpiAwarenessContext = _declare(user32, "SetProcessDpiAwarenessContext", wintypes.BOOL,
                                          LPVOID)
OpenClipboard = _declare(user32, "OpenClipboard", wintypes.BOOL, wintypes.HWND)
CloseClipboard = _declare(user32, "CloseClipboard", wintypes.BOOL)
EmptyClipboard = _declare(user32, "EmptyClipboard", wintypes.BOOL)
GetClipboardData = _declare(user32, "GetClipboardData", wintypes.HANDLE, wintypes.UINT)
SetClipboardData = _declare(user32, "SetClipboardData", wintypes.HANDLE, wintypes.UINT,
                            wintypes.HANDLE)
IsClipboardFormatAvailable = _declare(user32, "IsClipboardFormatAvailable", wintypes.BOOL,
                                      wintypes.UINT)
CountClipboardFormats = _declare(user32, "CountClipboardFormats", wintypes.UINT)
GetModuleHandleW = _declare(kernel32, "GetModuleHandleW", wintypes.HMODULE, wintypes.LPCWSTR)
GetCurrentThreadId = _declare(kernel32, "GetCurrentThreadId", wintypes.DWORD)
GetConsoleOutputCP = _declare(kernel32, "GetConsoleOutputCP", wintypes.UINT)
SetConsoleOutputCP = _declare(kernel32, "SetConsoleOutputCP", wintypes.BOOL, wintypes.UINT)
SetConsoleCP = _declare(kernel32, "SetConsoleCP", wintypes.BOOL, wintypes.UINT)
GlobalAlloc = _declare(kernel32, "GlobalAlloc", LPVOID, wintypes.UINT, ctypes.c_size_t)
GlobalLock = _declare(kernel32, "GlobalLock", LPVOID, LPVOID)
GlobalUnlock = _declare(kernel32, "GlobalUnlock", wintypes.BOOL, LPVOID)
GlobalSize = _declare(kernel32, "GlobalSize", ctypes.c_size_t, LPVOID)
GlobalFree = _declare(kernel32, "GlobalFree", LPVOID, LPVOID)
OpenProcess = _declare(kernel32, "OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL,
                       wintypes.DWORD)
GetExitCodeProcess = _declare(kernel32, "GetExitCodeProcess", wintypes.BOOL, wintypes.HANDLE,
                              LPVOID)
QueryFullProcessImageNameW = _declare(kernel32, "QueryFullProcessImageNameW", wintypes.BOOL,
                                      wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                      ctypes.POINTER(wintypes.DWORD))
CloseHandle = _declare(kernel32, "CloseHandle", wintypes.BOOL, wintypes.HANDLE)
GetCurrentProcess = _declare(kernel32, "GetCurrentProcess", wintypes.HANDLE)
OpenProcessToken = _declare(advapi32, "OpenProcessToken", wintypes.BOOL, wintypes.HANDLE,
                            wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
GetTokenInformation = _declare(advapi32, "GetTokenInformation", wintypes.BOOL, wintypes.HANDLE,
                               ctypes.c_int, LPVOID, wintypes.DWORD,
                               ctypes.POINTER(wintypes.DWORD))
CreateCompatibleDC = _declare(gdi32, "CreateCompatibleDC", HDC, HDC)
CreateCompatibleBitmap = _declare(gdi32, "CreateCompatibleBitmap", HBITMAP, HDC, ctypes.c_int,
                                  ctypes.c_int)
SelectObject = _declare(gdi32, "SelectObject", HGDIOBJ, HDC, HGDIOBJ)
DeleteObject = _declare(gdi32, "DeleteObject", wintypes.BOOL, HGDIOBJ)
DeleteDC = _declare(gdi32, "DeleteDC", wintypes.BOOL, HDC)
BitBlt = _declare(gdi32, "BitBlt", wintypes.BOOL, HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                  ctypes.c_int, HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD)
GetDIBits = _declare(gdi32, "GetDIBits", ctypes.c_int, HDC, HBITMAP, wintypes.UINT, wintypes.UINT,
                     LPVOID, LPVOID, wintypes.UINT)
CreateSolidBrush = _declare(gdi32, "CreateSolidBrush", HBRUSH, wintypes.DWORD)
CreatePen = _declare(gdi32, "CreatePen", HPEN, ctypes.c_int, ctypes.c_int, wintypes.DWORD)
FrameRect = _declare(user32, "FrameRect", ctypes.c_int, HDC, LPVOID, HBRUSH)
FillRect = _declare(user32, "FillRect", ctypes.c_int, HDC, LPVOID, HBRUSH)
GetStockObject = _declare(gdi32, "GetStockObject", HGDIOBJ, ctypes.c_int)
RegisterClassW = _declare(user32, "RegisterClassW", wintypes.ATOM, LPVOID)
UnregisterClassW = _declare(user32, "UnregisterClassW", wintypes.BOOL, wintypes.LPCWSTR,
                            wintypes.HINSTANCE)
CreateWindowExW = _declare(user32, "CreateWindowExW", wintypes.HWND, wintypes.DWORD,
                           wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, HMENU,
                           wintypes.HINSTANCE, LPVOID)
DestroyWindow = _declare(user32, "DestroyWindow", wintypes.BOOL, wintypes.HWND)
DefWindowProcW = _declare(user32, "DefWindowProcW", LRESULT, wintypes.HWND, wintypes.UINT,
                          wintypes.WPARAM, wintypes.LPARAM)
BeginPaint = _declare(user32, "BeginPaint", HDC, wintypes.HWND, LPVOID)
EndPaint = _declare(user32, "EndPaint", wintypes.BOOL, wintypes.HWND, LPVOID)
InvalidateRect = _declare(user32, "InvalidateRect", wintypes.BOOL, wintypes.HWND, LPVOID,
                          wintypes.BOOL)
GetMessageW = _declare(user32, "GetMessageW", wintypes.BOOL, LPVOID, wintypes.HWND,
                       wintypes.UINT, wintypes.UINT)
TranslateMessage = _declare(user32, "TranslateMessage", wintypes.BOOL, LPVOID)
DispatchMessageW = _declare(user32, "DispatchMessageW", LRESULT, LPVOID)
PostQuitMessage = _declare(user32, "PostQuitMessage", None, ctypes.c_int)
SetLayeredWindowAttributes = _declare(user32, "SetLayeredWindowAttributes", wintypes.BOOL,
                                      wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte,
                                      wintypes.DWORD)
ShellExecuteW = _declare(shell32, "ShellExecuteW", wintypes.HINSTANCE, wintypes.HWND,
                         wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                         ctypes.c_int)
CoInitializeEx = _declare(ole32, "CoInitializeEx", ctypes.c_long, LPVOID, wintypes.DWORD)
CoTaskMemFree = _declare(ole32, "CoTaskMemFree", None, LPVOID)
CLSIDFromString = _declare(ole32, "CLSIDFromString", ctypes.c_long, wintypes.LPCWSTR, LPVOID)
CoCreateInstance = _declare(ole32, "CoCreateInstance", ctypes.c_long, LPVOID, LPVOID,
                            wintypes.DWORD, LPVOID, LPVOID)
RtlGetVersion = _declare(ntdll := ctypes.WinDLL("ntdll"), "RtlGetVersion", ctypes.c_long, LPVOID)
DwmGetWindowAttribute = (_declare(dwmapi, "DwmGetWindowAttribute", ctypes.c_long, wintypes.HWND,
                                  wintypes.DWORD, LPVOID, wintypes.DWORD)
                         if dwmapi is not None else None)

_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
                              wintypes.LPARAM)


# --- process-wide setup ----------------------------------------------------------------

def enable_dpi_awareness() -> None:
    """Opt in to per-monitor DPI awareness, before any window or cursor call.

    Without it, a scaled display (125%, 150%) hands us "logical" coordinates from
    some APIs and physical ones from others, and a click aimed at (960, 540) lands
    somewhere else entirely. Every coordinate this tool prints or accepts is a
    physical pixel, so we opt in once, at import time.
    """
    try:  # Windows 10 1703+
        SetProcessDpiAwarenessContext(LPVOID(-4))  # PER_MONITOR_AWARE_V2
        return
    except (AttributeError, OSError):
        pass
    try:  # Windows 8.1+
        ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        return
    except (AttributeError, OSError):
        pass
    try:  # Vista+
        SetProcessDPIAware()
    except (AttributeError, OSError):  # pragma: no cover - ancient Windows
        pass


enable_dpi_awareness()


def set_console_utf8() -> None:
    """Print UTF-8 in the console, so ñ and accents are not mojibake.

    Python reconfigures its own streams to UTF-8, but the console code page
    decides how those bytes are rendered; 65001 is the one that can show what we
    type. A no-op when there is no console (output redirected to a file/pipe).
    """
    if not (GetConsoleOutputCP() and GetConsoleOutputCP() != 0xFFFFFFFF):
        return  # redirected: nothing to configure
    SetConsoleOutputCP(65001)
    SetConsoleCP(65001)


def os_version() -> tuple[int, int, int]:
    """(major, minor, build) of the running Windows, for version-dependent messages."""
    info = RTL_OSVERSIONINFOEXW()
    info.dwOSVersionInfoSize = ctypes.sizeof(RTL_OSVERSIONINFOEXW)
    if RtlGetVersion(ctypes.byref(info)) == 0:
        return (int(info.dwMajorVersion), int(info.dwMinorVersion),
                int(info.dwBuildNumber))
    return (0, 0, 0)


# --- windows ----------------------------------------------------------------------------

def _hwnd(value) -> int:
    """An HWND as a plain int (EnumWindows hands out c_void_p-ish handles)."""
    if isinstance(value, int):
        return value
    return int(ctypes.cast(value, LPVOID).value or 0)


def window_exists(hwnd: int) -> bool:
    return bool(IsWindow(wintypes.HWND(hwnd)))


def window_visible(hwnd: int) -> bool:
    return bool(IsWindowVisible(wintypes.HWND(hwnd)))


def _cloaked(hwnd: int) -> bool:
    """True for a window DWM keeps around but never shows (UWP ghosts, suspended apps)."""
    if DwmGetWindowAttribute is None:
        return False
    value = wintypes.DWORD(0)
    hr = DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_CLOAKED, ctypes.byref(value),
                               ctypes.sizeof(value))
    return hr == 0 and value.value != 0


def list_windows(visible_only: bool = True) -> list[int]:
    """Every top-level window handle, in z-order (frontmost first)."""
    found: list[int] = []

    def collect(hwnd, _lparam):
        handle = _hwnd(hwnd)
        if visible_only and (not IsWindowVisible(hwnd) or _cloaked(handle)):
            return True
        found.append(handle)
        return True

    callback = _ENUMPROC(collect)
    EnumWindows(callback, 0)
    return found


def window_text(hwnd: int) -> str:
    length = GetWindowTextLengthW(wintypes.HWND(hwnd))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    GetWindowTextW(wintypes.HWND(hwnd), buffer, length + 1)
    return buffer.value


def window_class(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    GetClassNameW(wintypes.HWND(hwnd), buffer, 256)
    return buffer.value


def window_pid(hwnd: int) -> int:
    pid = wintypes.DWORD(0)
    GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def window_rect(hwnd: int) -> RECT:
    """Window bounds in physical pixels, shadow excluded when DWM knows the frame."""
    rect = RECT()
    ok = False
    if DwmGetWindowAttribute is not None:
        ok = DwmGetWindowAttribute(wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
                                   ctypes.byref(rect), ctypes.sizeof(rect)) == 0
    if not ok:
        ok = bool(GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)))
    if not ok:
        raise _error("GetWindowRect")
    return rect


def foreground_window() -> int:
    return _hwnd(GetForegroundWindow())


def is_shell_window(hwnd: int) -> bool:
    return window_class(hwnd).lower() in _SHELL_CLASSES or hwnd == _hwnd(GetShellWindow())


def _thread_focus(thread_id: int) -> int:
    """The window holding a thread's keyboard focus, 0 if there is none.

    Windows remembers a background thread's last focus window, which is the one
    piece of state an automation tool cannot reconstruct for itself.
    """
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        return 0
    return _hwnd(info.hwndFocus)


def activate(hwnd: int) -> bool:
    """Bring a window to the front and give it keyboard focus.

    Windows refuses `SetForegroundWindow` from a process the user did not just
    interact with: that is the focus-stealing rule, and it is why a plain call
    silently does nothing for a console-launched tool. The way round it is to join
    the *foreground* thread's input queue for the duration of the call.

    (Measured on Windows 11, each from a clean start: a plain
    `SetForegroundWindow` does nothing, and so does joining the *target* window's
    own thread. Joining the foreground thread works, and injecting an Alt press to
    unlock the rule works too but is deliberately not used here: those keystrokes
    are real input for every other app, and a process that exits before the input
    queue drains can strand the modifier.)

    Note also what is *not* called: `SetActiveWindow`. With the queues joined it
    succeeds, and that is the problem -- it moves the focus from the window's
    focused child onto the top-level window, so every keypress afterwards is
    eaten by the window procedure instead of reaching the control the user can
    see the caret blinking in.

    Activating a window resets its thread's focus to the top-level window, and
    only a framework that bothers to remember its own last child puts it back. A
    bare Win32 app does not, so the focus is read beforehand and restored
    afterwards -- without it, `type --window` works on a text field until the
    field happens to be the thing that was focused.

    Returns whether the window really ended up in front.
    """
    if not window_exists(hwnd):
        raise Win32Error(f"window {hwnd} no longer exists", api="IsWindow", code=1400)
    if IsIconic(wintypes.HWND(hwnd)):
        ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
    if foreground_window() == hwnd:
        return True
    current = foreground_window()
    mine = GetCurrentThreadId()
    threads = [GetWindowThreadProcessId(wintypes.HWND(h), None) for h in (current, hwnd)]
    target_thread = threads[1]
    focus_before = _thread_focus(target_thread)
    attached = [t for t in threads if t and t != mine]
    for thread in attached:
        AttachThreadInput(mine, thread, True)
    try:
        BringWindowToTop(wintypes.HWND(hwnd))
        SetForegroundWindow(wintypes.HWND(hwnd))
        if (focus_before and _thread_focus(target_thread) != focus_before
                and _hwnd(GetAncestor(wintypes.HWND(focus_before), GA_ROOT)) == hwnd):
            SetFocus(wintypes.HWND(focus_before))
        return foreground_window() == hwnd
    finally:
        for thread in attached:
            AttachThreadInput(mine, thread, False)


def raise_window(hwnd: int) -> None:
    SetWindowPos(wintypes.HWND(hwnd), HWND_TOP, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def close_window(hwnd: int) -> None:
    """Ask the window to close (WM_CLOSE), so the app can save or ask first."""
    _check(bool(PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0)), "PostMessage(WM_CLOSE)")


def show_window(hwnd: int, how: int) -> None:
    ShowWindow(wintypes.HWND(hwnd), how)


def is_minimized(hwnd: int) -> bool:
    return bool(IsIconic(wintypes.HWND(hwnd)))


def is_maximized(hwnd: int) -> bool:
    return bool(IsZoomed(wintypes.HWND(hwnd)))


def move_resize(hwnd: int, x: int, y: int, width: int, height: int) -> None:
    _check(bool(SetWindowPos(wintypes.HWND(hwnd), None, int(x), int(y), int(width),
                             int(height), SWP_NOZORDER | SWP_NOACTIVATE)), "SetWindowPos")


def set_topmost(hwnd: int, topmost: bool) -> None:
    SetWindowPos(wintypes.HWND(hwnd), HWND_TOPMOST if topmost else HWND_NOTOPMOST, 0, 0, 0, 0,
                 SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)


def is_topmost(hwnd: int) -> bool:
    """True when the window floats above the others (WS_EX_TOPMOST)."""
    return bool(GetWindowLongW(wintypes.HWND(hwnd), GWL_EXSTYLE) & WS_EX_TOPMOST)


def monitor_rect(hwnd: int) -> RECT:
    """Bounds of the monitor a window sits on (all screens if Windows won't say)."""
    handle = MonitorFromWindow(wintypes.HWND(hwnd), MONITOR_DEFAULTTONEAREST)
    info = MONITORINFO()
    info.cbSize = ctypes.sizeof(MONITORINFO)
    if handle and GetMonitorInfoW(handle, ctypes.byref(info)):
        return info.rcMonitor
    return virtual_screen_rect()


def virtual_screen_rect() -> RECT:
    """Bounding box of every monitor, so a full screenshot covers them all."""
    left = GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = GetSystemMetrics(SM_YVIRTUALSCREEN)
    return RECT(left, top, left + GetSystemMetrics(SM_CXVIRTUALSCREEN),
                top + GetSystemMetrics(SM_CYVIRTUALSCREEN))


# --- input -------------------------------------------------------------------------------

def _send(events: list[INPUT]) -> None:
    if not events:
        return
    # One INPUT array, sent in a single call: cheaper than one call per event, and
    # the only way a long string goes in as a single atomic burst.
    batch = (INPUT * len(events))(*events)
    sent = SendInput(len(events), ctypes.byref(batch), ctypes.sizeof(INPUT))
    if sent != len(events):
        raise Win32Error(
            f"SendInput delivered {sent} of {len(events)} events -- either another "
            "process is injecting input at the same time, or the focused window "
            "belongs to an app running as administrator",
            api="SendInput", code=ctypes.get_last_error())


def _key_event(vk: int, name: str, up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if name in _EXTENDED_KEYS:
        flags |= KEYEVENTF_EXTENDEDKEY
    event = INPUT(type=INPUT_KEYBOARD)
    event.union.ki = KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=None)
    return event


def key_press(name: str, up: bool = False) -> None:
    """Press (or release) a key by its canonical name, VK translated."""
    try:
        vk = VK[name]
    except KeyError as exc:
        raise Win32Error(f"no virtual key for {name!r}", api="SendInput") from exc
    _send([_key_event(vk, name, up)])


def type_unicode(text: str) -> None:
    """Type `text` as Unicode, one UTF-16 code unit at a time.

    `KEYEVENTF_UNICODE` injects the character itself, so the active keyboard
    layout is irrelevant: "ñ", "€" and emoji arrive exactly as written. Non-BMP
    characters arrive as the two surrogate halves Python's str iterates over,
    which is precisely what Windows wants.
    """
    events: list[INPUT] = []
    for char in text:
        for up in (False, True):
            flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
            event = INPUT(type=INPUT_KEYBOARD)
            event.union.ki = KEYBDINPUT(wVk=0, wScan=ord(char) & 0xFFFF, dwFlags=flags,
                                        time=0, dwExtraInfo=None)
            events.append(event)
    _send(events)


def cursor_pos() -> tuple[int, int] | None:
    point = POINT()
    if not GetCursorPos(ctypes.byref(point)):
        return None
    return int(point.x), int(point.y)


def set_cursor_pos(x: int, y: int) -> bool:
    """Put the pointer exactly on (x, y). Exact: Windows has no acceleration here."""
    return bool(SetCursorPos(int(x), int(y)))


def mouse_event(flags: int, data: int = 0) -> None:
    event = INPUT(type=INPUT_MOUSE)
    event.union.mi = MOUSEINPUT(dx=0, dy=0, mouseData=data, dwFlags=flags, time=0,
                                dwExtraInfo=None)
    _send([event])


def click_button(button: str, down: bool) -> None:
    down_flag, up_flag = BUTTON_FLAGS[button]
    mouse_event(down_flag if down else up_flag)


def scroll(amount: int) -> None:
    """Positive scrolls up, negative down (Windows' own convention)."""
    mouse_event(MOUSEEVENTF_WHEEL, int(amount) * WHEEL_DELTA)


# --- clipboard ---------------------------------------------------------------------------

def _open_clipboard() -> None:
    """Open the clipboard, retrying briefly: another app may hold it for a moment."""
    for _ in range(CLIPBOARD_RETRIES):
        if OpenClipboard(None):
            return
        time.sleep(CLIPBOARD_RETRY_DELAY)
    raise _error("OpenClipboard")


def _put(format_id: int, payload: bytes) -> None:
    """Hand a global-memory block to the clipboard, which takes ownership of it."""
    handle = GlobalAlloc(GMEM_MOVEABLE | GMEM_ZEROINIT, len(payload))
    if not handle:
        raise _error("GlobalAlloc")
    pointer = GlobalLock(handle)
    if not pointer:
        GlobalFree(handle)
        raise _error("GlobalLock")
    try:
        ctypes.memmove(pointer, payload, len(payload))
    finally:
        GlobalUnlock(handle)
    _open_clipboard()
    try:
        EmptyClipboard()
        _check(bool(SetClipboardData(format_id, handle)), "SetClipboardData")
    finally:
        CloseClipboard()


def set_text(text: str) -> None:
    _put(CF_UNICODETEXT, text.encode("utf-16-le") + b"\x00\x00")


def set_files(paths: list[str]) -> None:
    """Put files on the clipboard the way Explorer does (CF_HDROP)."""
    header = ctypes.sizeof(DROPFILES)
    # fWide = 1: the path list behind the header is UTF-16 and double-null terminated.
    body = "".join(f"{p}\x00" for p in paths).encode("utf-16-le") + b"\x00\x00"
    drop = DROPFILES(pFiles=header, pt=POINT(0, 0), fNC=False, fWide=True)
    _put(CF_HDROP, bytes(drop) + body)


def _get(format_id: int) -> bytes | None:
    if not IsClipboardFormatAvailable(format_id):
        return None
    _open_clipboard()
    try:
        handle = GetClipboardData(format_id)
        if not handle:
            return None
        size = GlobalSize(handle)
        pointer = GlobalLock(handle)
        if not pointer or not size:
            return None
        try:
            return ctypes.string_at(pointer, size)
        finally:
            GlobalUnlock(handle)
    finally:
        CloseClipboard()


def get_text() -> str:
    return (_get(CF_UNICODETEXT) or b"").decode("utf-16-le", errors="replace").rstrip("\x00")


def get_files() -> list[str]:
    raw = _get(CF_HDROP)
    if not raw:
        return []
    head = DROPFILES.from_buffer_copy(raw[:ctypes.sizeof(DROPFILES)])
    if not head.fWide:  # an ANSI list; nothing modern produces those any more
        return []
    body = raw[head.pFiles:].decode("utf-16-le", errors="replace")
    return [chunk for chunk in body.split("\x00") if chunk]


def clipboard_has_restorable_content() -> bool:
    """True when the clipboard holds text or files (the formats we can put back)."""
    return bool(IsClipboardFormatAvailable(CF_UNICODETEXT)
                or IsClipboardFormatAvailable(CF_HDROP))


def clipboard_is_empty() -> bool:
    return CountClipboardFormats() == 0


def clear_clipboard() -> None:
    _open_clipboard()
    try:
        EmptyClipboard()
    finally:
        CloseClipboard()


# --- processes ---------------------------------------------------------------------------

def process_alive(pid: int) -> bool:
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        if not GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        CloseHandle(handle)


_process_names: dict[int, str] = {}


def process_path(pid: int) -> str:
    """Full image path of a process, e.g. `C:\\...\\chrome.exe` ("" if unknown)."""
    if pid in _process_names:
        return _process_names[pid]
    path = ""
    handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if handle:
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                path = buffer.value
        finally:
            CloseHandle(handle)
    # Cached either way: a dead pid answers "" just as consistently as a live one.
    _process_names[pid] = path
    return path


def process_name(pid: int) -> str:
    """Executable name of a process, lowercased and without `.exe` ("chrome")."""
    path = process_path(pid)
    if not path:
        return ""
    stem = os.path.basename(path)
    return stem[:-4].lower() if stem.lower().endswith(".exe") else stem.lower()


def terminate_process_tree(pid: int, force: bool = True) -> bool:
    """Stop a process and everything it started: Windows' answer to SIGTERM/SIGKILL.

    There is no process *group* to signal the way `killpg` does on POSIX -- and
    no way to ask nicely either -- so the polite step is WM_CLOSE to the windows
    (see `close_window`) and the firm step is `taskkill /T`.
    """
    args = ["taskkill", "/PID", str(int(pid)), "/T"] + (["/F"] if force else [])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise Win32Error(f"taskkill failed: {exc}", api="taskkill") from exc
    if process_alive(pid):
        message = (result.stderr or result.stdout or "").strip()
        raise Win32Error(f"could not stop process {pid}: {message or 'unknown error'}",
                         api="taskkill")
    return result.returncode == 0


def open_path(path: str) -> None:
    """Let the shell open a file, shortcut or URL, as double-clicking it would."""
    _check(int(ShellExecuteW(None, "open", str(path), None, None, SW_SHOWNORMAL)) > 32,
           "ShellExecute")


def open_path_elevated(command) -> None:
    """Start a command behind the UAC prompt (the shell's "runas" verb).

    ShellExecute detaches the child and gives no handle on it, so this is
    fire-and-forget by design: the user answers the prompt, the process runs with
    a full administrator token, and we never see its output.
    """
    parts = [str(part) for part in command]
    if not parts:
        raise Win32Error("nothing to elevate", api="ShellExecute")
    executable, arguments = parts[0], (subprocess.list2cmdline(parts[1:]) if len(parts) > 1
                                       else None)
    _check(int(ShellExecuteW(None, "runas", executable, arguments, None,
                             SW_SHOWNORMAL)) > 32, "ShellExecute(runas)")


def process_is_elevated() -> bool:
    """True when the current process holds an administrator token.

    Matters for input: Windows forbids `SendInput` into a window whose process runs
    with higher integrity than ours, so an elevated terminal cannot type into a
    normal app -- and, worse, neither can it when a normal terminal types into an
    elevated one.
    """
    class TOKEN_ELEVATION(ctypes.Structure):
        _fields_ = [("TokenIsElevated", wintypes.DWORD)]

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY,
                                     ctypes.byref(token)):
        return False
    try:
        elevation = TOKEN_ELEVATION()
        returned = wintypes.DWORD(0)
        if not advapi32.GetTokenInformation(token, TOKEN_ELEVATION_CLASS,
                                            ctypes.byref(elevation),
                                            ctypes.sizeof(elevation), ctypes.byref(returned)):
            return False
        return bool(elevation.TokenIsElevated)
    finally:
        kernel32.CloseHandle(token)


# --- virtual desktops (IVirtualDesktopManager) --------------------------------------------

CLSID_VirtualDesktopManager = "{A5CD92FF-29BE-454C-8D04-D82879FB3F1B}"
IID_IVirtualDesktopManager = "{2E941141-7F97-4756-BA1D-9DECDE894A3D}"

_COM_READY = False
_manager: int | None = None
_unavailable_reason: str | None = None


def _com_ready() -> None:
    global _COM_READY
    if not _COM_READY:
        # RPC_E_CHANGED_MODE just means somebody else chose the apartment model
        # already, which is fine: the calls below are still valid on that thread.
        CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        _COM_READY = True


def _guid(text: str) -> GUID:
    guid = GUID()
    _check(CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(guid)) == 0, "CLSIDFromString")
    return guid


def _method(interface: int, index: int, restype, *argtypes):
    """Bind the `index`-th entry of a COM vtable (0 is IUnknown::QueryInterface)."""
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(LPVOID))).contents
    return ctypes.WINFUNCTYPE(restype, LPVOID, *argtypes)(table[index])


def _hresult(hr: int, what: str) -> int:
    if hr < 0:
        raise Win32Error(f"{what} failed (0x{hr & 0xFFFFFFFF:08X})", api=what, code=hr)
    return hr


def manager():
    """The IVirtualDesktopManager singleton, or None where Windows has no desktops.

    Virtual desktops need Windows 10 1607+ (build 14393). Anything older, plus
    most Server editions and containers, does not expose them; there the callers
    fall back to a single desktop that always exists.
    """
    global _manager, _unavailable_reason
    if _manager is not None:
        return _manager
    if _unavailable_reason is not None:
        return None
    _com_ready()
    out = LPVOID()
    try:
        hr = CoCreateInstance(ctypes.byref(_guid(CLSID_VirtualDesktopManager)), None,
                              CLSCTX_INPROC_SERVER, ctypes.byref(_guid(IID_IVirtualDesktopManager)),
                              ctypes.byref(out))
    except OSError as exc:  # pragma: no cover - ole32 is always present on Windows
        _unavailable_reason = str(exc)
        return None
    if hr != 0 or not out:
        major, minor, build = os_version()
        _unavailable_reason = (
            f"this Windows has no virtual desktop support "
            f"(IVirtualDesktopManager returned 0x{hr & 0xFFFFFFFF:08X} on build "
            f"{build}); it needs Windows 10 1607 or newer"
        )
        del major, minor
        return None
    _manager = out.value
    return _manager


def virtual_desktops_supported() -> bool:
    return manager() is not None


def virtual_desktops_unavailable_reason() -> str | None:
    return _unavailable_reason


def _desktop_id(interface: int) -> str:
    guid = GUID()
    _hresult(_method(interface, 3, ctypes.c_long, ctypes.POINTER(GUID))(
        interface, ctypes.byref(guid)), "IVirtualDesktop::GetID")
    return str(guid)


def _desktop_name(interface: int) -> str:
    out = ctypes.c_wchar_p()
    _hresult(_method(interface, 4, ctypes.c_long, ctypes.POINTER(ctypes.c_wchar_p))(
        interface, ctypes.byref(out)), "IVirtualDesktop::GetName")
    if not out:
        return ""
    name = out.value or ""
    CoTaskMemFree(ctypes.cast(out, LPVOID))  # the callee allocated the string
    return name


def _release(interface: int) -> None:
    _method(interface, 2, ctypes.c_ulong)(interface)


def _desktop_count() -> int:
    handle = manager()
    return 0 if handle is None else int(_method(handle, 10, ctypes.c_uint)(handle))


def _desktop_at(position: int) -> int | None:
    handle = manager()
    if handle is None:
        return None
    return _method(handle, 12, LPVOID, wintypes.UINT)(handle, position) or None


def desktop_id_at(position: int) -> str:
    """Windows' own 0-based desktop index, as an id (GUID string)."""
    interface = _desktop_at(position)
    if interface is None:
        raise Win32Error(f"there is no virtual desktop at position {position}",
                         api="GetDesktopByIndex")
    try:
        return _desktop_id(interface)
    finally:
        _release(interface)


def desktop_name_at(position: int) -> str:
    """Windows' own name for a desktop (empty on builds that cannot name them)."""
    interface = _desktop_at(position)
    if interface is None:
        raise Win32Error(f"there is no virtual desktop at position {position}",
                         api="GetDesktopByIndex")
    try:
        return _desktop_name(interface)
    finally:
        _release(interface)


def desktop_count() -> int:
    return _desktop_count()


def desktop_position_of(desktop_id: str) -> int | None:
    wanted = desktop_id.strip("{}").upper()
    for position in range(_desktop_count()):
        if desktop_id_at(position).strip("{}").upper() == wanted:
            return position
    return None


def _user32_export(name: str):
    """user32!<GetWindowDesktopId> / <MoveWindowToDesktop>, when the build has them.

    Windows 10 1703+ exports both without documenting them, and they are cheaper
    than a COM round trip for the per-window queries. Older builds fall back to
    the COM interface.
    """
    return getattr(user32, name, None)


def window_desktop_id(hwnd: int) -> str | None:
    """Which virtual desktop a window lives on, as an id (None when it has none)."""
    getter = _user32_export("GetWindowDesktopId")
    if getter is not None:
        guid = GUID()
        return str(guid) if getter(wintypes.HWND(hwnd), ctypes.byref(guid)) == 0 else None
    handle = manager()
    if handle is None:
        return None
    guid = GUID()
    get_id = _method(handle, 4, ctypes.c_long, wintypes.HWND, ctypes.POINTER(GUID))
    return str(guid) if get_id(handle, wintypes.HWND(hwnd), ctypes.byref(guid)) == 0 else None


def _desktop_by_id(desktop_id: str) -> int:
    position = desktop_position_of(desktop_id)
    if position is None:
        raise Win32Error(f"there is no virtual desktop with id {desktop_id}",
                         api="MoveWindowToDesktop")
    interface = _desktop_at(position)
    if interface is None:  # pragma: no cover - it was there a moment ago
        raise Win32Error(f"there is no virtual desktop with id {desktop_id}",
                         api="MoveWindowToDesktop")
    return interface


def move_window_to_desktop(hwnd: int, desktop_id: str) -> None:
    """Move a window to the virtual desktop with that id."""
    if not window_exists(hwnd):
        raise Win32Error(f"window {hwnd} no longer exists", api="IsWindow", code=1400)
    raw = _user32_export("MoveWindowToDesktop")
    if raw is not None:
        text = desktop_id if desktop_id.startswith("{") else "{" + desktop_id + "}"
        if raw(wintypes.HWND(hwnd), ctypes.byref(_guid(text))) == 0:
            return
    interface = _desktop_by_id(desktop_id)
    handle = manager()
    try:
        _hresult(_method(handle, 5, ctypes.c_long, wintypes.HWND, LPVOID)(
            handle, wintypes.HWND(hwnd), interface), "IVirtualDesktopManager::MoveWindowToDesktop")
    finally:
        _release(interface)


def create_desktop(name: str) -> str:
    """Create a virtual desktop and return its id.

    Windows only ever appends: there is no insert position, and a new desktop is
    created on the background by default. An empty `name` gives the anonymous
    "Desktop N" Windows itself uses.
    """
    handle = manager()
    if handle is None:
        raise Win32Error(virtual_desktops_unavailable_reason() or "no virtual desktops",
                         api="CreateDesktop")
    out = LPVOID()
    create = _method(handle, 6, ctypes.c_long, wintypes.LPCWSTR, wintypes.LPCWSTR,
                     wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD, ctypes.POINTER(LPVOID))
    _hresult(create(handle, ctypes.c_wchar_p(name), None, None, False, 0, ctypes.byref(out)),
             "IVirtualDesktopManager::CreateDesktop")
    try:
        return _desktop_id(out.value)
    finally:
        _release(out.value)


def remove_desktop(desktop_id: str) -> None:
    """Close a virtual desktop. Windows moves its windows, it never closes them."""
    handle = manager()
    if handle is None:
        raise Win32Error(virtual_desktops_unavailable_reason() or "no virtual desktops",
                         api="CloseDesktop")
    interface = _desktop_by_id(desktop_id)
    try:
        _hresult(_method(handle, 8, ctypes.c_long, LPVOID)(handle, interface),
                 "IVirtualDesktopManager::CloseDesktop")
    finally:
        _release(interface)


def switch_to_desktop(desktop_id: str) -> None:
    """Show a virtual desktop to the user."""
    handle = manager()
    if handle is None:
        raise Win32Error(virtual_desktops_unavailable_reason() or "no virtual desktops",
                         api="SwitchToDesktop")
    interface = _desktop_by_id(desktop_id)
    try:
        _hresult(_method(handle, 7, ctypes.c_long, LPVOID)(handle, interface),
                 "IVirtualDesktopManager::SwitchToDesktop")
    finally:
        _release(interface)


# --- screenshots -------------------------------------------------------------------------

def _grab(source_dc: int, width: int, height: int, source_x: int, source_y: int,
          hwnd: int | None = None) -> tuple[int, int, bytes]:
    """Copy a rectangle (or compose a window) into a top-down RGB buffer."""
    mem_dc = CreateCompatibleDC(source_dc)
    if not mem_dc:
        raise _error("CreateCompatibleDC")
    bitmap = CreateCompatibleBitmap(source_dc, width, height)
    if not bitmap:
        DeleteDC(mem_dc)
        raise _error("CreateCompatibleBitmap")
    previous = SelectObject(mem_dc, HGDIOBJ(bitmap))
    try:
        if hwnd is None:
            ok = BitBlt(mem_dc, 0, 0, width, height, source_dc, source_x, source_y,
                        SRCCOPY | CAPTUREBLT)
        else:
            # PW_RENDERFULLCONTENT asks DWM to compose the window itself, which is
            # what makes this work for a covered (but not minimized) window.
            ok = PrintWindow(wintypes.HWND(hwnd), mem_dc, PW_RENDERFULLCONTENT)
        _check(bool(ok), "PrintWindow" if hwnd is not None else "BitBlt")
        header = BITMAPINFOHEADER()
        header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        header.biWidth = width
        header.biHeight = -height  # negative: top-down, so no flip is needed later
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = BI_RGB
        buffer = ctypes.create_string_buffer(width * 4 * height)
        info = BITMAPINFO()
        info.bmiHeader = header
        _check(bool(GetDIBits(mem_dc, bitmap, 0, height, buffer, ctypes.byref(info),
                              DIB_RGB_COLORS)), "GetDIBits")
        return width, height, _bgra_to_rgb(bytes(buffer))
    finally:
        SelectObject(mem_dc, previous)
        DeleteObject(bitmap)
        DeleteDC(mem_dc)


def _bgra_to_rgb(pixels: bytes) -> bytes:
    """Drop the alpha byte GDI always fills in: a PNG wants three channels."""
    out = bytearray(len(pixels) // 4 * 3)
    out[0::3] = pixels[2::4]
    out[1::3] = pixels[1::4]
    out[2::3] = pixels[0::4]
    return bytes(out)


def _looks_blank(pixels: bytes) -> bool:
    """True when the capture is one flat colour -- PrintWindow's usual failure."""
    sample = pixels[:4096 - 4096 % 4]
    return bool(sample) and sample == sample[:4] * (len(sample) // 4)


def capture_screen(rect: RECT | None = None) -> tuple[int, int, bytes]:
    """The screen, or a rectangle of it, as (width, height, RGB bytes)."""
    area = rect or virtual_screen_rect()
    width, height = area.width, area.height
    if width <= 0 or height <= 0:
        raise Win32Error("the screen reports no size (is a display connected?)",
                         api="GetSystemMetrics")
    screen_dc = GetDC(None)
    if not screen_dc:
        raise _error("GetDC")
    try:
        return _grab(screen_dc, width, height, area.left, area.top)
    finally:
        ReleaseDC(None, screen_dc)


def capture_window(hwnd: int) -> tuple[int, int, bytes]:
    """A single window as (width, height, RGB bytes), even if it is behind others."""
    if not window_exists(hwnd):
        raise Win32Error(f"window {hwnd} no longer exists", api="IsWindow", code=1400)
    rect = window_rect(hwnd)
    width, height = rect.width, rect.height
    if width <= 0 or height <= 0:
        raise Win32Error("the window has no size (it may be minimized)", api="GetWindowRect")
    screen_dc = GetDC(None)
    if not screen_dc:
        raise _error("GetDC")
    try:
        try:
            result = _grab(screen_dc, width, height, 0, 0, hwnd=hwnd)
            if not _looks_blank(result[2]):
                return result
        except Win32Error:
            pass  # DWM would not compose it; fall back to copying the screen
        # PrintWindow gave nothing (common for hardware-accelerated windows), so
        # copy the pixels the user can actually see instead.
        return _grab(screen_dc, width, height, rect.left, rect.top)
    finally:
        ReleaseDC(None, screen_dc)


# --- interactive region selection ----------------------------------------------------------

_PICKER_CLASS = "ControlMarionetteRegionPicker"
_YELLOW = 0x0000FFFF  # COLORREF is 0x00BBGGRR
_ORANGE = 0x0080FFFF
_picker_proc: "_PickerProc | None" = None  # keeps the ctypes callback alive


class _PickerState:
    """What the drag-select window knows: where the drag started and where it ended."""

    def __init__(self):
        self.start: tuple[int, int] | None = None
        self.current: tuple[int, int] | None = None
        self.result: RECT | None = None
        self.cancelled = False

    def rect(self) -> RECT | None:
        if self.start is None or self.current is None:
            return None
        (x1, y1), (x2, y2) = self.start, self.current
        area = RECT(x1, y1, x2, y2).normalized()
        return area if area.width > 1 and area.height > 1 else None


def _point_from_lparam(lparam: int) -> tuple[int, int]:
    """Mouse coordinates out of an LPARAM: two signed 16-bit halves."""
    x, y = lparam & 0xFFFF, (lparam >> 16) & 0xFFFF
    return (x - 0x10000 if x & 0x8000 else x, y - 0x10000 if y & 0x8000 else y)


def _picker_wndproc(hwnd, message, wparam, lparam):
    state: _PickerState = _picker_wndproc.state
    if message == WM_LBUTTONDOWN:
        state.start = _point_from_lparam(lparam)
        state.current = state.start
    elif message == WM_MOUSEMOVE and state.start is not None:
        state.current = _point_from_lparam(lparam)
        InvalidateRect(hwnd, None, False)
    elif message == WM_LBUTTONUP:
        state.current = _point_from_lparam(lparam)
        state.result = state.rect()
        state.cancelled = state.result is None
        DestroyWindow(hwnd)
    elif message == WM_RBUTTONDOWN or (message == WM_KEYDOWN and wparam == VK_ESCAPE):
        state.cancelled = True
        DestroyWindow(hwnd)
    elif message == WM_ERASEBKGND:
        return 1  # keep the translucent overlay; erasing would flash it opaque
    elif message == WM_PAINT:
        paint = PAINTSTRUCT()
        hdc = BeginPaint(hwnd, ctypes.byref(paint))
        try:
            area = state.rect()
            if area is not None:
                outer = CreateSolidBrush(_YELLOW)
                inner = CreateSolidBrush(_ORANGE)
                try:
                    FrameRect(hdc, ctypes.byref(area), outer)
                    shrunk = RECT(area.left + 1, area.top + 1, area.right - 1,
                                  area.bottom - 1)
                    FrameRect(hdc, ctypes.byref(shrunk), inner)
                finally:
                    DeleteObject(outer)
                    DeleteObject(inner)
        finally:
            EndPaint(hwnd, ctypes.byref(paint))
    elif message == WM_DESTROY:
        PostQuitMessage(0)
        return 0
    return DefWindowProcW(hwnd, message, wparam, lparam)


_picker_wndproc.state = _PickerState()  # type: ignore[attr-defined]


def select_region() -> RECT | None:
    """Let the user drag a rectangle; returns it, or None if they cancelled.

    A translucent topmost window covers the whole virtual screen and paints the
    rubber band, so what is being selected stays visible. Escape, a right click or
    a zero-size drag cancels.
    """
    screen = virtual_screen_rect()
    _com_ready()
    state = _PickerState()
    _picker_wndproc.state = state  # type: ignore[attr-defined]
    instance = GetModuleHandleW(None)
    window_class = WNDCLASSW()
    window_class.style = CS_HREDRAW | CS_VREDRAW
    window_class.lpfnWndProc = ctypes.cast(_WNDPROC(_picker_wndproc), LPVOID)
    window_class.hInstance = wintypes.HINSTANCE(instance)
    window_class.lpszClassName = _PICKER_CLASS
    if not RegisterClassW(ctypes.byref(window_class)) and \
            ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
        raise _error("RegisterClassW")
    hwnd = CreateWindowExW(WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_TOPMOST | WS_EX_NOACTIVATE,
                           _PICKER_CLASS, "", WS_POPUP, screen.left, screen.top,
                           screen.width, screen.height, None, None,
                           wintypes.HINSTANCE(instance), None)
    if not hwnd:
        UnregisterClassW(_PICKER_CLASS, wintypes.HINSTANCE(instance))
        raise _error("CreateWindowExW")
    message = MSG()
    try:
        SetLayeredWindowAttributes(hwnd, 0, 30, LWA_ALPHA)
        ShowWindow(hwnd, SW_SHOW)
        SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        while GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            TranslateMessage(ctypes.byref(message))
            DispatchMessageW(ctypes.byref(message))
    finally:
        if window_exists(_hwnd(hwnd)):
            DestroyWindow(hwnd)
        UnregisterClassW(_PICKER_CLASS, wintypes.HINSTANCE(instance))
    return None if state.cancelled else state.result
