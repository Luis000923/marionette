# control (marionette)

Full desktop control for KDE Plasma on Wayland: windows, input, screenshots, apps, and
browser automation. Stdlib + subprocess wrappers around `kdotool`, `ydotool`, and
`spectacle`; Playwright is used only for browser DOM control.

## Setup

```
uv sync
uv run playwright install chromium
sudo ./scripts/install.sh   # installs ydotool/kdotool/wl-clipboard/AT-SPI, enables ydotoold, group perms
```

## Token efficiency for agents driving this tool

See [AGENT_GUIDELINES.md](AGENT_GUIDELINES.md) for the five-rule quick reference.
The rest of this section is the fuller narrative version of the same rules.

An LLM agent (Claude Code or otherwise) issuing these commands pays tokens for
everything it reads back — command output, and especially images. These habits
keep that cost down without losing verification quality:

1. **Skip `uv run`'s resolve step once the venv is synced.** `uv run control ...`
   re-checks (and sometimes reinstalls) the local package on every invocation,
   printing noise like `Building marionette... Uninstalled... Installed...`
   before the real output. Just call `control` instead — it is symlinked onto the
   PATH at `~/.local/bin/control`, so it resolves from any working directory:

   ```
   control win list --json
   ```

   Same output, none of the resolver chatter. Re-run `uv sync` (or fall back to
   `uv run`) only after actually changing dependencies.

2. **Prefer state you can already read over a screenshot.** A screenshot costs
   dramatically more tokens than text once it's read back as an image. Most
   verifications don't need one:
   - a window's title changing (`win list --json` before/after) proves an
     action landed (see the Brave search-and-verify example above);
   - a window disappearing from `win list --json` proves it closed;
   - in the browser, `text_content()` / `control browser snapshot` (DOM text)
     proves page state far more cheaply than a picture of the page.

   Reach for `shot`/`browser screenshot` only when the *visual* layout itself
   is what's being checked, not just "did the action happen".

3. **When a screenshot is unavoidable, take the smallest one that answers the
   question.** `shot window` (or `--window PATTERN`) instead of `shot full`;
   crop to the region that matters instead of the whole desktop.

4. **Let the tool filter, not the agent reading the output.** `win list brave`
   instead of `win list ""` followed by scanning the full list yourself —
   smaller output in, smaller output back.

5. **Batch independent steps into one shell call.** Launch + wait + verify as
   one `&&`-chained command instead of three separate tool round-trips, when
   the steps don't depend on inspecting intermediate output.

6. **Cache what you already queried.** Re-use a window's id/geometry from the
   last `win list --json` instead of re-querying state that hasn't changed.

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

## Launching apps

`control app launch ID` takes the app's `.desktop` id, which is often not the
command name: KCalc is `org.kde.kcalc`, Dolphin `org.kde.dolphin`. When the id
is wrong, the error's hint names the likely one — matching the id's last
segment, the display name, and typos:

```
$ control app launch kcalc
[ERROR] app_launch_error: gtk-launch: no such application kcalc (did you mean: org.kde.kcalc? ...)
$ control app list calc          # search ids and names
```

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

See "Token efficiency for agents driving this tool" above — once you've located a
window, prefer driving it with keyboard shortcuts (`control key ...`) and cached
window state over repeated screenshot-and-inspect round-trips.

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

## Controlling a browser you already have open

`control browser` normally launches its own Chromium with a persistent, isolated
profile. Sometimes you want the opposite: drive the Brave/Chrome/Chromium window
the *user* already has open, with their real logins and tabs.

### Chromium/Brave: Chrome DevTools Protocol (precise, DOM-level)

