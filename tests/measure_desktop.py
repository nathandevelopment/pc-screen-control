# -*- coding: utf-8 -*-
"""
Measures what this server can actually see and do on the machine it runs on.

This is the script that produced the numbers in the README. It is included so
those numbers can be reproduced and, where they are wrong for your setup,
contradicted. It imports the shipped server code and calls its real tools - no
reimplementation.

    python tests/measure_desktop.py

Writes measurement_report.md next to this file.

  1  every open window, with its node count and verdict
  2  the actionable control tree of a few named applications
  3  a real write test: operate a control and measure the effect

Step 3 expands and re-collapses one tree item in a File Explorer window. It
changes nothing else and leaves no side effects.
"""
import io
import os
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, os.pardir, "src"))

OUT = []


def p(s=""):
    OUT.append(s)
    try:
        print(s)
    except Exception:
        pass


def save():
    path = os.path.join(HERE, "measurement_report.md")
    io.open(path, "w", encoding="utf-8").write("\n".join(OUT) + "\n")
    return path


p("# Desktop measurement - %s" % time.strftime("%Y-%m-%d %H:%M"))
p()

try:
    import server as S
    p("Server module: **%s**" % S.SERVER_VERSION)
    p("Tools: **%d**" % len(S.TOOLS))
    p()
except Exception:
    p("```")
    p(traceback.format_exc())
    p("```")
    save()
    sys.exit(1)

# --------------------------------------------------------------------- step 1
p("## 1 - every open window")
p()
windows = []
try:
    t0 = time.time()
    d = S.t_describe_screen({})
    p("Duration: %.1fs - windows: %d" % (time.time() - t0, d["count"]))
    p()
    p("| Window | Class | Nodes | Verdict |")
    p("|---|---|---:|---|")
    for w in d["windows"]:
        p("| %s | %s | %d | **%s** |" % (
            (w["title"] or "(untitled)")[:44], w["class"][:22],
            w["probe_nodes"], w["verdict"]))
    p()
    windows = d["windows"]
except Exception:
    p("```")
    p(traceback.format_exc())
    p("```")

# --------------------------------------------------------------------- step 2
p("## 2 - actionable control tree")
p()


def find(*needles):
    for w in windows:
        hay = (w["title"] + " " + w["class"]).lower()
        if any(n.lower() in hay for n in needles):
            return w
    return None


TARGETS = (
    ("File Explorer", ("cabinetwclass",)),
    ("DaVinci Resolve", ("resolve", "davinci")),
    ("Lightroom Classic", ("agwinmainframe", "lightroom")),
    ("Blender", ("ghost_windowclass", "blender")),
)

for label, keys in TARGETS:
    w = find(*keys)
    p("### %s" % label)
    if not w:
        p("Not open - skipped.")
        p()
        continue
    try:
        t0 = time.time()
        r = S.t_read_ui_tree({"window_handle": w["handle"],
                              "max_nodes": 2500, "only_actionable": True})
        p("`%s` - **%d actionable nodes** - %.1fs"
          % (w["class"], r["nodes_returned"], time.time() - t0))
        if r.get("warning"):
            p()
            p("> %s" % r["warning"])

        found = []

        def collect(node):
            if node is None or len(found) >= 12:
                return
            if node.get("actions") and node.get("name"):
                found.append(node)
            for kid in node.get("children") or []:
                collect(kid)

        collect(r.get("tree"))
        if found:
            p()
            p("| Element | Type | Actions |")
            p("|---|---|---|")
            for g in found:
                p("| %s | %s | %s |" % (g["name"][:40], g["role"],
                                        ", ".join(g.get("actions", []))))
        else:
            p()
            p("No named element with an action - this window paints its own UI.")
    except Exception:
        p("```")
        p(traceback.format_exc())
        p("```")
    p()

# --------------------------------------------------------------------- step 3
p("## 3 - write test, without a screenshot")
p()
w = find("cabinetwclass")
if not w:
    p("No File Explorer window open - skipped.")
else:
    try:
        r = S.t_read_ui_tree({"window_handle": w["handle"], "max_nodes": 3000})
        target = []

        def search(node):
            if node is None or target:
                return
            if "expand" in (node.get("actions") or []) and node.get("name"):
                target.append(node)
            for kid in node.get("children") or []:
                search(kid)

        search(r.get("tree"))

        if not target:
            p("No expandable element found.")
        else:
            z = target[0]
            p("Target: **%s** (%s)" % (z["name"], z["role"]))
            p()
            result = S.t_expand({"ref": z["ref"]})
            time.sleep(0.8)
            p("- `expand` returned:")
            p()
            p("```json")
            p(repr(result))
            p("```")
            try:
                S.t_expand({"ref": z["ref"], "collapse": True})
                p("- reset cleanly")
            except Exception:
                pass
            p()
            verified = bool(result.get("effect_verified"))
            p("**Result: write access %s.**"
              % ("works, and the effect is measurable" if verified
                 else "ran, but no state change was measurable"))
    except Exception:
        p("```")
        p(traceback.format_exc())
        p("```")

p()
p("---")
p("Produced by `tests/measure_desktop.py` against the code in `src/`.")
print("\nReport written to: %s" % save())
