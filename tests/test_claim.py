# -*- coding: utf-8 -*-
"""
Claiming a window, and the rescue that makes it safe to do.

The feature rests on an asymmetry in Windows: a window may sit at coordinates
where no monitor is, but the mouse pointer may not - the cursor is clamped to
the union of the real monitors. So a window parked out there keeps running and
stays operable through the accessibility interface, while the person at the desk
can neither see it nor click into it. Not because something is guarding it;
because the pointer cannot arrive.

That is also exactly what makes it dangerous. A window parked outside every
monitor when this process dies is a window its owner cannot get back - visible
in the taskbar, unreachable with the mouse. So the position is written to disk
before the move, and read back on the next start.

Checked here:
  1. the parking spot really is outside every monitor
  2. the pointer cannot follow it there
  3. the window comes back to the pixel
  4. a killed server does not strand it - the next start rescues it
"""
import ctypes
import ctypes.wintypes as w
import json
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
        import uiautomation as auto
    except ImportError:
        print("SKIP: uiautomation is not installed")
        return 0

    import server

    u = ctypes.WinDLL("user32", use_last_error=True)
    u.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]

    def offene():
        return {int(k.NativeWindowHandle): k
                for k in auto.GetRootControl().GetChildren()
                if k.NativeWindowHandle}

    # Match on the window list rather than the process id: several bundled
    # Windows apps hand off to another process, so the pid we started is not
    # the pid that owns the window.
    vorher = offene()
    subprocess.Popen(["explorer.exe"])
    ziel = None
    for _ in range(20):
        time.sleep(0.5)
        neu = [h for h in offene() if h not in vorher]
        if neu:
            ziel = neu[0]
            break
    if ziel is None:
        print("SKIP: no window appeared to test with")
        return 0

    heimat = server._fenster_rect(ziel)
    print("Testing with window %d at %s" % (ziel, heimat[:2]))

    vx, vy, vb, vh = server._virtueller_bildschirm()
    if vb < 200 or vh < 200:
        # A headless runner reports a tiny or empty virtual screen, so there is
        # no "outside the monitors" to park in and no cursor clamp to observe.
        # That is the environment, not the feature. On a real desktop this runs
        # in full. Skip honestly rather than fail a machine with no screen.
        print("SKIP: no real screen on this machine (virtual desktop %dx%d). "
              "This test needs a monitor; it runs in full on one." % (vb, vh))
        try:
            u.PostMessageW(ziel, 0x0010, 0, 0)
        except Exception:
            pass
        return 0

    print()
    print("1 - claim it")
    out = server.t_claim_window({"window_handle": ziel})
    check("claimed", out.get("ok") is True)
    check("parked outside every monitor",
          out.get("parked_at", [0])[0] >= vx + vb,
          "x=%s, monitors end at %s" % (out.get("parked_at", ["?"])[0], vx + vb))

    print()
    print("2 - the pointer cannot follow")
    vorher_maus = w.POINT()
    u.GetCursorPos(ctypes.byref(vorher_maus))
    u.SetCursorPos(out["parked_at"][0] + 50, out["parked_at"][1] + 50)
    time.sleep(0.2)
    jetzt = w.POINT()
    u.GetCursorPos(ctypes.byref(jetzt))
    u.SetCursorPos(vorher_maus.x, vorher_maus.y)
    check("cursor stayed on the monitors", jetzt.x < vx + vb,
          "landed at x=%d" % jetzt.x)

    print()
    print("3 - the parking spot is on disk, so a crash cannot strand it")
    pfad = server._parkplatz_datei()
    auf_platte = {}
    if os.path.isfile(pfad):
        with open(pfad, encoding="utf-8") as fh:
            auf_platte = json.load(fh)
    check("written to disk before anything else", str(ziel) in auf_platte,
          os.path.basename(pfad))

    print()
    print("4 - release puts it back to the pixel")
    zurueck = server.t_release_window({"window_handle": ziel})
    check("released", zurueck.get("ok") is True)
    check("exactly where it was", zurueck.get("exact") is True,
          "%s vs %s" % (zurueck.get("back_at"), zurueck.get("was_at")))
    check("no longer listed as claimed", str(ziel) not in server._BEANSPRUCHT)

    print()
    print("5 - the rescue path itself")
    # Simulate a crash: park it, leave the record behind, wipe memory, and let
    # the startup rescue find it - which is what a killed server leaves behind.
    server.t_claim_window({"window_handle": ziel})
    server._BEANSPRUCHT.clear()               # as if this process had died
    weit_weg = server._fenster_rect(ziel)
    check("window is out there before the rescue",
          bool(weit_weg) and weit_weg[0] >= vx + vb, "x=%s" % weit_weg[0])
    server._verwaiste_zurueckholen()
    time.sleep(0.4)
    daheim = server._fenster_rect(ziel)
    check("startup rescue brought it home",
          bool(daheim) and daheim[0] < vx + vb, "x=%s" % daheim[0])
    check("the record was cleared afterwards", not server._BEANSPRUCHT)

    try:
        u.PostMessageW(ziel, 0x0010, 0, 0)     # WM_CLOSE
    except Exception:
        pass

    print()
    print("-" * 66)
    print("RESULT:", "OK" if not failures else "FAILED: " + ", ".join(failures))
    print("-" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
