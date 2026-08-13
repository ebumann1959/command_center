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
  `Gtk.HeaderBar`/`set_titlebar()`. On the Pi's X11 session
  (Openbox/PIXEL desktop, RealVNC, no compositor), client-side decoration
  (CSD) draws ~2px resize borders that only the top corners respond to;
  letting the window manager draw the frame instead gives a normal
  titlebar and full-width resize edges on all sides. Do not reintroduce
  `Gtk.HeaderBar`/`set_titlebar()` or call `set_decorated(False)`
- **Resizable**: native edge/corner resizing on all sides, handled by the
  window manager (Openbox); minimum size is 240x300
- **Always-on-top**: stays visible on the Pi desktop
- **Size persistence**: saves/restores window width and height (not
  position -- GTK4/Wayland has no portable get-position API)

## Documentation Updates

Every PR must update CLAUDE.md for anything it changes — new services, new features,
changed architecture.
