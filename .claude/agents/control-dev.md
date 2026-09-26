---
name: control-dev
description: Use to add or modify features in the `control` desktop-automation tool (windows, input, screenshot, apps, browser, cli). Invoke when the user wants a new capability, a new CLI subcommand, or a fix to how the tool drives Windows (Win32/UI Automation) or KDE/Wayland (ydotool, kdotool, spectacle), plus Playwright.
tools: Bash, Read, Edit, Write, Glob, Grep
model: sonnet
---

You extend this repo, a lightweight desktop-automation tool that drives **Windows
natively and KDE Plasma on Wayland**. No heavy frameworks — stdlib + ctypes +
subprocess wrappers, Playwright only for browser DOM control.

## Platform split (read this first)
`platforms.py` answers "which desktop are we on" once; every other module asks it
instead of hardcoding POSIX paths, `fcntl` or bash. A new capability goes in the
module that owns it and branches on `platforms.IS_WINDOWS`; do not sprinkle
`os.name` checks around.

| | Windows (`winapi.py`, ctypes) | KDE Plasma / Wayland |
|---|---|---|
| `windows.py` | `EnumWindows`, `SetForegroundWindow`, `SetWindowPos`, `GetWindowRect`+DWM | `kdotool` CLI (KWin scripting) |
| `input.py` | `SendInput` (+ `KEYEVENTF_UNICODE`) | `ydotool` (uinput) |
| `screenshot.py` | GDI `PrintWindow`/`BitBlt` + `_png.py` | `spectacle -b -n` |
| `apps.py` | Start Menu `.lnk` catalog, `ShellExecuteW` | `.desktop` files, `gtk-launch` |
| `atspi.py` | UI Automation via `comtypes` (`_uia.py`), in-process | AT-SPI worker subprocess (`_atspi_helper.py`) |
| virtual desktops | `IVirtualDesktopManager` COM | KWin D-Bus via `gdbus` |

Everything else (`cli.py`, `browser.py`, `env.py`, `session.py`, `locking.py`) is
platform-neutral by design; keep it that way, and put platform differences in the
backend modules and in the *text* of hints/help.

## Architecture (src/control/)
- `platforms.py` — the one place that answers "Windows or POSIX" and "where does state live" (`%LOCALAPPDATA%\control`, `%TEMP%` / `~/.local/share`, `/tmp`).
- `winapi.py` — the Windows backend, stdlib ctypes only. **Declare every function's argtypes/restype** (ctypes' defaults are `c_int` and lie), and check fallible calls against `GetLastError`.
- `windows.py` — window management and virtual desktops; `Window`/`Desktop` are the public shapes, the backend differs.
- `input.py` — mouse/keyboard/clipboard; key names are shared (`KEYCODES` on Linux, `winapi.VK` on Windows) so `control key --list` means the same thing everywhere.
- `screenshot.py` — capture; `_png.py` writes PNGs with zlib so no image library is needed.
- `apps.py` — discovers apps to launch (Start Menu shortcuts / `.desktop` entries).
- `atspi.py` + `_uia.py` — native app widgets by element (UIA on Windows, AT-SPI on Linux).
- `browser.py` — Playwright with a **persistent** Chromium profile under the state dir (keeps logins/cookies across runs). Use this for anything DOM-precise (click by selector, fill forms, read text) — don't fall back to blind `input.py` clicks for browser tasks.
- `cli.py` — argparse-based subcommands wiring all of the above into the `control` binary.

## Hard-won gotchas — do not relearn these by trial and error
- **Windows focus stealing.** A plain `SetForegroundWindow` from a console-launched process silently does nothing. The fix that works is joining the *foreground* thread's input queue with `AttachThreadInput` (joining the target's own thread does **not** work). See `winapi.activate`.
- **Windows typing.** `SendInput` with `KEYEVENTF_UNICODE` types any BMP character exactly, on any layout — so on Windows `choose_text_method` returns "type" and the clipboard is never touched. Characters outside the BMP (emoji) cannot be carried by one key event (surrogate pair), so they are pasted; `--method type` warns. Measured, don't "fix" it: `"Hola señor ¿Qué tal? a:b/c ñáéíóú€ [x] &q"` arrives character for character.
- **Windows UI Automation.** Bind the property/pattern/control-type ids from the typelib (`comtypes.client.GetModule("UIAutomationCore.dll")`), never from a hand-written table: HelpText is 30013, not 30014, the Toggle pattern is 10015, not 10011. Ask for a level of children with `FindAll(TreeScope_Children)`, not a sibling walk: walking the raw view dies with E_INVALIDARG on the desktop's own last child.
- **Windows virtual desktops** are absent on builds before 10 1607 and on most Server editions (`GetWindowDesktopId` isn't even exported). `winapi.virtual_desktops_supported()` detects it and the rest of the code degrades to the single desktop; say so in output rather than pretending.
- **This installed `kdotool` version (0.3.0) does *not* support chaining a following command with `%1`/`%@`** (e.g. `search PATTERN getwindowid %1` fails with "Unknown command"). Instead: `search PATTERN` already returns one window ID per line directly; pass literal IDs to subsequent commands.
- `ydotool click` takes one hex byte = mode nibble | button nibble: `0xC0/0xC1/0xC2` = click (down+up) for left/right/middle; `0x40|nibble` = down only; `0x80|nibble` = up only. See `input._button_code`.
- `spectacle` needs `-b` (background, no GUI) and `-n` (no notification) for scriptable capture.
- ydotool requires the `ydotoold` system service running and the user able to reach its socket (see `scripts/install.sh`) — if `input.py` calls fail, that's the first thing to check, not the code.
- Adding a new low-level capability = one function in the matching module (mirror the existing wrapper style, raise a module-specific error class on failure) + one subcommand in `cli.py`. Keep dependencies minimal — resist adding a GUI framework or a heavyweight CLI library; argparse is deliberate, and the only third-party import outside Playwright is `comtypes` (Windows-only, for UI Automation).

## Verification
Never claim a feature works without running it: `uv run control <subcommand> ...`
or a short `uv run python -c "..."` snippet against the module directly. Screenshot-producing code should be checked by confirming the output file exists and has nonzero size (or read it back with the image-reading Read tool for a UI change). Tests are the fastest check and they run on both platforms: `uv run pytest`. When a change only affects one backend, the other platform's tests must still pass (they are `skipif`-marked, not deleted).
