# Guide

Five minutes, three steps, one thing people get wrong.

## 1 · Python

You need [Python 3.9 or newer](https://www.python.org/downloads/). During its
setup, tick **"Add python.exe to PATH"** — that box is the single most common
reason an install fails later.

Already have it? Then skip this.

## 2 · Install the extension

Download `pc-screen-control.mcpb` from [Releases](../../releases).

> ### Do not double-click it
> Windows has no handler for `.mcpb` and will ask you which program to open it
> with. There is no right answer to that question.

Instead, install it from inside Claude:

```
Claude  →  Settings  →  Extensions  →  Advanced  →  Install extension…
```

Pick the file you downloaded. It appears in the list as **PC Screen Control**.

## 3 · Restart Claude — completely

Close it including the tray icon, then start it again. Claude reads its
extensions only at startup, so skipping this is the same as not installing it.

There is no fourth step. Nothing to switch on afterwards.

---

## Check it worked

Ask Claude:

> run describe_screen

You should get a list of your open windows, each with a verdict:

```
Explorer         233 nodes   readable      real controls, addressable by name
Chrome           207 nodes   readable      woken: true
Paint             2 nodes    canvas-only   paints its own interface
```

If instead you see nothing, open *Settings → Extensions* and check that PC
Screen Control is listed and enabled.

---

## What to expect once it runs

**Ask for what you mean, not for where to click.** "Refresh this folder" works
better than "click the button at the top right", because the tool finds controls
by name.

**Most of it never touches your mouse.** Reading the screen and operating
controls go through the accessibility interface, so you can carry on working
while Claude works.

**When it does need the mouse, you will see it.** A cyan glow appears around the
whole screen for exactly as long as your pointer or keyboard is taken — and
never otherwise. That only happens on surfaces that paint themselves and expose
no controls: editing canvases, video timelines, games. Everywhere else the glow
stays off.

![The screen edge while input is taken](img/edge-glow.png)

*The real gradient from `src/overlay.py`, drawn over a blank desktop.*

---

## When you are using the computer at the same time

If Claude needs the mouse while you are typing, it does not just barge in.

**1. The edge breathes.** It grows slowly inward over about a second. That is
your moment — finish the word you are on.

**2. It snaps back.** Fast, like a released rubber band. That snap is the
instant your input is held. Your keystrokes and clicks pause; Claude's own pass
through.

**3. It hands everything back.** The window you were in, the field you were
typing in, and the position of your text cursor. You carry on where you were.

**Escape always stops it.** It is never swallowed. One press unlocks, restores,
and tells Claude you took over. There is also a ten-second limit and an
automatic release if anything goes wrong, so you cannot be locked out.

**If you are gaming, or doing anything that must not be interrupted**, say:

> *"Set the guard so my input has priority."*

Claude then never takes over on its own. It waits and shows a small card, and
acts only once you click it or stop for a moment. `set_guard priority:"claude"`
switches back.

If you are away from the keyboard, none of this happens — Claude just gets on
with it.

---

## Updates

Claude can check for a newer version, at most about once a month, and only when
it looks. There is no background check and no ping at startup.

If there is one, it offers to download the new `.mcpb` to your Downloads folder.
You then install it the same way as the first time — **over the old one, no
uninstall** — and restart Claude. The higher version number replaces the old
extension automatically.

You can also ask at any time: *"Is there a new version of PC Screen Control?"*

---

## When something does not work

**First, ask Claude to run `self_test`.** It checks ten things in plain
language and every failure tells you the fix. Most problems below are on that
list, and its output is also the most useful thing to paste into a bug report.

**The extension installed but does nothing / shows an error.** This is almost
always Python. Either it is not installed, or the box **"Add python.exe to
PATH"** was not ticked during its setup, so Claude cannot find it to start the
server. Reinstall Python from [python.org](https://www.python.org/downloads/)
with that box ticked, then remove and reinstall the extension so it points at
the real Python. If you have the *Microsoft Store* version of Python, that one
often does not work here — install the one from python.org instead. `self_test`
will tell you which Python it is running and whether it is the Store stub.

**A window shows `shallow` or few nodes.** Ask Claude to read it once anyway —
browsers and Electron apps build their accessibility tree only when something
first asks for it.

**A window shows `canvas-only`.** That application draws its own interface and
publishes no controls. Claude can still see it with `capture` and operate it
with `click` and `drag`; it just costs more and cannot prove the result.

**Nothing at all happens in one particular app.** Check whether it runs as
administrator. Windows blocks input from a normal process to an elevated one by
design, and nothing gets around that from user space.

**Something looks wrong.** Open an
[issue](../../issues/new/choose) and paste the `describe_screen` entry for that
window. It carries the window class and UI framework, which is the one piece of
information that makes a report actionable.

---

## Removing it

*Settings → Extensions →* remove it there. Nothing else is left behind: no
registry keys, no system settings, nothing outside your user profile. See
[SECURITY.md](../.github/SECURITY.md) for the full list of what it touches.
