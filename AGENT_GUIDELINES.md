# Agent guidelines: token-efficient use of `control`

Five rules for any agent (Claude Code or otherwise) driving this tool. They exist
because every one of them was a real, observed token cost in a live session —
this file is the permanent reference so a fresh session doesn't have to
rediscover them by burning tokens first.

## 1. Call the venv binary directly, not `uv run`

`uv run control ...` re-resolves the project and sometimes reinstalls the local
package on every single invocation, printing resolver noise
(`Building marionette... Uninstalled 1 package... Installed 1 package...`)
before the command's actual output. Once `uv sync` has been run, just call
`control` — it is symlinked onto the PATH at `~/.local/bin/control`, so it works
from any directory:

```bash
control win list --json
```

Same output, zero resolver chatter. The underlying venv binary is
`/home/luis/Documentos/Marionette/.venv/bin/control` if you ever need the
absolute path. Only go back to `uv run` right after actually changing a
dependency in `pyproject.toml`.

## 2. Prioritize text and JSON over screenshots

An image read back by an agent costs dramatically more tokens than a line of
JSON. Most verifications don't need a picture at all:

- a window's title changing, or a window appearing/disappearing, is provable
  with `win list --json` before/after — no screenshot needed;
- page state in the browser is provable with `control browser snapshot` or
  `text_content()` (DOM text), which is orders of magnitude cheaper than a
  screenshot of the same page.

Reach for an image only when the *visual* layout itself is the thing being
checked, not just "did the action happen".

## 3. Minimize the visual area when a screenshot is unavoidable

When a picture really is required:

- `control shot window` (or `--window PATTERN`) instead of `control shot full`
  — only the relevant window, not the whole desktop;
- DOM text (`control browser snapshot`) instead of a browser screenshot
  whenever the question is "what does the page say", not "what does it look
  like".

## 4. Filter at the source

Pass a pattern to the CLI instead of listing everything and scanning the
output yourself:

```bash
control win list brave        # not: control win list "" | grep brave
control app list dolphin      # not: control app list | grep -i dolphin
```

Smaller output requested is smaller output paid for.

## 5. Batch independent commands into one invocation

When steps don't depend on inspecting each other's intermediate output, chain
them in a single shell call instead of one tool round-trip per step:

```bash
control app launch org.kde.dolphin --wait && control win list dolphin --json
```

Fewer tool round-trips, less repeated overhead per call.

---

See also the "Token efficiency for agents driving this tool" section in
[README.md](README.md) for the fuller narrative version of these same rules.

## Browsing: pick the lightest tool

1. Just reading a page: `control browser fetch URL` (Obscura, no window).
2. Browser already open: `control browser detect`. With a debug port, use `--cdp-port`; without one,
   drive it visually (`click --text`, `type`, `key`, `--window`). `browser relaunch --yes` restarts it
   with CDP on its own profile: ask the user first, it closes their windows.
3. Logins/forms in a fresh browser: `browser launch` + `--cdp-port`, or the `Browser` class.
