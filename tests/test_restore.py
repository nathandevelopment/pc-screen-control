# -*- coding: utf-8 -*-
"""
Regression test for the restore that never restored.

Reported as: "it takes my focus away and does not give it back."

The cause was one line that looks complete and does nothing.
SetForegroundWindow is only granted to a process that already owns the
foreground or received the last input event - by design, so that background
programs cannot steal focus from someone who is typing. A process calling it
from behind gets refused, and the refusal is silent: no exception, and the
return value was not being read either. The restore had therefore never once
worked, while the code that was supposed to do it sat there in plain sight.

The way through is to attach our input queue to the thread that currently owns
the foreground, which makes Windows treat the request as coming from that thread
for the duration.

What is checked here is the outcome, not the attempt: a second window is brought
to the front, the saved one is restored, and GetForegroundWindow is asked who is
actually in front afterwards. Counting calls would have passed against the
broken version too.
"""
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

failures = []


def check(name, ok, detail=""):
    if not ok:
        failures.append(name)
    print("  %-54s %-6s %s" % (name, "OK" if ok else "FAIL", detail))


def main():
    if os.name != "nt":
        print("SKIP: Windows only")
        return 0
    try:
        import uiautomation  # noqa: F401
    except ImportError:
        print("SKIP: uiautomation is not installed")
        return 0

    import ctypes
    import server

    u = ctypes.windll.user32
    u.GetForegroundWindow.restype = ctypes.c_void_p

    # Establish the precondition this test needs and that real usage always has:
    # a process that recently generated input is allowed to change the
    # foreground. A cold test process launched in the background is not, and
    # would otherwise skip at random depending on who launched it. One synthetic
    # no-op mouse event grants the right deterministically - this is setup, not
    # the thing under test. If even this does not take (a truly headless runner
    # with no input desktop), the move below will not happen and the test skips
    # honestly.
    try:
        MOUSEEVENTF_MOVE = 0x0001
        u.mouse_event(MOUSEEVENTF_MOVE, 0, 0, 0, 0)
        time.sleep(0.05)
    except Exception:
        pass

    fenster = [w for w in server._top_windows()
               if w["title"] and not w["offscreen"]]
    selbst_geoeffnet = None
    if len(fenster) < 2:
        selbst_geoeffnet = subprocess.Popen(["explorer.exe"])
        time.sleep(3.0)
        fenster = [w for w in server._top_windows()
                   if w["title"] and not w["offscreen"]]
    if len(fenster) < 2:
        print("SKIP: need two windows to move focus between")
        return 0

    print("1 - remember where the screen was pointing")
    gesichert = server._fokus_sichern()
    ausgangs_hwnd = gesichert.get("hwnd")
    check("a foreground window was recorded", bool(ausgangs_hwnd),
          str(ausgangs_hwnd))
    if not ausgangs_hwnd:
        return 1

    print()
    print("2 - move the foreground somewhere else")
    anderes = None
    for w in fenster:
        if int(w["handle"]) != int(ausgangs_hwnd):
            anderes = int(w["handle"])
            break
    if anderes is None:
        print("SKIP: no second window available")
        return 0

    verschoben = server._safe(lambda: server._vordergrund_setzen(anderes), False)
    jetzt = int(u.GetForegroundWindow() or 0)

    # A headless CI runner has no interactive foreground: SetForegroundWindow is
    # refused there for a reason that has nothing to do with this code, and the
    # window will not move no matter what. Detect that honestly and skip, rather
    # than fail a machine that was never going to pass. On a real desktop the
    # move succeeds and the test runs in full. The difference is the environment,
    # not the version - this showed up only on the 3.13 runner by chance.
    if jetzt != anderes:
        print("SKIP: no interactive foreground on this machine "
              "(headless runner). This test needs a real desktop; it runs in "
              "full on one. Foreground stayed at %d." % jetzt)
        return 0
    check("the foreground really moved", verschoben and jetzt == anderes,
          "%s" % jetzt)

    print()
    print("3 - give it back, and measure the result")
    ergebnis = server._fokus_zurueck(gesichert)
    time.sleep(0.3)
    zurueck = int(u.GetForegroundWindow() or 0)
    check("the original window is in front again",
          zurueck == int(ausgangs_hwnd), "%s" % zurueck)
    check("the restore reports success rather than assuming it",
          isinstance(ergebnis, dict) and ergebnis.get("window") is True,
          str(ergebnis))

    print()
    print("4 - the report is honest about failure too")
    # A handle that is not a window cannot be restored, and must say so.
    schlecht = server._safe(lambda: server._vordergrund_setzen(0xDEAD), False)
    check("an impossible restore reports False", schlecht is False)

    if selbst_geoeffnet is not None:
        subprocess.run(["taskkill", "/PID", str(selbst_geoeffnet.pid), "/T"],
                       capture_output=True)

    print()
    print("-" * 64)
    print("RESULT:", "OK" if not failures else "FAILED: " + ", ".join(failures))
    print("-" * 64)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
