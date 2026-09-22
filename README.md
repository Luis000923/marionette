# control (marionette)

Full desktop control for KDE Plasma on Wayland: windows, input, screenshots, apps, and
browser automation. Stdlib + subprocess wrappers around `kdotool`, `ydotool`, and
`spectacle`; Playwright is used only for browser DOM control.

## Setup

```
uv sync
uv run playwright install chromium
sudo ./scripts/install.sh   # installs ydotool/kdotool, enables ydotoold, group perms
```

### Sudo password (optional)

A few setup/repair tasks genuinely need root (restarting `ydotoold`, fixing `uinput`
group membership). Rather than prompting interactively, `control.sudo.run_as_root`
reads a password from the `CONTROL_SUDO_PASSWORD` environment variable and pipes it to
`sudo -S` over stdin (never as a CLI argument, never logged).

```
cp .env.example .env
# edit .env and set CONTROL_SUDO_PASSWORD="your-password"
```

`.env` is gitignored and loaded automatically the first time `control.sudo.run_as_root`
is called. This is only used for the narrow root-requiring setup helpers
(`control.sudo.restart_ydotoold`, `control.sudo.ensure_uinput_group`) — everyday
commands never touch sudo.

## Waiting for slow-starting apps

Heavy apps (video editors, IDEs, office suites) can take several seconds to show a
window after launch. `control app launch` supports `--wait` to poll window state
instead of firing-and-forgetting:

```
control app launch org.kde.kdenlive --wait --timeout 45
```

This prints the new window's id/class/title once found (or exits non-zero if no
matching window appears within the timeout). Programmatically, `apps.launch(app_id,
wait=True, timeout=...)` returns a `windows.Window` (or `None` on timeout) instead of
the raw `subprocess.CompletedProcess`.

Tip for token-efficient automation: once you've located a window, prefer driving it
with keyboard shortcuts (`control key ...`) and cached window state (id/geometry from
`control win list --json`) over repeated screenshot-and-inspect round-trips — re-query
the window only when you expect its state actually changed.