Persistent Chromium without Brave: `control browser launch [--browser chromium|brave]
--port 9222` starts a detached browser (Playwright's bundled Chromium by default) so tabs
survive between CLI calls; then use `control browser --cdp-port 9222 goto|fill|click|snapshot ...`.

Fastest path for Brave: `control browser brave` starts it with a loopback-only debug
port and a dedicated profile (Chromium 136+ ignores the debug port on the default
profile), or reuses one already up. Then `export CONTROL_CDP_PORT=9222` so every
`control browser ...` attaches without repeating `--cdp-port`. `browser snapshot`
caps output at 6000 chars (`--max-chars N`, `0` = all) and `--json` keeps real UTF-8.

Start the browser with a debug port open (do this once, before running any
`control browser` command against it):

```
brave --remote-debugging-port=9222
# or: chromium --remote-debugging-port=9222
# or: google-chrome --remote-debugging-port=9222
```

Then point `control browser` at that port instead of launching a new browser:

```
control browser attach --port 9222 --json          # sanity check: lists open tabs
control browser --cdp-port 9222 tabs                # * marks the tab in front
control browser --cdp-port 9222 goto github.com --wait-loaded
control browser --cdp-port 9222 dom                 # what can I click, and with which selector?
control browser --cdp-port 9222 click "a.header-search-button"
```

Every `browser` subcommand accepts `--cdp-port`. It acts on **the tab in front**
(the most recently shown one, as reported by the browser's `/json/list`), not on
whichever tab Playwright happens to list last. Pick another one with
`--tab PATTERN` (substring of its URL or title, or its number from `tabs`):

```
control browser --cdp-port 9222 --tab "Google Slides" snapshot
control browser --cdp-port 9222 tab gmail --wait-loaded   # also brings it to the front
```

Closing the command never closes the browser or its tabs — it only disconnects.
If the debug port isn't reachable, the command fails with a clear
`cannot reach a Chromium debug port at localhost:PORT` error.

#### Reading the page: `browser dom` and `browser snapshot`

Read the page before acting on it, instead of clicking at guessed coordinates:

```
control browser --cdp-port 9222 dom                 # interactive elements, one per line
control browser --cdp-port 9222 dom --root "[role=dialog]"   # only inside a dialog
control browser --cdp-port 9222 dom --ax            # accessibility tree, straight from CDP
control browser --cdp-port 9222 snapshot            # plain visible text
```

`dom` lists links, buttons, inputs, `[role=...]` widgets, contenteditables and
headings, each with its role, accessible name, a **CSS selector that is unique on
the page** (ready to pass to `click`/`fill`/`check`) and the center of its box in
viewport coordinates, flagged `(disabled)`, `(offscreen)` or `(checked)` where it
applies:

```
button "Enviar"             button[data-testid="send"]   @640,512
textbox "Buscar"            input[name="q"]              @186,252
link "Siguiente" (offscreen) #next                        @43,1480
```

`--ax` asks Chromium for its accessibility tree (`Accessibility.getFullAXTree`)
and prunes it to what a screen reader would announce — useful on canvas-heavy or
heavily componentised apps where the DOM is noise. Both take `--limit N`
(default 300) and say when they truncated. Password field values are never
included.

#### Clicking reliably: checks first, then native or JS clicks

`browser click` first checks the target (see `browser check` below), then:

| `--mode` | What happens | Use it for |
|---|---|---|
| `auto` (default) | Native click when the element is interactable; otherwise, or if the native click fails, a JS click | almost everything |
| `native` | Playwright's trusted CDP mouse click at the element's center; fails on hidden/covered targets | sites that reject untrusted events |
| `js` (or `--js`) | The pointer/mouse event sequence plus `el.click()`, dispatched inside the page | React/Vue listeners that ignore a physical click, targets behind overlays |

The JS click bubbles and is `composed`, so delegated listeners (React attaches
them to the root) see it; its events have `isTrusted: false`. A disabled element
is refused in every mode, since clicking it would silently do nothing. The JSON
result says which `method` was used and, on a fallback, a `note` explaining why.

```
control browser --cdp-port 9222 check "button.upload"
# 'button.upload': 1 match(es), <button> 'Subir' at 640,512 -- NOT interactable: it is covered by <div.cookie-banner>
control browser --cdp-port 9222 click "button.upload" --js
```

`browser check SELECTOR` reports (as JSON with `--json`) whether the selector
exists, how many elements match, and whether the first visible match is
`visible`, `enabled`, `in_viewport` and actually `receives_pointer` at its
center (`document.elementFromPoint`) — naming the element covering it if not.

#### Waiting until a page is really loaded: `--wait-loaded`

`goto`, `click`, `tab` and `focus-and-goto` take:

- `--wait-loaded`: return only once `document.readyState === "complete"` on the
  *new* document (the page being left is never mistaken for it: documents are
  told apart by `performance.timeOrigin`);
- `--network-idle`: the same, plus no request finishing for 0.5 s (single-page
  apps that keep fetching after `load`);
- `--load-timeout S` (default 30).

`control browser wait-loaded [--network-idle]` waits on the current tab without
doing anything else. A page that doesn't get there in time fails with
`page_not_loaded` and the last `readyState` seen.

#### Files without the file dialog

```
control browser --cdp-port 9222 upload "input[type=file]" ~/deck.pptx
control browser --cdp-port 9222 upload "button.choose-file" ~/deck.pptx   # intercepts the chooser
control browser --cdp-port 9222 drop "#dropzone" ~/deck.pptx
```

`browser upload` sets the files on a file input directly (hidden inputs too), or,
given the button that opens the chooser, intercepts the chooser over CDP and
answers it — the GTK/KDE dialog never opens. `browser drop` builds a
`DataTransfer` holding the files' real bytes and dispatches
`dragenter`/`dragover`/`drop` on the element: no mouse, no file manager, no screen
coordinates. `handled: true` means the page called `preventDefault()` on the
drop, which is how drop zones say they took the files (limit: 100 MB in total).

### Any other browser (Firefox, or Chromium without a debug port): keyboard fallback

A "focus the window, then enter the URL" fallback for when CDP isn't an option:

```
control browser focus-and-goto "Brave" "https://example.com"
control browser focus-and-goto "Firefox" gemini     # site aliases resolve too
```

It focuses the first window matching the pattern (via `windows.py`), presses
Ctrl+L, enters the URL and presses Enter. The URL is **pasted** through the
Wayland clipboard when `wl-copy` is installed, and only typed otherwise:
`ydotool type` sends US-layout keycodes, so on other layouts `:` and `/` come out
wrong (a Spanish layout turns `https://` into `httpsÑ--` and the browser searches
for that). Pasting replaces the clipboard's contents; the JSON result says
`entered_by: "paste" | "type"`.

If the browser does have a debug port, `focus-and-goto` can also wait for the
page — it watches the navigation over CDP until the new document is ready:

```
control browser --cdp-port 9222 focus-and-goto Brave github.com --wait-loaded
```

## Mouse, drag-and-drop and the clipboard

### Pointer placement

`click -x X -y Y` and `drag` place the pointer in a closed loop: ydotool 1.0's
`mousemove --absolute` is unreliable on Wayland (it usually leaves the pointer at
1,1, and relative moves are scaled by pointer acceleration), so after the
absolute move the real position is read back with `kdotool getmouselocation`
and corrected until it is exact — typically 3–4 moves, ~40 ms. A target that
can't be reached (off-screen coordinates) is an error, not a silent miss.
Coordinates are compositor (logical) pixels, the same space as window geometry.

### Typing text with accents, ñ and symbols

`ydotool type` sends US-layout keycodes for ASCII and silently **drops**
everything else, so on a Spanish layout `presentación aquí a:b/c` arrives as
`presentacin aqu aÑb-c`. `control type` therefore picks the method per text:

| Text | `--method auto` (default) does |
|---|---|
| only ASCII letters, digits, spaces, newlines, `,` `.` | types it (fast, clipboard untouched) |
| anything else: `á é í ó ú ñ ü ¿ ¡ € : / @ ( ...`, emoji, CJK | pastes it through the clipboard, byte-exact UTF-8 |

```
control type "¿Qué tal, señor Núñez?" --window Brave      # pasted
control type "cd ~/Música" --window kitty                 # pasted with Ctrl+Shift+V
echo "acción: ñandú" | control type - --window kate       # stdin is read as UTF-8
control type "p4ssw0rd" --method type --window Brave      # force typing (see below)
```

- The previous clipboard contents (text or a copied file list) are put back
  after the paste, and an empty clipboard is emptied again; the JSON result says
  `clipboard_restored`. A copied image can't be put back (`false`).
- Terminals (kitty, konsole, alacritty, foot, wezterm, ...) get Ctrl+Shift+V;
  everything else Ctrl+V. Override with `--paste-keys shift+insert`.
- Without `wl-clipboard`, text with non-ASCII characters is an error
  (`clipboard_error`) instead of silently losing letters; ASCII symbols are then
  typed with a `warning`, since they are only right on a US layout.
- `--method type` forces typing and warns about the characters that will be
  dropped. Use it for secrets: a clipboard manager (Klipper) may keep pasted text
  in its history.
- Text stays UTF-8 end to end whatever the locale: stdin is decoded from its raw
  bytes (invalid UTF-8 is an `encoding_error`, a BOM is dropped), arguments
  mangled by a Latin-1/C locale (`presentaciÃ³n`) are repaired before parsing,
  stdout is switched to UTF-8, and wl-copy/wl-paste always exchange UTF-8 bytes.
  `control clip set -` reads stdin the same way.

### `control drag`

```
control drag --from 300,420 --to 1100,380                      # e.g. Dolphin -> a web page
control drag --from 300,420 --to 1100,380 --steps 30 --hold 250 --window Dolphin
```

Presses at `--from`, pauses `--hold` ms (default 150), glides to `--to` in
`--steps` moves (default 20) so the source sees a real drag start and the drop
target gets `dragenter`/`dragover`, pauses again, and releases. While the button
is down only relative moves are sent (an absolute move would drag the item to
the screen corner first), the whole gesture holds the input lock, and the
button is released even if a move fails midway. For web drop zones reachable
over CDP, `browser drop` is more reliable than a physical drag.

### `control clip` (wl-clipboard)

```
control clip set "some text"                     # copy (the text is never echoed back)
echo "long text" | control clip set -
control clip set --file ~/deck.pptx --paste --window Brave   # copy a file, Ctrl+V into Brave
control clip get                                 # print the clipboard
control clip get --mime text/uri-list
```

`--file` (repeatable) copies files the way a file manager does (`text/uri-list`
with `file://` URIs), which lets a page's paste handler or an upload area take
them with Ctrl+V instead of going through a file dialog. `--paste` presses
Ctrl+V right after copying; with `--window` the focus and the paste happen under
the input lock. Needs `wl-clipboard` (`sudo pacman -S wl-clipboard`; the install
script adds it).

### Key names

`control key --list` prints every key name, grouped (modifiers, editing,
navigation, function, letters, digits, punctuation, numpad, system), plus the
aliases it accepts: names are case-insensitive, and the usual spellings from
X11/xdotool work too — `Return` → `enter`, `Escape` → `esc`, `Control` →
`ctrl`, `PgUp` → `pageup`, `win`/`cmd` → `super`, `del`, `bksp`, `-`, `/`...
Key names are *physical* keys at their US positions; to enter text (accents,
symbols) use `control type`, which handles layouts for you.

```
control key ctrl shift t
control key Control Return          # same as: control key ctrl enter
control key --list --json
```

## Reading the screen without image tokens

`shot` can post-process locally, so the agent rarely needs to look at a picture:

```bash
control shot window --window Firefox --ocr --grep "login|sign in"   # 'x,y,w,h text' lines
control shot full --crop 0,0,400,300 --scale 0.5                    # smaller image if you must look
control click -x 640 -y 400 --then-shot Firefox --ocr               # act + read the result
control click --text "guardar|save" --window Firefox [--nth 2]       # click on-screen text found by OCR
```

OCR coordinates are screen coordinates, ready for `click`. It uses `tesseract`
with `spa+eng` data from `$CONTROL_TESSDATA` or `~/.local/share/marionette/tessdata`.
When the agent cursor is visible, `click` and `drag` glide it to the target.

## Agent cursor (Marionette's own pointer)

Wayland has one real pointer, so a second one is impossible. `control cursor`
gives the agent a visible, click-through overlay arrow instead; the user's
pointer is only borrowed for the instant of a click and then put back.

```bash
control cursor show            # start the overlay (needs PySide6 in /usr/bin/python3,
                               # or set CONTROL_CURSOR_PYTHON)
control cursor move 800 400    # glide there (--duration 0 to jump)
control cursor click           # click where the arrow is; -x/-y moves first
control cursor status --json   # {"visible":..,"x":..,"y":..}
control cursor hide
```

`--no-restore` on `click` leaves the real pointer on the target.

## Native desktop apps (AT-SPI)

Browsers have the DOM; native apps have the **accessibility tree** — the same
one screen readers use. Through AT-SPI, `control a11y` lists an app's widgets
with their role, name, states, available actions and text, reads a field's
content, and operates widgets by *element* instead of by pixel:

```
control a11y enable                                   # once per login session
control a11y status                                   # bus on? worker OK? which apps?
control a11y tree --window Dolphin --interactive      # what can I operate here?
control a11y tree --app kate --role "push button" --name Guardar
control a11y read --window "Correo" --name Asunto     # a field's text, no screenshot
control a11y do --app dolphin --role button --name Atrás      # runs the Press action
control a11y focus --app kate --role text && control type "¡Hola, señor!"
control a11y set-text --app kate --role text "texto nuevo"    # where the app allows it
control a11y wait --app dolphin --role "list item" --name informe.pdf --settle 300
```

Output, one element per line — role, name, `[states]`, `{actions}`, text, and
the element's **path** (child indices from the application, usable as `--path`):

```
# dolphin (pid 77937, Qt)
button "Atrás" {ShowMenu, Press, SetFocus}          /0/6/1
button "Adelante" [disabled]                        /0/6/2
list item "cap1-vul-003.png" [focused, selected]    /0/0/0/0/0/0/2/0/0/2
```

- **Choosing the app:** `--window PATTERN` (the app owning that window, and only
  that window's frame; nothing is focused), `--app NAME` or `--pid N`.
- **Choosing the element:** `--path`, or filters `--role`, `--name`, `--text`
  (substrings, case-insensitive), `--state focused` (repeatable),
  `--interactive`, plus `--nth N`. With filters `tree` is a flat list of
  matches. Hidden (not "showing") elements are skipped unless `--all`.
- **Role names are normalized:** case, CamelCase, `_`/`-` and spacing don't
  matter, and common spellings map to AT-SPI's own — `PushButton`/`push button`
  → `button` (at-spi2-core 2.5x renamed it; `button` matches both spellings),
  `checkbox` → `check box`, `textbox`/`input` → `entry`, `tab` → `page tab`,
  `menuItem` → `menu item`, `window` → `frame`, `dropdown` → `combo box`.
- **`do` needs no focus and no visibility.** The action runs inside the app, so
  it works on a window on another virtual desktop — the user's screen does not
  move. Apps implement actions themselves: "accepted" does not always mean
  "done" (a Dolphin breadcrumb button accepts `Press` and ignores it), so
  confirm the effect with `read`/`wait`/`win wait-settled`.
- **Coordinates:** `box` is relative to the window. Wayland clients don't know
  where their window is, so AT-SPI can't give screen coordinates; prefer `do`.
- **`wait`** blocks until a match appears (or `--gone`), then with `--settle MS`
  until the app's visible tree has not changed for that long — the native
  counterpart of `browser --wait-loaded`.

### Setup and what each toolkit exposes

AT-SPI is reached through PyGObject (`gi`), a C extension distributions build
for *their* Python. Instead of compiling it into the project venv, the tree is
walked by a small worker (`src/control/_atspi_helper.py`) run by the system
interpreter, which already has it; it talks JSON with the venv and uses only
the standard library besides `gi`. Packages (the install script adds them):

| Distribution | Packages |
|---|---|
| Arch | `python-gobject at-spi2-core` |
| Debian/Ubuntu | `python3-gi gir1.2-atspi-2.0 at-spi2-core` |
| Fedora | `python3-gobject at-spi2-core` |

`CONTROL_ATSPI_PYTHON=/path/to/python3` picks another interpreter. `control a11y
status` says whether the bus is on, which interpreter runs the worker, and which
apps publish a tree.

| Toolkit | After `control a11y enable` |
|---|---|
| Qt / KDE (Dolphin, Kate, Plasma, System Settings) | full tree immediately, no restart |
| GTK | full tree (GTK 4 always publishes it) |
| Chromium, Brave, Chrome | window frames only, until restarted with `--force-renderer-accessibility` |
| Electron (VS Code, Slack, Discord...) | same flag; VS Code also honours `"editor.accessibilitySupport": "on"` |
| kitty, most terminals | no tree (use `control type`/`key`) |

`control a11y enable` sets `org.a11y.Status.IsEnabled` for the current login
session only (`--off` reverts it). An app whose tree comes back empty gets a
`hint` in the output explaining which of the above applies.

### Waiting for a window to settle

```
control win activate Kate && control win wait-settled Kate && control key ctrl s
```

`win wait-settled PATTERN` returns once the window's title, geometry and focus
have not changed for `--stable` ms (default 300): KWin animates activation,
maximizing and moving (~150–250 ms), and apps retitle themselves while loading.
Input sent mid-animation can land in the wrong place; this replaces the blind
`sleep 1`. It exits with `timeout` if the window never settles.

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

### Throwaway sandboxes and guard rails

```
control sandbox -- ./agent_task.sh
control sandbox --name qa-run --keep-windows -- python run_checks.py
```

```
control sandbox --timeout 120 -- ./agent_task.sh     # never runs longer than 2 minutes
```

`control sandbox -- COMMAND...` creates a fresh session on its own virtual
desktop (without switching the screen to it), runs COMMAND with
`CONTROL_SESSION_ID` set — so every window/app command inside is scoped to that
desktop — and afterwards closes the windows the command opened there and
removes the desktop. Only windows that did not exist before the run *and* live
on exactly that desktop are closed (never panels or windows pinned to all
desktops). If one refuses to close (an unsaved-changes dialog), the desktop is
kept instead of removed, since removing it would drop that window onto the
user's desktop — and so is the session record, so the desktop is never
orphaned: `control session end --name NAME --remove-desktop` finishes the job.
The exit status is COMMAND's; the report (`closed`, `killed_pids`, `left`,
`desktop_removed`, `session_kept`, `timed_out`) goes to stderr, or to stdout
with `--json`.

`--timeout S` bounds the run. When it expires the shutdown goes from polite to
firm: the windows the run opened are asked to close (apps get to save or ask),
then COMMAND's process group gets SIGTERM and, after 5 s, SIGKILL — the group
includes apps started through `control app launch`, since gtk-launch runs them
as its children. An app whose window still won't close gets SIGTERM, then
SIGKILL, *only* if that process owns no window outside the sandbox (a browser
that opened one sandbox window keeps the user's other windows). The exit status
is then 124, as with `timeout(1)`.

Inside any session, commands that would reach outside the session's desktop
are refused with `session_guard` unless `--force` is given: `desktop switch` to
another desktop, and `win send` of a window to another desktop.

A limit worth knowing: Wayland input (`type`, `key`, `click`) goes to the
focused window, and focusing a window on another desktop makes KWin show that
desktop. Reading and acting through AT-SPI (`a11y read/do/wait`) and CDP
(`browser ...`) do not need focus, so they keep working on a sandbox desktop
the user never sees.

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
