# PC Screen Control — where this is going

*Written by the maintainer. Read this before proposing changes.*

---

## Start here

**Current state:** v1.0.0 is released and working. 35 tools, 9 test files in CI
on Python 3.9 / 3.11 / 3.13, all green. The published release matches this
commit. The consistency checker that compares every number and claim across
README, manifest, CHANGELOG, SECURITY and the built package passes.

**Your job:** write the code. Take items from the Version 1 checklist below, in
order — A1 (verify the update hash) is the most valuable and the most
self-contained. One item per PR, with the *reason* in the commit message.

**Not your job:** building the `.mcpb`, running the Windows GUI tests,
publishing releases, or checking that the docs still match the code. That is
done separately on a real Windows desktop, because most of these tests need one.
Assume every claim you make will be measured before it ships.

**Before you propose anything**, read `CHANGELOG.md`. Several obvious ideas were
tried and rejected with reasons recorded there.

---

## What this project is, in one paragraph

Most tools that let an AI use a Windows PC take a screenshot and guess a
coordinate. This one hands the AI the accessibility tree instead — the same
structured data a screen reader uses — so a control is pressed by name and every
action reports the element's state before and after. That part is no longer
novel; at least five projects now do it, including one from Scott Hanselman.

**What is not solved anywhere else is the human sitting at the same desk.**
Every other Windows MCP server behaves as though the computer were empty. This
one assumes someone is using it, and treats their attention, their focus and
their keystrokes as things that cost something to take. That is the thesis, and
everything in Version 1 exists to make it true rather than merely claimed.

---

## The rule this project is built on

> **Measure before you claim. If a function does not report whether it worked,
> it will eventually stop working and nobody will notice.**

This is not a slogan. It is the reason for most of the code. Three separate
defects survived for weeks because the code that was supposed to do the work
looked complete and silently did nothing:

- `SetForegroundWindow` is refused for a background process, silently. The
  restore had never once run.
- `_ref_for` returned nothing for almost every element, which quietly forced
  three tools down onto the mouse.
- `stdin` was never pinned to UTF-8, so every non-English character sent to any
  tool was destroyed on the way in while the replies looked perfect.

Each was found by a test that checked the **outcome**, not the call. Several
tests had to be fixed first because they passed against broken code. When you
add a test, delete the fix in a scratch copy and confirm the test fails. A test
that cannot fail is a feeling, not a test.

---

## Response to the Copilot review

The review was accurate and I am acting on most of it. Where I am not, here is
why — do not re-propose these without new evidence.

| Point | Verdict |
|---|---|
| **Update download is unverified** | **Correct, and the sharpest finding.** The release notes publish a SHA-256 and `check_for_update` never checks it. In scope for v1. |
| **Broad `except` blocks swallow errors** | **Correct, and this project has been bitten by it three times.** In scope for v1, but narrowly: `_safe()` stays, and gains a diagnostic channel. |
| **Low-level input hooks look like a keylogger** | **Correct that AV will complain.** Already disclosed in SECURITY.md. Needs a FAQ, not a redesign — the hooks are the feature. |
| **No type hints, no linter** | Correct. Cheap. In scope for v1. |
| **Modularise the single file** | **Rejected for v1.** One readable file is a deliberate trust decision: a stranger installing something that controls their PC can audit it in one sitting. Splitting it into eight modules makes the code nicer for me and the audit harder for them. Revisit if it passes ~4000 lines. |
| **Windows-only, single maintainer** | Correct and unfixable. Already stated plainly in the README. `docs/PORTING.md` maps every pattern onto the macOS API and is honest that a map is not an implementation. |
| **External security audit before wide distribution** | Agreed in principle, not affordable. The substitute is that the whole server is one readable file with its tests shipped next to it. Say so; do not pretend otherwise. |

**Yes, open PRs** for: signature verification, exception narrowing, type hints,
linting, the AV FAQ. **No PRs** for: splitting the file, removing the hooks,
adding a framework, adding a dependency that is not strictly needed.

---

# Version 1.0 — the goal

**Theme: it works on a computer someone is still using, and it can prove it.**

Version 1 is finished when a stranger can install it, work alongside it for an
afternoon, and never once be surprised by it. Nothing in this list is a feature
idea; each one closes a gap between what the README promises and what the code
does.

### A. Trust — the part I cannot hand-wave

- [ ] **Verify the downloaded `.mcpb` against the published SHA-256.**
      `check_for_update` already reads the release JSON, and the release body
      already contains the hash. Parse it, hash the download, refuse and delete
      on mismatch, and report which hash was expected. Never install; only
      download and verify.
- [ ] **A diagnostic channel for swallowed errors.** `_safe()` currently
      discards the exception. It should record type, message and call site in a
      ring buffer that `self_test` can return. Keep `_safe()` — the swallowing
      is deliberate, because one dead control must not kill a tree walk — but
      stop making the swallowed thing invisible.
- [ ] **Type hints on every public function, `ruff` in CI.** No behaviour
      changes in the same commit.
