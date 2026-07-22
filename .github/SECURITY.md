# Security

This software controls your computer. That is its purpose and also its threat
model.

## What it can do

It runs with your user account's privileges. Anything you can do by clicking and
typing, it can do: press buttons, enter text, close windows, drag things, read
documents and web pages that are open on screen.

It is driven by an AI. Assume it will occasionally misidentify a control, and
that a web page or document it reads may contain text aimed at redirecting it.
Do not leave it unattended in front of anything irreversible.

### It installs low-level input hooks, and you should know exactly when

The input guard — the thing that pauses your typing while the pointer is being
used — works by installing `WH_KEYBOARD_LL` and `WH_MOUSE_LL` hooks. Those are
system-wide, and they are the same mechanism a keylogger uses. Saying so plainly
is more useful than hoping nobody notices, so:

- **They exist only while an action is holding your input.** They are installed
  when the lock closes and removed when it opens, not at startup and not between
  calls.
- **They live in `src/overlay.py`, a separate process** with no file access, no
  network code and no connection to the server other than one pipe carrying the
  words `warn`, `lock`, `release`, `wait_on`, `wait_off`, `off` and `quit`.
- **Nothing is recorded.** The callbacks look at one field — whether Windows
  marked the event as injected — and return either "swallow" or "pass on". No
  key value is stored, logged, counted or sent. The whole hook path is about
  forty lines; read them.
- **Escape is never swallowed**, and a ten-second watchdog releases the hooks
  even if something goes wrong. If the process dies, Windows removes them
  automatically — measured at 0.1s after killing the parent outright.
- **You can switch the whole thing off** with `set_guard enabled:false`, in
  which case no hook is ever installed. Actions then run without pausing you.

Your antivirus may object to this, and that is a reasonable thing for it to do.

## What it cannot do

- **Network only on demand, one tool, one destination.** Exactly one tool,
  `check_for_update`, reaches the network, and only when it is called — there
  is no background check, no ping at startup, no telemetry. It contacts GitHub's
  public release API for this repository and, if you ask, downloads the new
  `.mcpb` to your Downloads folder. The monthly reminder is enforced by a local
  timestamp, so even that runs offline unless a month has passed. Every one of
  the other 31 tools makes no outbound connection at all. You can verify all of
  this by reading the one file.
- **No elevated processes.** Windows blocks input from a lower-integrity process
  to a higher one. Anything running as administrator is invisible to it.
- **No credentials.** It has no keychain or credential API. It can read a
  password field's *label* and type into it, so treat logins as out of scope.
- **No keystroke logging.** The hooks described above decide swallow-or-pass and
  keep nothing. There is no buffer, no file, no counter of what you typed.
- **No persistence.** No service, no scheduled task, no startup entry, no shell
  extension. It runs only while your MCP client runs it.

## What the installer touches

Four operations, all inside your own user profile, none needing administrator
rights:

| | |
|---|---|
| installs | `uiautomation` and `pillow` via pip |
| creates | `%LOCALAPPDATA%\pc-screen-control\` |
| modifies | your Claude config — one entry, existing entries preserved |
| backs up | each config before the first change, never overwriting that backup |

It writes via a temporary file and an atomic replace, then reads the result back
to confirm. No registry key, no system setting, no file association, nothing
outside your user profile. `tests/test_installer.py` covers this and you can run
it yourself.

To remove: `UNINSTALL.bat`. It takes out only its own entry.

## Reporting a problem

Open an issue. If you would rather not post it publicly, open an issue saying
only that and I will follow up.

Be realistic about what you are dealing with: a hobby project maintained by one
person. No security team, no response time, no bounty. If you need those, this
is not the right dependency — and that is a reasonable conclusion to reach.
