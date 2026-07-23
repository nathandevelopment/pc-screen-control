# -*- coding: utf-8 -*-
"""
Print the config other MCP clients need, with your real path already filled in.

This does not change anything. It reads nothing but its own location, touches no
network, and edits no client's settings - it just prints the block you paste, so
you do not have to guess the path to server.py. See docs/OTHER_CLIENTS.md for
where each client wants it.
"""
import json
import os
import sys


def find_server():
    """Best effort: the installed copy first, then a source checkout."""
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""),
                     "pc-screen-control", "server.py"),
        os.path.join(here, "..", "src", "server.py"),
        os.path.join(here, "server.py"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    # Nothing found - hand back a clearly-fake path so the output is still
    # copy-pasteable and the user sees exactly what to replace.
    return r"C:\Tools\pc-screen-control\server.py"


def main():
    server = find_server()
    forward = server.replace("\\", "/")     # JSON is happiest with these
    found = os.path.isfile(server)

    print("=" * 68)
    print("PC Screen Control - config for other MCP clients")
    print("=" * 68)
    if found:
        print("Found your server at:\n  %s\n" % server)
    else:
        print("Could not find an installed server.py, so the path below is a\n"
              "PLACEHOLDER - replace it with the real one.\n")

    print("-- Config-file clients (Cursor, VS Code, Cline, Continue, Zed) ------")
    block = {
        "mcpServers": {
            "pc-screen-control": {
                "command": "python",
                "args": [forward],
            }
        }
    }
    print(json.dumps(block, indent=2))
    print()
    print("-- GPT via the OpenAI Agents SDK (Python) --------------------------")
    print('    from agents.mcp import MCPServerStdio')
    print('    MCPServerStdio(params={"command": "python",')
    print('                           "args": ["%s"]})' % forward)
    print()
    print("If 'python' is not on your PATH, use the full path to python.exe in")
    print("'command'. Full walkthrough: docs/OTHER_CLIENTS.md")
    print("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
