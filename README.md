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
reads `CONTROL_SUDO_PASSWORD` from `.env` and pipes it to `sudo -S` over stdin.

```
cp .env.example .env
chmod 600 .env
# edit .env and set CONTROL_SUDO_PASSWORD="your-password"
```

How the password is protected:

- **It never enters `os.environ`.** `load_env()` skips secret keys, and `sudo.py`
  reads the value straight from the file (`env.get_secret`) at the moment it runs.
  So the password doesn't appear in `/proc/<pid>/environ`, and child processes
  (gtk-launch, the apps it starts, Chromium, sudo itself) don't inherit it. If you
  export it in your shell yourself, it is still stripped from every child's
  environment.
- **It's never an argument and never logged.** Arguments are visible to every user
  through `ps`.
- **The file must be private.** If an `.env` is readable by group or others (for
  example mode `644`), `control` prints a warning on stderr, and **refuses to read
  the password from it**. It also refuses a file owned by another user. Set
  `CONTROL_ENV_STRICT=1` to reject insecure `.env` files entirely, even ones
  without secrets.

`.env` is gitignored. The password is only used by the narrow setup helpers
(`control.sudo.restart_ydotoold`, `control.sudo.ensure_uinput_group`). Everyday
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

Two guarantees make `--wait` safe to chain with input commands:

- **Only a new window counts.** Before running `gtk-launch`, `control` records the
  ids of the windows that already match. While waiting, it ignores those ids. So
  if Kdenlive is already open, `--wait` waits for the *new* window instead of
  returning the old one immediately. In Python, take the baseline yourself with
  `windows.window_ids(pattern)` and pass it to
  `windows.wait_for_window(pattern, known_ids=...)`.
- **The new window gets focus.** Once found (and moved to its desktop, if any), the
  window is activated, so the next `control type` goes to it rather than to
  whatever had focus before. Pass `--no-activate` (or `activate=False`) to leave
  focus alone. Note that focusing a window on another desktop makes KWin switch to
  that desktop.

To wait for a window you didn't launch (a dialog, say), use
`control win wait PATTERN [--timeout S]`. It returns as soon as any matching window
exists and focuses it.

Tip for token-efficient automation: once you've located a window, prefer driving it
with keyboard shortcuts (`control key ...`) and cached window state (id/geometry from
`control win list --json`) over repeated screenshot-and-inspect round-trips — re-query
the window only when you expect its state actually changed.

## Asking other AIs for a second opinion

`control browser` knows a few AI chat sites by name, so an agent (Claude, for
instance) can check its reasoning against another model with one command.

```
control browser sites                        # the catalog; "ask" marks the drivable ones
control browser goto gemini                  # aliases work anywhere a URL does
control browser ask gemini "¿Cómo optimizo esta función en Python?"
control --json browser ask chatgpt "Review this approach: ..."
cat snippet.py | control browser ask claude -          # long prompts / code from stdin
control browser ask gemini "¿Y con numpy?" --continue  # follow up in the same conversation
```

| Site | Alias(es) | `goto` | `ask` |
|---|---|---|---|
| ChatGPT | `chatgpt`, `gpt`, `openai` | yes | yes |
| Gemini | `gemini`, `bard` | yes | yes |
| Claude | `claude` | yes | yes |
| Perplexity, Copilot, DeepSeek, Mistral Le Chat | `perplexity`, `copilot`, `deepseek`, `mistral` | yes | no (bookmarks) |

`goto` also accepts full URLs and bare hosts (`example.com` becomes
`https://example.com`). An unknown word is an error, never a web search.

### Log in once

These sites need an account, and `ask` never types credentials. Log in by hand
once. The cookies stay in the persistent Chromium profile, and every later
command reuses them:

```
control browser login gemini        # opens a window; returns once the chat box appears
```

`login` is always headed, even with `--headless`, because you type the password
in the window. With a session (`--session NAME`), each agent has its own
profile, so each agent logs in separately. Two commands can't use the same
profile at the same time: Chromium locks it, and `control` reports that as a
clear error instead of a stack trace.

### How `ask` works

1. It opens the site in the session's profile, or reuses a tab already on it.
2. It waits for the chat box. If it gets redirected to a sign-in page, or sees
   logged-out markers, it fails with `login_required`.
3. It counts the assistant messages already on the page, then types the question
   and clicks Send (or presses Enter).
4. It waits for a **new** assistant message. The answer counts as finished once
   its text hasn't changed for `--settle` seconds (default 2) and the Stop button
   is gone.
5. It prints the answer. With `--json`:

   ```json
   {"site": "gemini", "url": "https://gemini.google.com/app/…", "response": "…",
    "complete": true, "elapsed": 7.4, "question_chars": 38, "continued": false,
    "profile": "…/browser-profile"}
   ```

If `--timeout` (default 180 s) runs out mid-answer, you get the partial text with
`"complete": false` (and a warning on stderr in text mode). No answer at all
fails with `response_timeout`. `--continue` reopens the last conversation this
profile had with that site. It's remembered in `control-ai-threads.json` inside
the profile.

