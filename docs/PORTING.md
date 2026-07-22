# Porting this to macOS

Nothing here is implemented. This is the map I made before deciding not to
build it yet, kept in the repository because it is the useful half of the work
— and because it makes the boundary between "the idea" and "the Windows part"
explicit for anyone reading the code.

## The short version

macOS has the same thing under a different name. Windows calls it **UI
Automation**; macOS calls it the **Accessibility API**, and the object you talk
to is `AXUIElement`. Both exist for the same original reason — screen readers —
and both therefore expose what a human would need in order to operate the
interface without seeing it. That is precisely what this server needs.

The design ports completely. **No line of code ports.** Windows speaks COM,
macOS speaks Objective-C, and the pattern model differs enough that a
translation layer would be more work than a clean second implementation.

## What maps to what

| this server | Windows UIA | macOS AX |
|---|---|---|
| `read_ui_tree` | `TreeWalker` over `IUIAutomationElement` | `AXChildren` recursion |
| element name | `Name` | `AXTitle`, falling back to `AXDescription` |
| element role | `ControlType` | `AXRole` + `AXSubrole` |
| `invoke` | `InvokePattern.Invoke` | `AXUIElementPerformAction(kAXPressAction)` |
| `toggle` | `TogglePattern` | `AXValue` on a checkbox (0/1), set via `AXUIElementSetAttributeValue` |
| `set_text` | `ValuePattern.SetValue` | `AXValue` (writable when `AXUIElementIsAttributeSettable`) |
| `set_value` | `RangeValuePattern` | `AXValue` with `AXMinValue` / `AXMaxValue` |
| `expand` | `ExpandCollapsePattern` | `AXDisclosing` (outlines), `AXExpanded` (popups) |
| `select` | `SelectionItemPattern` | `AXUIElementPerformAction(kAXPickAction)` |
| `scroll` by percent | `ScrollPattern.SetScrollPercent` | no direct equivalent — set `AXValue` on the scroll bar, or `AXScrollToVisible` on a child |
| `window` move/resize | `TransformPattern` / `SetWindowPos` | `AXPosition` and `AXSize`, both settable |
| `window` state | `WindowPattern.SetWindowVisualState` | `AXMinimized` (settable), `AXFullScreen` |
| `element_from_point` | `ElementFromPoint` | `AXUIElementCopyElementAtPosition` |
| `get_focus` | `GetFocusedElement` | `AXFocusedUIElement` on the system-wide element |
| `read_text` | `TextPattern` | `AXValue` / `AXSelectedText` on an `AXTextArea` |
| `capture` | `ImageGrab` | `CGWindowListCreateImage` — can capture a window that is behind another, which Windows cannot |
| `list_windows` | root element children | `CGWindowListCopyWindowInfo`, or `AXWindows` per running application |
| the edge overlay | layered window + `UpdateLayeredWindow` | borderless `NSWindow`, `ignoresMouseEvents = true`, `level = .screenSaver` |
| holding the user's input | `SetWindowsHookEx` with `WH_KEYBOARD_LL` / `WH_MOUSE_LL` | `CGEventTapCreate` at `cgSessionEventTap`, returning `nil` to swallow |
| telling our input from theirs | `LLKHF_INJECTED` / `LLMHF_INJECTED` flag | compare `CGEventGetIntegerValueField(event, .eventSourceStateID)` against our own source |
| foreground window, for the takeover check | `GetForegroundWindow` | `NSWorkspace.shared.frontmostApplication` |
| focused control, for the same check | `GetFocusedControl` | `AXFocusedUIElement` on the system-wide element |
| idle time | `GetLastInputInfo` | `CGEventSourceSecondsSinceLastEventType` |

## Where it gets harder, and where it gets easier

**Harder on macOS**

- **Permission is explicit and up front.** Nothing works at all until the user
  ticks the app under System Settings → Privacy & Security → Accessibility. The
  Windows version simply runs. A macOS port must detect
  `AXIsProcessTrustedWithOptions` and guide the user there instead of failing
  with an empty tree, which is what a naive port does.
- **You address applications, not a desktop.** There is no single root holding
  every window. You start from a process id
  (`AXUIElementCreateApplication(pid)`), so `describe_screen` has to enumerate
  running applications first.
- **The Python bridge is heavier.** `pyobjc` pulls in a large dependency, and
  the raw `ApplicationServices` calls are `ctypes`-unfriendly compared to
  Windows' COM.

**Easier on macOS**

- **Cocoa applications are consistently well exposed.** The accessibility tree
  is largely a by-product of AppKit, so a standard Mac app tends to be more
  completely readable than a standard Windows app.
- **Window capture is better.** `CGWindowListCreateImage` can photograph a
  window that is covered by another one. On Windows the window has to be
  visible, which is why `capture` here focuses it first.
- **Fewer frameworks.** Windows has Win32, WinForms, WPF, UWP, WinUI, Qt and
  Electron, each exposing a different amount. macOS has AppKit, SwiftUI, and
  the same Qt/Electron problem — a much shorter list of behaviours to learn.

## What would stay shared

The parts worth keeping identical, because they are the actual design and not
the plumbing:

- the tool names and their JSON schemas
- **before/after verification on every action** — the reason this is more
  useful than a screenshot loop
- the `readable` / `shallow` / `canvas-only` verdict
- the cost ladder: tree → pattern → image → mouse, cheapest rung that works
- `wait_for` polling a condition instead of sleeping
- the edge indicator being on **only** while physical input is taken
- **no tool stepping down a rung silently** — `invoke` refusing rather than
  clicking, and `"how"` / `"took_input"` in every reply that did step down
- the input guard's shape: announce, hold, act, restore, with Escape never
  swallowed — and the ordering that makes it work, which is *freeze first, then
  read the target, then act*. Checking before locking is a race on any operating
  system, not a Windows detail.
- the takeover check comparing **both** the frontmost application and the
  focused control, because typing follows the focus and an application can stay
  frontmost while the focus moves inside it

## Realistic shape of the work

A second implementation of the backend, roughly the size of the current one,
sharing the protocol loop and tool registry. Not a port — a sibling. The
honest estimate is that the Windows file took longer to *learn* than to write,
and the same would be true again.
