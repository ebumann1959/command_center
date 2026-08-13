#!/usr/bin/env bash
# Launcher for pi-control-panel. Does NOT need to run as root -- main.py
# uses `sudo -n` internally for the specific systemctl actions it needs.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! python3 -c "import gi; gi.require_version('Gtk', '4.0'); from gi.repository import Gtk" >/dev/null 2>&1; then
    echo "error: python3 GTK4 bindings (PyGObject) not found." >&2
    echo "       install with: sudo apt install python3-gi gir1.2-gtk-4.0" >&2
    exit 1
fi

if groups | grep -q docker; then
    :
else
    echo "warning: current user is not in the 'docker' group -- docker-based" >&2
    echo "         service toggles (HiveMind, GlitchTip) may fail. Fix with:" >&2
    echo "         sudo usermod -aG docker \"\$USER\" && re-login" >&2
fi

exec python3 "${SCRIPT_DIR}/main.py" "$@"