Errors an agent can branch on: `login_required`, `selector_not_found`,
`response_timeout`, `unknown_site`, `browser_error`.

### When a site changes its layout

The selectors in `browser.AI_SITES` are ordered fallbacks, and they cover English
and Spanish UIs. They will still break when a site redesigns its page. That
shows up as `selector_not_found`. Fix it without touching code by pointing
`CONTROL_AI_SITES` at a JSON file. Selectors from the file are tried **before**
the built-in ones. The same file can add a new site:

```json
{
  "gemini": {"input": ["div.new-composer[contenteditable='true']"]},
  "local":  {"url": "http://localhost:3000/chat", "input": ["textarea"],
             "send": ["button[type=submit]"], "response": [".assistant"],
             "busy": [".spinner"]}
}
```

Fields: `url`, `input`, `send`, `response`, `busy`, `login`, `login_hosts`,
`hosts`, `description`.

### Things to keep in mind

- **Whatever you ask is sent to that provider**, under the account logged in to
  the profile. Don't paste secrets, credentials or private code you aren't allowed
  to share.
- Automating a web UI may be restricted by the provider's terms of service. Use
  your own account, at a human pace. If you need volume, use the provider's API.
- Headless Chromium is more likely to hit bot checks (Cloudflare and similar),
  which is why the browser runs headed by default.

## Running several agents in parallel

Several agents can drive the same Plasma session at once. Each one gets its own
**KWin virtual desktop**, browser profile and log. All of them share **one input
queue**, because keyboard and mouse events go through a single global device.

### 1. Give each agent a workspace

```
control session init --name agent_1     # creates the virtual desktop "control:agent_1"
control session init --name agent_2     # creates "control:agent_2"
```

`session init` is idempotent. Running it again reattaches to the same desktop, so
an orchestrator can call it every time an agent starts. Two processes can run it
for the same name at the same moment and only one desktop is created. Use
`--desktop N` to pin an existing desktop instead, and `--activate` to switch to
it.

Each session keeps its state in `~/.local/share/control/sessions/<name>/`:

| Path | Purpose |
|---|---|
| `session.json` | the desktop number/UUID the agent owns |
| `browser-profile/` | a separate Chromium profile (cookies, logins, storage) |
| `control.log` | the agent's own log (`Session.log(...)`) |
| `.env` *(optional)* | per-agent config, loaded automatically |

### 2. Point each agent at its session

Pick one of these:

```
export CONTROL_SESSION_ID=agent_1             # for the whole shell
control --session agent_1 win list            # for one command
control --env-file agents/agent_1.env ...     # a file that sets CONTROL_SESSION_ID=agent_1
```

When a session is active, window and app commands are **scoped to its desktop by
default**:

```
control --session agent_1 app launch org.kde.kcalc   # the window is moved to agent_1's desktop
control --session agent_1 win list --json            # only agent_1's windows
control --session agent_2 win close kcalc            # can't see or close agent_1's kcalc
```

An explicit `--desktop` overrides the session. It takes a 1-based number or a
desktop **name**:

```
control app launch org.kde.kcalc --desktop agent_7   # creates desktop "agent_7" if missing
control win list --json --desktop agent_7            # read-only: fails if it doesn't exist
control win send kcalc scratch                       # moves the window, creating "scratch"
```

Only commands that place something (`app launch`, `win send`) create a missing
named desktop. Queries never create desktops as a side effect. Creation is
serialized between processes, so two agents asking for the same new name get one
desktop.

With no session and no `--desktop`, commands act on every desktop, as they always
did. `--desktop` is accepted by `win list|activate|close|move|resize|send|wait`,
`app launch`, `snapshot`, and by `click`, `type`, `key` and `shot` (where it
scopes `--window`).

If `CONTROL_SESSION_ID` names a session that was never initialised, desktop-scoped
commands fail with an error instead of silently acting on every desktop.

Sessions remember their desktop by KWin UUID, not just by number. When another
agent removes a desktop, KWin renumbers the ones after it; the session looks its
UUID up and follows the new number, so it never acts on another agent's desktop.

`app launch --desktop` claims **only the window that launch created**. It records
which matching windows already existed and waits for a new one. Launches of the
same app are serialized between agents, so if two agents start Firefox at the
same time, each still gets its own window.

Environment precedence, highest first: variables already exported in the shell,
then `--env-file`, then the session's `.env`, then the repo `.env`.

### 3. Input is serialized automatically

`ydotoold` writes to a single kernel `uinput` device. If two agents typed at the
same time, their keystrokes would mix into one stream. So every `ydotool` call
takes a file lock on `/tmp/control_input.lock` (`fcntl.flock`, plus a thread
lock inside each process). Other agents wait for their turn instead of
interleaving.

- **Gestures are atomic.** `drag`, `double_click` and `click` with coordinates
  hold the lock for the whole sequence, so no other agent can act between a
  button press and its release.
