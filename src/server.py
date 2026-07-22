#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PC Screen Control - the screen as structure, with images on demand.

An MCP server exposing Windows UI Automation. Instead of screenshots and pixel
coordinates it reads the real control tree of an application and operates
controls through their accessibility actions.

Two design decisions worth knowing:
  * Every action returns element state BEFORE and AFTER, so its effect is
    verifiable from the response alone - no screenshot needed to confirm.
  * capture() returns a real image over MCP - of the screen, a window, or a
    SINGLE element. Element-level capture is something screenshot tools
    cannot do.

Requires: uiautomation, pillow
"""

import sys
import os
import json
import base64
import io as _io
import traceback

SERVER_NAME = "pc-screen-control"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

# MCP speaks UTF-8 in both directions. Windows does not: a pipe defaults to the
# machine's ANSI code page, so on a German system "Grusse" arrives as mojibake
# and every umlaut a caller sends is silently destroyed. All three streams have
# to be pinned, stdin included - that one is easy to forget because output looks
# correct while input is already broken.
_PROTO_OUT = sys.stdout
try:
    _PROTO_OUT.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.stdout = sys.stderr

os.environ.setdefault("QT_ACCESSIBILITY", "1")


def _ensure_dependencies():
    """
    Install missing dependencies on first run.

    Rationale: this ships as a desktop extension that users install with a
    double click. Telling them to open a terminal first is where most give up.
    Only the two declared dependencies are installed, nothing else. Skipped
    entirely when running from a frozen build, which bundles them already.
    """
    if getattr(sys, "frozen", False):
        return
    import importlib
    missing = []
    for module, package in (("uiautomation", "uiautomation"),
                            ("comtypes", "comtypes"),
                            ("PIL", "pillow")):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if not missing:
        return
    sys.stderr.write("[setup] installing missing dependencies: %s\n"
                     % ", ".join(missing))
    sys.stderr.flush()
    try:
        import subprocess
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet",
             "--disable-pip-version-check", "--no-input"] + missing,
            check=False, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        importlib.invalidate_caches()
        sys.stderr.write("[setup] done\n")
    except Exception as e:
        sys.stderr.write("[setup] failed: %s - install manually with: "
                         "pip install %s\n" % (e, " ".join(missing)))
    sys.stderr.flush()


_ensure_dependencies()

try:
    import uiautomation as auto
    try:
        auto.Logger.SetLogFile(os.devnull)
    except Exception:
        pass
    try:
        auto.SetGlobalSearchTimeout(2)
    except Exception:
        pass
    _UIA_ERROR = None
except Exception as _e:
    auto = None
    _UIA_ERROR = "%s: %s" % (type(_e).__name__, _e)

try:
    from PIL import ImageGrab, Image
    _PIL_ERROR = None
except Exception as _e:
    ImageGrab = None
    _PIL_ERROR = "%s: %s" % (type(_e).__name__, _e)

DEFAULT_MAX_DEPTH = 20
DEFAULT_MAX_NODES = 1200
HARD_MAX_NODES = 6000
NAME_CLIP = 300


def _require_uia():
    if auto is None:
        raise RuntimeError("uiautomation is not installed (%s)" % _UIA_ERROR)


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _role(el):
    n = _safe(lambda: el.ControlTypeName, "") or ""
    return n.replace("ControlType", "") or "Unknown"


def _rect(el):
    def g():
        r = el.BoundingRectangle
        return [int(r.left), int(r.top), int(r.right), int(r.bottom)]
    return _safe(g)


def _pat(el, name):
    """
    Get a UI Automation pattern from any element.

    The obvious route - el.GetInvokePattern() - is a trap: the uiautomation
    package puts those helpers on the *subclasses*, so GetGridPattern exists on
    a ListControl but not on a Control, and GetWindowPattern exists on a
    WindowControl but not on the PaneControl that many applications use for
    their main window. Asking through the class therefore reports "no such
    capability" for controls that plainly have it, and every _safe() around it
    swallows the AttributeError without a word.

    GetPattern(PatternId.X) lives on Control itself and answers for any
    element, which is also how UI Automation is meant to be used.
    """
    pid = getattr(auto.PatternId, name, None)
    if pid is None:
        return None
    return _safe(lambda: el.GetPattern(pid))


_AKTIONEN = (
    ("InvokePattern", "invoke"),
    ("TogglePattern", "toggle"),
    ("ValuePattern", "set_text"),
    ("ExpandCollapsePattern", "expand"),
    ("SelectionItemPattern", "select"),
    ("RangeValuePattern", "set_value"),
    ("TextPattern", "read_text"),
    ("ScrollPattern", "scroll"),
    ("GridPattern", "read_table"),
    ("TablePattern", "read_table"),
    ("TransformPattern", "window"),
    ("WindowPattern", "window"),
)


def _actions(el):
    a = []
    for pattern, name in _AKTIONEN:
        if name not in a and _pat(el, pattern) is not None:
            a.append(name)
    return a


def _value(el):
    v = _safe(lambda: _pat(el, "ValuePattern").Value)
    if v:
        return str(v)[:NAME_CLIP]
    rv = _safe(lambda: _pat(el, "RangeValuePattern").Value)
    if rv is not None:
        return rv
    tg = _safe(lambda: _pat(el, "TogglePattern").ToggleState)
    if tg is not None:
        return {0: "off", 1: "on", 2: "mixed"}.get(int(tg), str(tg))
    return None


def _state(el):
    """Compact state - the basis for before/after comparison."""
    ex = _safe(lambda: _pat(el, "ExpandCollapsePattern").ExpandCollapseState)
    return {
        "name": (_safe(lambda: el.Name, "") or "")[:NAME_CLIP],
        "value": _value(el),
        "expanded": {0: "collapsed", 1: "expanded", 2: "partial",
                     3: "leaf"}.get(ex) if ex is not None else None,
        "selected": _safe(lambda: _pat(el, "SelectionItemPattern").IsSelected),
        "focused": _safe(lambda: el.HasKeyboardFocus, None),
        "enabled": _safe(lambda: el.IsEnabled, None),
        "rect": _rect(el),
    }


def _wirkung(vorher, nachher):
    """What changed? This is the proof that an action had an effect."""
    diff = {}
    for k in vorher:
        if vorher.get(k) != nachher.get(k):
            diff[k] = {"before": vorher.get(k), "after": nachher.get(k)}
    return diff


def _describe(el, ref):
    d = {"ref": ref, "role": _role(el),
         "name": (_safe(lambda: el.Name, "") or "")[:NAME_CLIP]}
    aid = _safe(lambda: el.AutomationId, "") or ""
    if aid:
        d["automation_id"] = aid
    cls = _safe(lambda: el.ClassName, "") or ""
    if cls:
        d["class"] = cls
    v = _value(el)
    if v is not None and v != "":
        d["value"] = v
    if _safe(lambda: el.IsEnabled, True) is False:
        d["enabled"] = False
    if _safe(lambda: el.IsOffscreen, False) is True:
        d["offscreen"] = True
    if _safe(lambda: el.HasKeyboardFocus, False) is True:
        d["focused"] = True
    if _safe(lambda: _pat(el, "SelectionItemPattern").IsSelected) is True:
        d["selected"] = True
    a = _actions(el)
    if a:
        d["actions"] = a
    r = _rect(el)
    if r:
        d["rect"] = r
    return d


def _top_windows():
    _require_uia()
    out = []
    for w in auto.GetRootControl().GetChildren():
        try:
            h = w.NativeWindowHandle
            if not h:
                continue
            name = (w.Name or "").strip()
            cls = w.ClassName or ""
            if not name and cls in ("Progman", "WorkerW"):
                continue
            out.append({"handle": int(h), "title": name[:NAME_CLIP],
                        "class": cls, "role": _role(w), "rect": _rect(w),
                        "framework": _safe(lambda: w.FrameworkId, "") or "?",
                        "offscreen": bool(_safe(lambda: w.IsOffscreen, False))})
        except Exception:
            continue
    return out


def _window_by(handle=None, title=None):
    _require_uia()
    if handle:
        el = auto.ControlFromHandle(int(handle))
        if el is None:
            raise ValueError("No window with handle %s" % handle)
        return el, int(handle)
    if title:
        needle = title.lower()
        best = None
        for w in _top_windows():
            t = w["title"].lower()
            if t == needle:
                best = w
                break
            if needle in t and best is None:
                best = w
        if best is None:
            raise ValueError("No window matches %r" % title)
        return auto.ControlFromHandle(best["handle"]), best["handle"]
    raise ValueError("Provide window_handle or window_title")


def _resolve(ref):
    _require_uia()
    hs, _, path = str(ref).partition(":")
    el = auto.ControlFromHandle(int(hs))
    if el is None:
        raise ValueError("Window %s no longer exists - re-read the tree." % hs)
    if path:
        for p in path.split("."):
            kids = el.GetChildren()
            i = int(p)
            if i < 0 or i >= len(kids):
                raise ValueError("Ref %r is stale - re-read the tree." % ref)
            el = kids[i]
    return el


def _geschwister_index(parent, kind):
    """Which child of `parent` is `kind`, numbered the way _walk numbers them."""
    kids = _safe(lambda: parent.GetChildren(), []) or []
    r = _rect(kind)
    n = _safe(lambda: kind.Name, "")
    t = _safe(lambda: kind.ControlTypeName, "")
    for i, k in enumerate(kids):
        if (_rect(k) == r
                and _safe(lambda: k.Name, "") == n
                and _safe(lambda: k.ControlTypeName, "") == t):
            return i
    return None


def _ref_for(el):
    """
    Build a usable ref for any element - including one inside a dialog that was
    never listed as a window.

    This is load-bearing far beyond its size. Without a ref, a caller can *name*
    a control but not operate it, and the only way left to touch it is the
    mouse. So a weakness here quietly pushes the whole server down the cost
    ladder it exists to avoid.

    The previous version only returned a ref when the parent had no window
    handle of its own. That is true exactly one level below the desktop root -
    but the root itself carries a handle, so the test never fired and this
    returned None for nearly everything. element_from_point and get_focus could
    describe a control and not act on it, and the input guard could not save the
    focus it promised to restore.

    The rule that actually holds: walk up until the parent has no parent, which
    is the desktop root. Whatever sits directly below the root is the top-level
    window, and its handle anchors the ref - the same anchor _resolve expects.
    """
    chain = []
    cur = el
    for _ in range(80):
        parent = _safe(lambda: cur.GetParentControl())
        if parent is None:
            return None                        # walked past the root
        if _safe(lambda: parent.GetParentControl()) is None:
            h = _safe(lambda: cur.NativeWindowHandle, 0)
            if not h:
                return None
            chain.reverse()
            return "%d:%s" % (int(h), ".".join(str(c) for c in chain))
        idx = _geschwister_index(parent, cur)
        if idx is None:
            return None
        chain.append(idx)
        cur = parent
    return None


def _walk(el, hwnd, path, depth, max_depth, budget, only_actionable):
    if budget["n"] >= budget["max"]:
        return None
    ref = "%d:%s" % (hwnd, path) if path else "%d:" % hwnd
    node = _describe(el, ref)
    budget["n"] += 1
    if depth >= max_depth:
        k = _safe(lambda: el.GetChildren(), []) or []
        if k:
            node["truncated_children"] = len(k)
        return node
    children = _safe(lambda: el.GetChildren(), []) or []
    out = []
    for i, c in enumerate(children):
        if budget["n"] >= budget["max"]:
            node["truncated_children"] = len(children) - i
            break
        s = _walk(c, hwnd, ("%s.%d" % (path, i)) if path else str(i),
                  depth + 1, max_depth, budget, only_actionable)
        if s is not None:
            out.append(s)
    if out:
        node["children"] = out
    if only_actionable and not node.get("actions") and not node.get("children"):
        budget["n"] -= 1
        return None
    return node


CHROMIUM_KLASSEN = ("Chrome_WidgetWin", "Chrome_RenderWidget")


# The verdict only needs to distinguish 0-2, 3-19 and 20+. Counting to 400
# every time answered a question nobody asked and made the tool everyone is
# told to start with the slowest one in the set.
PROBE_LIMIT = 120
WACH_LIMIT = 150

# Chromium keeps its accessibility tree once it has built it, so a window only
# has to be woken once. What costs the time is not the waking but the deep walk
# that measures the result - twenty-two levels through a web page, per window,
# per call. Measured with four Chromium windows open: 11.4s when re-walking
# every time, 8.9s when only skipping the wake, and under 2s once the count
# itself is remembered.
#
# The remembered number can go stale when a page changes. That is acceptable
# because nothing depends on its exact value: it decides readable vs shallow,
# and a window that was readable does not become unreadable. describe_screen
# marks the entry as cached so the number is never mistaken for fresh.
_GEWECKT = {}


def _zaehlen(el, tiefe, max_tiefe, budget):
    """
    Count nodes and nothing else.

    The probe used to build a full description of every node it counted, and
    describing a node asks it about twelve different patterns. Twelve COM calls
    per node, times a hundred nodes, times every open window - for a number
    that only has to land in one of three buckets. Counting alone is the same
    tree walk without any of that.
    """
    budget["n"] += 1
    if budget["n"] >= budget["max"] or tiefe >= max_tiefe:
        return
    for k in (_safe(lambda: el.GetChildren(), []) or []):
        if budget["n"] >= budget["max"]:
            return
        _zaehlen(k, tiefe + 1, max_tiefe, budget)


def _probe(hwnd, limit=PROBE_LIMIT, tiefe=8):
    el = auto.ControlFromHandle(hwnd)
    if el is None:
        return 0
    b = {"n": 0, "max": limit}
    _zaehlen(el, 0, tiefe, b)
    return b["n"]


def _aufwecken(hwnd, klasse):
    """
    Chromium builds its accessibility tree only once something asks for it,
    and the first walk is what asks. A shallow probe therefore measures the
    tree from *before* the question was heard and reports a browser, an
    Electron editor or a chat client as nearly empty.

    Measured: a Claude window probed at 13 nodes, then 207 once asked
    properly. The window never changed - only the order of asking did.
    """
    if not any(klasse.startswith(k) for k in CHROMIUM_KLASSEN):
        return None, False
    if hwnd in _GEWECKT:
        return _GEWECKT[hwnd], True
    import time as _t
    _probe(hwnd, limit=60, tiefe=6)
    _t.sleep(0.35)
    # Deeper than the normal probe on purpose: a web page nests far more than
    # a native dialog, and at depth 8 a fully exposed page still looks empty.
    n = _probe(hwnd, limit=WACH_LIMIT, tiefe=22)
    if n >= 20:
        if len(_GEWECKT) > 200:          # Fensterhandles werden wiederverwendet
            _GEWECKT.clear()
        _GEWECKT[hwnd] = n
    return n, False


# ------------------------------------------------------------------ reading
def t_describe_screen(args):
    res = []
    for w in _top_windows():
        if not w["title"] or w["offscreen"]:
            continue
        n = _probe(w["handle"])
        geweckt, aus_speicher = None, False
        if n < 20:
            geweckt, aus_speicher = _aufwecken(w["handle"], w["class"])
            if geweckt and geweckt > n:
                n = geweckt

        if n <= 2:
            verdict = "canvas-only"
            note = ("Paints its own interface. capture() shows it, click and "
                    "drag operate it - it costs more, it is not impossible.")
        elif n < 20:
            verdict = "shallow"
            note = ("Few controls exposed. Try read_ui_tree once anyway - some "
                    "frameworks build their tree only when first asked.")
        else:
            verdict = "readable"
            note = "Real controls - fully addressable."

        eintrag = {"handle": w["handle"], "title": w["title"],
                   "class": w["class"], "framework": w["framework"],
                   "rect": w["rect"], "probe_nodes": n, "verdict": verdict,
                   "note": note}
        if geweckt is not None:
            eintrag["woken"] = True
            if aus_speicher:
                # Never let a remembered number pass for a fresh measurement.
                eintrag["cached"] = True
                eintrag["note"] += (" Node count is from when this window was "
                                    "first woken; read_ui_tree for a current "
                                    "one.")
        res.append(eintrag)
    res.sort(key=lambda r: -r["probe_nodes"])
    return {"windows": res, "count": len(res),
            "note": "Work down the ladder and stop at the first rung that "
                    "works: read_ui_tree / find_elements, then invoke / "
                    "set_text / set_value / toggle / select, then capture, "
                    "and only then click / drag / send_keys - the last rung "
                    "takes the user's mouse away from them."}


def t_list_windows(args):
    w = [x for x in _top_windows() if not x["offscreen"] and x["title"]]
    return {"windows": w, "count": len(w)}


def t_read_ui_tree(args):
    el, hwnd = _window_by(args.get("window_handle"), args.get("window_title"))
    md = int(args.get("max_depth", DEFAULT_MAX_DEPTH))
    mn = min(int(args.get("max_nodes", DEFAULT_MAX_NODES)), HARD_MAX_NODES)
    b = {"n": 0, "max": mn}
    tree = _walk(el, hwnd, "", 0, md, b, bool(args.get("only_actionable", False)))
    r = {"window": {"handle": hwnd, "title": (el.Name or "")[:NAME_CLIP]},
         "nodes_returned": b["n"], "tree": tree}
    if b["n"] >= b["max"]:
        r["note"] = "Node budget reached - raise max_nodes or use only_actionable."
    if b["n"] <= 2:
        r["warning"] = "Canvas-only: no addressable controls. Use capture()."
    return r


def t_find_elements(args):
    q = str(args.get("query", "")).lower().strip()
    if not q:
        raise ValueError("query is required")
    el, hwnd = _window_by(args.get("window_handle"), args.get("window_title"))
    rf = (args.get("role") or "").lower()
    lim = int(args.get("limit", 30))
    hits, stack, seen = [], [(el, "")], 0
    while stack and len(hits) < lim and seen < 4000:
        cur, path = stack.pop()
        seen += 1
        ref = "%d:%s" % (hwnd, path) if path else "%d:" % hwnd
        nm = (_safe(lambda: cur.Name, "") or "").lower()
        ai = (_safe(lambda: cur.AutomationId, "") or "").lower()
        wo = "automation_id" if q in ai else ("name" if q in nm else None)
        if wo and (not rf or rf in _role(cur).lower()):
            d = _describe(cur, ref)
            d["matched_on"] = wo
            hits.append(d)
        kids = _safe(lambda: cur.GetChildren(), []) or []
        for i in range(len(kids) - 1, -1, -1):
            stack.append((kids[i], ("%s.%d" % (path, i)) if path else str(i)))

    ergebnis = {"matches": hits, "count": len(hits), "scanned": seen}
    if not hits:
        ergebnis["note"] = (
            "Nothing matched. Control names follow the WINDOW'S language, not "
            "yours - a German Windows says 'Speichern', not 'Save'. Read the "
            "tree once and search for what is actually written there, or "
            "search by automation_id, which does not change with language.")
    elif all(h.get("matched_on") == "name" for h in hits):
        ergebnis["note"] = ("Matched on the display name, which is "
                            "language-dependent. Where an automation_id is "
                            "shown, prefer it - it survives translation.")
    return ergebnis


def t_element_from_point(args):
    _require_uia()
    x, y = int(args["x"]), int(args["y"])

    # Windows answers ControlFromPoint for coordinates that are nowhere near a
    # screen, handing back the desktop root. Reporting found:true for a point
    # outside every monitor is a lie that sends the caller looking for a
    # control that was never there.
    ox, oy, vw, vh = _virtueller_bildschirm()
    if vw and not (ox <= x < ox + vw and oy <= y < oy + vh):
        raise RuntimeError(
            "Point %d,%d is outside every screen. The desktop spans %d,%d to "
            "%d,%d - check the rect you took these coordinates from."
            % (x, y, ox, oy, ox + vw, oy + vh))

    el = auto.ControlFromPoint(x, y)
    if el is None:
        return {"found": False, "point": [x, y]}
    ref = _ref_for(el)
    d = _describe(el, ref or "")
    if not ref:
        d["ref"] = None
    return {"found": True, "point": [x, y], "element": d}


def t_get_focus(args):
    _require_uia()
    el = _safe(lambda: auto.GetFocusedControl())
    if el is None:
        return {"found": False}
    ref = _ref_for(el)
    d = _describe(el, ref or "")
    if not ref:
        d["ref"] = None
    return {"found": True, "element": d}


def t_read_text(args):
    el = _resolve(args["ref"])
    tp = _pat(el, "TextPattern")
    if tp is not None:
        t = _safe(lambda: tp.DocumentRange.GetText(-1), "")
        return {"text": (t or "")[:20000], "source": "TextPattern"}
    parts, stack, seen = [], [el], 0
    while stack and seen < 800 and len(parts) < 400:
        c = stack.pop()
        seen += 1
        n = _safe(lambda: c.Name, "") or ""
        if n.strip():
            parts.append(n.strip())
        for k in reversed(_safe(lambda: c.GetChildren(), []) or []):
            stack.append(k)
    return {"text": "\n".join(parts)[:20000], "source": "gesammelte Namen"}


def t_get_text(args):
    el = _resolve(args["ref"])
    return _state(el)


# ------------------------------------------------------------------ image
def _virtueller_bildschirm():
    """Origin and size of the whole desktop across all monitors."""
    try:
        import ctypes
        g = ctypes.windll.user32.GetSystemMetrics
        x, y, cx, cy = g(76), g(77), g(78), g(79)
        if cx > 0 and cy > 0:
            return int(x), int(y), int(cx), int(cy)
    except Exception:
        pass
    return 0, 0, 0, 0


def t_capture(args):
    """Image of the screen, a window, or a SINGLE element."""
    if ImageGrab is None:
        raise RuntimeError("pillow is missing (%s). Run: pip install pillow" % _PIL_ERROR)
    ref = args.get("ref")
    hwnd = args.get("window_handle")
    titel = args.get("window_title")
    box = None
    beschreibung = "full screen"

    if ref:
        el = _resolve(ref)
        if args.get("focus", True):
            _safe(lambda: el.SetFocus())
        box = _rect(el)
        beschreibung = "element: %s (%s)" % (
            (_safe(lambda: el.Name, "") or "?")[:60], _role(el))
    elif hwnd or titel:
        el, h = _window_by(hwnd, titel)
        if args.get("focus", True):
            _safe(lambda: el.SetActive())
            import time as _t
            _t.sleep(0.4)
        box = _rect(el)
        beschreibung = "window: %s" % ((_safe(lambda: el.Name, "") or "?")[:60])

    # Multi-monitor: UIA reports coordinates relative to the primary screen,
    # so a monitor placed to the left or above has negative values. Pillow
    # indexes the grabbed image from the top-left of the *virtual* desktop.
    # Grab everything, then translate - clamping to zero would silently return
    # the wrong region on any left-hand or top-hand second monitor.
    ox, oy, vw, vh = _virtueller_bildschirm()
    img = ImageGrab.grab(all_screens=True)

    if box:
        if box[2] <= box[0] or box[3] <= box[1]:
            raise RuntimeError("Element has no visible area.")
        links, oben = box[0] - ox, box[1] - oy
        rechts, unten = box[2] - ox, box[3] - oy
        sichtbar = (max(0, links), max(0, oben),
                    min(img.size[0], rechts), min(img.size[1], unten))
        if sichtbar[2] <= sichtbar[0] or sichtbar[3] <= sichtbar[1]:
            raise RuntimeError(
                "Element lies outside every screen (rect %s, desktop spans "
                "%d,%d to %d,%d). It is probably minimised or scrolled away."
                % (box, ox, oy, ox + vw, oy + vh))
        img = img.crop(sichtbar)
        if (sichtbar[0], sichtbar[1], sichtbar[2], sichtbar[3]) != \
                (links, oben, rechts, unten):
            beschreibung += " (clipped to the visible area)"

    voll = img.size
    maxpx = int(args.get("max_px", 1400))
    if max(img.size) > maxpx:
        img.thumbnail((maxpx, maxpx), Image.LANCZOS)

    buf = _io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")

    info = "%s | original %dx%d | returned %dx%d" % (
        beschreibung, voll[0], voll[1], img.size[0], img.size[1])
    return {"_content": [
        {"type": "image", "data": b64, "mimeType": "image/png"},
        {"type": "text", "text": info},
    ]}


# ---------------------------------------------------------------- actions
def _mit_wirkung(el, aktion, fn):
    """Runs an action and returns its verifiable effect."""
    import time as _t
    vorher = _state(el)
    fn()
    _t.sleep(float(0.5))
    nachher = _state(el)
    diff = _wirkung(vorher, nachher)
    return {"ok": True, "action": aktion,
            "element": vorher.get("name"),
            "before": vorher, "after": nachher,
            "changed": diff,
            "effect_verified": bool(diff),
            "note": ("State changed measurably." if diff else
                        "No state change measurable on the element itself - "
                        "the effect may be elsewhere. Use read_ui_tree on the "
                        "window or capture() to verify.")}


def t_invoke(args):
    """
    Press a control through the accessibility interface, never by pointer.

    There used to be a fourth branch here: if no pattern answered, it called
    el.Click() and moved the user's real mouse. That made a tool documented as
    "your cursor is never touched" quietly do the one thing it promised not to,
    without the edge glow and without the input guard, because the fallback sat
    outside both. A cheap tool that escalates in silence is worse than one that
    refuses, since the caller never learns that a cheaper route was missing.

    It now refuses and says what the element actually offers, so the decision to
    spend the mouse is made deliberately, by name, one level up.
    """
    el = _resolve(args["ref"])
    ip = _pat(el, "InvokePattern")
    if ip is not None:
        return _mit_wirkung(el, "invoke", lambda: ip.Invoke())
    sp = _pat(el, "SelectionItemPattern")
    if sp is not None:
        return _mit_wirkung(el, "select", lambda: sp.Select())
    tp = _pat(el, "TogglePattern")
    if tp is not None:
        return _mit_wirkung(el, "toggle", lambda: tp.Toggle())
    ep = _pat(el, "ExpandCollapsePattern")
    if ep is not None:
        return _mit_wirkung(el, "expand", lambda: ep.Expand())

    kann = _actions(el)
    r = _rect(el)
    raise RuntimeError(
        "This element publishes no way to be pressed - no Invoke, Selection, "
        "Toggle or ExpandCollapse pattern. It offers: %s. Nothing here can "
        "press it without the pointer, so this tool will not do it behind your "
        "back. If a real click is worth it, call click(x=%d, y=%d), which "
        "announces itself at the screen edge and hands input back afterwards."
        % (", ".join(kann) if kann else "nothing",
           (r[0] + r[2]) // 2, (r[1] + r[3]) // 2))


def t_toggle(args):
    el = _resolve(args["ref"])
    tp = _pat(el, "TogglePattern")
    if tp is None:
        raise RuntimeError("Not toggleable.")
    return _mit_wirkung(el, "toggle", lambda: tp.Toggle())


def t_expand(args):
    el = _resolve(args["ref"])
    ep = _pat(el, "ExpandCollapsePattern")
    if ep is None:
        raise RuntimeError("Not expandable or collapsible.")
    if args.get("collapse"):
        return _mit_wirkung(el, "collapse", lambda: ep.Collapse())
    return _mit_wirkung(el, "expand", lambda: ep.Expand())


def t_select(args):
    el = _resolve(args["ref"])
    sp = _pat(el, "SelectionItemPattern")
    if sp is None:
        raise RuntimeError("Not selectable.")
    return _mit_wirkung(el, "select", lambda: sp.Select())


def t_set_text(args):
    el = _resolve(args["ref"])
    vp = _pat(el, "ValuePattern")
    if vp is None:
        raise RuntimeError("Not writable - focus it and use send_keys.")
    txt = str(args.get("text", ""))
    return _mit_wirkung(el, "set_text", lambda: vp.SetValue(txt))


def t_focus_window(args):
    el, hwnd = _window_by(args.get("window_handle"), args.get("window_title"))
    _safe(lambda: el.SetActive())
    return {"ok": True, "handle": hwnd, "title": (el.Name or "")[:NAME_CLIP]}


def t_send_keys(args):
    _require_uia()
    import time as _t
    el = None
    vorher = None
    if args.get("ref"):
        el = _resolve(args["ref"])
        if "set_text" in _actions(el) and not args.get("force"):
            raise RuntimeError(
                "This element accepts its value directly. Use set_text: it is "
                "atomic, cannot be corrupted by a stray keystroke, and does "
                "not occupy the keyboard. Pass force=true to type anyway.")
        vorher = _state(el)
        _safe(lambda: el.SetFocus())
    # With no ref this follows whatever holds the keyboard, so the target has to
    # be verified under the lock - see _eingabe_laeuft. With a ref the focus was
    # just set explicitly above, so there is nothing to drift.
    wache = None if args.get("ref") else (args, "send these keystrokes")
    with _eingabe_laeuft(wache):
        auto.SendKeys(str(args["keys"]), waitTime=0.02)
        _t.sleep(0.4)
    if el is not None:
        nachher = _state(el)
        return {"ok": True, "sent": args["keys"], "before": vorher,
                "after": nachher, "changed": _wirkung(vorher, nachher),
                "effect_verified": bool(_wirkung(vorher, nachher))}
    return {"ok": True, "sent": args["keys"],
            "note": "Sent to whatever had focus. Confirm with get_focus or "
                    "read_ui_tree that it landed where you intended."}


def t_menu(args):
    """
    Open a menu and read what is in it.

    Menus are the one part of a Windows UI that does not exist until asked for:
    the items are built at the moment the menu opens and vanish when it closes.
    Reading the tree beforehand therefore never finds them. This opens the
    menu, waits for the popup to appear, and returns its items with refs -
    after which 'invoke' picks one, or 'close' dismisses it with Escape.
    """
    _require_uia()
    import time as _t

    aktion = args.get("action", "open")

    if aktion == "close":
        with _eingabe_laeuft():
            auto.SendKeys("{Esc}")
            _t.sleep(0.25)
        return {"ok": True, "action": "close"}

    vorher = {int(w["handle"]) for w in _top_windows()}

    if args.get("ref"):
        el = _resolve(args["ref"])
        if args.get("context", True):
            # Three ways to open a context menu, cheapest first. The pointer is
            # the last of them, not the first: a right-click was the only route
            # here until it turned out that a great many controls either expose
            # ExpandCollapse or answer the Applications key, both of which cost
            # the user nothing and neither of which moves the cursor.
            wie = None
            ep = _pat(el, "ExpandCollapsePattern")
            if ep is not None and _safe(lambda: ep.Expand(), "fehler") != "fehler":
                _t.sleep(0.25)
                if {int(w["handle"]) for w in _top_windows()} - vorher:
                    wie = "ExpandCollapsePattern"

            if wie is None and _safe(lambda: el.SetFocus(), "fehler") != "fehler":
                with _eingabe_laeuft():
                    auto.SendKeys("{Apps}")     # the context-menu key
                    _t.sleep(0.3)
                if {int(w["handle"]) for w in _top_windows()} - vorher:
                    wie = "Applications key"

            if wie is None:
                r = _rect(el)
                if not r:
                    raise RuntimeError("Element has no area to right-click.")
                heimat = _maus_merken()
                with _eingabe_laeuft():
                    auto.RightClick((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
                    _t.sleep(0.1)
                    _maus_zurueck(heimat)
                wie = "right-click"
            args["_wie"] = wie
        else:
            ep = _pat(el, "ExpandCollapsePattern")
            if ep is None:
                _safe(lambda: _pat(el, "InvokePattern").Invoke())
            else:
                _safe(lambda: ep.Expand())
    elif "x" in args and "y" in args:
        heimat = _maus_merken()
        with _eingabe_laeuft():
            auto.RightClick(int(args["x"]), int(args["y"]))
            _t.sleep(0.1)
            _maus_zurueck(heimat)
    else:
        raise RuntimeError("Pass ref, or x and y.")

    # Wait for the popup rather than sleeping a fixed amount.
    frist = _t.time() + float(args.get("timeout", 3))
    popup = None
    while _t.time() < frist:
        _t.sleep(0.12)
        for f in _top_windows():
            if int(f["handle"]) in vorher:
                continue
            if f["class"] in ("#32768", "Net UI Tool Window") \
                    or "menu" in f["role"].lower() \
                    or "popup" in f["class"].lower():
                popup = f
                break
        if popup:
            break

    if popup is None:
        return {"ok": False, "action": "open", "items": [],
                "note": "No menu window appeared. Some applications draw their "
                        "menus themselves - use capture() to look, then click()."}

    el = auto.ControlFromHandle(int(popup["handle"]))
    baum = t_read_ui_tree({"window_handle": int(popup["handle"]),
                           "max_nodes": 400, "only_actionable": True})

    eintraege = []

    def sammeln(node):
        if node is None:
            return
        if node.get("name") and node.get("actions"):
            eintraege.append({"ref": node["ref"], "name": node["name"],
                              "role": node["role"],
                              "actions": node.get("actions", []),
                              "enabled": node.get("enabled", True)})
        for kind in node.get("children") or []:
            sammeln(kind)

    sammeln(baum.get("tree"))
    wie = args.get("_wie", "right-click")
    return {"ok": True, "action": "open", "menu_window": popup["handle"],
            "items": eintraege, "count": len(eintraege),
            "how": wie,
            "took_input": wie == "right-click",
            "note": "Pick one with invoke(ref). Dismiss with "
                    "menu({action:'close'}) if none of them fit."}




# ------------------------------------------------ pointer, wheel, keyboard
def _was_liegt_dort(x, y):
    el = _safe(lambda: auto.ControlFromPoint(int(x), int(y)))
    if el is None:
        return None
    return {"name": (_safe(lambda: el.Name, "") or "")[:120],
            "role": _role(el), "ref": _ref_for(el)}


def t_read_table(args):
    """
    Read a table, grid or details list as rows and columns.

    Reading a spreadsheet through the generic tree costs one round trip per
    cell and throws away the thing that made it a table - which cell sits in
    which row and column. GridPattern answers that directly, and TablePattern
    adds the headers.
    """
    _require_uia()
    el = _resolve(args["ref"]) if args.get("ref") else _window_by(
        args.get("window_handle"), args.get("window_title"))[0]

    gp = _pat(el, "GridPattern")
    if gp is None:
        gefunden = _finde_raster(el)
        if gefunden is None:
            raise RuntimeError(
                "No grid found here. read_ui_tree the window and look for an "
                "element whose actions include 'read_table', then pass its "
                "ref. Lists that are not grids have no rows and columns - "
                "read those as ordinary children.")
        el, gp = gefunden

    zeilen_gesamt = int(_safe(lambda: gp.RowCount, 0) or 0)
    spalten_gesamt = int(_safe(lambda: gp.ColumnCount, 0) or 0)

    kopf = None
    tp = _pat(el, "TablePattern")
    if tp is not None:
        h = _safe(lambda: tp.GetColumnHeaders(), None)
        if h:
            kopf = [(_safe(lambda c=c: c.Name, "") or "") for c in h]

    von = max(0, int(args.get("start_row", 0)))
    wie_viele = min(int(args.get("max_rows", 100)), 500)
    bis = min(zeilen_gesamt, von + wie_viele)

    zeilen = _lies_ueber_raster(gp, von, bis, spalten_gesamt)
    weg = "grid_pattern"

    # Prove it before returning it. Some containers answer GetItem with the
    # header for every row - File Explorer is one - and a table where every
    # line is identical to the heading is worse than no table at all, because
    # it looks like data.
    if _alle_gleich(zeilen):
        ueber_kinder = _lies_ueber_kinder(el, von, bis, spalten_gesamt)
        if ueber_kinder and not _alle_gleich(ueber_kinder):
            zeilen, weg = ueber_kinder, "row_elements"

    ergebnis = {"ok": True, "rows_total": zeilen_gesamt,
                "columns": spalten_gesamt, "headers": kopf,
                "start_row": von, "rows_returned": len(zeilen),
                "rows": zeilen, "method": weg}
    if _alle_gleich(zeilen) and len(zeilen) > 1:
        ergebnis["warning"] = (
            "Every row came back identical, so this is almost certainly not "
            "real data. The list is probably virtualised - scroll it into "
            "view first, or read the row elements from read_ui_tree instead.")
    ergebnis["note"] = (("More rows exist - call again with start_row=%d." % bis)
                        if bis < zeilen_gesamt
                        else "Complete: every row is included.")
    return ergebnis


def _alle_gleich(zeilen):
    return len(zeilen) > 1 and all(z == zeilen[0] for z in zeilen)


def _zelltext(z):
    """
    The text in a cell.

    Value before Name, and that order is the whole trick. In a details list a
    cell is *named* after its column - every cell in the first column is
    called "Name" - while what the cell actually says lives in its value.
    Reading the name first returns the column headings once per row, which
    looks like a table and is not one.

    The same trap again one level down: a cell that HAS a value pattern and an
    empty value is genuinely empty - a folder has no size - and falling through
    to its name would print the column heading in that one cell. So once a
    value pattern exists, its answer is final, empty included.
    """
    if z is None:
        return None
    vp = _pat(z, "ValuePattern")
    if vp is not None:
        wert = _safe(lambda: vp.Value, None)
        return str(wert) if wert else ""
    for hole in (lambda: _pat(z, "LegacyIAccessiblePattern").Value,
                 lambda: z.Name):
        wert = _safe(hole, None)
        if wert:
            return str(wert)
    return ""


def _lies_ueber_raster(gp, von, bis, spalten):
    zeilen = []
    for r in range(von, bis):
        zeilen.append([_zelltext(_safe(lambda r=r, c=c: gp.GetItem(r, c)))
                       for c in range(spalten)])
    return zeilen


ZEILEN_ROLLEN = ("DataItem", "ListItem", "TreeItem")


def _sammle_zeilen(el, tiefe=0, gefunden=None):
    """
    Find the row elements, wherever they sit.

    They are not reliably direct children of the grid: File Explorer puts a
    container in between, and other applications nest deeper still. Searching
    only one level down is why the first attempt at this returned the header
    row over and over.
    """
    if gefunden is None:
        gefunden = []
    if tiefe > 6 or len(gefunden) > 800:
        return gefunden
    for k in (_safe(lambda: el.GetChildren(), []) or []):
        if _role(k) in ZEILEN_ROLLEN:
            gefunden.append(k)
        else:
            _sammle_zeilen(k, tiefe + 1, gefunden)
    return gefunden


def _lies_ueber_kinder(el, von, bis, spalten):
    """
    Read the row elements directly.

    Every grid is also a list of rows, and a row is a list of cells. Where the
    pattern refuses to hand out cells - which is what a virtualised list does,
    because the rows you have not scrolled to do not exist yet - the tree
    still has the ones that are on screen.
    """
    reihen = _sammle_zeilen(el)
    if not reihen:
        return None
    raus = []
    for zeile in reihen[von:bis]:
        zellen = [_zelltext(c)
                  for c in (_safe(lambda: zeile.GetChildren(), []) or [])]
        if not zellen:
            zellen = [_zelltext(zeile)]
        if spalten and len(zellen) < spalten:
            zellen += [""] * (spalten - len(zellen))
        raus.append(zellen[:spalten] if spalten else zellen)
    return raus


def _finde_raster(el, tiefe=0):
    """Find the nearest grid below this element. Data grids are usually two or
    three levels below the window, not at the top of it."""
    if tiefe > 12:
        return None
    for kind in (_safe(lambda: el.GetChildren(), []) or []):
        gp = _pat(kind, "GridPattern")
        if gp is not None and int(_safe(lambda: gp.RowCount, 0) or 0) > 0:
            return kind, gp
        tiefer = _finde_raster(kind, tiefe + 1)
        if tiefer:
            return tiefer
    return None


def t_set_value(args):
    """
    Set a numeric control exactly - sliders, spinners, scroll position.

    This is the tool that removes most of the reason to touch the mouse. A
    slider dragged by pixels lands where the pixels land; RangeValuePattern
    lands on the number you asked for, and does not move the cursor.
    """
    _require_uia()
    el = _resolve(args["ref"])

    if "percent" in args and args.get("percent") is not None:
        sp = _pat(el, "ScrollPattern")
        if sp is not None:
            pct = max(0.0, min(100.0, float(args["percent"])))
            axis = args.get("axis", "vertical")
            return _mit_wirkung(
                el, "set_value",
                lambda: sp.SetScrollPercent(
                    pct if axis == "horizontal" else -1,
                    pct if axis != "horizontal" else -1))

    rp = _pat(el, "RangeValuePattern")
    if rp is None:
        raise RuntimeError(
            "Element has no numeric value to set. Read it with get_text: if "
            "'set_value' is not in its actions, this control is not numeric.")

    lo = _safe(lambda: rp.Minimum)
    hi = _safe(lambda: rp.Maximum)

    # A pattern object that answers None to everything belongs to an element
    # that no longer exists. Refs are paths through the tree, so when the
    # window's contents change - a folder gaining a file, a list re-sorting -
    # the same path can land on a different element or on nothing at all.
    # Returning "ok" with three Nones is how an automation reports success for
    # something it never touched.
    if lo is None and hi is None and _safe(lambda: rp.Value) is None:
        raise RuntimeError(
            "This element answers nothing any more - minimum, maximum and "
            "value are all empty. The ref is almost certainly stale: refs are "
            "positions in the tree, and the window's contents have changed "
            "since you read it. Read the tree again and use the new ref.")

    if "percent" in args and args.get("percent") is not None:
        if lo is None or hi is None:
            raise RuntimeError("Element reports no range, so percent is "
                               "meaningless. Pass an absolute 'value'.")
        ziel = lo + (hi - lo) * max(0.0, min(100.0, float(args["percent"]))) / 100.0
    else:
        ziel = float(args["value"])

    if lo is not None and hi is not None and not (lo <= ziel <= hi):
        raise RuntimeError("Value %g is outside the element's range %g..%g"
                           % (ziel, lo, hi))

    vorwert = _safe(lambda: rp.Value)
    erg = _mit_wirkung(el, "set_value", lambda: rp.SetValue(ziel))
    erg["requested"] = ziel
    erg["range"] = [lo, hi]
    ist = _safe(lambda: _pat(el, "RangeValuePattern").Value)
    erg["actual"] = ist

    # Three different outcomes hide behind "no error", and calling all three a
    # success is how an automation quietly does nothing for ten minutes.
    if ist is None:
        erg["exact"] = None
    elif abs(float(ist) - ziel) < 1e-6:
        erg["exact"] = True
    elif vorwert is not None and abs(float(ist) - float(vorwert)) < 1e-6:
        erg["exact"] = False
        erg["effect_verified"] = False
        erg["note"] = (
            "The control did NOT move: it still reads %s. Its value is "
            "readable but not settable - scroll bars are the usual case, "
            "where you move the content and the bar follows. Scroll the "
            "container instead, or use set_value with 'percent' on the "
            "element that owns the scroll pattern." % ist)
    else:
        erg["exact"] = False
        erg["note"] = ("Control snapped to %s - it only accepts certain "
                       "steps." % ist)
    return erg


_WINDOW_STATES = {"normal": 0, "maximized": 1, "minimized": 2}


def t_window(args):
    """
    Move, resize and change the state of a window - without the mouse.

    Uses the window's own Transform and Window patterns where available and
    falls back to the Win32 call, because a fair number of windows expose
    neither pattern while still being perfectly movable.
    """
    _require_uia()
    import time as _t
    hwnd = int(args["window_handle"])
    el = auto.ControlFromHandle(hwnd)
    if el is None:
        raise RuntimeError("No window with handle %d" % hwnd)

    vorher = {"rect": _rect(el), "state": _window_state(el)}
    getan = []

    zustand = args.get("state")
    if zustand:
        if zustand not in _WINDOW_STATES:
            raise RuntimeError("state must be one of %s"
                               % ", ".join(_WINDOW_STATES))
        wp = _pat(el, "WindowPattern")
        if wp is not None and _safe(
                lambda: wp.CanMaximize or zustand != "maximized", True):
            _safe(lambda: wp.SetWindowVisualState(_WINDOW_STATES[zustand]))
        else:
            _win32_show(hwnd, zustand)
        getan.append("state=" + zustand)
        _t.sleep(0.35)

    will_move = "x" in args or "y" in args
    will_size = "width" in args or "height" in args
    if will_move or will_size:
        r = _rect(el) or [0, 0, 0, 0]
        x = int(args.get("x", r[0]))
        y = int(args.get("y", r[1]))
        w = int(args.get("width", r[2] - r[0]))
        h = int(args.get("height", r[3] - r[1]))

        tp = _pat(el, "TransformPattern")
        erledigt = False
        if tp is not None:
            if will_move and _safe(lambda: tp.CanMove, False):
                erledigt = _safe(lambda: (tp.Move(x, y), True)[1], False)
            if will_size and _safe(lambda: tp.CanResize, False):
                erledigt = _safe(lambda: (tp.Resize(w, h), True)[1],
                                 False) or erledigt
        if not erledigt:
            _win32_move(hwnd, x, y, w, h)
        getan.append("geometry=%d,%d %dx%d" % (x, y, w, h))
        _t.sleep(0.35)

    if not getan:
        raise RuntimeError("Nothing to do. Pass state and/or x/y/width/height.")

    nachher = {"rect": _rect(el), "state": _window_state(el)}
    diff = _wirkung(vorher, nachher)
    return {"ok": True, "action": "window", "window_handle": hwnd,
            "applied": getan, "before": vorher, "after": nachher,
            "changed": diff, "effect_verified": bool(diff),
            "note": None if diff else
            "No change measurable - the window may be fixed size or already "
            "in that state."}


def _window_state(el):
    """
    Read a window's state, falling back to Win32.

    Not every top-level window carries a WindowPattern - many applications use
    a Pane as their main window - so the pattern alone would report None and
    the before/after comparison would have nothing to compare.
    """
    wp = _pat(el, "WindowPattern")
    if wp is not None:
        for name in ("WindowVisualState", "CurrentWindowVisualState"):
            st = _safe(lambda n=name: getattr(wp, n))
            if st is not None:
                umkehr = {v: k for k, v in _WINDOW_STATES.items()}
                return umkehr.get(int(st), str(st))

    hwnd = _safe(lambda: el.NativeWindowHandle, 0)
    if not hwnd:
        return None
    import ctypes

    class WINDOWPLACEMENT(ctypes.Structure):
        _fields_ = [("length", ctypes.c_uint), ("flags", ctypes.c_uint),
                    ("showCmd", ctypes.c_uint),
                    ("ptMinPosition", ctypes.c_long * 2),
                    ("ptMaxPosition", ctypes.c_long * 2),
                    ("rcNormalPosition", ctypes.c_long * 4)]
    wp32 = WINDOWPLACEMENT()
    wp32.length = ctypes.sizeof(WINDOWPLACEMENT)
    if not ctypes.windll.user32.GetWindowPlacement(int(hwnd),
                                                   ctypes.byref(wp32)):
        return None
    return {1: "normal", 2: "minimized", 3: "maximized"}.get(
        wp32.showCmd, "normal")


def _win32_show(hwnd, zustand):
    import ctypes
    ctypes.windll.user32.ShowWindow(
        hwnd, {"normal": 9, "maximized": 3, "minimized": 6}[zustand])


def _win32_move(hwnd, x, y, w, h):
    import ctypes
    SWP_NOZORDER, SWP_NOACTIVATE = 0x0004, 0x0010
    ctypes.windll.user32.SetWindowPos(
        hwnd, 0, int(x), int(y), int(w), int(h),
        SWP_NOZORDER | SWP_NOACTIVATE)


def t_clipboard(args):
    """
    Read or write the Windows clipboard.

    Moving 2000 characters into an editor by typing them takes 2000 keystrokes
    that any stray click can corrupt. Clipboard plus Ctrl+V is one operation.
    Writing replaces whatever the user had copied, so the previous content is
    returned - put it back when you are done.
    """
    _require_uia()
    modus = args.get("mode", "read")
    vorher = _safe(lambda: auto.GetClipboardText(), None)
    if modus == "read":
        return {"ok": True, "mode": "read", "text": vorher,
                "length": len(vorher or "")}
    if modus != "write":
        raise RuntimeError("mode must be 'read' or 'write'")
    text = str(args["text"])
    auto.SetClipboardText(text)
    jetzt = _safe(lambda: auto.GetClipboardText(), None)
    return {"ok": True, "mode": "write", "length": len(text),
            "replaced": vorher, "effect_verified": jetzt == text,
            "note": "The user's previous clipboard content is in 'replaced'. "
                    "Restore it if this was a one-off paste."}


# ---------------------------------------------------------------------------
# The edge indicator.
#
# Only the two operations that genuinely take the user's input away turn it on:
# coordinate mouse actions and key sending. Everything else runs through the
# accessibility API and leaves cursor and keyboard alone, so lighting up for
# those would train the user to ignore the light.
# ---------------------------------------------------------------------------

# The guard settings. priority "claude" = Claude takes over with a warning and
# restores after; "me" = Claude waits for a "go" from the user before acting.
GUARD = {"priority": "claude", "idle_ms": 1500, "enabled": True}

_OVERLAY = {"proc": None, "off": False, "tiefe": 0,
            "abort": False, "go": False}


def _overlay_starten():
    if _OVERLAY["off"] or _OVERLAY["proc"] is not None:
        return _OVERLAY["proc"]
    skript = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "overlay.py")
    if not os.path.isfile(skript) or os.name != "nt":
        _OVERLAY["off"] = True
        return None
    try:
        import subprocess
        _OVERLAY["proc"] = subprocess.Popen(
            [sys.executable, skript], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            bufsize=1, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        import threading
        threading.Thread(target=_overlay_lesen, daemon=True).start()
    except Exception as e:
        sys.stderr.write("[overlay] not available: %s\n" % e)
        _OVERLAY["off"] = True
    return _OVERLAY["proc"]


def _overlay_lesen():
    """The overlay reports 'abort' (Escape) and 'go' (wait card clicked)."""
    p = _OVERLAY["proc"]
    if not p or not p.stdout:
        return
    try:
        for zeile in p.stdout:
            wort = zeile.strip().lower()
            if wort == "abort":
                _OVERLAY["abort"] = True
            elif wort == "go":
                _OVERLAY["go"] = True
    except Exception:
        pass


def _overlay_sagen(befehl):
    p = _overlay_starten()
    if p is None or p.poll() is not None:
        return
    try:
        p.stdin.write(befehl + "\n")
        p.stdin.flush()
    except Exception:
        _OVERLAY["proc"] = None


# ---------------------------------------------------------------------------
# Takeover detection.
#
# A keystroke sent without a target goes wherever the focus happens to be. That
# is fine right up until the user clicks somewhere between one call and the
# next - then the Enter meant for a form lands in their chat window, and nothing
# anywhere notices. Warning about it in a note does not help: notes are read
# after the damage.
#
# Distinguishing "the user moved" from "we moved" looks like it needs to know
# who generated an event, and GetLastInputInfo cannot tell - injected input
# counts as input there too. But the question can be asked without that. The
# foreground window is recorded after every single tool call, so anything we did
# is already in the baseline. If the foreground has moved by the time the next
# call starts, the move came from outside. That is the user, or a window that
# stole focus on its own - and neither is somewhere to type blindly.
#
# Watching the foreground window alone is not enough, and that showed up the
# second time this went wrong: the window never changed. The click landed on a
# different control *inside* the same window, and a keystroke follows keyboard
# focus, not the window. So the fingerprint has to be the focused control, with
# the window as the coarser half of the same check.
_LAGE = {"hwnd": 0, "titel": "", "fokus": None, "gesetzt": 0.0}


def _vordergrund():
    try:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _fokus_kennung():
    """
    A cheap fingerprint of whatever holds the keyboard right now.

    Deliberately not the rectangle: controls move when a window is resized or a
    list scrolls, and refusing over that would be noise. Type, automation id and
    name are what identify a control across those, and the id in particular does
    not change with the display language.
    """
    try:
        el = auto.GetFocusedControl()
        if el is None:
            return None
        return (_safe(lambda: el.ControlTypeName, "") or "",
                _safe(lambda: el.AutomationId, "") or "",
                (_safe(lambda: el.Name, "") or "")[:60])
    except Exception:
        return None


def _fenstertitel(h):
    if not h:
        return ""
    try:
        el = auto.ControlFromHandle(int(h))
        return (_safe(lambda: el.Name, "") or "")[:80]
    except Exception:
        return ""


def _lage_merken():
    """Record where the keyboard was pointing when a tool finished."""
    import time as _t
    h = _vordergrund()
    if h:
        _LAGE["hwnd"] = h
        _LAGE["fokus"] = _fokus_kennung()
        _LAGE["gesetzt"] = _t.time()


def _beschreibe_fokus(k):
    if not k:
        return "nothing in particular"
    art, kennung, name = k
    if name and kennung:
        return "%s %r (id %s)" % (art or "control", name, kennung)
    if name:
        return "%s %r" % (art or "control", name)
    if kennung:
        return "%s (id %s)" % (art or "control", kennung)
    return art or "control"


def _lage_pruefen(args, was):
    """
    Refuse blind input when the target moved since the last call.

    Two levels, because the first one alone was not enough. The coarse level is
    the foreground window. The fine level is the focused control, which catches
    the case that actually bit twice: the same window stays in front while the
    click lands in a different field inside it. A keystroke follows the focus,
    not the window, so the focus is the thing that has to match.

    Pass force=true to go ahead regardless.
    """
    if args.get("force"):
        return
    alt = _LAGE.get("hwnd") or 0
    if not alt:
        return                                  # first call, nothing to compare
    import time as _t
    her = _t.time() - float(_LAGE.get("gesetzt") or 0)

    jetzt = _vordergrund()
    if jetzt and jetzt != alt:
        raise RuntimeError(
            "Refusing to %s: the foreground window changed since the last "
            "call, so this would land somewhere it was not meant to. Expected "
            "%r (handle %d), found %r (handle %d), %.1fs later. Nothing here "
            "moved it, so the user did - or a window took focus on its own. "
            "Read the screen again before acting. If this really is the right "
            "target, bring it forward with focus_window first, or pass "
            "force=true."
            % (was, _fenstertitel(alt) or "?", alt,
               _fenstertitel(jetzt) or "?", jetzt, her))

    alter_fokus = _LAGE.get("fokus")
    neuer_fokus = _fokus_kennung()
    if alter_fokus and neuer_fokus and neuer_fokus != alter_fokus:
        raise RuntimeError(
            "Refusing to %s: the window is the same but the keyboard focus "
            "moved inside it, %.1fs after the last call. It was on %s and is "
            "now on %s. Typing follows the focus, not the window, so this "
            "would go into the wrong field. Nothing here moved it, so the user "
            "clicked somewhere. Read the screen again, or set the focus you "
            "want explicitly by passing a ref, or pass force=true."
            % (was, her, _beschreibe_fokus(alter_fokus),
               _beschreibe_fokus(neuer_fokus)))


def _leerlauf_ms():
    """How long since the user last touched anything."""
    import ctypes

    class LII(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
    lii = LII()
    lii.cbSize = ctypes.sizeof(LII)
    if ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
        return ctypes.windll.kernel32.GetTickCount() - lii.dwTime
    return 999999


# ---- state that gets saved before a takeover and restored after -----------
def _fokus_sichern():
    """Foreground window, focused element and its text selection."""
    zustand = {"hwnd": None, "ref": None, "sel": None,
               "kennung": _safe(_fokus_kennung)}
    try:
        import ctypes
        zustand["hwnd"] = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        pass
    try:
        el = auto.GetFocusedControl()
        if el is not None:
            zustand["ref"] = _ref_for(el)
            tp = _pat(el, "TextPattern")
            if tp is not None:
                rng = _safe(lambda: tp.GetSelection())
                if rng:
                    r0 = rng[0]
                    zustand["sel"] = (
                        _safe(lambda: r0.GetText(-1)),
                        el)
    except Exception:
        pass
    return zustand


def _vordergrund_setzen(hwnd):
    """
    Put a window back in front, and report whether it actually happened.

    SetForegroundWindow on its own is not enough, and it fails *silently*.
    Windows grants the foreground only to a process that already holds it or
    received the last input event - deliberately, so that background programs
    cannot steal focus while someone is typing. That protection is correct. It
    also means the obvious one-line call does nothing at all and returns without
    complaint, which is how a restore can look implemented for weeks and never
    once have run. It was reported as "it takes my focus and does not give it
    back", and that is exactly what was happening.

    The documented way through is to attach our input queue to the thread that
    currently owns the foreground: for the duration of that attachment Windows
    treats the request as coming from the foreground thread itself, so the call
    is granted. We detach immediately afterwards.
    """
    import ctypes
    if not hwnd:
        return False
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    u.SetForegroundWindow.restype = ctypes.c_bool
    u.BringWindowToTop.argtypes = [ctypes.c_void_p]
    u.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
    u.IsIconic.argtypes = [ctypes.c_void_p]
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u.GetWindowThreadProcessId.restype = ctypes.c_ulong

    ziel = ctypes.c_void_p(int(hwnd))
    if int(u.GetForegroundWindow() or 0) == int(hwnd):
        return True                              # never moved, nothing to do

    if u.IsIconic(ziel):
        u.ShowWindow(ziel, 9)                    # SW_RESTORE

    vorne = u.GetForegroundWindow()
    fremd = u.GetWindowThreadProcessId(ctypes.c_void_p(vorne or 0), None)
    eigen = k.GetCurrentThreadId()
    angehaengt = False
    if fremd and fremd != eigen:
        angehaengt = bool(u.AttachThreadInput(eigen, fremd, True))
    try:
        u.BringWindowToTop(ziel)
        u.SetForegroundWindow(ziel)
    finally:
        if angehaengt:
            u.AttachThreadInput(eigen, fremd, False)

    # Say it worked only if it worked.
    return int(u.GetForegroundWindow() or 0) == int(hwnd)


def _fokus_zurueck(zustand):
    """Give back the window, the focused control and the caret, and prove it."""
    if not zustand:
        return {"window": False, "control": False}
    ergebnis = {"window": _safe(lambda: _vordergrund_setzen(zustand.get("hwnd")),
                                False),
                "control": False}
    ref = zustand.get("ref")
    if ref:
        try:
            el = _resolve(ref)
            if _safe(lambda: el.SetFocus(), "fehler") != "fehler":
                jetzt = _fokus_kennung()
                ergebnis["control"] = bool(jetzt) and jetzt == zustand.get("kennung")
        except Exception:
            pass
    return ergebnis


# How long to wait after the lock engages before reading the screen. A click or
# keystroke made a moment earlier is still travelling through the message queue
# when the lock closes; reading immediately would see the state *before* that
# last input landed. 40 ms is past the queue and far below anything a person
# notices.
BERUHIGEN_MS = 40

# What the last release actually managed to give back. Tools copy this into
# their reply so "your focus is restored" is a measurement, not a promise.
_RUECKGABE = {}


class _eingabe_laeuft(object):
    """
    Wraps every operation that takes the physical mouse or keyboard.

    Three behaviours depending on who has priority and whether the user is
    active right now:

      - guard off, or user idle      -> lock immediately, no announcement
      - priority "claude", user busy -> rubber-band warning, then lock
      - priority "me", user busy      -> wait, show a card, act only on "go"

    On exit the edge fades and the saved focus is restored. Escape at any time
    aborts and raises, so the calling tool stops.

    Pass pruefen=(args, description) to have the takeover check run *inside* the
    lock. The order matters more than it looks. Checking first and locking
    afterwards leaves a gap - short, but a click lands in a millisecond, and a
    gap that only fails sometimes is worse than no check at all, because it
    teaches you to trust it. Locked first, the screen cannot move while it is
    being read, so what the check sees is what the action will hit.
    """

    def __init__(self, pruefen=None):
        self.pruefen = pruefen
        self.gesichert = None

    def _freigeben(self):
        _OVERLAY["tiefe"] = max(0, _OVERLAY["tiefe"] - 1)
        if _OVERLAY["tiefe"] == 0:
            _overlay_sagen("release")
            # Recorded rather than discarded: a restore that quietly fails is
            # indistinguishable from one that never ran, and that is how this
            # went unnoticed before. _letzte_rueckgabe is read by the tools so
            # the reply can say whether the screen was really handed back.
            _RUECKGABE.clear()
            _RUECKGABE.update(_fokus_zurueck(self.gesichert) or {})

    def __enter__(self):
        _OVERLAY["tiefe"] += 1
        if _OVERLAY["tiefe"] != 1:
            return self
        _OVERLAY["abort"] = False
        _OVERLAY["go"] = False
        self.gesichert = _fokus_sichern()

        import time as _t

        if not GUARD.get("enabled", True) or _OVERLAY["off"]:
            _overlay_sagen("lock")
            self._nach_dem_sperren()
            return self

        beschaeftigt = _leerlauf_ms() < GUARD.get("idle_ms", 1500)

        if beschaeftigt and GUARD.get("priority") == "me":
            # User has priority: wait for their go, or for them to go idle.
            _overlay_sagen("wait_on")
            frist = _t.time() + 120
            while _t.time() < frist:
                if _OVERLAY["go"]:
                    break
                if _leerlauf_ms() > GUARD.get("idle_ms", 1500) + 800:
                    break                    # they stopped; take over quietly
                _t.sleep(0.1)
            _overlay_sagen("release")
            _t.sleep(0.05)
            _overlay_sagen("lock")
        elif beschaeftigt:
            # Claude has priority: announce, then lock. The overlay flips to
            # 'hold' itself at the end of the pulse; we wait that long. The
            # pulse is deliberately a window in which the user may still type -
            # so whatever they did with it is exactly what the check below has
            # to see, and it can only see it once the lock has closed.
            _overlay_sagen("warn")
            _t.sleep((900 + 180) / 1000.0 + 0.1)
        else:
            _overlay_sagen("lock")

        self._nach_dem_sperren()
        return self

    def _nach_dem_sperren(self):
        """Let the last input land, then verify the target, still under lock."""
        if self.pruefen is None:
            return
        import time as _t
        _t.sleep(BERUHIGEN_MS / 1000.0)
        try:
            _lage_pruefen(*self.pruefen)
        except Exception:
            # __exit__ does not run when __enter__ raises, so hand the input
            # back here or the user stays frozen out.
            self._freigeben()
            raise

    def __exit__(self, *exc):
        self._freigeben()
        return False


def _abbruch_pruefen():
    """Raised inside a locked operation when the user hit Escape."""
    if _OVERLAY.get("abort"):
        raise RuntimeError("Aborted by the user (Escape). The screen and focus "
                           "have been handed back.")


def _maus_merken():
    """Where the user left the cursor. Coordinate actions put it back."""
    import ctypes

    class PT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    p = PT()
    if ctypes.windll.user32.GetCursorPos(ctypes.byref(p)):
        return (int(p.x), int(p.y))
    return None


def _maus_zurueck(pos):
    if not pos:
        return False
    import ctypes
    return bool(ctypes.windll.user32.SetCursorPos(int(pos[0]), int(pos[1])))


def t_click(args):
    """Click a screen coordinate. Fallback layer for canvas-only windows."""
    _require_uia()
    import time as _t
    x, y = int(args["x"]), int(args["y"])
    knopf = args.get("button", "left")
    anzahl = int(args.get("count", 1))
    vorher = _was_liegt_dort(x, y)
    heimat = _maus_merken()
    # The coordinate came from a screen read earlier. Verify under the
    # lock that the same thing is still in front of it.
    with _eingabe_laeuft((args, "click at these coordinates")):
        if knopf == "right":
            auto.RightClick(x, y)
        elif knopf == "middle":
            auto.MiddleClick(x, y)
        elif anzahl >= 2:
            auto.Click(x, y)
            _t.sleep(0.05)
            auto.Click(x, y)
        else:
            auto.Click(x, y)
        _t.sleep(0.5)
        nachher = _was_liegt_dort(x, y)
        zurueck = (_maus_zurueck(heimat)
                   if args.get("restore_cursor", True) else False)
    return {"ok": True, "point": [x, y], "button": knopf, "count": anzahl,
            "element_before": vorher, "element_after": nachher,
            "changed": vorher != nachher, "cursor_restored": zurueck,
            "note": "For canvas-only windows use capture() to verify."}


def t_drag(args):
    """
    Drag. Two forms:
      ref + dx/dy   -> from the element centre (this is how sliders work)
      x1,y1,x2,y2   -> free coordinates
    """
    _require_uia()
    import time as _t
    heimat = _maus_merken()
    if args.get("ref"):
        el = _resolve(args["ref"])
        r = _rect(el)
        if not r:
            raise RuntimeError("Element has no area.")
        # Ask the pattern directly rather than going through the action list:
        # the list is assembled for display and its contents have changed
        # before, which would silently turn this guard off.
        if _pat(el, "RangeValuePattern") is not None and not args.get("force"):
            raise RuntimeError(
                "This element accepts an exact numeric value (range %s..%s, "
                "currently %s). Use set_value instead of dragging it: it is "
                "precise, and it does not move the user's cursor. Pass "
                "force=true to drag anyway."
                % (_safe(lambda: _pat(el, "RangeValuePattern").Minimum),
                   _safe(lambda: _pat(el, "RangeValuePattern").Maximum),
                   _safe(lambda: _pat(el, "RangeValuePattern").Value)))
        vorher = _state(el)
        x1 = (r[0] + r[2]) // 2
        y1 = (r[1] + r[3]) // 2
        x2 = x1 + int(args.get("dx", 0))
        y2 = y1 + int(args.get("dy", 0))
        with _eingabe_laeuft():
            auto.DragDrop(x1, y1, x2, y2, waitTime=0.3)
            _t.sleep(0.5)
            nachher = _state(el)
            zurueck = (_maus_zurueck(heimat)
                       if args.get("restore_cursor", True) else False)
        return {"ok": True, "from": [x1, y1], "to": [x2, y2],
                "before": vorher, "after": nachher,
                "changed": _wirkung(vorher, nachher),
                "cursor_restored": zurueck,
                "effect_verified": bool(_wirkung(vorher, nachher))}
    x1, y1 = int(args["x1"]), int(args["y1"])
    x2, y2 = int(args["x2"]), int(args["y2"])
    # Free coordinates, so the same verification as click applies.
    with _eingabe_laeuft((args, "drag across these coordinates")):
        auto.DragDrop(x1, y1, x2, y2, waitTime=0.3)
        _t.sleep(0.5)
        zurueck = (_maus_zurueck(heimat)
                   if args.get("restore_cursor", True) else False)
    return {"ok": True, "from": [x1, y1], "to": [x2, y2],
            "cursor_restored": zurueck,
            "note": "Free drag - verify the result with capture()."}


def t_scroll(args):
    """Scroll. By ref (preferred) or coordinate."""
    _require_uia()
    import time as _t
    menge = int(args.get("amount", 3))
    richtung = args.get("direction", "down")
    heimat = _maus_merken()

    # The cheap path first: a control with a scroll pattern can be moved to an
    # exact position without touching the wheel or the cursor.
    if args.get("ref"):
        el = _resolve(args["ref"])
        sp = _pat(el, "ScrollPattern")
        if sp is not None and not args.get("force_wheel"):
            jetzt = _safe(lambda: sp.VerticalScrollPercent)
            if jetzt is not None and jetzt >= 0:
                schritt = 10.0 * menge / 3.0
                ziel = jetzt + (schritt if richtung == "down" else -schritt)
                ziel = max(0.0, min(100.0, ziel))
                erg = _mit_wirkung(el, "scroll",
                                   lambda: sp.SetScrollPercent(-1, ziel))
                erg["method"] = "scroll_pattern"
                erg["percent"] = ziel
                erg["note"] = ("Moved by pattern, not by wheel - the cursor "
                               "was not touched.")
                return erg

    with _eingabe_laeuft():
        if args.get("ref"):
            el = _resolve(args["ref"])
            r = _rect(el)
            if r:
                auto.MoveTo((r[0] + r[2]) // 2, (r[1] + r[3]) // 2)
            vorher = _state(el)
        elif "x" in args and "y" in args:
            auto.MoveTo(int(args["x"]), int(args["y"]))
            vorher = None
        else:
            vorher = None
        _t.sleep(0.15)
        if richtung == "up":
            auto.WheelUp(menge)
        else:
            auto.WheelDown(menge)
        _t.sleep(0.4)
        erg = {"ok": True, "direction": richtung, "amount": menge,
               "method": "wheel"}
        if args.get("ref"):
            nachher = _state(_resolve(args["ref"]))
            erg["changed"] = _wirkung(vorher, nachher)
        erg["cursor_restored"] = (_maus_zurueck(heimat)
                                  if args.get("restore_cursor", True) else False)
    return erg


def t_hold_key(args):
    _require_uia()
    import time as _t
    taste = str(args["key"])
    dauer = min(float(args.get("seconds", 1)), 30)
    with _eingabe_laeuft((args, "hold this key down")):
        try:
            auto.SendKeys("{%s down}" % taste)
            _t.sleep(dauer)
        finally:
            # Never leave a key stuck down, whatever happened in between.
            auto.SendKeys("{%s up}" % taste)
    return {"ok": True, "key": taste, "seconds": dauer}


def t_wait(args):
    import time as _t
    s = min(float(args.get("seconds", 1)), 60)
    _t.sleep(s)
    return {"ok": True, "waited": s}


# ------------------------------------------------------ waiting on state
def t_wait_for(args):
    """
    Wait until a condition holds, instead of sleeping blindly.
      ref + expect   -> until a field reaches the given value
      window_title   -> until a window appears
      query          -> until an element with that name exists
    """
    _require_uia()
    import time as _t
    frist = min(float(args.get("timeout", 10)), 120)
    takt = 0.4
    ende = _t.time() + frist

    if args.get("window_title"):
        titel = args["window_title"].lower()
        while _t.time() < ende:
            for w in _top_windows():
                if titel in w["title"].lower():
                    return {"ok": True, "found": "window", "window": w,
                            "waited_seconds": round(frist - (ende - _t.time()), 1)}
            _t.sleep(takt)
        return {"ok": False, "reason": "Window did not appear within %ss" % frist}

    if args.get("query"):
        while _t.time() < ende:
            try:
                r = t_find_elements({"query": args["query"],
                                     "window_handle": args.get("window_handle"),
                                     "window_title": args.get("in_window"),
                                     "limit": 3})
                if r["count"]:
                    return {"ok": True, "found": "element",
                            "matches": r["matches"],
                            "waited_seconds": round(frist - (ende - _t.time()), 1)}
            except Exception:
                pass
            _t.sleep(takt)
        return {"ok": False, "reason": "Element did not appear within %ss" % frist}

    if args.get("ref"):
        erwartet = args.get("expect")
        feld = args.get("field", "value")
        start = _state(_resolve(args["ref"]))
        while _t.time() < ende:
            try:
                jetzt = _state(_resolve(args["ref"]))
                if erwartet is not None:
                    if str(jetzt.get(feld)) == str(erwartet):
                        return {"ok": True, "state": jetzt,
                                "waited_seconds": round(frist - (ende - _t.time()), 1)}
                elif jetzt != start:
                    return {"ok": True, "state": jetzt,
                            "changed": _wirkung(start, jetzt),
                            "waited_seconds": round(frist - (ende - _t.time()), 1)}
            except Exception:
                pass
            _t.sleep(takt)
        return {"ok": False, "reason": "State did not change within %ss" % frist}

    raise ValueError("Provide ref, window_title or query")


# ------------------------------------------------------ multi-step batch
def t_batch(args):
    """
    Run several steps in ONE call, each returning its own result.
    Stops at the first failure and reports where it stopped.
    Example: [{"tool":"invoke","args":{"ref":"..."}},
              {"tool":"wait_for","args":{"query":"Save"}}]
    """
    schritte = args.get("steps") or []
    if not isinstance(schritte, list) or not schritte:
        raise ValueError("steps is missing or empty")
    erg = []
    for i, s in enumerate(schritte):
        name = s.get("tool")
        t = _BY_NAME.get(name)
        if t is None:
            erg.append({"step": i, "tool": name, "error": "unknown tool"})
            return {"executed": i, "aborted": True, "results": erg}
        if name in ("batch", "capture"):
            erg.append({"step": i, "tool": name,
                        "error": "not allowed inside batch"})
            return {"executed": i, "aborted": True, "results": erg}
        try:
            out = t["_fn"](s.get("args") or {})
            erg.append({"step": i, "tool": name, "result": out})
        except Exception as e:
            erg.append({"step": i, "tool": name,
                        "error": "%s: %s" % (type(e).__name__, e)})
            return {"executed": i, "aborted": True, "results": erg}
    return {"executed": len(schritte), "aborted": False, "results": erg}


# --------------------------------------------------------------- processes
def t_launch_app(args):
    """Start a program and wait until its window exists."""
    import subprocess, time as _t
    befehl = str(args["command"])
    titel = args.get("await_title")
    try:
        subprocess.Popen(befehl, shell=True)
    except Exception as e:
        raise RuntimeError("Failed to start: %s" % e)
    if titel:
        return t_wait_for({"window_title": titel,
                           "timeout": args.get("timeout", 45)})
    _t.sleep(2)
    return {"ok": True, "started": befehl}


def t_close_window(args):
    """
    Close a window through WindowPattern, and only fall back to the keyboard.

    This used to send Alt+F4 unconditionally: it had to focus the window first,
    which yanks the user's foreground away, and it did so outside the input
    guard so nothing announced it. WindowPattern.Close() asks the window to
    close itself, touches no key and no focus, and works on nearly everything
    with a title bar. Alt+F4 stays as the documented fallback and now says so.
    """
    el, h = _window_by(args.get("window_handle"), args.get("window_title"))
    titel = (_safe(lambda: el.Name, "") or "")[:120]
    import time as _t

    wp = _pat(el, "WindowPattern")
    if wp is not None and _safe(lambda: wp.Close(), "fehler") != "fehler":
        _t.sleep(0.6)
        noch_da = any(w["handle"] == h for w in _top_windows())
        return {"ok": not noch_da, "window": titel, "still_open": noch_da,
                "how": "WindowPattern.Close",
                "took_input": False}

    with _eingabe_laeuft():
        _safe(lambda: el.SetActive())
        auto.SendKeys("{Alt}{F4}")
    _t.sleep(1.0)
    noch_da = any(w["handle"] == h for w in _top_windows())
    return {"ok": not noch_da, "window": titel, "still_open": noch_da,
            "how": "Alt+F4",
            "took_input": True,
            "note": ("This window publishes no WindowPattern, so the keyboard "
                     "was used as a fallback and your focus moved. Anything "
                     "you were typing went to this window for that moment.")}


# ---------------------------------------------------------------------------
# Update check.
#
# This is the ONLY tool that touches the network, and it does so only when it
# is called. There is no background ping, no check at startup, no telemetry.
# The monthly cadence is kept LOCALLY: with only_if_due the tool reads a
# timestamp on disk and returns without any network call unless a month has
# passed. So the assistant can safely offer a check at the start of a session -
# the network is reached at most once every thirty days, and only then.
# ---------------------------------------------------------------------------

RELEASE_API = ("https://api.github.com/repos/nathandevelopment/"
               "pc-screen-control/releases/latest")


def _stempel_pfad():
    return os.path.join(INSTALL_DIR, "last_update_check.txt")


def _tage_seit_letztem_check():
    try:
        import time as _t
        with open(_stempel_pfad(), "r", encoding="utf-8") as fh:
            wann = float(fh.read().strip())
        return (_t.time() - wann) / 86400.0
    except Exception:
        return None          # noch nie geprueft


def _stempel_setzen():
    try:
        import time as _t
        os.makedirs(INSTALL_DIR, exist_ok=True)
        with open(_stempel_pfad(), "w", encoding="utf-8") as fh:
            fh.write(str(_t.time()))
    except Exception:
        pass


def _version_tupel(s):
    teile = []
    for stueck in str(s).lstrip("vV").split("."):
        ziffern = "".join(c for c in stueck if c.isdigit())
        teile.append(int(ziffern) if ziffern else 0)
    return tuple(teile)


def t_check_for_update(args):
    """Look for a newer release. The only tool that goes online, on demand."""
    nur_faellig = bool(args.get("only_if_due", False))
    laden = bool(args.get("download", False))
    tage = _tage_seit_letztem_check()

    # Monatsrhythmus: wenn erst kuerzlich geprueft, gar nicht erst ins Netz.
    if nur_faellig and tage is not None and tage < 30:
        return {"ok": True, "checked": False, "due": False,
                "days_since_last_check": round(tage, 1),
                "current": SERVER_VERSION,
                "note": "Checked %.0f days ago - a month has not passed, so "
                        "nothing was fetched. This ran entirely offline."
                        % tage}

    import json as _json
    import ssl
    import urllib.request

    req = urllib.request.Request(
        RELEASE_API, headers={"User-Agent": "pc-screen-control"})
    try:
        with urllib.request.urlopen(
                req, timeout=15, context=ssl.create_default_context()) as r:
            daten = _json.loads(r.read())
    except Exception as e:
        code = getattr(e, "code", None)
        if code == 404:
            _stempel_setzen()
            return {"ok": True, "checked": True, "newer_available": False,
                    "current": SERVER_VERSION, "latest": None,
                    "note": "No public release exists yet. You are on the "
                            "current build."}
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                "note": "Could not reach GitHub. This is the only tool that "
                        "needs the network; everything else works offline."}

    _stempel_setzen()
    neueste = daten.get("tag_name") or ""
    neuer = _version_tupel(neueste) > _version_tupel(SERVER_VERSION)
    anhang = None
    for a in daten.get("assets", []):
        if str(a.get("name", "")).endswith(".mcpb"):
            anhang = a
            break

    erg = {"ok": True, "checked": True, "current": SERVER_VERSION,
           "latest": neueste, "newer_available": neuer,
           "release_notes": (daten.get("body") or "")[:1500],
           "release_url": daten.get("html_url"),
           "download_url": anhang.get("browser_download_url") if anhang else None}

    if not neuer:
        erg["note"] = "You are on the latest version."
        return erg

    if laden and erg["download_url"]:
        ziel_ordner = os.path.join(os.path.expanduser("~"), "Downloads")
        try:
            os.makedirs(ziel_ordner, exist_ok=True)
            ziel = os.path.join(ziel_ordner, os.path.basename(anhang["name"]))
            dl = urllib.request.Request(
                erg["download_url"], headers={"User-Agent": "pc-screen-control"})
            with urllib.request.urlopen(
                    dl, timeout=60,
                    context=ssl.create_default_context()) as r, \
                    open(ziel, "wb") as fh:
                fh.write(r.read())
            erg["downloaded_to"] = ziel
            erg["note"] = (
                "Downloaded %s to your Downloads folder. To update: in Claude, "
                "Settings -> Extensions -> Install extension, pick this file - "
                "it replaces the running version - then restart Claude. Nothing "
                "to uninstall first." % os.path.basename(ziel))
        except Exception as e:
            erg["download_error"] = "%s: %s" % (type(e).__name__, e)
            erg["note"] = ("Version %s is available at the release page, but "
                           "the automatic download failed (%s). Download it "
                           "there by hand." % (neueste, e))
    else:
        erg["note"] = (
            "Version %s is available (you have %s). Ask me to download it, or "
            "get it from the release page and install it via Settings -> "
            "Extensions -> Install extension." % (neueste, SERVER_VERSION))
    return erg


def t_set_guard(args):
    """
    Change how the input guard behaves.

    priority "claude" (default): when the user is typing, Claude shows a
    rubber-band warning, then locks their input, does the work, and hands focus
    back. priority "me": Claude does not take over on its own - it waits and
    shows a small card, and only acts once the user clicks it (or stops for a
    moment). "me" is for gaming or anything that must not be interrupted.
    """
    if "priority" in args:
        p = args["priority"]
        if p not in ("claude", "me"):
            raise RuntimeError("priority must be 'claude' or 'me'")
        GUARD["priority"] = p
    if "enabled" in args:
        GUARD["enabled"] = bool(args["enabled"])
    if "idle_ms" in args:
        GUARD["idle_ms"] = max(300, int(args["idle_ms"]))
    return {"ok": True, "guard": dict(GUARD),
            "note": ("Claude announces and takes over; your input is held and "
                     "your focus restored afterwards."
                     if GUARD["priority"] == "claude" and GUARD["enabled"] else
                     "Your input has priority; Claude waits for your go."
                     if GUARD["enabled"] else
                     "Guard off; coordinate actions run without a pause.")}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}
REF = {"type": "object", "properties": {"ref": S}, "required": ["ref"]}

TOOLS = [
    {"name": "describe_screen", "_fn": t_describe_screen,
     "description": "START HERE for ANY task on this computer - before taking a screenshot, before moving the mouse, before anything else. A screenshot is a picture made for human eyes; this hands you the same screen as data. Returns every visible window with a verdict: 'readable' (real controls you can address by name), 'shallow', or 'canvas-only' (paints its own interface). Cheaper than a screenshot, and it gives you names instead of coordinates - so you press the right thing and can prove it worked. Then work DOWN the ladder and stop at the first rung that works: read_ui_tree / find_elements -> invoke / set_text / set_value / toggle / select / window -> capture -> click / drag / send_keys. Rungs one and two go through the accessibility interface and leave the user free to keep working; the last rung takes their mouse or keyboard away and should be the exception, not the habit.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "capture", "_fn": t_capture,
     "description": "Returns an IMAGE - of the whole screen, one window, or a SINGLE element by ref. Use for canvas-only windows (Adobe, DaVinci, games), to verify a result, or when appearance matters. Element crops are far more precise than a screenshot.",
     "inputSchema": {"type": "object", "properties": {
         "ref": S, "window_handle": I, "window_title": S,
         "max_px": I, "focus": B}}},
    {"name": "list_windows", "_fn": t_list_windows,
     "description": "All visible top-level windows with handle, title and UI framework.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_ui_tree", "_fn": t_read_ui_tree,
     "description": "A window's control tree as data: every button, field, list and menu with name, role, value, state and available actions. Each node carries a 'ref' for invoke/set_text/toggle.",
     "inputSchema": {"type": "object", "properties": {
         "window_handle": I, "window_title": S, "max_depth": I,
         "max_nodes": I, "only_actionable": B}}},
    {"name": "find_elements", "_fn": t_find_elements,
     "description": "COST: passive. Search a window's controls by name or automation id. Cheaper than reading the whole tree. Says which of the two matched: an automation_id is the same in every language, a name is whatever the window is translated into - on a German Windows the save button is called 'Speichern'.",
     "inputSchema": {"type": "object", "properties": {
         "query": S, "window_handle": I, "window_title": S, "role": S, "limit": I},
         "required": ["query"]}},
    {"name": "element_from_point", "_fn": t_element_from_point,
     "description": "Screen coordinate to element - what is actually at x/y.",
     "inputSchema": {"type": "object", "properties": {"x": I, "y": I},
                     "required": ["x", "y"]}},
    {"name": "get_focus", "_fn": t_get_focus,
     "description": "Which element currently holds keyboard focus.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_text", "_fn": t_read_text,
     "description": "Reads the actual text content of an element - documents, editors, web pages, lists.",
     "inputSchema": REF},
    {"name": "get_text", "_fn": t_get_text,
     "description": "Full state of one element: name, value, expanded, selected, focused, enabled, position.",
     "inputSchema": REF},
    {"name": "invoke", "_fn": t_invoke,
     "description": "Operate an element by ref: press a button, activate a menu item, choose an entry. Returns state BEFORE and AFTER - the confirmation is in the response, no screenshot needed to verify.",
     "inputSchema": REF},
    {"name": "set_text", "_fn": t_set_text,
     "description": "Writes text directly into a field and returns before/after.",
     "inputSchema": {"type": "object", "properties": {"ref": S, "text": S},
                     "required": ["ref", "text"]}},
    {"name": "toggle", "_fn": t_toggle,
     "description": "Flips a checkbox or toggle button, with before/after.",
     "inputSchema": REF},
    {"name": "expand", "_fn": t_expand,
     "description": "Expands or collapses a tree node, combo box or dropdown, with before/after.",
     "inputSchema": {"type": "object", "properties": {"ref": S, "collapse": B},
                     "required": ["ref"]}},
    {"name": "select", "_fn": t_select,
     "description": "Selects a list item, tab or radio button, with before/after.",
     "inputSchema": REF},
    {"name": "read_table", "_fn": t_read_table,
     "description": "COST: passive. Reads a table, grid or details list as rows and columns, with headers - Excel, data grids, Explorer in details view. Far cheaper than walking the tree cell by cell, and it keeps which cell is in which row. Pass a ref whose actions include 'read_table', or just a window and it will find the grid. Paged via start_row / max_rows.",
     "inputSchema": {"type": "object", "properties": {
         "ref": S, "window_handle": I, "window_title": S,
         "start_row": I, "max_rows": I}}},
    {"name": "set_value", "_fn": t_set_value,
     "description": "COST: passive. Sets a numeric control to an exact value - sliders, spinners, scroll position. Use this for anything whose actions include 'set_value' INSTEAD of dragging it: dragging lands where the pixels land, this lands on the number, and it does not move the user's cursor. Either 'value' (absolute) or 'percent' (0-100 of the control's own range). Reports whether the control snapped to a step.",
     "inputSchema": {"type": "object", "properties": {
         "ref": S, "value": {"type": "number"}, "percent": {"type": "number"},
         "axis": {"type": "string", "enum": ["vertical", "horizontal"]}},
         "required": ["ref"]}},
    {"name": "window", "_fn": t_window,
     "description": "COST: passive. Moves, resizes, minimises, maximises or restores a window without the mouse. Pass state ('normal'/'maximized'/'minimized') and/or x, y, width, height. Use this to arrange the screen before working - e.g. put a window somewhere it is fully visible before capture().",
     "inputSchema": {"type": "object", "properties": {
         "window_handle": I,
         "state": {"type": "string", "enum": ["normal", "maximized", "minimized"]},
         "x": I, "y": I, "width": I, "height": I},
         "required": ["window_handle"]}},
    {"name": "clipboard", "_fn": t_clipboard,
     "description": "COST: passive, but it overwrites what the user had copied. Reads or writes the clipboard. For long text this beats send_keys by far: one operation instead of hundreds of keystrokes, and nothing can garble it. Writing returns the previous content in 'replaced' - put it back afterwards.",
     "inputSchema": {"type": "object", "properties": {
         "mode": {"type": "string", "enum": ["read", "write"]}, "text": S}}},
    {"name": "menu", "_fn": t_menu,
     "description": "COST: brief mouse use, cursor is put back. Opens a context or application menu and returns its items with refs - menus do not exist in the tree until opened, so this is the only way to see them. Then invoke(ref) to pick one, or menu({action:'close'}) to dismiss.",
     "inputSchema": {"type": "object", "properties": {
         "ref": S, "x": I, "y": I, "context": B, "timeout": I,
         "action": {"type": "string", "enum": ["open", "close"]}}}},
    {"name": "click", "_fn": t_click,
     "description": "COST: TAKES THE USER'S MOUSE. Click a screen coordinate - left, right or middle, single or double. LAST RESORT, for canvas-only windows (Adobe, DaVinci, games) where no addressable control exists. If read_ui_tree returns a ref for the thing you want, use invoke() instead. The cursor is returned to where the user left it.",
     "inputSchema": {"type": "object", "properties": {
         "x": I, "y": I, "button": {"type": "string", "enum": ["left", "right", "middle"]},
         "count": I, "restore_cursor": B}, "required": ["x", "y"]}},
    {"name": "drag", "_fn": t_drag,
     "description": "COST: TAKES THE USER'S MOUSE. Free drag from x1,y1 to x2,y2 - timelines, reordering, selection rectangles. With ref plus dx/dy it drags from the element centre, but for a slider use set_value instead; this refuses unless force=true. The cursor is returned afterwards.",
     "inputSchema": {"type": "object", "properties": {
         "ref": S, "dx": I, "dy": I, "x1": I, "y1": I, "x2": I, "y2": I,
         "force": B, "restore_cursor": B}}},
    {"name": "scroll", "_fn": t_scroll,
     "description": "COST: passive when the element has a scroll pattern (it then jumps by percent and the cursor is untouched), otherwise takes the mouse wheel. Prefer passing ref over coordinates for exactly that reason. For long lists, panels, web pages.",
     "inputSchema": {"type": "object", "properties": {
         "ref": S, "x": I, "y": I,
         "direction": {"type": "string", "enum": ["up", "down"]}, "amount": I,
         "force_wheel": B, "restore_cursor": B}}},
    {"name": "wait_for", "_fn": t_wait_for,
     "description": "Waits until something happens instead of sleeping blindly: until a window appears (window_title), an element exists (query), or a state changes (ref, optionally expect+field). Makes sequences reliable - after any click that loads something, wait here instead of guessing.",
     "inputSchema": {"type": "object", "properties": {
         "ref": S, "expect": S, "field": S, "window_title": S,
         "query": S, "in_window": S, "window_handle": I, "timeout": I}}},
    {"name": "batch", "_fn": t_batch,
     "description": "Runs several steps in ONE call, each with its own result and effect verification. Stops at the first failure and reports where. Example: [{'tool':'invoke','args':{...}},{'tool':'wait_for','args':{'query':'Done'}}]. Saves round trips on predictable sequences.",
     "inputSchema": {"type": "object", "properties": {
         "steps": {"type": "array", "items": {"type": "object"}}},
         "required": ["steps"]}},
    {"name": "hold_key", "_fn": t_hold_key,
     "description": "COST: TAKES THE USER'S KEYBOARD. Holds a key down, e.g. Shift for multi-select. Always released, even if something fails in between.",
     "inputSchema": {"type": "object", "properties": {"key": S, "seconds": I},
                     "required": ["key"]}},
    {"name": "wait", "_fn": t_wait,
     "description": "Waits a fixed time. Only when wait_for does not apply.",
     "inputSchema": {"type": "object", "properties": {"seconds": I}}},
    {"name": "launch_app", "_fn": t_launch_app,
     "description": "Starts a program and optionally waits until its window appears (await_title).",
     "inputSchema": {"type": "object", "properties": {
         "command": S, "await_title": S, "timeout": I}, "required": ["command"]}},
    {"name": "close_window", "_fn": t_close_window,
     "description": "Closes a window and verifies it actually closed.",
     "inputSchema": {"type": "object", "properties": {
         "window_handle": I, "window_title": S}}},
    {"name": "focus_window", "_fn": t_focus_window,
     "description": "Brings a window to the foreground.",
     "inputSchema": {"type": "object", "properties": {"window_handle": I,
                                                      "window_title": S}}},
    {"name": "send_keys", "_fn": t_send_keys,
     "description": "COST: TAKES THE USER'S KEYBOARD, and goes wherever focus happens to be. Right for shortcuts ({Ctrl}s, {Alt}{F4}, {Esc}) and for canvas apps. WRONG for filling a field - use set_text, or clipboard plus {Ctrl}v for long text. Refuses on an element that accepts set_text unless force=true.",
     "inputSchema": {"type": "object", "properties": {"keys": S, "ref": S,
                                                      "force": B},
                     "required": ["keys"]}},
    {"name": "set_guard", "_fn": t_set_guard,
     "description": "Sets who has priority while Claude uses the mouse or keyboard. 'claude' (default): when the user is active, a rubber-band pulse warns them, their input is briefly held, and their window and text cursor are put back afterwards. 'me': Claude never takes over on its own - it waits and shows a small card, acting only when the user clicks it. Use 'me' when the user is gaming or doing something that must not be interrupted. Also toggles the whole guard on/off.",
     "inputSchema": {"type": "object", "properties": {
         "priority": {"type": "string", "enum": ["claude", "me"]},
         "enabled": B, "idle_ms": I}}},
    {"name": "check_for_update", "_fn": t_check_for_update,
     "description": "Checks GitHub for a newer version of this extension. THE ONLY TOOL THAT USES THE NETWORK, and only when called - there is no background check. At the start of a session you MAY call it once with only_if_due:true; that reads a local timestamp and returns instantly without any network unless a month has passed since the last real check, so it is safe and quiet. If newer_available is true, tell the user the version and offer to fetch it; call again with download:true to save the .mcpb to their Downloads folder. They then install it via Settings -> Extensions -> Install extension, which replaces the running version - no uninstall needed - and restart Claude. Do not nag: mention an update at most once per session.",
     "inputSchema": {"type": "object", "properties": {
         "only_if_due": B, "download": B}}},
]

_BY_NAME = {t["name"]: t for t in TOOLS}


def _send(m):
    _PROTO_OUT.write(json.dumps(m, ensure_ascii=True) + "\n")
    _PROTO_OUT.flush()


def _handle(msg):
    method = msg.get("method")
    rid = msg.get("id")
    p = msg.get("params") or {}
    if method == "initialize":
        _send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION}}})
        return
    if method in ("notifications/initialized", "initialized"):
        return
    if method == "ping":
        _send({"jsonrpc": "2.0", "id": rid, "result": {}})
        return
    if method == "tools/list":
        _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": t["name"], "description": t["description"],
             "inputSchema": t["inputSchema"]} for t in TOOLS]}})
        return
    if method == "tools/call":
        t = _BY_NAME.get(p.get("name"))
        if t is None:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": "Unknown tool"}})
            return
        try:
            out = t["_fn"](p.get("arguments") or {})
            if isinstance(out, dict) and "_content" in out:
                content = out["_content"]
            else:
                content = [{"type": "text",
                            "text": json.dumps(out, ensure_ascii=True, indent=2)}]
            _send({"jsonrpc": "2.0", "id": rid, "result": {"content": content}})
        except Exception as e:
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text",
                             "text": "%s: %s" % (type(e).__name__, e)}],
                "isError": True}})
        # Refresh the takeover baseline after every call, successful or not.
        # Anything this server just did to the foreground belongs in the
        # baseline; only a change that appears *between* calls came from
        # somewhere else, and that is exactly what _lage_pruefen looks for.
        _safe(_lage_merken)
        return
    if rid is not None:
        _send({"jsonrpc": "2.0", "id": rid,
               "error": {"code": -32601, "message": "Method not found"}})


# ---------------------------------------------------------------------------
# Setup mode: python server.py --install
#
# Why this lives here instead of in a separate installer script: the installer
# needs to know the interpreter that will actually run the server. Running the
# installation from inside the server file makes sys.executable the single
# source of truth, so the config can never point at a Python that lacks the
# dependencies - the most common failure mode of hand-written MCP configs.
# ---------------------------------------------------------------------------

def _install_dir():
    """Per-platform application data directory, taken from the environment."""
    if sys.platform == "win32":
        basis = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        basis = os.path.join(os.path.expanduser("~"), "Library",
                             "Application Support")
    else:
        basis = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
    return os.path.join(basis, "pc-screen-control")


INSTALL_DIR = _install_dir()


def _config_candidates():
    """
    Where each client keeps its config, derived from the environment of the
    machine this runs on. Never guessed, never hard-coded: a missing variable
    means that client is skipped rather than written to a relative path.

    The macOS paths are already here although the tools are Windows-only,
    because this is exactly the part where a port would otherwise guess - and
    a wrong guess here silently rewrites someone else's config file.
    """
    out = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            out.append(("Claude Desktop", os.path.join(
                appdata, "Claude", "claude_desktop_config.json")))
    elif sys.platform == "darwin":
        out.append(("Claude Desktop", os.path.join(
            os.path.expanduser("~"), "Library", "Application Support",
            "Claude", "claude_desktop_config.json")))
    home = os.path.expanduser("~")
    if home and home != "~":
        out.append(("Claude Code", os.path.join(home, ".claude.json")))
    return tuple(out)


CONFIG_CANDIDATES = _config_candidates()


def _log_path():
    """In the unpacked download next to the scripts, so people find it. Falls
    back to the install directory when run from there, so it never litters a
    directory it does not own."""
    here = os.path.dirname(os.path.abspath(__file__))
    parent = os.path.dirname(here)
    if os.path.basename(here).lower() == "src" and os.path.isdir(parent):
        return os.path.join(parent, "install_log.txt")
    return os.path.join(here, "install_log.txt")


_LOG_PATH = _log_path()
_LOG_LINES = []


def _say(msg=""):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()
    _LOG_LINES.append(msg)


def _write_log():
    """A console window can be closed before it is read. The log is what
    people attach to a bug report."""
    try:
        with open(_LOG_PATH, "w", encoding="utf-8") as fh:
            fh.write("\n".join(_LOG_LINES) + "\n")
    except Exception:
        pass


MITZUKOPIEREN = ("overlay.py",)


def _install_copy_self():
    """
    Copy the server and everything it loads at runtime to a stable location,
    so the config keeps working after the downloaded folder is moved or
    deleted - which is the first thing most people do.
    """
    import shutil
    quelle = os.path.dirname(os.path.abspath(__file__))
    src = os.path.abspath(__file__)
    dst = os.path.join(INSTALL_DIR, os.path.basename(src))
    if os.path.normcase(src) == os.path.normcase(dst):
        return dst
    os.makedirs(INSTALL_DIR, exist_ok=True)
    shutil.copy2(src, dst)
    for name in MITZUKOPIEREN:
        p = os.path.join(quelle, name)
        if os.path.isfile(p):
            shutil.copy2(p, os.path.join(INSTALL_DIR, name))
    return dst


def _install_write_config(label, path, server_path):
    """Merge one entry into an MCP client config without touching anything
    else in it. Always writes a backup first."""
    import shutil
    parent = os.path.dirname(path)
    if not os.path.isdir(parent):
        return "skipped", "%s is not installed" % label

    data = {}
    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                text = fh.read().strip()
            data = json.loads(text) if text else {}
        except Exception as e:
            return "failed", "existing config is not valid JSON (%s) - not touched" % e
        if not isinstance(data, dict):
            return "failed", "existing config is not a JSON object - not touched"
        # Only ever write the pristine backup, never overwrite it. Running the
        # installer a second time must not destroy the one copy that still
        # represents the state before this software existed on the machine.
        if not os.path.isfile(path + ".backup"):
            shutil.copy2(path, path + ".backup")

    servers = data.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        return "failed", "'mcpServers' is not an object - not touched"

    existed = SERVER_NAME in servers
    entry = {"command": sys.executable, "args": [server_path]}
    servers[SERVER_NAME] = entry

    # Write to a temporary file and replace, so an interrupted write can never
    # leave the user with a truncated config. These files can be large and are
    # owned by another program.
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        if isinstance(e, PermissionError):
            return "failed", ("no write permission - close %s completely "
                              "(check the system tray) and run this again"
                              % label)
        return "failed", str(e)

    # Read it back. Claiming success without checking is how installers lie.
    try:
        with open(path, "r", encoding="utf-8") as fh:
            check = json.load(fh)
        if check.get("mcpServers", {}).get(SERVER_NAME) != entry:
            return "failed", "written, but the entry did not read back"
    except Exception as e:
        return "failed", "written, but could not be re-read (%s)" % e

    return ("updated" if existed else "added"), path


def _install_selftest():
    try:
        import uiautomation as _a
        n = len(_a.GetRootControl().GetChildren())
        return True, "%d top-level windows visible" % n
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)


def install():
    try:
        return _install()
    finally:
        _write_log()


def _install():
    import datetime
    _say()
    _say("  PC Screen Control %s - setup   %s"
         % (SERVER_VERSION, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    _say("  " + "-" * 52)
    _say()

    if os.name != "nt":
        _say("  [x] Windows only. This does nothing on %s." % sys.platform)
        return 1

    _say("  [1/4] Python %s" % sys.version.split()[0])
    _say("        %s" % sys.executable)

    _say("  [2/4] Dependencies ...")
    _ensure_dependencies()
    ok, detail = _install_selftest()
    if ok:
        _say("        ok - %s" % detail)
    else:
        _say("        [x] uiautomation could not be loaded:")
        _say("            %s" % detail)
        _say("        Try manually:  \"%s\" -m pip install uiautomation pillow"
             % sys.executable)
        return 1

    _say("  [3/4] Installing to %s" % INSTALL_DIR)
    try:
        server_path = _install_copy_self()
    except Exception as e:
        _say("        [x] copy failed: %s" % e)
        return 1
    _say("        ok")

    _say("  [4/4] Registering with MCP clients ...")
    any_ok = False
    for label, path in CONFIG_CANDIDATES:
        state, detail = _install_write_config(label, path, server_path)
        if state in ("added", "updated"):
            any_ok = True
            _say("        %-16s %s   %s" % (label, state, detail))
        elif state == "skipped":
            _say("        %-16s skipped (%s)" % (label, detail))
        else:
            _say("        %-16s FAILED - %s" % (label, detail))

    _say()
    if any_ok:
        _say("  Done. Restart Claude, then ask it to run describe_screen.")
    else:
        _say("  No MCP client found. Add this to your client config yourself:")
        _say()
        _say(json.dumps({"mcpServers": {SERVER_NAME: {
            "command": sys.executable, "args": [server_path]}}}, indent=2))
    _say()
    return 0


def _deinstall_config(label, path):
    """Take our entry out again, leaving everything else exactly as it was."""
    import shutil
    if not os.path.isfile(path):
        return "skipped", "no config"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        return "failed", "config is not valid JSON (%s) - not touched" % e
    server = data.get("mcpServers")
    if not isinstance(server, dict) or SERVER_NAME not in server:
        return "skipped", "no entry"

    shutil.copy2(path, path + ".before-uninstall")
    del server[SERVER_NAME]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, path)
        with open(path, "r", encoding="utf-8") as fh:
            if SERVER_NAME in json.load(fh).get("mcpServers", {}):
                return "failed", "entry is still there after writing"
    except Exception as e:
        try:
            os.remove(tmp)
        except Exception:
            pass
        if isinstance(e, PermissionError):
            return "failed", "close %s completely and try again" % label
        return "failed", str(e)
    return "removed", "%d other entries untouched" % len(server)


def uninstall():
    try:
        return _uninstall()
    finally:
        _write_log()


def _uninstall():
    import datetime
    import shutil
    _say()
    _say("  PC Screen Control %s - remove   %s"
         % (SERVER_VERSION, datetime.datetime.now().strftime("%Y-%m-%d %H:%M")))
    _say("  " + "-" * 52)
    _say()

    _say("  [1/2] Removing the entry from every MCP client ...")
    for label, path in CONFIG_CANDIDATES:
        zustand, detail = _deinstall_config(label, path)
        _say("        %-16s %-8s %s" % (label, zustand, detail))

    _say("  [2/2] Deleting %s" % INSTALL_DIR)
    if os.path.isdir(INSTALL_DIR):
        try:
            shutil.rmtree(INSTALL_DIR)
            _say("        done")
        except Exception as e:
            _say("        [x] %s" % e)
            _say("        Claude may still be running it. Close Claude and "
                 "run this again.")
            return 1
    else:
        _say("        was not there")

    _say()
    _say("  Removed. Restart Claude and the tools are gone.")
    _say()
    _say("  Nothing else was touched: no registry keys, no system settings,")
    _say("  no files outside your user profile. The Python packages")
    _say("  (uiautomation, pillow) are left installed - remove them with")
    _say("  'pip uninstall uiautomation pillow' if you want them gone.")
    _say()
    _say("  A copy of each config from before this ran is next to it as")
    _say("  <name>.before-uninstall")
    _say()
    return 0


def main():
    if "--uninstall" in sys.argv:
        try:
            rc = uninstall()
        except Exception:
            _say()
            _say(traceback.format_exc())
            _write_log()
            rc = 1
        sys.exit(rc)
    if "--install" in sys.argv:
        try:
            rc = install()
        except Exception:
            _say()
            _say(traceback.format_exc())
            _write_log()
            rc = 1
        sys.exit(rc)
    sys.stderr.write("[%s %s] ready\n" % (SERVER_NAME, SERVER_VERSION))
    sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        try:
            _handle(msg)
        except Exception:
            sys.stderr.write(traceback.format_exc())
            sys.stderr.flush()


if __name__ == "__main__":
    main()
