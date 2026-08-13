#!/usr/bin/python3
"""
Pi Control Panel
=================

A GTK4 touchscreen widget for a Raspberry Pi 5 that lets you flip services
on/off with a row of switches.

What it controls
-----------------
Two kinds of backends:

  * systemd units (started/stopped via ``systemctl``)
  * docker compose projects (started/stopped via ``docker compose``)

Status is polled every 10 seconds in a background thread so the UI never
blocks. Each poll makes exactly two subprocess calls total -- one batched
``systemctl is-active`` covering every systemd-backed service, and one
``docker ps`` covering every docker-compose-backed service -- rather than
one subprocess per service (docker CLI startup is expensive on a Pi).
Toggling a switch runs the start/stop command in its own background
thread, disabling the switch and showing a small busy indicator until the
command finishes and the real status has been re-confirmed.

Privileges
----------
This app is NOT meant to be run as root. Instead:

  * systemd start/stop commands are run as ``sudo -n systemctl ...`` -- the
    ``-n`` flag means "non-interactive": if a password would be required,
    the command fails immediately instead of hanging forever on a prompt
    that can never be answered from a background thread. Status checks
    (``systemctl is-active``) are run without sudo since that normally
    works for any user.

    For start/stop to work without a password prompt, add a narrowly
    scoped NOPASSWD rule with ``sudo visudo -f /etc/sudoers.d/pi-control-panel``,
    e.g.:

        evan ALL=(root) NOPASSWD: /usr/bin/systemctl start ollama, \
            /usr/bin/systemctl stop ollama, \
            /usr/bin/systemctl start chroma-server, \
            /usr/bin/systemctl stop chroma-server, \
            /usr/bin/systemctl start claims-rag, \
            /usr/bin/systemctl stop claims-rag, \
            /usr/bin/systemctl start claims-rag-staging, \
            /usr/bin/systemctl stop claims-rag-staging, \
            /usr/bin/systemctl start hivemind-tunnel, \
            /usr/bin/systemctl stop hivemind-tunnel, \
            /usr/bin/systemctl start pokemon-play, \
            /usr/bin/systemctl stop pokemon-play, \
            /usr/bin/systemctl start shady-engine, \
            /usr/bin/systemctl stop shady-engine, \
            /usr/bin/systemctl start sous-chef, \
            /usr/bin/systemctl stop sous-chef, \
            /usr/bin/systemctl start hermes-gateway, \
            /usr/bin/systemctl stop hermes-gateway, \
            /usr/bin/systemctl start voice-control, \
            /usr/bin/systemctl stop voice-control, \
            /usr/bin/systemctl start wayvnc, \
            /usr/bin/systemctl stop wayvnc

  * docker commands run as the invoking user directly. This requires the
    user to be a member of the ``docker`` group (``sudo usermod -aG docker
    $USER``, then re-login). If docker commands fail, the app surfaces the
    error in the row instead of crashing.

This app never stores a password, never attempts interactive privilege
escalation, and never writes sudoers rules on your behalf -- if sudo isn't
configured, it tells you exactly what to add via an in-row error label.

Running it
----------
    ./start.sh

or directly:

    python3 main.py
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import Gdk, GLib, Graphene, Gtk  # noqa: E402

CONFIG_DIR = Path.home() / ".config" / "pi-control-panel"
CONFIG_FILE = CONFIG_DIR / "position.json"

# Resolved lazily by inspecting running containers; see resolve_glitchtip_dir().
GLITCHTIP_DIR: Optional[str] = None


# ---------------------------------------------------------------------------
# Service definitions
# ---------------------------------------------------------------------------


class Service:
    """Base class for a single toggleable service/row."""

    name: str

    def is_active(self) -> bool:
        raise NotImplementedError

    def start(self) -> tuple[bool, str]:
        raise NotImplementedError

    def stop(self) -> tuple[bool, str]:
        raise NotImplementedError


class SystemdService(Service):
    """A service controlled by a systemd unit."""

    def __init__(self, name: str, unit: str):
        self.name = name
        self.unit = unit

    def is_active(self) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", self.unit],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.stdout.strip() == "active"
        except Exception:
            return False

    def _sudo_systemctl(self, action: str) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                ["sudo", "-n", "systemctl", action, self.unit],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                return True, ""
            stderr = result.stderr.strip()
            if "password" in stderr.lower():
                return False, (
                    f"sudo requires a password for 'systemctl {action} {self.unit}'. "
                    "Add a NOPASSWD sudoers rule (see main.py docstring / "
                    "sudo visudo -f /etc/sudoers.d/pi-control-panel)."
                )
            return False, stderr or f"systemctl {action} {self.unit} failed"
        except FileNotFoundError:
            return False, "sudo not found on this system"
        except subprocess.TimeoutExpired:
            return False, f"systemctl {action} {self.unit} timed out"
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)

    def start(self) -> tuple[bool, str]:
        return self._sudo_systemctl("start")

    def stop(self) -> tuple[bool, str]:
        return self._sudo_systemctl("stop")


class DockerComposeService(Service):
    """A service controlled by a docker compose project."""

    def __init__(
        self,
        name: str,
        project: str,
        start_cmd: list[str],
        stop_cmd: list[str],
        cwd: Optional[str] = None,
        cwd_resolver: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.name = name
        self.project = project
        self.start_cmd = start_cmd
        self.stop_cmd = stop_cmd
        self._cwd = cwd
        self._cwd_resolver = cwd_resolver

    def _resolve_cwd(self) -> str:
        if self._cwd:
            return self._cwd
        if self._cwd_resolver is not None:
            resolved = self._cwd_resolver()
            if resolved:
                return resolved
        return str(Path.home())

    def is_active(self) -> bool:
        try:
            result = subprocess.run(
                [
                    "docker",
                    "ps",
                    "--filter",
                    f"label=com.docker.compose.project={self.project}",
                    "--format",
                    "{{.Names}}",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return bool(result.stdout.strip())
        except Exception:
            return False

    def _run(self, cmd: list[str]) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self._resolve_cwd(),
            )
            if result.returncode == 0:
                return True, ""
            return False, result.stderr.strip() or f"{' '.join(cmd)} failed"
        except FileNotFoundError:
            return False, "docker not found on this system"
        except subprocess.TimeoutExpired:
            return False, f"{' '.join(cmd)} timed out"
        except Exception as exc:  # pragma: no cover - defensive
            return False, str(exc)

    def start(self) -> tuple[bool, str]:
        return self._run(self.start_cmd)

    def stop(self) -> tuple[bool, str]:
        return self._run(self.stop_cmd)


def batch_systemd_is_active(units: list[str]) -> dict[str, bool]:
    """Check active state for many systemd units in a single subprocess call.

    ``systemctl is-active unit1 unit2 ...`` prints one line per unit, in
    argument order, regardless of the overall exit code (which is nonzero
    if *any* unit isn't active) -- so this replaces N `systemctl is-active`
    subprocess spawns with exactly one.
    """
    if not units:
        return {}
    try:
        result = subprocess.run(
            ["systemctl", "is-active", *units],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = result.stdout.splitlines()
        return {
            unit: (lines[i].strip() == "active" if i < len(lines) else False)
            for i, unit in enumerate(units)
        }
    except Exception:
        return {unit: False for unit in units}


def batch_docker_running_projects() -> set[str]:
    """Return the set of compose project names with at least one running
    container, from a single ``docker ps`` call.

    Docker CLI startup is expensive on a Pi, so this replaces one
    ``docker ps``/``docker compose ps`` invocation per docker-backed
    service with a single call covering all of them.
    """
    try:
        result = subprocess.run(
            [
                "docker", "ps",
                "--filter", "status=running",
                "--format", '{{.Label "com.docker.compose.project"}}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}
    except Exception:
        return set()


def resolve_glitchtip_dir() -> Optional[str]:
    """Find GlitchTip's docker compose working directory by inspecting a
    running (or previously run) container's compose labels. Cached in the
    module-level GLITCHTIP_DIR constant once resolved.
    """
    global GLITCHTIP_DIR
    if GLITCHTIP_DIR is not None:
        return GLITCHTIP_DIR
    try:
        ps_result = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=com.docker.compose.project=glitchtip",
                "--format",
                "{{.ID}}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        container_id = ps_result.stdout.strip().splitlines()[0] if ps_result.stdout.strip() else None
        if not container_id:
            return None
        inspect_result = subprocess.run(
            [
                "docker",
                "inspect",
                container_id,
                "--format",
                '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}',
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        working_dir = inspect_result.stdout.strip()
        if working_dir:
            GLITCHTIP_DIR = working_dir
            return GLITCHTIP_DIR
    except Exception:
        pass
    # Could not determine it -- fall back to $HOME and let the row's error
    # label tell the user. Set GLITCHTIP_DIR manually near the top of this
    # file if this keeps happening on your system.
    return None


def build_services() -> dict[str, list[Service]]:
    """Return the category -> [Service, ...] map used to build the UI."""

    hivemind_dir = "/home/Evan/archetype-claims-os"

    return {
        "AI & Inference": [
            SystemdService("Ollama", "ollama"),
            SystemdService("Chroma Server", "chroma-server"),
            SystemdService("Claims RAG (prod)", "claims-rag"),
            SystemdService("Claims RAG (staging)", "claims-rag-staging"),
        ],
        "Platforms": [
            DockerComposeService(
                "HiveMind Prod",
                "hivemind-prod",
                start_cmd=[
                    "docker", "compose",
                    "-f", "docker-compose.yml",
                    "-f", "docker-compose.prod.yml",
                    "-p", "hivemind-prod",
                    "up", "-d",
                ],
                stop_cmd=["docker", "compose", "-p", "hivemind-prod", "down"],
                cwd=hivemind_dir,
            ),
            DockerComposeService(
                "HiveMind Staging",
                "hivemind-staging",
                start_cmd=[
                    "docker", "compose",
                    "-f", "docker-compose.yml",
                    "-f", "docker-compose.staging.yml",
                    "-p", "hivemind-staging",
                    "up", "-d",
                ],
                stop_cmd=["docker", "compose", "-p", "hivemind-staging", "down"],
                cwd=hivemind_dir,
            ),
            DockerComposeService(
                "GlitchTip",
                "glitchtip",
                start_cmd=["docker", "compose", "-p", "glitchtip", "up", "-d"],
                stop_cmd=["docker", "compose", "-p", "glitchtip", "down"],
                cwd_resolver=resolve_glitchtip_dir,
            ),
            SystemdService("Cloudflare Tunnel", "hivemind-tunnel"),
        ],
        "Projects": [
            SystemdService("Pokemon Brain", "pokemon-play"),
            SystemdService("Shady Engine", "shady-engine"),
            SystemdService("Sous-Chef", "sous-chef"),
        ],
        "Agents & Comms": [
            SystemdService("Hermes Gateway", "hermes-gateway"),
        ],
        "System": [
            SystemdService("Voice Control", "voice-control"),
            SystemdService("WayVNC", "wayvnc"),
        ],
    }


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

CSS = b"""
window.pi-control-panel {
    background-color: #0a0a0a;
}

.section-header {
    color: #00e5ff;
    font-weight: bold;
    font-size: 16px;
    margin-top: 14px;
    margin-bottom: 4px;
    margin-left: 10px;
}

.service-row {
    min-height: 44px;
    padding: 8px 14px;
    border-radius: 8px;
    background-color: #141414;
    margin: 3px 8px;
}

.service-row.changing {
    opacity: 0.55;
}

.service-label {
    color: #e6f7ff;
    font-size: 15px;
}

.service-status-label {
    color: #7ab8c4;
    font-size: 12px;
}

switch {
    min-width: 56px;
    min-height: 32px;
}

switch slider {
    min-width: 28px;
    min-height: 28px;
}

switch:checked {
    background-color: #00838f;
}

scrolledwindow {
    background-color: #0a0a0a;
}

label.error-label {
    color: #ff8866;
    font-size: 11px;
}
"""


# ---------------------------------------------------------------------------
# Resize grips
# ---------------------------------------------------------------------------
#
# Why this exists (measured on the Pi, 2026-08-12):
#
#   $ xprop -root _NET_SUPPORTING_WM_CHECK      -> Openbox (rpd-rc.xml)
#   $ xprop -id <win> _NET_FRAME_EXTENTS        -> 0, 0, 28, 0
#   $ grep border /usr/share/themes/PiXonyx/openbox-3/themerc
#       border.width: 0
#       window.handle.width: 0
#
# left = right = bottom = 0. The Openbox theme shipped with Raspberry Pi OS
# (PiXonyx) draws a 28px titlebar and *nothing else* -- there is no side
# border and no bottom handle to grab, which is exactly why only the top
# corners ever responded. This is a property of the WM theme, not of the
# window: switching between CSD (PR #3) and SSD (PR #4) cannot fix it, and
# raising the theme's border.width would repaint every window on the desktop.
#
# So the window supplies its own hit zones. Invisible strips are stacked in a
# Gtk.Overlay along the left/right/bottom edges and the two bottom corners; a
# press on one calls Gdk.Toplevel.begin_resize(), which on X11 sends the WM a
# _NET_WM_MOVERESIZE message. Openbox then runs its own normal interactive
# resize -- identical to dragging a border on a theme that has one, but with a
# grab zone we control the size of (10px edges / 20px corners) instead of 0px.

RESIZE_GRIP_PX = 10
RESIZE_CORNER_PX = 20

# (edge, halign, valign, width, height, cursor-name)
# Edges first, corners last: Gtk.Overlay stacks in insertion order, so the
# corner boxes sit on top of the two edge strips they overlap.
_RESIZE_GRIPS = (
    (Gdk.SurfaceEdge.WEST, Gtk.Align.START, Gtk.Align.FILL,
     RESIZE_GRIP_PX, -1, "w-resize"),
    (Gdk.SurfaceEdge.EAST, Gtk.Align.END, Gtk.Align.FILL,
     RESIZE_GRIP_PX, -1, "e-resize"),
    (Gdk.SurfaceEdge.SOUTH, Gtk.Align.FILL, Gtk.Align.END,
     -1, RESIZE_GRIP_PX, "s-resize"),
    (Gdk.SurfaceEdge.SOUTH_WEST, Gtk.Align.START, Gtk.Align.END,
     RESIZE_CORNER_PX, RESIZE_CORNER_PX, "sw-resize"),
    (Gdk.SurfaceEdge.SOUTH_EAST, Gtk.Align.END, Gtk.Align.END,
     RESIZE_CORNER_PX, RESIZE_CORNER_PX, "se-resize"),
)


class ServiceRow(Gtk.Box):
    """One row: label + optional status/error text on the left, switch on the right."""

    def __init__(self, service: Service):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.service = service
        self.add_css_class("service-row")

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        self.label = Gtk.Label(label=service.name, xalign=0.0)
        self.label.add_css_class("service-label")
        text_box.append(self.label)

        self.status_label = Gtk.Label(label="", xalign=0.0)
        self.status_label.add_css_class("service-status-label")
        self.status_label.set_visible(False)
        text_box.append(self.status_label)

        self.append(text_box)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        self.append(self.spinner)

        self.switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self.switch.connect("state-set", self.on_state_set)
        self.append(self.switch)

        self._busy = False

    def set_active_silently(self, active: bool) -> None:
        """Update the switch state without re-triggering state-set."""
        self.switch.handler_block_by_func(self.on_state_set)
        self.switch.set_active(active)
        self.switch.set_state(active)
        self.switch.handler_unblock_by_func(self.on_state_set)

    def set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.switch.set_sensitive(not busy)
        if busy:
            self.add_css_class("changing")
            self.spinner.set_visible(True)
            self.spinner.start()
            self.status_label.set_text(message)
            self.status_label.set_visible(True)
            self.status_label.remove_css_class("error-label")
        else:
            self.remove_css_class("changing")
            self.spinner.stop()
            self.spinner.set_visible(False)

    def show_error(self, message: str) -> None:
        self.status_label.set_text(message)
        self.status_label.add_css_class("error-label")
        self.status_label.set_visible(True)

    def clear_status(self) -> None:
        self.status_label.set_visible(False)
        self.status_label.set_text("")
        self.status_label.remove_css_class("error-label")

    def on_state_set(self, switch: Gtk.Switch, requested_state: bool) -> bool:
        if self._busy:
            # A command is already in flight; ignore extra toggles.
            return True

        action_word = "Starting" if requested_state else "Stopping"
        self.set_busy(True, f"{action_word}...")

        def worker() -> None:
            if requested_state:
                ok, err = self.service.start()
            else:
                ok, err = self.service.stop()
            # Re-confirm real status rather than trusting the command result.
            actual = self.service.is_active()
            GLib.idle_add(self._finish, actual, ok, err)

        threading.Thread(target=worker, daemon=True).start()

        # Returning True prevents GTK from applying requested_state itself;
        # we drive the final state ourselves in _finish() once confirmed.
        return True

    def _finish(self, actual_active: bool, ok: bool, err: str) -> bool:
        self.set_busy(False)
        self.set_active_silently(actual_active)
        if not ok and err:
            self.show_error(err)
        else:
            self.clear_status()
        return GLib.SOURCE_REMOVE

    def refresh_from_poll(self, actual_active: bool) -> None:
        if self._busy:
            # A start/stop command is in flight for this row -- a poll
            # result racing against it is stale by definition; let
            # _finish() (which re-confirms status itself) drive the
            # final state instead of overwriting it here.
            return
        if self.switch.get_active() == actual_active:
            # Nothing changed -- skip touching the switch entirely.
            # Calling set_active()/set_state() even with the same value
            # still re-triggers GTK's switch slider animation on every
            # 5s poll, which is what produced the on/off "flicker" for
            # services that were steadily active the whole time.
            return
        self.set_active_silently(actual_active)


class PiControlPanelWindow(Gtk.ApplicationWindow):
    def __init__(self, app: Gtk.Application):
        super().__init__(application=app)
        self.add_css_class("pi-control-panel")
        self.set_title("Pi Control")
        self.set_resizable(True)
        self.set_size_request(240, 300)
        self.set_default_size(*self._load_size())

        # Best-effort always-on-top: GTK4 removed Gtk.Window.set_keep_above,
        # and there is no portable cross-compositor replacement -- whether
        # this window stays above others depends entirely on the Wayland
        # compositor (labwc/wayfire) or X11 window manager in use. Some
        # X11 window managers respect legacy hints GTK sets automatically
        # for utility-type windows; on Wayland the only reliable route is a
        # compositor-specific protocol (e.g. wlr-layer-shell) that plain
        # GTK4 does not expose. No further action is taken here beyond
        # relying on those defaults -- pin the window via your compositor
        # if it supports it.

        self.services_by_row: list[ServiceRow] = []
        self._poll_in_flight = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # The window content sits under an overlay that carries the resize
        # grips -- see the _RESIZE_GRIPS comment above for why the WM frame
        # cannot provide them here.
        self._overlay = Gtk.Overlay()
        self._overlay.set_child(outer)
        self.set_child(self._overlay)
        self._add_resize_grips(self._overlay)

        scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_margin_top(6)
        content.set_margin_bottom(10)
        scroller.set_child(content)
        outer.append(scroller)

        self._build_service_sections(content)

        self.connect("close-request", self.on_close_request)

        # Kick off periodic polling every 10 seconds, plus an immediate poll.
        #
        # NOTE: poll_all_services() always returns GLib.SOURCE_CONTINUE (it's
        # meant to be a repeating timeout callback). Passing it straight to
        # GLib.idle_add() for the "immediate poll" was a real bug: idle_add
        # treats a SOURCE_CONTINUE return the same as a timeout source would
        # -- "call me again" -- but an idle source refires on every main-loop
        # idle iteration, not once every N seconds. That turned the "one
        # immediate poll" into an unbounded, back-to-back polling loop
        # (measured: a fresh `systemctl`/`docker` subprocess pair firing
        # roughly once per second, not once per timer interval), which was
        # the dominant CPU cost -- independent of and larger than the
        # per-poll subprocess count fixed above. Call it directly instead so
        # it runs exactly once at startup.
        self.poll_all_services()
        GLib.timeout_add_seconds(10, self.poll_all_services)

    # -- Resize grips ------------------------------------------------------

    def _add_resize_grips(self, overlay: Gtk.Overlay) -> None:
        """Stack invisible edge/corner drag zones on top of the content."""
        self._resize_grips: list[Gtk.Widget] = []
        for edge, halign, valign, width, height, cursor_name in _RESIZE_GRIPS:
            grip = Gtk.Box()
            grip.set_halign(halign)
            grip.set_valign(valign)
            grip.set_size_request(width, height)
            cursor = Gdk.Cursor.new_from_name(cursor_name, None)
            if cursor is not None:
                grip.set_cursor(cursor)

            # button=0 so touch presses (which carry no button number) are
            # handled too -- this is a touchscreen widget first.
            gesture = Gtk.GestureClick()
            gesture.set_button(0)
            gesture.connect("pressed", self._on_grip_pressed, edge)
            grip.add_controller(gesture)

            overlay.add_overlay(grip)
            self._resize_grips.append(grip)

    def _on_grip_pressed(
        self,
        gesture: Gtk.GestureClick,
        _n_press: int,
        x: float,
        y: float,
        edge: Gdk.SurfaceEdge,
    ) -> None:
        if not self.get_resizable() or self.is_maximized() or self.is_fullscreen():
            return

        surface = self.get_native().get_surface() if self.get_native() else None
        if surface is None or not isinstance(surface, Gdk.Toplevel):
            return

        event = gesture.get_current_event()
        if event is None:
            return
        device = event.get_device()
        if device is None:
            return

        grip = gesture.get_widget()
        # begin_resize() wants the press position in surface coordinates, but
        # the gesture reports it relative to the grip widget.
        point = grip.compute_point(self, Graphene.Point().init(x, y))
        if isinstance(point, tuple):
            ok, point = point
            if not ok:
                return
        if point is None:
            return

        button = gesture.get_current_button() or 1
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        surface.begin_resize(
            edge, device, button, point.x, point.y, event.get_time()
        )

    # -- UI construction -----------------------------------------------

    def _build_service_sections(self, content: Gtk.Box) -> None:
        for category, services in build_services().items():
            header = Gtk.Label(label=category, xalign=0.0)
            header.add_css_class("section-header")
            content.append(header)

            for service in services:
                row = ServiceRow(service)
                self.services_by_row.append(row)
                content.append(row)

    # -- Polling ----------------------------------------------------------

    def poll_all_services(self) -> bool:
        if self._poll_in_flight:
            # The previous poll (e.g. a slow `docker ps` call) hasn't
            # returned yet. Starting another one here would let two
            # worker threads race, and whichever result lands second
            # -- not whichever is freshest -- would win, bouncing the
            # switch back and forth. Skip this tick; the timer keeps
            # firing every 10s regardless.
            return GLib.SOURCE_CONTINUE

        self._poll_in_flight = True
        rows = list(self.services_by_row)

        def worker() -> None:
            # One subprocess call per *backend kind*, not one per service:
            # `systemctl is-active` accepts multiple unit names and prints
            # one status line per unit, and a single `docker ps` lists every
            # running container's compose project. This replaces ~11
            # `systemctl` spawns + 3 `docker` spawns per poll with exactly
            # two subprocess calls total.
            systemd_rows = [r for r in rows if isinstance(r.service, SystemdService)]
            docker_rows = [r for r in rows if isinstance(r.service, DockerComposeService)]

            systemd_states = batch_systemd_is_active(
                [r.service.unit for r in systemd_rows]
            )
            running_projects = batch_docker_running_projects()

            results = []
            for row in systemd_rows:
                results.append((row, systemd_states.get(row.service.unit, False)))
            for row in docker_rows:
                results.append((row, row.service.project in running_projects))
            GLib.idle_add(self._apply_poll_results, results)

        threading.Thread(target=worker, daemon=True).start()
        return GLib.SOURCE_CONTINUE

    def _apply_poll_results(self, results: list[tuple[ServiceRow, bool]]) -> bool:
        for row, active in results:
            row.refresh_from_poll(active)
        self._poll_in_flight = False
        return GLib.SOURCE_REMOVE

    # -- Position persistence ---------------------------------------------

    def _load_size(self) -> tuple[int, int]:
        try:
            if CONFIG_FILE.exists():
                data = json.loads(CONFIG_FILE.read_text())
                width = int(data.get("width", 420))
                height = int(data.get("height", 640))
                return max(width, 240), max(height, 300)
        except Exception:
            pass
        return 420, 640

    def _save_position(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            width = self.get_width()
            height = self.get_height()
            data = {"width": width, "height": height}

            # Best-effort: GTK4 does not expose a portable get-position API
            # (Wayland deliberately disallows clients from querying/setting
            # their own global position for security reasons). On X11 this
            # could be attempted via lower-level Xlib calls, but that is
            # outside plain GTK4/GDK and is intentionally not implemented
            # here to keep this app portable across X11 and Wayland
            # (labwc/wayfire) sessions on Raspberry Pi OS.
            CONFIG_FILE.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    def on_close_request(self, *_args) -> bool:
        self._save_position()
        return False  # allow the window to actually close


class PiControlPanelApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.evan.pi_control_panel")
        self.window: Optional[PiControlPanelWindow] = None

    def do_activate(self) -> None:
        self._load_css()
        if self.window is None:
            self.window = PiControlPanelWindow(self)
        self.window.present()

    def _load_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )


def main() -> int:
    if os.geteuid() == 0:
        print(
            "Warning: running as root is not required and not recommended. "
            "This app uses 'sudo -n' internally for systemd actions; see "
            "the module docstring for the NOPASSWD sudoers setup instead.",
            flush=True,
        )

    app = PiControlPanelApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
