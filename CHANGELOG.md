# Changelog

## 1.0.0

First public release. The version numbers before this one were private
iterations and are not published; 1.0.0 is where the 32 tool names become a
promise — if one of them changes meaning, the major number changes with it.

### What it does

Windows already publishes what is on screen as structured data — every button,
field and list with its name and its state, the same thing screen readers read.
This hands that to Claude instead of a screenshot, so a control is pressed by
name rather than by guessed coordinate, and every action reports the element's
state before and after rather than assuming it worked.

**32 tools**, arranged as a cost ladder: read the tree, operate controls, read
tables as rows and columns, set sliders to an exact number, move and resize
windows, read and write the clipboard, open menus. Coordinate input and screen
capture remain for surfaces that genuinely paint themselves — editing canvases,
video timelines, games. Every tool states its price and `describe_screen` names
the order to work through, stopping at the first rung that works.

**The input guard.** When Claude needs the mouse or keyboard while you are
actually using the computer, the screen edge breathes slowly inward for ~0.9s —
time to finish the word — then snaps back in ~0.18s. The snap is the instant
your input is held: your keystrokes and clicks pause, Claude's own pass through,
and afterwards your window, focus and text caret are restored. Escape is never
swallowed. `set_guard priority:"me"` inverts it — Claude waits for a click on a
card instead of ever taking over.

**check_for_update.** The only tool that reaches the network, and only when it
is called. The monthly reminder is gated by a local timestamp, so it stays
offline unless a month has passed. No background check, no telemetry.

### What measurement changed

Every number below came from a test that ships in `tests/`, so it can be
contradicted on your own machine.

- **`describe_screen` 11.4s → 3.4s.** Two causes. Chromium's wake-up ran on
  every call although Chromium keeps its tree once built, so the result is now
  remembered per window and marked `cached` when served from memory. And the
  probe built a *full description* of every node it counted — twelve COM calls
  per node — for a number that only has to land in one of three buckets.
  Counting is now just the walk. The second fix was the larger half, and the
  twelve-pattern lookup it removed had itself been a fix from earlier, which
  made the most-used tool three times slower without anyone noticing until
  something measured it.
- **Capability detection was silently blind.** `GetInvokePattern()` and its
  siblings live on the *subclasses* of the uiautomation package, so calling
  them on a generic element raised an error that the safety wrapper swallowed —
  every element looked as though it supported nothing. `GetPattern(PatternId.X)`
  lives on the base class and answers for any element.
- **Chromium looked unreadable and was not.** A Claude window measures 13 nodes
  on a first shallow look and 207 once asked properly. The probe was wrong, not
  the browser. That covers VS Code, Slack, Discord, Teams, Notion and every web
  app.
- **Empty table cells printed their column heading.** A folder had the size
  "Größe". Once a cell has a value pattern its answer is final, empty included.
- **`element_from_point` reported success for coordinates on no screen at all.**
  Windows answers `ControlFromPoint(-99999, -99999)` with the desktop root. It
  now checks against the virtual desktop and says where that actually is.
- **`set_value` claimed success on an immovable scroll bar.** It now compares
  against the value it read first and says plainly that the control did not
  move.
- **`capture` clamped negative coordinates**, which is the wrong region on a
  monitor placed left of or above the primary one.
- **Every umlaut sent to a tool was destroyed.** MCP speaks UTF-8; a Windows
  pipe defaults to the machine's ANSI code page, measured here as `cp1252`.
  Output was pinned to UTF-8 and input was not, so results looked correct while
  arguments were already mojibake. All three streams are pinned now — this is
  easy to miss precisely because the visible half works.
- **A broken helper was pushing everything onto the mouse.** `_ref_for` turns an
  element into a ref you can act on. Its stop condition required the parent to
  have no window handle, which is true exactly one level below the desktop root
  — and the root carries a handle, so the branch never ran and it returned
  nothing for practically every element. The damage was entirely indirect:
  `element_from_point` and `get_focus` could describe a control and hand back no
  way to operate it, so the only route left was the pointer, and the input guard
  could not save the focus it promises to restore. Three tools, one line.
- **`invoke` reached for the real mouse when no pattern answered**, outside the
  edge glow and outside the input guard — in a tool documented as never touching
  your cursor. It now refuses, names what the element does offer, and prints the
  exact `click(x, y)` call if you decide the pointer is worth it.
- **`close_window` always sent Alt+F4**, which needs to steal your foreground
  first. `WindowPattern.Close()` asks the window to close itself and costs
  nothing; the keyboard is now the fallback and says when it was used.
- **`menu` went straight to the right button.** It now tries the expand pattern,
  then the context-menu key, then the pointer.

- **Blind keystrokes went wherever focus had drifted to.** This one was caught
  by watching it happen: an assistant read a form, the person clicked into a
  chat window, and the next `Enter` landed in the chat. The tool had always
  returned a note saying "confirm this landed where you intended" — read after
  the damage, so not a safeguard at all. Telling "the user moved" from "we
  moved" appears to need the source of an event, which Windows will not give:
  `GetLastInputInfo` counts injected input too. The question turns out not to
  need it. The foreground window is recorded after **every** tool call, so
  anything this server did is already in the baseline; a change appearing
  between calls came from outside. `send_keys` without a target, and `click`,
  `drag` and `hold_key` on coordinates, now refuse in that case and name both
  windows. `force: true` overrides.
- **Watching the window alone missed it, twice.** The second time, the window
  never changed — the click landed on a different control *inside* the window
  that was already in front, and a keystroke follows the keyboard focus, not the
  window. The fingerprint is now the focused control as well: its type, its
  automation id and its name. Deliberately not its rectangle, since controls
  move when a window is resized or a list scrolls, and refusing over that would
  be noise rather than safety.
- **And the check ran before the lock, which is a race.** Verifying the target
  and *then* freezing input leaves a gap, and a click lands in a millisecond. A
  check that only works sometimes is worse than no check, because it gets
  trusted. Input is now held first, the screen is given 40 ms for the last
  keystroke to finish travelling through the message queue, and only then is the
  target read — so what the check sees is what the action will hit. If it moved,
  the lock is released again and nothing is typed. The rubber-band pulse is
  deliberately a window in which you may still type, which makes this ordering
  necessary rather than merely tidy: whatever you did with that second is
  exactly what has to be seen, and it can only be seen once the lock has closed.

Every tool that steps down a rung now reports `"how"` and `"took_input"` in its
reply, so a fallback is never silent.

### Approaches rejected, and why

- **`BlockInput` for the guard.** Needs administrator rights. A tool strangers
  install should not demand them.
- **Windows toast notifications for the "waiting" prompt.** Could not carry an
  actionable button without a registered application identity.
- **One full-screen window for the edge glow.** ~36 MB per frame; animation
  impossible. Four thin edge bars measure 0.6 ms per frame, which is what makes
  the pulse possible.

### Known limits

Windows only — `docs/PORTING.md` maps every pattern used here onto the macOS
Accessibility API, but a map is not an implementation. Administrator processes
are invisible by Windows design. Control names follow the window's language;
`find_elements` also searches `automation_id`, which does not translate, and
says which one matched.
