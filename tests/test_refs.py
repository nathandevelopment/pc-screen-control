# -*- coding: utf-8 -*-
"""
Regression test for the ref builder, and for the promise that cheap tools do
not reach for the mouse behind your back.

Two things are checked, and both were real defects.

_ref_for could not build a ref. Its stop condition required the parent to have
no window handle, which only holds one level below the desktop root - and the
root carries a handle, so the branch never ran and it returned None for
practically every element. The damage was indirect and therefore easy to miss:
element_from_point and get_focus could describe a control but hand back nothing
to act on, so the only remaining way to touch it was the pointer. A broken
helper was quietly pushing the whole server onto the one rung it exists to
avoid. The input guard was affected too - it could not save the focus it
promises to restore.

And invoke used to end with el.Click() when no pattern answered. A tool
documented as "your cursor is never touched" moved the real mouse, outside the
edge glow and outside the input guard. It must refuse instead, and say what the
element does offer.

Run this on any Windows desktop. It uses Explorer, which every install has.
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
    print("  %-52s %-6s %s" % (name, "OK" if ok else "FAIL", detail))


def main():
    try:
        import uiautomation as auto
    except ImportError:
        print("SKIP: uiautomation is not installed")
        return 0

    import server

    # Use a window that is already open. An earlier version started Explorer on
    # every run, which is fine on a CI runner that is thrown away afterwards and
    # rude on a real desktop - five runs left five windows lying around. Only
    # open one if there is genuinely nothing to look at, and close it again.
    selbst_geoeffnet = None
    windows = [w for w in server._top_windows()
               if w["title"] and not w["offscreen"]]
    if not windows:
        print("No window open; starting Explorer for the test")
        selbst_geoeffnet = subprocess.Popen(["explorer.exe"])
        time.sleep(3.0)
        windows = [w for w in server._top_windows()
                   if w["title"] and not w["offscreen"]]
    if not windows:
        print("SKIP: no window to test against")
        return 0
    print("Testing against %d open window(s)" % len(windows))

    print()
    print("1 - a ref can be built for a real element")
    target = None
    for w in windows:
        el = auto.ControlFromHandle(int(w["handle"]))
        kids = server._safe(lambda: el.GetChildren(), []) or []
        if kids:
            target = kids[0]
            break
    check("found an element to test with", target is not None)
    if target is None:
        return 1

    ref = server._ref_for(target)
    check("_ref_for returns a ref, not None", bool(ref), repr(ref))

    print()
    print("2 - the ref resolves back to the same element")
    if ref:
        back = server._safe(lambda: server._resolve(ref))
        same = (back is not None
                and server._rect(back) == server._rect(target)
                and server._safe(lambda: back.Name, "")
                == server._safe(lambda: target.Name, ""))
        check("_resolve(ref) finds the same element again", same)

    print()
    print("3 - element_from_point hands back something usable")
    r = server._rect(target)
    if r and r[2] > r[0]:
        mid_x, mid_y = (r[0] + r[2]) // 2, (r[1] + r[3]) // 2
        out = server.t_element_from_point({"x": mid_x, "y": mid_y})
        found = out.get("found") and out.get("element", {}).get("ref")
        check("element_from_point returns a ref you can act on",
              bool(found), out.get("element", {}).get("ref"))

    print()
    print("4 - invoke refuses rather than reaching for the mouse")
    # A plain pane publishes no Invoke/Toggle/Selection/ExpandCollapse pattern.
    dumb = None
    for w in windows:
        el = auto.ControlFromHandle(int(w["handle"]))
        for kid in (server._safe(lambda: el.GetChildren(), []) or []):
            acts = server._actions(kid)
            if not any(a in acts for a in
                       ("invoke", "toggle", "select", "expand")):
                dumb = kid
                break
        if dumb is not None:
            break

    if dumb is None:
        print("     (no pattern-less element found; nothing to prove here)")
    else:
        dumb_ref = server._ref_for(dumb)
        if not dumb_ref:
            print("     (could not build a ref for it; skipped)")
        else:
            try:
                server.t_invoke({"ref": dumb_ref})
                check("invoke refused a pattern-less element", False,
                      "it did something instead of refusing")
            except RuntimeError as e:
                said = "click(" in str(e) and "no way to be pressed" in str(e)
                check("invoke refuses and names the pointer alternative", said)
            except Exception as e:
                check("invoke refuses cleanly", False, type(e).__name__)

    if selbst_geoeffnet is not None:
        # Leave the desktop as it was found.
        subprocess.run(["taskkill", "/PID", str(selbst_geoeffnet.pid), "/T"],
                       capture_output=True)

    print()
    print("-" * 62)
    print("RESULT:", "OK" if not failures else "FAILED: " + ", ".join(failures))
    print("-" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
