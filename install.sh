#!/usr/bin/env bash
# Razer Control installer: installs for the current user, plus a udev rule (needs admin rights once).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
APP_ID=io.github.voidrlm.RazerControl

# ---- dependency check -------------------------------------------------------
if ! python3 - <<'PY' 2>/dev/null
import sys
assert sys.version_info >= (3, 9)
import gi
gi.require_version("Gtk", "4.0"); gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw
assert (Gtk.get_major_version(), Gtk.get_minor_version()) >= (4, 10)
assert (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6)
PY
then
    echo "Missing dependencies: Python 3.9+, PyGObject, GTK 4.10+ and libadwaita 1.6+ are required." >&2
    echo >&2
    echo "  Fedora:         sudo dnf install python3-gobject gtk4 libadwaita" >&2
    echo "  Debian/Ubuntu:  sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1" >&2
    echo "  Arch:           sudo pacman -S python-gobject gtk4 libadwaita" >&2
    echo "  openSUSE:       sudo zypper install python3-gobject typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1" >&2
    echo >&2
    echo "(Ubuntu 24.04 ships libadwaita 1.5, which is too old; Ubuntu 25.04+ works.)" >&2
    echo "The command-line tool works without GTK: $here/razer-control-cli" >&2
    exit 1
fi

# ---- user install -----------------------------------------------------------
mkdir -p ~/.local/bin ~/.local/share/applications
ln -sf "$here/razer-control" ~/.local/bin/razer-control
ln -sf "$here/razer-control-cli" ~/.local/bin/razer-control-cli
rm -f ~/.local/share/applications/io.github.arjun.RazerControl.desktop  # older app id
sed "s|^Exec=.*|Exec=$HOME/.local/bin/razer-control|" "$here/$APP_ID.desktop" > ~/.local/share/applications/$APP_ID.desktop
update-desktop-database ~/.local/share/applications 2>/dev/null || true
echo "Installed launcher (~/.local/bin/razer-control) and app-grid entry."

# ---- GNOME top-bar battery indicator ----------------------------------------
EXT_UUID=razer-battery@arjun
if command -v gnome-shell >/dev/null; then
    mkdir -p ~/.local/share/gnome-shell/extensions
    ln -sfn "$here/gnome-extension/$EXT_UUID" ~/.local/share/gnome-shell/extensions/$EXT_UUID
    if ! gnome-extensions enable "$EXT_UUID" 2>/dev/null; then
        # GNOME hasn't seen it yet (new extensions load at the next login): enable it for then.
        python3 - "$EXT_UUID" <<'PY2' || true
import ast, subprocess, sys
cur = subprocess.run(["gsettings", "get", "org.gnome.shell", "enabled-extensions"], capture_output=True, text=True).stdout
cur = cur.strip().removeprefix("@as ")
exts = ast.literal_eval(cur) if cur else []
if sys.argv[1] not in exts:
    subprocess.run(["gsettings", "set", "org.gnome.shell", "enabled-extensions", str(exts + [sys.argv[1]])], check=True)
PY2
        echo "Installed the Razer Battery top-bar extension; it appears after you log out and back in."
    else
        echo "Installed and enabled the Razer Battery top-bar extension."
    fi
fi

# ---- udev rule (device access without root) ---------------------------------
rule=/etc/udev/rules.d/70-razer-control.rules
if [ ! -f "$rule" ] || ! cmp -s "$here/70-razer-control.rules" "$rule"; then
    echo "Installing udev rule to $rule (needs your admin password)…"
    cmd="install -m644 '$here/70-razer-control.rules' '$rule' && udevadm control --reload && udevadm trigger --subsystem-match=hidraw --action=change"
    if [ -t 0 ] && command -v sudo >/dev/null; then
        sudo sh -c "$cmd"
    else
        pkexec sh -c "$cmd"  # graphical password prompt when there's no terminal
    fi
fi

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "Note: ~/.local/bin is not on your PATH; the app-grid entry still works." ;; esac
echo "Done. If your Razer device was already plugged in, unplug and replug it once, then open \"Razer Control\"."
