# -*- coding: utf-8 -*-
"""
The edge overlay and the input guard.

Two jobs in one small always-on process:

  1. The edge glow that shows when Claude is taking the physical mouse or
     keyboard - and, on request, the rubber-band pulse that announces it is
     about to.
  2. The input guard itself: low-level hooks that swallow the user's keystrokes
     and clicks while Claude works, let Claude's own (injected) input through,
     and always let Escape through as an abort.

Why a separate process: hooks and a layered window both need a running message
loop, and that loop must not share a thread with the protocol. If it dies, the
server carries on and Windows tears the hooks down automatically - the user can
never be locked out by a crash.

Why four edge bars instead of one full-screen window: a full-screen layered
window is ~36 MB per frame and cannot be animated. Four thin bars are ~0.6 ms
per frame (measured), so the pulse is smooth.

Protocol, one word per line.
  in  (stdin):  warn | lock | release | wait_on | wait_off | off | quit
  out (stdout): abort   (user pressed Escape)
                go      (user clicked the wait card)
"""
import ctypes
import ctypes.wintypes as w
import sys
import threading
import time

COLOUR = (34, 211, 238)       # cyan
THICKNESS = 46                # resting inward reach, px
PEAK_ALPHA = 165
INHALE_MS = 900               # slow build inward
EXHALE_MS = 180               # fast snap back - the "now" instant
RELEASE_MS = 420              # gentle fade at the end
MAX_DEPTH = 260               # how far inward the inhale stretches
WATCHDOG_MS = 10000           # hard auto-unlock

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WPARAM, LPARAM = ctypes.c_size_t, ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, ctypes.c_uint, WPARAM, LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, WPARAM, LPARAM)

WS_POPUP = 0x80000000
WS_EX = (0x00080000 | 0x00000020 | 0x00000080 | 0x08000000 | 0x00000008)
ULW_ALPHA = 0x02
SW_HIDE, SW_SHOWNA = 0, 8
WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01
VK_ESCAPE = 0x1B
WM_TIMER, WM_QUIT = 0x0113, 0x0012
WM_KEYDOWN, WM_SYSKEYDOWN = 0x0100, 0x0104
WM_LBUTTONDOWN = 0x0201


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", w.DWORD), ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long), ("biPlanes", w.WORD),
                ("biBitCount", w.WORD), ("biCompression", w.DWORD),
                ("biSizeImage", w.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", w.DWORD),
                ("biClrImportant", w.DWORD)]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
                ("SourceConstantAlpha", ctypes.c_ubyte),
                ("AlphaFormat", ctypes.c_ubyte)]


