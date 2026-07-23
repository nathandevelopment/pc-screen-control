# -*- coding: utf-8 -*-
"""
Regression test for the encoding bug that ate every umlaut.

MCP speaks UTF-8 in both directions. A pipe on Windows does not: it defaults to
the machine's ANSI code page, measured as cp1252 on a German install. The server
pinned its *output* to UTF-8 and left *input* alone, so replies looked perfectly
correct while the arguments had already been destroyed on the way in. "Grusse"
with an umlaut arrived as mojibake, and nothing anywhere reported a problem.

Two details make or break this test, and both were got wrong first time round.

The request must be written with ensure_ascii=False. Python's json.dumps escapes
non-ASCII to \\uXXXX by default, which makes the request pure ASCII and
therefore immune to any code page - the test then passes whether the bug is
present or not. Real MCP clients are not Python: JavaScript's JSON.stringify
emits raw UTF-8, which is what exposed the bug in the first place.

And stdin is forced to cp1252 through PYTHONIOENCODING, so the test reproduces
a German Windows on any machine, including an English CI runner.

Both halves were verified by deleting the fix in a scratch copy and confirming
that this test then fails. A test that cannot fail proves nothing.
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(os.path.dirname(HERE), "src", "server.py")

# Umlauts, an eszett, an em dash and a currency sign: one character from each
# range that a code page would handle differently.
PROBE = "Grüße — ÄÖÜ äöü €"


def main():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp1252"      # the hostile case, forced

    request = "\n".join([
        json.dumps({"jsonrpc": "2.0", "id": 1,
                    "method": "initialize", "params": {}}),
        # launch_app echoes the command back verbatim in its reply, which makes
        # it the cheapest tool to prove a round trip with. The path cannot
        # exist, and the reply carries the string either way. Since 1.1.0
        # launch_app validates its input and the plain path would be rejected as
        # a non-existent program, so we pass confirm:true - the explicit path
        # that runs the command as given and echoes it back unchanged, which is
        # exactly the verbatim round trip this test needs.
        # ensure_ascii=False is the point of the whole test - see the module
        # docstring.
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "launch_app",
                               "arguments": {"command": "Z:\\" + PROBE,
                                             "confirm": True}}},
                   ensure_ascii=False),
    ])

    proc = subprocess.run([sys.executable, SERVER], input=request,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=120, env=env)

    returned = None
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == 2 and "result" in message:
            inner = json.loads(message["result"]["content"][0]["text"])
            returned = inner.get("started", "")

    if returned is None:
        print("FAIL: the server sent no usable reply")
        print(proc.stderr[:2000])
        return 1

    if not returned.endswith(PROBE):
        print("FAIL: non-ASCII did not survive the round trip")
        print("  sent    :", PROBE)
        print("  returned:", returned)
        print("  This is the cp1252 bug. src/server.py must reconfigure")
        print("  sys.stdin to UTF-8, not only stdout and stderr.")
        return 1

    print("OK: non-ASCII survives a round trip even with stdin forced to cp1252")
    return 0


if __name__ == "__main__":
    sys.exit(main())
