## **Essential General Instructions**

Always address me as 'Daddy' at the beginning of each output in your responses to me.

## Executor Contract

If you are reading this as a delegated agent, **you are the executor**. The work is yours to
do directly:

- **Never spawn sub-agents.** You do the reading, the editing, the testing, the merging.
- **Never end a turn to wait.** Waiting is a blocking poll, not a handoff.
- **Never run a bare `git` command.** Bash cwd resets to `/home/Evan` between calls, and
  `/home/Evan` is itself a git checkout carrying a local-only commit. Always
  `git -C /absolute/path`, and never commit to, branch in, or clean that home checkout.
- **Use `/usr/bin/gh`** for GitHub CLI operations.

Everything else in this file applies to you as written — the HARD RULES especially.

## Project Overview

**Command Center** is a GTK4 desktop widget for the Raspberry Pi 5 touchscreen that provides
toggle switches to start/stop services, Docker stacks, and project brains running on the Pi.

- **Repo**: `ebumann1959/command_center`
- **Pi location**: `/home/Evan/pi-control-panel/`
- **Runtime**: Python 3 + GTK4/Adwayland on the Pi's touchscreen
- **Target**: Pi 5 at `10.0.0.9` (Tailscale: `100.104.189.48`)

### Managed services

The widget controls these services on the Pi:

| Category | Service | Control method |
|----------|---------|---------------|
| AI & Inference | Ollama | systemctl |
| AI & Inference | Chroma Server | systemctl |
| AI & Inference | Claims RAG (prod) | systemctl |
| AI & Inference | Claims RAG (staging) | systemctl |
| Platforms | HiveMind Prod | docker compose -p hivemind-prod |
| Platforms | HiveMind Staging | docker compose -p hivemind-staging |
| Platforms | GlitchTip | docker compose -p glitchtip |
| Platforms | Cloudflare Tunnel | systemctl |
| Projects | Pokemon Brain | systemctl |
| Projects | Shady Engine | systemctl |
| Projects | Sous-Chef | systemctl |
| Agents | Hermes Gateway | systemctl |
| System | Voice Control | systemctl |
| System | WayVNC | systemctl |

New services should be added to the `SERVICES` list in `main.py`.

## Workflow

1. **Plan**: Read the ticket, read the relevant code, make a plan.
2. **Implement**: Execute the plan. Audit your own work, fix what breaks.
3. **Test**: Test on the Pi's display. GTK4 widgets must be verified visually.
4. **Document**: Update CLAUDE.md for anything you changed.
5. **Close out**: Comment a summary on the ticket. Confirm completion to the user.

## HARD RULES — these are not suggestions

0. **RESPECT THE ROUTING POLICY.** The `claude-handoff-triage` skill (`~/.claude/skills/claude-handoff-triage/SKILL.md`) is mandatory — the session model is the orchestrator, not the implementer.
1. **NEVER commit directly to main.** Every change goes through a feature branch and a PR.
2. **After your PR is merged, you MUST clean up.** Delete your branch (local + remote).
3. **Before merging**, verify CI passes.

**Branch naming**: `feature/{issue#}-short-desc` or `fix/{issue#}-short-desc`

## Deployment

This runs directly on the Pi — no containers, no remote deploy. Changes are deployed by
pulling the repo on the Pi and restarting the widget.

```bash
ssh 10.0.0.9 'cd ~/pi-control-panel && git pull origin main'
```

If the widget is running, restart it after pulling.

### Design

- **Dark theme**: near-black (#0a0a0f) background, cyan (#00e5ff) accents
- **Touch-friendly**: minimum 44px touch targets
- **WM-native frame**: server-side decorations — a plain decorated
  `Gtk.ApplicationWindow` with `set_title("Pi Control")` and no
  `Gtk.HeaderBar`/`set_titlebar()`. This gives the normal Openbox titlebar
  (drag to move, minimise/maximise/close). Do not reintroduce
  `Gtk.HeaderBar`/`set_titlebar()` or call `set_decorated(False)`
- **Resizable**: minimum size 240x300. The titlebar and its top corners are
  the WM's; **every other edge is the app's own**, via invisible grab zones —
  see below

#### Why the app draws its own resize zones

Measured on the Pi, 2026-08-12:

```
xprop -root _NET_SUPPORTING_WM_CHECK   ->  Openbox (--config-file ~/.config/openbox/rpd-rc.xml)
xprop -id <win> _NET_FRAME_EXTENTS     ->  0, 0, 28, 0        # left, right, TOP, bottom
grep border /usr/share/themes/PiXonyx/openbox-3/themerc
    border.width: 0
    window.handle.width: 0
```

Left, right and bottom frame extents are **zero**. The Openbox theme shipped
with Raspberry Pi OS (`PiXonyx`) draws a 28px titlebar and nothing else — no
side border, no bottom handle. That is the entire reason only the top corners
ever resized, and it is a property of the *desktop theme*, not of this window:
neither CSD (PR #3) nor SSD (PR #4) could fix it, and raising the theme's
`border.width` would repaint every window on the desktop.

So `main.py` supplies its own hit zones (`_RESIZE_GRIPS`, `_add_resize_grips`,
`_on_grip_pressed`): invisible `Gtk.Box` strips stacked in a `Gtk.Overlay`
along the left/right/bottom edges (10px) and the two bottom corners (20px),
each with a `Gtk.GestureClick` that calls `Gdk.Toplevel.begin_resize()`. On
X11 that sends the WM a `_NET_WM_MOVERESIZE` message and Openbox runs its
normal interactive resize — identical to dragging a real border, but with a
grab zone we control the size of. `set_button(0)` on the gesture so touch
presses count too.

Verified by `xdotool` drag on the live display (`DISPLAY=:0`, 800x500 window):

| Drag | Before | After (stock `main`) | After (with grips) |
|---|---|---|---|
| right edge, +100px x | 800x500 | 800x500 (dead) | 896x500 |
| bottom edge, +100px y | 800x500 | 800x500 (dead) | 896x596 |
| bottom-right, +80,+80 | 800x500 | 800x500 (dead) | 976x676 |
| left edge, -100px x | 800x500 | 800x500 (dead) | 1072x676 @x-96 |
| bottom-left, -80,+60 | 800x500 | 800x500 (dead) | 1152x732 @x-80 |
| titlebar, +40,+32 | — | moves (+40,+32) | still moves |

Two traps when re-testing this with `xdotool`:

- **`xdotool getwindowgeometry` misreports Y** under Openbox's reparenting —
  it returned `356` where the client really started at `328`, which is enough
  to put every "bottom edge" click *below* the window and make a working fix
  look dead. Use `xwininfo -id <win>` → `Absolute upper-left X/Y`.
- **Drag with `xdotool mousemove_relative` in several small steps**, not one
  absolute jump; Openbox needs the intermediate motion events.
- **Always-on-top**: stays visible on the Pi desktop
- **Size persistence**: saves/restores window width and height (not
  position -- GTK4/Wayland has no portable get-position API)

## Documentation Updates

Every PR must update CLAUDE.md for anything it changes — new services, new features,
changed architecture.