class WNDCLASS(ctypes.Structure):
    _fields_ = [("style", ctypes.c_uint), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", w.DWORD), ("scanCode", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", w.POINT), ("mouseData", w.DWORD), ("flags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


def _declare():
    user32.RegisterClassW.restype = w.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASS)]
    user32.CreateWindowExW.restype = w.HWND
    user32.CreateWindowExW.argtypes = [
        w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, w.LPVOID]
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [w.HWND, ctypes.c_uint, WPARAM, LPARAM]
    user32.GetDC.restype = w.HDC
    user32.GetDC.argtypes = [w.HWND]
    user32.ReleaseDC.argtypes = [w.HWND, w.HDC]
    user32.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
    user32.MoveWindow.argtypes = [w.HWND, ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int, w.BOOL]
    user32.GetSystemMetrics.restype = ctypes.c_int
    user32.UpdateLayeredWindow.restype = w.BOOL
    user32.UpdateLayeredWindow.argtypes = [
        w.HWND, w.HDC, ctypes.POINTER(w.POINT), ctypes.POINTER(w.SIZE),
        w.HDC, ctypes.POINTER(w.POINT), w.COLORREF,
        ctypes.POINTER(BLENDFUNCTION), w.DWORD]
    user32.SetWindowsHookExW.restype = w.HHOOK
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, w.HINSTANCE,
                                         w.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [w.HHOOK, ctypes.c_int, WPARAM, LPARAM]
    user32.UnhookWindowsHookEx.argtypes = [w.HHOOK]
    user32.UnhookWindowsHookEx.restype = w.BOOL
    user32.SetTimer.restype = ctypes.c_void_p
    user32.SetTimer.argtypes = [w.HWND, ctypes.c_void_p, w.UINT, ctypes.c_void_p]
    user32.KillTimer.argtypes = [w.HWND, ctypes.c_void_p]
    user32.GetCursorPos.argtypes = [ctypes.POINTER(w.POINT)]
    gdi32.CreateCompatibleDC.restype = w.HDC
    gdi32.CreateCompatibleDC.argtypes = [w.HDC]
    gdi32.CreateDIBSection.restype = w.HBITMAP
    gdi32.CreateDIBSection.argtypes = [
        w.HDC, ctypes.POINTER(BITMAPINFOHEADER), w.UINT,
        ctypes.POINTER(ctypes.c_void_p), w.HANDLE, w.DWORD]
    gdi32.SelectObject.restype = w.HGDIOBJ
    gdi32.SelectObject.argtypes = [w.HDC, w.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [w.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [w.HDC]
    kernel32.GetModuleHandleW.restype = w.HMODULE
    kernel32.GetModuleHandleW.argtypes = [w.LPCWSTR]
    kernel32.GetCurrentThreadId.restype = w.DWORD
    user32.PostThreadMessageW.argtypes = [w.DWORD, ctypes.c_uint, WPARAM, LPARAM]


def _dpi_bewusst():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


def virtueller_bildschirm():
    g = user32.GetSystemMetrics
    x, y, cx, cy = g(76), g(77), g(78), g(79)
    if cx <= 0 or cy <= 0:
        x, y, cx, cy = 0, 0, g(0), g(1)
    return int(x), int(y), int(cx), int(cy)


def _bar_pixels(breite, hoehe, seite, tiefe, staerke):
    """
    Premultiplied BGRA for one edge bar, bottom-up.

    'seite' is which edge (top/bottom/left/right). The glow is brightest at the
    screen edge and fades to nothing at depth 'tiefe'. 'staerke' scales the
    whole thing 0..1 for the fade in and out.
    """
    r, g, b = COLOUR
    tiefe = max(1, int(tiefe))

    def farbe(d):
        t = 1.0 - (d / float(tiefe))
        a = int(PEAK_ALPHA * t * t * staerke)
        return bytes((b * a // 255, g * a // 255, r * a // 255, a))

    if seite in ("top", "bottom"):
        # each row is one colour across the full width; distance = row from edge
        reihen = []
        for y in range(hoehe):
            d = y if seite == "bottom" else (hoehe - 1 - y)
            reihen.append(farbe(d) * breite if d < tiefe
                          else b"\x00\x00\x00\x00" * breite)
        return b"".join(reihen)      # already bottom-up for our purpose
    else:
        # each row identical; within the row, distance = column from edge
        zeile = bytearray(b"\x00\x00\x00\x00" * breite)
        for x in range(breite):
            d = x if seite == "left" else (breite - 1 - x)
            if d < tiefe:
                zeile[x * 4:(x + 1) * 4] = farbe(d)
        return bytes(zeile) * hoehe


class Bar(object):
    """One edge strip as its own click-through layered window."""

    def __init__(self, seite):
        self.seite = seite
        self.hwnd = None
        self.rect = (0, 0, 0, 0)

    def erzeugen(self, hinst, klasse, proc):
        self.hwnd = user32.CreateWindowExW(
            WS_EX, klasse, "psc-%s" % self.seite, WS_POPUP,
            0, 0, 10, 10, None, None, hinst, None)

    def platzieren_und_zeichnen(self, tiefe, staerke):
        vx, vy, vw, vh = virtueller_bildschirm()
        dick = max(1, int(min(tiefe, MAX_DEPTH)))
        if self.seite == "top":
            x, y, cx, cy = vx, vy, vw, dick
        elif self.seite == "bottom":
            x, y, cx, cy = vx, vy + vh - dick, vw, dick
        elif self.seite == "left":
            x, y, cx, cy = vx, vy, dick, vh
        else:
            x, y, cx, cy = vx + vw - dick, vy, dick, vh
        self.rect = (x, y, cx, cy)
        pixel = _bar_pixels(cx, cy, self.seite, tiefe, staerke)

        kopf = BITMAPINFOHEADER()
        kopf.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        kopf.biWidth, kopf.biHeight = cx, cy
        kopf.biPlanes, kopf.biBitCount, kopf.biCompression = 1, 32, 0

        sdc = user32.GetDC(None)
        mdc = gdi32.CreateCompatibleDC(sdc)
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(mdc, ctypes.byref(kopf), 0,
                                     ctypes.byref(bits), None, 0)
        if bmp:
            ctypes.memmove(bits, pixel, len(pixel))
            alt = gdi32.SelectObject(mdc, bmp)
            blend = BLENDFUNCTION(0, 0, 255, 1)
            user32.UpdateLayeredWindow(
                self.hwnd, sdc, ctypes.byref(w.POINT(x, y)),
                ctypes.byref(w.SIZE(cx, cy)), mdc,
                ctypes.byref(w.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(mdc, alt)
            gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, sdc)

    def zeigen(self, an):
        if self.hwnd:
            user32.ShowWindow(self.hwnd, SW_SHOWNA if an else SW_HIDE)


class Guard(object):
    """The whole overlay: bars, animation, hooks, wait card."""

    def __init__(self):
        self.bars = [Bar(s) for s in ("top", "bottom", "left", "right")]
        self.zustand = "off"          # off warn hold release wait
        self.start = 0.0
        self.lock_seit = 0.0
        self.k_hook = None
        self.m_hook = None
        self._kp = HOOKPROC(self._tasten)
        self._mp = HOOKPROC(self._maus)
        self._wndproc = WNDPROC(lambda h, m, wp, lp:
                                user32.DefWindowProcW(h, m, wp, lp))
        self.timer_hwnd = None
        self.thread_id = 0

    # ---- hooks -----------------------------------------------------------
    def _gesperrt(self):
        return self.zustand == "hold"

    def _tasten(self, code, wparam, lparam):
        if code >= 0:
            d = ctypes.cast(lparam,
                            ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            eigen = bool(d.flags & LLKHF_INJECTED)
            if not eigen and d.vkCode == VK_ESCAPE and \
                    wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                _sende("abort")                    # Escape always aborts
                return user32.CallNextHookEx(None, code, wparam, lparam)
            if self._gesperrt() and not eigen:
                return 1                            # swallow real keystroke
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _maus(self, code, wparam, lparam):
        if code >= 0:
            d = ctypes.cast(lparam,
                            ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            eigen = bool(d.flags & LLMHF_INJECTED)
            if self.zustand == "wait" and not eigen and \
                    wparam == WM_LBUTTONDOWN and self._auf_karte(d.pt.x, d.pt.y):
                _sende("go")
                return 1
            if self._gesperrt() and not eigen:
                return 1                            # swallow real click/move
        return user32.CallNextHookEx(None, code, wparam, lparam)

    def _haken_an(self):
        if self.k_hook:
            return
        hmod = kernel32.GetModuleHandleW(None)
        self.k_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._kp, hmod, 0)
        self.m_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, self._mp, hmod, 0)

    def _haken_aus(self):
        if self.k_hook:
            user32.UnhookWindowsHookEx(self.k_hook)
            self.k_hook = None
        if self.m_hook:
            user32.UnhookWindowsHookEx(self.m_hook)
            self.m_hook = None

    # ---- wait card (drawn on the bottom bar area) ------------------------
    def _karte_rect(self):
        vx, vy, vw, vh = virtueller_bildschirm()
        b, h = 300, 68
        return (vx + vw - b - 40, vy + vh - h - 40, b, h)

    def _auf_karte(self, x, y):
        cx, cy, cw, ch = self._karte_rect()
        return cx <= x <= cx + cw and cy <= y <= cy + ch

    # ---- animation -------------------------------------------------------
    def _alle_zeigen(self, an):
        for b in self.bars:
            b.zeigen(an)

    def _zeichne(self, tiefe, staerke):
        for b in self.bars:
            b.platzieren_und_zeichnen(tiefe, staerke)

    def tick(self):
        jetzt = time.time()
        t = (jetzt - self.start) * 1000.0

        if self.zustand == "warn":
            if t < INHALE_MS:
                # deep, slow inhale: depth grows, easing out
                f = t / INHALE_MS
                f = 1 - (1 - f) * (1 - f)
                self._zeichne(THICKNESS + (MAX_DEPTH - THICKNESS) * f,
                              0.35 + 0.35 * f)
            elif t < INHALE_MS + EXHALE_MS:
                # fast exhale: snap back to a thin bright edge
                f = (t - INHALE_MS) / EXHALE_MS
                self._zeichne(MAX_DEPTH - (MAX_DEPTH - THICKNESS) * f,
                              0.7 + 0.3 * f)
            else:
                self._zustand("hold")

        elif self.zustand == "release":
            f = t / RELEASE_MS
            if f >= 1.0:
                self._alle_zeigen(False)
                self._zustand("off")
            else:
                self._zeichne(THICKNESS, 1.0 - f)

        elif self.zustand == "hold":
            # steady; watchdog only
            if (jetzt - self.lock_seit) * 1000.0 > WATCHDOG_MS:
                _sende("abort")
                self.release()

    def _zustand(self, neu):
        self.zustand = neu
        if neu == "hold":
            self.lock_seit = time.time()
            self._haken_an()
            self._zeichne(THICKNESS, 1.0)
        elif neu == "off":
            self._haken_aus()

    # ---- commands from the server ---------------------------------------
    def warn(self):
        self._alle_zeigen(True)
        self.start = time.time()
        self._zustand("warn")

    def lock(self):
        """No announcement - user is idle. Straight to hold."""
        self._alle_zeigen(True)
        self._zustand("hold")

    def wait_on(self):
        self._alle_zeigen(True)
        self.zustand = "wait"
        self._zeichne(THICKNESS, 0.5)
        # card is part of the bottom bar's redraw region; kept simple: the
        # glow signals waiting, the click area is the card rectangle.
        self._haken_an()          # need the mouse hook to catch the GO click

    def wait_off(self):
        self._haken_aus()
        self.zustand = "off"
        self._alle_zeigen(False)

    def release(self):
        if self.zustand in ("hold", "wait", "warn"):
            self._haken_aus()
            self.start = time.time()
            self.zustand = "release"

    def off(self):
        self._haken_aus()
        self.zustand = "off"
        self._alle_zeigen(False)


_STDOUT_LOCK = threading.Lock()


def _sende(wort):
    with _STDOUT_LOCK:
        try:
            sys.stdout.write(wort + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def main():
    _declare()
    _dpi_bewusst()
    hinst = kernel32.GetModuleHandleW(None)
    klasse = "PcScreenControlEdge"
    guard = Guard()

    wc = WNDCLASS()
    wc.lpfnWndProc = guard._wndproc
    wc.hInstance = hinst
    wc.lpszClassName = klasse
    if not user32.RegisterClassW(ctypes.byref(wc)):
        fehler = ctypes.get_last_error()
        if fehler not in (0, 1410):
            raise ctypes.WinError(fehler)
    for b in guard.bars:
        b.erzeugen(hinst, klasse, guard._wndproc)

    guard.thread_id = kernel32.GetCurrentThreadId()
    # a hidden helper window would be cleaner, but a thread timer is enough:
    user32.SetTimer(None, None, 16, None)     # ~60 Hz WM_TIMER
    sys.stderr.write("[overlay] ready %s\n" % (virtueller_bildschirm(),))
    sys.stderr.flush()

    def lesen():
        try:
            for zeile in sys.stdin:
                b = zeile.strip().lower()
                if b == "warn":
                    guard.warn()
                elif b == "lock":
                    guard.lock()
                elif b == "release":
                    guard.release()
                elif b == "wait_on":
                    guard.wait_on()
                elif b == "wait_off":
                    guard.wait_off()
                elif b == "off":
                    guard.off()
                elif b in ("quit", "exit"):
                    break
        except Exception:
            pass
        user32.PostThreadMessageW(guard.thread_id, WM_QUIT, 0, 0)

    threading.Thread(target=lesen, daemon=True).start()

    msg = w.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_TIMER:
            guard.tick()
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    guard.off()


if __name__ == "__main__":
    main()