- [ ] **An antivirus FAQ in the docs.** Why it triggers, what the hooks
      actually do, how to verify that claim yourself, how to switch the guard
      off entirely with `set_guard enabled:false`.

### B. Coexistence — the thesis, made true

Most of this is done. What remains is the last case.

- [x] Focus, window and text caret restored after every action, measured
- [x] Restore happens **under the lock**, before input is handed back
- [x] Takeover refused when the window *or* the focused control moved
- [x] `claim_window` parks a window where the mouse physically cannot reach
- [x] Crash rescue: a parked window is written to disk and recovered on restart
- [x] Rubber-band pulse, Escape never swallowed, ten-second watchdog
- [ ] **Window-targeted input (`PostMessage`) as rung 3.5.** Decision gate:
      measure `WM_LBUTTONDOWN` / `WM_CHAR` against Win32, Qt (DaVinci Resolve)
      and Chromium. Ship only if it works on at least two of the three, and
      report per call which route was used. If it fails the gate, say so in the
      README and leave the canvas case on rung 4. **Do not ship it untested
      because it sounds good.**

### C. Idiot-proofing — the part that decides whether anyone gets this far

- [x] `self_test`: ten checks, plain language, every failure carries its fix
- [x] Irreversible actions refuse on the first call and describe the loss
- [x] The first run explains its own delay in the first reply
- [x] Claimed windows are marked wherever windows are listed
- [ ] **Make the Python requirement survivable.** It is the single biggest
      reason someone never gets this working. In order: detect the Microsoft
      Store placeholder and say so by name; make the failure message name the
      exact checkbox that was missed; offer an optional second release asset
      with Python embedded. Optional, never instead of the small file — a 30 MB
      download from an unknown developer reads as *more* suspicious, not less.
- [ ] **A `describe_screen` that is honest about cost.** It is 3.4s and every
      task is told to start with it. Either make it faster or make the reply say
      what it spent.

### D. Housekeeping

- [ ] **The published release must equal the code.** It is currently several
      commits behind. From the first real download onward: any change to the
      contents gets a new version number. No exceptions, no "it is only a
      docs change".
- [x] An automated consistency checker that compares every number and claim
      across README, manifest, CHANGELOG, SECURITY and the built package —
      and that is itself tested by reintroducing known defects.

### Version 1 is done when

1. `self_test` returns `Everything works` on a clean Windows install with only
   the documented prerequisites.
2. Every claim in the README maps to a test in `tests/` that fails if the claim
   stops being true.
3. The downloaded update is verified against a published hash.
4. CI is green on Python 3.9, 3.11 and 3.13.
5. A person who has never seen this can install it and get a window list
   without asking me anything.

---

# Version 2.0 — deliberately after 1.0

**Theme: it also sees and hears.**

Do not start this before 1.0 ships. It is a different problem with a different
risk profile, and mixing the two would mean neither is finished. Version 1 is
about *not disturbing the human*. Version 2 is about *perceiving what has no
accessibility tree at all*.

### The gap it closes

The cost ladder ends at rung 4 for a reason: editing canvases, video timelines
and games publish no controls, so there is nothing to read and nothing to press
by name. Today the honest answer is "take a picture and click a coordinate".
That is the same guessing this project exists to replace — it is simply where
the structured data runs out.

### What it should become

- **Images, precisely.** Not "there is a button somewhere" but position, state,
  colour, text and relationship, at a quality good enough to act on rather than
  to describe.
- **Video as time, not as frames.** Analyse on a fixed cadence — roughly every
  0.5 s — and emit a timestamped text record rather than a pile of pictures.
  Movement, cuts, what changed and when.
- **Audio as text with timestamps.** Speech, music, silence, level. On the same
  clock as the video record.
- **Fusion.** The three streams share one timeline, so a question like "what
  happened at 0:42" has one answer assembled from all of them, not three
  answers that have to be reconciled by the reader.

### Why timestamps are the design and not a detail

Text is what a model reasons over well. A timestamp is what makes separate
observations comparable. Get the clock right and the three streams merge almost
for free; get it wrong and no amount of model quality repairs it.

### Constraints carried over from Version 1

- Everything local unless the user explicitly asks otherwise. `check_for_update`
  must remain the only tool that reaches the network by default.
- Every observation reports its own confidence and its own cost, the same way
  every action reports before and after.
- No new dependency without a measurement showing what it buys.
- If an approach cannot be measured, it does not ship. This applies to
  perception more than anywhere else, because a description is *always*
  plausible — which is exactly why it must be checked against something.

---

## How to work on this

1. Read `CHANGELOG.md` before proposing anything. Several obvious ideas were
   already tried and rejected with reasons — `BlockInput` needs administrator
   rights, Windows toasts cannot carry an actionable button, a full-screen
   overlay cannot be animated at 36 MB per frame.
2. Measure first, then write. Numbers in this repository come from scripts in
   `tests/` that ship with it, so anyone can contradict them.
3. Small commits with the *reason* in the message, not the diff. The diff is
   already visible.
4. If you find a claim in the docs that the code does not support, that is a
   bug in the code or a bug in the docs — never a thing to leave alone.