- **You can group steps.** In Python, hold the lock across several actions:

  ```python
  from control import input as inp

  with inp.input_lock():
      inp.click("left", 400, 300)
      inp.type_text("hello")
      inp.press_key("enter")
  ```

- **Focus and input are atomic from the CLI.** Keyboard focus is as global as
  `uinput`. If agent A activated its window and released the lock before typing,
  agent B could activate its own window in between, and A's text would land in
  B's window. `--window PATTERN` avoids this: it takes the lock, focuses the
  window, waits until KWin reports it active, sends the input, then releases.

  ```
  control --session agent_1 type "hello" --window kcalc
  control --session agent_1 key ctrl s --window "Kate"
  control --session agent_1 click -x 400 -y 300 --window firefox
  control --session agent_1 shot window --window kcalc
  echo "long text" | control type - --window kate     # read the text from stdin
  ```

  If the window doesn't take focus within a second, no input is sent and the
  command fails with `focus_error`.
- **Tuning.** `CONTROL_INPUT_LOCK_TIMEOUT` (seconds, default 30) sets how long to
  wait before raising `LockTimeout`. `CONTROL_INPUT_LOCK` changes the lock path.
  `CONTROL_INPUT_LOCK_DISABLED=1` turns locking off for single-agent use.

Keep the time you hold the lock short. While one agent holds it, every other
agent's input waits.

### 4. State queries never block

`win list`, `session status` and `snapshot` never take the input lock, so they
return immediately even while another agent is typing. They also run their
`kdotool`/D-Bus calls concurrently with `asyncio`, instead of one after another
per window.

```
control session status --json   # which agent owns which desktop, and its windows
control snapshot --pretty       # desktops + sessions + windows + current desktop, in one JSON
```

In Python: `windows.search_async(...)`, `windows.list_desktops_async()`,
`session.status_async()`.

`windows.search()` keeps its original behavior (sequential lookups) for backward
compatibility. `windows.list_windows()` is the concurrent version.

### Per-agent traces and files

With an active session:

- every command appends one line to that session's `control.log`, with the command
  name and its outcome (`type -> ok`, `win close -> window_not_found`). Arguments
  are never logged, since they can contain typed text or form contents;
- screenshots go to `sessions/<name>/screenshots/` instead of the shared
  `/tmp/control-screenshots/`;
- `control browser ...` uses `sessions/<name>/browser-profile/`, so agents never
  share cookies or fight over Chromium's profile lock.

### Managing virtual desktops directly

```
control desktop list [--json]                # * marks the current desktop
control desktop create "control:scratch"     # add a desktop (--ensure reuses an existing one)
control desktop remove "control:scratch"     # by name or 1-based number
control desktop switch 3                     # or by name: control desktop switch control:scratch
control desktop current
control win send kcalc 3                     # move a window to desktop 3
```

Desktops are created and removed through KWin's `org.kde.KWin.VirtualDesktopManager`
D-Bus API via `gdbus`, because `kdotool` can't create desktops.
Numbering: the CLI and `kdotool` count desktops from 1, while KWin's D-Bus API
counts positions from 0. `windows.Desktop` has both, `.number` and `.position`.

### Cleaning up

```
control session end --name agent_1 --remove-desktop
```

This deletes the session record and, with `--remove-desktop`, its virtual
desktop. KWin moves any remaining windows to another desktop; it does not close
them. The browser profile and log are kept so you can inspect them afterwards.

## Machine-readable output for agents

Every command supports JSON, so an agent never has to parse prose. Turn it on in
any of three ways:

```
control --json win list          # global flag
control win list --json          # after the subcommand
export CONTROL_JSON=1            # for every command in this shell
```

In JSON mode:

- **A successful command prints exactly one JSON value on stdout.** Queries print
  their data (`win list` prints an array of `{"id", "title", "class", "desktop"}`).
  Actions print what they did, for example
  `{"ok": true, "action": "close", "window": {...}}`. `type` reports only the
  number of characters typed, never the text.
- **A failed command prints one JSON object on stdout** and exits with status 1:

  ```json
  {"error": "no window matching 'Nope' on desktop 2", "hint": "list candidates with: control win list --json", "type": "window_not_found"}
  ```

  `type` is a stable, snake_case identifier to branch on (`window_not_found`,
  `timeout`, `desktop_error`, `lock_timeout`, `session_error`, `unknown_key`,
  `focus_error`, `kdotool_error`, `ydotool_error`, ...). `hint` is the next command
  to try.
- **Usage errors are JSON too** (`"type": "usage_error"`, exit status 2).
- **Unexpected errors don't print a traceback.** They are reported as
  `"type": "unexpected"`. Set `CONTROL_DEBUG=1` to get the traceback.

Warnings (such as an insecure `.env`) go to stderr, so stdout always stays valid
JSON.

Without JSON mode, output is tab-separated text as before, and errors go to
stderr as `[ERROR] <type>: <message> (<hint>)`.
