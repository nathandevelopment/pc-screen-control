# Why your antivirus may flag this, and what to do

Short version: this tool reads other applications and can move the mouse and
keyboard. That is structurally the same thing spyware does, so a scanner that
looks at behaviour rather than identity is *right* to raise an eyebrow. Nothing
here is hidden, so here is exactly what it does and how to check that claim
yourself.

## What triggers it

Two things, and only these two.

**1. Low-level input hooks.** The input guard - the part that pauses your typing
for the moment the pointer is being used - installs `WH_KEYBOARD_LL` and
`WH_MOUSE_LL`. Those are the same Windows mechanism a keylogger uses. A scanner
cannot tell "swallow this keystroke so it does not land in the wrong window"
apart from "record this keystroke and send it somewhere" by the API alone - the
call looks identical. So some scanners flag the presence of the hook.

**2. Reading other applications and moving the pointer.** A program that
enumerates other windows, reads their contents and can drive the mouse fits the
generic behavioural pattern of remote-control malware. This is the entire
purpose of the tool, and it is done through the same Accessibility API a screen
reader uses.

## What it does NOT do

- **It records no keystrokes.** The hook callback looks at one field - whether
  Windows marked the event as injected - and returns either "swallow" or "pass
  on". No key value is read, stored, counted, logged or sent. The whole hook
  path is about forty lines in `src/overlay.py`; read them.
- **It reaches the network exactly once, on request.** Only `check_for_update`
  makes any outbound connection, and only when you call it. Every other tool is
  offline. See `SECURITY.md`.
- **The hooks exist only while an action is holding your input** - installed
  when the lock closes, removed when it opens. Not at startup, not in the
  background, not between calls.

## How to check all of that yourself

You do not have to take any of the above on faith. The server is one readable
Python file:

1. Open `src/overlay.py` and search for `WH_KEYBOARD_LL`. The callback around it
   is short. Confirm it stores nothing.
2. Open `src/server.py` and search for `urllib` - the network code. Confirm it
   sits only inside `check_for_update`.
3. Run `tests/` on your own machine. The numbers in the README come from those
   scripts, and they ship so you can contradict them.

## How to make the warning stop

Pick whichever you are comfortable with, most-preferred first.

- **Turn the input guard off entirely.** Tell Claude: *"set the guard to
  disabled"* (`set_guard enabled:false`). No hook is ever installed after that.
  Actions still work; they just no longer pause you or warn you first. If the
  hook is what your scanner objects to, this removes the cause.
- **Verify the download, then whitelist the file.** After
  `check_for_update download:true`, compare the file's SHA-256 against the one
  in the release notes (`Get-FileHash pc-screen-control.mcpb -Algorithm
  SHA256`). Once it matches, you know the bytes are exactly what was published,
  and adding that file to your scanner's exclusions is a decision you can make
  with the hash in front of you.
- **Report it as a false positive.** Most scanners have a "submit for review"
  path. A behavioural flag on an open-source accessibility tool is a textbook
  false positive, and reporting it helps the next person too.

## What we will not tell you to do

We will not tell you to blanket-disable your antivirus, and you should be wary
of any tool that does. The warning is not wrong about *what the software can
do* - it can read your screen and drive your input. It is only wrong about
*intent*, and intent is exactly the thing you should verify for yourself rather
than accept from the author. The whole design of this project - one readable
file, shipped tests, a published hash - exists so that you can.
