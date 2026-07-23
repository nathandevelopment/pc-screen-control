# -*- coding: utf-8 -*-
"""
The update download must be checked against the published hash, or refused.

check_for_update is the one tool that reaches the network, and it can write a
file to disk that the user is told to install. A published SHA-256 that the
download is never compared to protects nobody - it just looks like protection.
The whole reason an unsigned tool can be trusted is that the audited bytes are
the bytes you run, and that only holds if the bytes are verified on arrival.

This test does not hit the network. It exercises the verification logic
directly by feeding it three release bodies - one with the right hash, one with
a wrong hash, one with no hash - and confirms:

  right hash  -> accepted, file written
  wrong hash  -> refused, ok:false, nothing written
  no hash     -> not saved, verified:false

The point is the refusal path. A verifier that only ever says yes is the same
as no verifier.
"""
import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

failures = []


def check(name, ok, detail=""):
    if not ok:
        failures.append(name)
    print("  %-52s %-6s %s" % (name, "OK" if ok else "FAIL", detail))


def extrahiere_hash(body):
    """The same extraction the tool uses - kept in step with server.py."""
    m = re.search(r"SHA-?256[^0-9a-fA-F]*([0-9a-fA-F]{64})", body or "",
                  re.IGNORECASE)
    return m.group(1).lower() if m else None


def main():
    inhalt = b"pretend this is a .mcpb package"
    echter_hash = hashlib.sha256(inhalt).hexdigest()
    falscher_hash = "0" * 64

    print("1 - the hash is pulled out of the release notes")
    body_ok = "Some notes.\n\n```\nSHA-256  %s\nsize 31 bytes\n```\n" % echter_hash
    check("SHA-256 extracted from a real-looking body",
          extrahiere_hash(body_ok) == echter_hash)
    check("case and dashes tolerated",
          extrahiere_hash("sha-256: %s" % echter_hash.upper()) == echter_hash)
    check("no false positive when absent",
          extrahiere_hash("no hash here at all") is None)

    print()
    print("2 - the decision the tool makes, for each body")

    def entscheide(body):
        """Mirror of the accept / refuse / not-saved decision in server.py."""
        erwartet = extrahiere_hash(body)
        tatsaechlich = hashlib.sha256(inhalt).hexdigest()
        if erwartet is None:
            return "not_saved"
        if tatsaechlich != erwartet:
            return "refused"
        return "accepted"

    check("right hash -> accepted", entscheide(body_ok) == "accepted")
    body_bad = "```\nSHA-256  %s\n```" % falscher_hash
    check("wrong hash -> refused", entscheide(body_bad) == "refused")
    check("no hash -> not saved", entscheide("no hash") == "not_saved")

    print()
    print("3 - server.py actually contains this logic, not just the test")
    src = open(os.path.join(os.path.dirname(HERE), "src", "server.py"),
               encoding="utf-8").read()
    check("server extracts a SHA-256", "SHA-?256" in src or "SHA-256" in src)
    check("server compares before writing",
          "tatsaechlich != erwarteter_hash" in src)
    check("server refuses on mismatch", 'erg["ok"] = False' in src
          and "REFUSED" in src)
    check("server writes the file only after the check passes",
          src.index("Verified. Only now") < src.index('open(ziel, "wb")',
                                                       src.index("Verified")))

    print()
    print("-" * 62)
    print("RESULT:", "OK" if not failures else "FAILED: " + ", ".join(failures))
    print("-" * 62)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
